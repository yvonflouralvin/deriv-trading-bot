"""
Service 3 — Moteur de stratégie.

- publie le catalogue des types de stratégie (dossier strategies/) en base ;
- charge les stratégies activées depuis le Monitor et se recharge dès qu'elles
  changent (LISTEN "strategies") ;
- écoute le flux du collecteur (LISTEN "market") : à chaque clôture de bougie,
  évalue les stratégies de cet actif / unité de temps ;
- sur un signal : contrôle du risque (un contrat à la fois, nombre de trades et
  perte max du jour), puis exécution :
    * paper : contrat simulé, résolu avec les vrais ticks enregistrés ;
    * demo  : vrai contrat Rise/Fall sur le compte DÉMO Deriv (un compte réel
              est toujours refusé) ;
- exécute les backtests demandés depuis le Monitor (LISTEN "backtests") ;
- publie chaque signal / trade sur le canal "engine" (relayé au Monitor).
"""

import asyncio
import json
import logging
import os
import signal as signals_mod
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

import backtest
from analysis.service import TREND_TFS, AnalysisService
from portfolio.service import PortfolioService
from deriv import TOKEN, USE_NEW_API, DerivClient, DerivError
from strategies import REGISTRY, Bar

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("engine")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trading:trading@db:5432/trading")
DEMO_TRADING_ENABLED = os.getenv("DEMO_TRADING_ENABLED", "1") == "1"
SCHEMA_FILE = Path(os.getenv("SCHEMA_FILE", Path(__file__).with_name("schema.sql")))
PAPER_TIMEOUT_S = 600          # contrat paper sans ticks de sortie après ce délai -> erreur
BACKTEST_TICKS_MAX_DAYS = 3    # au-delà, le backtest utilise les bougies 1m


def utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


def epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt else None


TODAY_SQL = "(date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


class DemoRefused(Exception):
    pass


# --------------------------------------------------------------------------- #
# Exécution sur le compte démo Deriv
# --------------------------------------------------------------------------- #
class DemoExecutor:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.client: DerivClient | None = None
        self.currency = "USD"
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self.client and not self.client.closed.is_set())

    async def ensure(self):
        async with self.lock:
            if self.connected:
                return
            if not TOKEN:
                raise DemoRefused("DERIV_TOKEN absent : impossible de trader en démo")
            client = DerivClient(on_stream=self.on_stream)
            await client.connect()
            if USE_NEW_API:
                is_demo = "/demo" in client.url
            else:
                auth = (await client.send({"authorize": TOKEN}))["authorize"]
                is_demo = bool(auth.get("is_virtual"))
            if not is_demo:
                await client.close()
                raise DemoRefused("le compte Deriv n'est pas un compte démo : trading refusé")
            try:
                bal = (await client.send({"balance": 1}))["balance"]
                self.currency = bal.get("currency") or "USD"
            except DerivError as e:
                log.warning("Solde démo illisible : %s", e)
            self.client = client
            log.info("Exécution démo prête (devise %s)", self.currency)
            # reprendre le suivi des contrats démo restés ouverts
            async with self.engine.pool.acquire() as con:
                rows = await con.fetch("SELECT contract_id FROM trades WHERE mode='demo' AND status='open' "
                                       "AND contract_id IS NOT NULL")
            for r in rows:
                await self.track(r["contract_id"])

    async def track(self, contract_id: str):
        try:
            await self.client.send({"proposal_open_contract": 1, "contract_id": int(contract_id), "subscribe": 1})
        except (DerivError, ConnectionError) as e:
            log.warning("Suivi du contrat %s impossible : %s", contract_id, e)

    def on_stream(self, msg: dict):
        if msg.get("msg_type") != "proposal_open_contract":
            return
        poc = msg.get("proposal_open_contract") or {}
        if poc.get("is_sold") or poc.get("status") in ("won", "lost", "sold"):
            sub = (msg.get("subscription") or {}).get("id")
            asyncio.create_task(self.engine.settle_demo(poc, sub))

    async def open(self, st: dict, side: str) -> dict:
        """Achète un contrat Rise/Fall ; renvoie les infos d'achat."""
        await self.ensure()
        req = {
            "proposal": 1, "subscribe": 1, "amount": st["stake"], "basis": "stake",
            "contract_type": side, "currency": self.currency,
            "duration": st["duration"], "duration_unit": st["duration_unit"],
            ("underlying_symbol" if USE_NEW_API else "symbol"): st["symbol"],
        }
        resp = await self.client.send(req)
        prop = resp["proposal"]
        sub = (resp.get("subscription") or {}).get("id")
        try:
            buy = (await self.client.send({"buy": prop["id"], "price": float(prop["ask_price"])}))["buy"]
        finally:
            if sub:
                try:
                    await self.client.send({"forget": sub})
                except DerivError:
                    pass
        return {"contract_id": str(buy["contract_id"]), "stake": float(buy.get("buy_price", st["stake"])),
                "payout": float(buy.get("payout") or prop.get("payout") or 0) or None,
                "start_time": buy.get("start_time")}


# --------------------------------------------------------------------------- #
# Moteur
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.strategies: dict[int, dict] = {}
        self.by_key: dict[tuple[str, int], list[int]] = {}
        self.last_bucket: dict[tuple[str, int], int] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.demo = DemoExecutor(self)
        self.reload_needed = asyncio.Event()
        self.backtest_queue: asyncio.Queue = asyncio.Queue()
        self.stats = {"signals": 0, "trades": 0, "started": time.time()}

    # ---- catalogue / configuration --------------------------------------- #
    async def publish_kinds(self):
        async with self.pool.acquire() as con:
            await con.executemany("""
                INSERT INTO strategy_kinds(kind, name, description, params, updated_at)
                VALUES($1,$2,$3,$4, now())
                ON CONFLICT (kind) DO UPDATE SET name=EXCLUDED.name, description=EXCLUDED.description,
                  params=EXCLUDED.params, updated_at=now()""",
                [(k, s.name, s.description, json.dumps([p.to_dict() for p in s.params])) for k, s in REGISTRY.items()])
            await con.execute("DELETE FROM strategy_kinds WHERE NOT (kind = ANY($1::text[]))", list(REGISTRY))
        log.info("Types de stratégie publiés : %s", ", ".join(REGISTRY))

    async def load_strategies(self):
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT * FROM strategies WHERE enabled")
        strategies, by_key = {}, {}
        for r in rows:
            impl = REGISTRY.get(r["kind"])
            if impl is None:
                log.warning("Stratégie %s : type inconnu %s (ignorée)", r["id"], r["kind"])
                continue
            st = dict(r)
            st["impl"] = impl
            st["p"] = impl.resolve_params(json.loads(r["params"]) if isinstance(r["params"], str) else r["params"])
            strategies[r["id"]] = st
            by_key.setdefault((r["symbol"], r["tf"]), []).append(r["id"])
        self.strategies, self.by_key = strategies, by_key
        log.info("%d stratégie(s) active(s) : %s", len(strategies),
                 ", ".join(f"#{s['id']} {s['name']} [{s['mode']}] {s['symbol']}/{s['tf']}s" for s in strategies.values())
                 or "aucune")

    # ---- flux du collecteur ---------------------------------------------- #
    def on_market(self, _c, _p, _ch, payload: str):
        try:
            m = json.loads(payload)
        except ValueError:
            return
        for tf_s, c in m.get("c", {}).items():
            key = (m["s"], int(tf_s))
            if key not in self.by_key:
                continue
            prev = self.last_bucket.get(key)
            self.last_bucket[key] = c[0]
            if prev is not None and c[0] > prev:
                asyncio.create_task(self.on_candle_close(key, prev, m["e"], m["q"]))

    async def on_candle_close(self, key, closed_ts: int, at: int, price: float):
        ids = [i for i in self.by_key.get(key, []) if i in self.strategies]
        if not ids:
            return
        need = min(2000, max(200, max(self.strategies[i]["impl"].warmup(self.strategies[i]["p"]) for i in ids) * 3))
        async with self.pool.acquire() as con:
            rows = await con.fetch("""
                SELECT extract(epoch FROM ts)::bigint AS t, open, high, low, close FROM candles
                WHERE symbol=$1 AND tf=$2 AND ts <= to_timestamp($3) ORDER BY ts DESC LIMIT $4""",
                key[0], key[1], closed_ts, need)
        bars = [Bar(r["t"], r["open"], r["high"], r["low"], r["close"]) for r in reversed(rows)]
        if not bars or bars[-1].time != closed_ts:
            log.warning("Bougie clôturée %s %s introuvable, évaluation sautée", key, closed_ts)
            return
        for sid in ids:
            st = self.strategies.get(sid)
            if not st:
                continue
            try:
                side = st["impl"].signal_on_last(bars, st["p"])
            except Exception:  # noqa: BLE001
                log.exception("Stratégie #%s : erreur de calcul", sid)
                continue
            if side:
                await self.handle_signal(st, side, closed_ts, at, price)

    # ---- signaux et risque ------------------------------------------------ #
    async def risk_block(self, con, st) -> str | None:
        if await con.fetchval("SELECT count(*) FROM trades WHERE strategy_id=$1 AND status='open'", st["id"]):
            return "contrat déjà ouvert"
        n, pnl = await con.fetchrow(f"""
            SELECT count(*), coalesce(sum(profit), 0) FROM trades
            WHERE strategy_id=$1 AND status <> 'error' AND entry_time >= {TODAY_SQL}""", st["id"])
        if n >= st["max_trades_per_day"]:
            return "limite de trades du jour atteinte"
        if -pnl >= st["max_daily_loss"]:
            return "perte maximale du jour atteinte"
        return None

    async def handle_signal(self, st: dict, side: str, closed_ts: int, at: int, price: float):
        lock = self.locks.setdefault(st["id"], asyncio.Lock())
        async with lock:
            async with self.pool.acquire() as con:
                block = await self.risk_block(con, st)
                demo = st["mode"] == "demo"
                mode = "demo" if demo and DEMO_TRADING_ENABLED else "paper"
                action = f"skipped:{block}" if block else f"traded:{mode}"
                sig_id = await con.fetchval("""
                    INSERT INTO signals(strategy_id, symbol, tf, candle_ts, side, price, action)
                    VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
                    st["id"], st["symbol"], st["tf"], utc(closed_ts), side, price, action)
            self.stats["signals"] += 1
            log.info("Signal %s #%s %s %s @ %s -> %s", side, st["id"], st["name"], st["symbol"], price, action)
            await self.publish({"type": "signal", "symbol": st["symbol"], "strategy_id": st["id"],
                                "side": side, "time": at, "price": price, "action": action})
            if block:
                return
            if demo and not DEMO_TRADING_ENABLED:
                log.warning("DEMO_TRADING_ENABLED=0 : stratégie #%s exécutée en paper", st["id"])
            if mode == "demo":
                await self.open_demo(st, sig_id, side, at, price)
            else:
                await self.open_paper(st, sig_id, side, at, price)

    async def open_paper(self, st, sig_id, side, at, price):
        async with self.pool.acquire() as con:
            row = await con.fetchrow("""
                INSERT INTO trades(strategy_id, signal_id, mode, symbol, side, stake, payout, duration,
                                   duration_unit, entry_time, entry_spot)
                VALUES($1,$2,'paper',$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
                st["id"], sig_id, st["symbol"], side, st["stake"], round(st["stake"] * (1 + st["payout_ratio"]), 4),
                st["duration"], st["duration_unit"], utc(at), price)
        self.stats["trades"] += 1
        await self.publish_trade(row)

    async def open_demo(self, st, sig_id, side, at, price):
        try:
            info = await self.demo.open(st, side)
        except (DerivError, DemoRefused, ConnectionError, OSError, asyncio.TimeoutError, KeyError) as e:
            log.error("Achat démo refusé (#%s) : %s", st["id"], e)
            async with self.pool.acquire() as con:
                row = await con.fetchrow("""
                    INSERT INTO trades(strategy_id, signal_id, mode, symbol, side, stake, duration, duration_unit,
                                       entry_time, entry_spot, status, error)
                    VALUES($1,$2,'demo',$3,$4,$5,$6,$7,$8,$9,'error',$10) RETURNING *""",
                    st["id"], sig_id, st["symbol"], side, st["stake"], st["duration"], st["duration_unit"],
                    utc(at), price, str(e)[:500])
            await self.publish_trade(row)
            return
        async with self.pool.acquire() as con:
            row = await con.fetchrow("""
                INSERT INTO trades(strategy_id, signal_id, mode, symbol, side, stake, payout, duration,
                                   duration_unit, entry_time, entry_spot, contract_id)
                VALUES($1,$2,'demo',$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                st["id"], sig_id, st["symbol"], side, info["stake"], info["payout"], st["duration"],
                st["duration_unit"], utc(info["start_time"] or at), price, info["contract_id"])
        self.stats["trades"] += 1
        log.info("Contrat démo %s acheté (#%s %s %s)", info["contract_id"], st["id"], side, st["symbol"])
        await self.publish_trade(row)
        await self.demo.track(info["contract_id"])

    # ---- clôture des contrats ---------------------------------------------- #
    async def settle_demo(self, poc: dict, sub_id: str | None):
        cid = str(poc.get("contract_id"))
        profit = float(poc.get("profit") or 0)
        status = "won" if profit > 0 else "lost"
        exit_spot = poc.get("exit_tick") or poc.get("sell_spot") or poc.get("current_spot")
        exit_t = poc.get("exit_tick_time") or poc.get("sell_time") or time.time()
        for _ in range(5):  # le contrat peut se terminer avant que la ligne soit insérée
            async with self.pool.acquire() as con:
                row = await con.fetchrow("""
                    UPDATE trades SET status=$2, profit=$3, exit_spot=$4, exit_time=$5,
                      entry_spot=coalesce($6, entry_spot)
                    WHERE contract_id=$1 AND status='open' RETURNING *""",
                    cid, status, profit, float(exit_spot) if exit_spot else None, utc(exit_t),
                    float(poc["entry_tick"]) if poc.get("entry_tick") else None)
            if row:
                log.info("Contrat démo %s %s (%+.2f)", cid, status, profit)
                await self.publish_trade(row)
                break
            await asyncio.sleep(1)
        if sub_id and self.demo.connected:
            try:
                await self.demo.client.send({"forget": sub_id})
            except DerivError:
                pass

    async def paper_resolver(self):
        """Résout les contrats paper avec les ticks enregistrés par le collecteur."""
        while True:
            await asyncio.sleep(1)
            try:
                async with self.pool.acquire() as con:
                    open_trades = await con.fetch("SELECT * FROM trades WHERE mode='paper' AND status='open'")
                    for t in open_trades:
                        await self.resolve_paper(con, t)
            except Exception:  # noqa: BLE001
                log.exception("Résolution paper")

    async def resolve_paper(self, con, t):
        entry = t["entry_time"]
        if t["duration_unit"] == "t":
            ex = await con.fetchrow("""
                SELECT ts, quote FROM ticks WHERE symbol=$1 AND ts > $2 ORDER BY ts OFFSET $3 LIMIT 1""",
                t["symbol"], entry, t["duration"] - 1)
        else:
            secs = t["duration"] * (60 if t["duration_unit"] == "m" else 1)
            ex = await con.fetchrow("""
                SELECT ts, quote FROM ticks WHERE symbol=$1 AND ts >= $2 + make_interval(secs => $3)
                ORDER BY ts LIMIT 1""", t["symbol"], entry, secs)
        if ex is None:
            if time.time() - entry.timestamp() > PAPER_TIMEOUT_S:
                row = await con.fetchrow("UPDATE trades SET status='error', error='aucun tick de sortie' "
                                         "WHERE id=$1 RETURNING *", t["id"])
                await self.publish_trade(row)
            return
        won = ex["quote"] > t["entry_spot"] if t["side"] == "CALL" else ex["quote"] < t["entry_spot"]
        profit = round((t["payout"] - t["stake"]) if won else -t["stake"], 4)
        row = await con.fetchrow("""
            UPDATE trades SET status=$2, profit=$3, exit_time=$4, exit_spot=$5 WHERE id=$1 AND status='open'
            RETURNING *""", t["id"], "won" if won else "lost", profit, ex["ts"], ex["quote"])
        if row:
            await self.publish_trade(row)

    # ---- publication ------------------------------------------------------ #
    async def publish(self, event: dict):
        try:
            async with self.pool.acquire() as con:
                await con.execute("SELECT pg_notify('engine', $1)", json.dumps(event))
        except Exception as e:  # noqa: BLE001
            log.warning("Publication impossible : %s", e)

    async def publish_trade(self, row):
        if row is None:
            return
        await self.publish({"type": "trade", "symbol": row["symbol"], "trade": trade_json(row)})

    # ---- backtests -------------------------------------------------------- #
    async def backtest_worker(self):
        async with self.pool.acquire() as con:
            for r in await con.fetch("SELECT id FROM backtests WHERE status IN ('pending','running') ORDER BY id"):
                self.backtest_queue.put_nowait(r["id"])
        while True:
            bt_id = await self.backtest_queue.get()
            try:
                await self.run_backtest(bt_id)
            except Exception as e:  # noqa: BLE001
                log.exception("Backtest %s en erreur", bt_id)
                async with self.pool.acquire() as con:
                    await con.execute("UPDATE backtests SET status='error', error=$2, finished_at=now() WHERE id=$1",
                                      bt_id, str(e)[:500])

    async def run_backtest(self, bt_id: int):
        async with self.pool.acquire() as con:
            bt = await con.fetchrow("UPDATE backtests SET status='running' WHERE id=$1 AND status IN ('pending','running') "
                                    "RETURNING *", bt_id)
            if not bt:
                return
            impl = REGISTRY.get(bt["kind"])
            if impl is None:
                raise ValueError(f"type de stratégie inconnu : {bt['kind']}")
            params = impl.resolve_params(json.loads(bt["params"]) if isinstance(bt["params"], str) else bt["params"])
            start, end = epoch(bt["date_from"]), epoch(bt["date_to"])
            warm = impl.warmup(params) * 3 * bt["tf"]
            rows = await con.fetch("""
                SELECT extract(epoch FROM ts)::bigint AS t, open, high, low, close FROM candles
                WHERE symbol=$1 AND tf=$2 AND ts >= to_timestamp($3) AND ts < to_timestamp($4) ORDER BY ts""",
                bt["symbol"], bt["tf"], start - warm, end)
            bars = [Bar(r["t"], r["open"], r["high"], r["low"], r["close"]) for r in rows]
            margin = backtest.duration_seconds(bt["duration"], bt["duration_unit"], bt["symbol"]) + bt["tf"] + 120
            first_tick = await con.fetchval("SELECT extract(epoch FROM min(ts))::bigint FROM ticks WHERE symbol=$1",
                                            bt["symbol"])
            if first_tick and first_tick <= start and end - start <= BACKTEST_TICKS_MAX_DAYS * 86400:
                ticks = await con.fetch("""SELECT extract(epoch FROM ts)::bigint AS t, quote FROM ticks
                    WHERE symbol=$1 AND ts >= to_timestamp($2) AND ts < to_timestamp($3) ORDER BY ts""",
                    bt["symbol"], start, end + margin)
                source = backtest.TickSource([r["t"] for r in ticks], [r["quote"] for r in ticks])
            else:
                m1 = await con.fetch("""SELECT extract(epoch FROM ts)::bigint AS t, close FROM candles
                    WHERE symbol=$1 AND tf=60 AND ts >= to_timestamp($2) AND ts < to_timestamp($3) ORDER BY ts""",
                    bt["symbol"], start - 120, end + margin)
                source = backtest.CandleSource([r["t"] for r in m1], [r["close"] for r in m1])
        cfg = backtest.Config(bt["symbol"], bt["tf"], bt["stake"], bt["payout_ratio"], bt["duration"], bt["duration_unit"])
        t0 = time.monotonic()
        stats, trades = await asyncio.to_thread(backtest.run, impl, params, bars, source, cfg, start)
        stats["bars"] = sum(1 for b in bars if b.time >= start)
        stats["params"] = params
        stats["seconds"] = round(time.monotonic() - t0, 2)
        async with self.pool.acquire() as con:
            await con.execute("""UPDATE backtests SET status='done', stats=$2, trades=$3, finished_at=now()
                                 WHERE id=$1""", bt_id, json.dumps(stats), json.dumps(trades[-3000:]))
        log.info("Backtest %s : %d trades, %s%% gagnants, P&L %s (%s)", bt_id, stats["trades"],
                 round((stats["win_rate"] or 0) * 100, 1), stats["pnl"], stats["resolution"])

    # ---- divers ------------------------------------------------------------ #
    async def heartbeat(self):
        while True:
            try:
                async with self.pool.acquire() as con:
                    open_n = await con.fetchval("SELECT count(*) FROM trades WHERE status='open'")
                    info = {"strategies": len(self.strategies), "open_trades": open_n,
                            "signals": self.stats["signals"], "trades": self.stats["trades"],
                            "demo_enabled": DEMO_TRADING_ENABLED, "demo_connected": self.demo.connected,
                            "uptime_s": int(time.time() - self.stats["started"]),
                            "portfolio": getattr(getattr(self, "portfolio", None), "status", None)}
                    await con.execute("""
                        INSERT INTO service_status(name, last_seen, info) VALUES('engine', now(), $1)
                        ON CONFLICT (name) DO UPDATE SET last_seen=now(), info=EXCLUDED.info""", json.dumps(info))
            except Exception as e:  # noqa: BLE001
                log.warning("Battement de cœur non écrit : %s", e)
            await asyncio.sleep(15)

    async def reloader(self):
        while True:
            await self.reload_needed.wait()
            self.reload_needed.clear()
            await asyncio.sleep(0.3)  # regrouper les modifications rapprochées
            await self.load_strategies()


def trade_json(r) -> dict:
    return {"id": r["id"], "strategy_id": r["strategy_id"], "mode": r["mode"], "symbol": r["symbol"],
            "side": r["side"], "stake": r["stake"], "payout": r["payout"], "status": r["status"],
            "entry_time": epoch(r["entry_time"]), "entry_spot": r["entry_spot"],
            "exit_time": epoch(r["exit_time"]), "exit_spot": r["exit_spot"], "profit": r["profit"],
            "error": r["error"]}


async def connect_db() -> asyncpg.Pool:
    delay = 2
    while True:
        try:
            return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("Base indisponible (%s), nouvel essai dans %ds", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signals_mod.SIGINT, signals_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    pool = await connect_db()
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(424242)")
        try:
            await con.execute(SCHEMA_FILE.read_text())
        finally:
            await con.execute("SELECT pg_advisory_unlock(424242)")

    eng = Engine(pool)
    await eng.publish_kinds()
    await eng.load_strategies()
    log.info("Trading démo %s", "activé" if DEMO_TRADING_ENABLED else "DÉSACTIVÉ (tout en paper)")

    analysis = AnalysisService(pool)
    log.info("Analyse de tendance sur %s", ", ".join(f"{tf // 60} min" for tf in TREND_TFS))
    portfolio = PortfolioService(pool)
    eng.portfolio = portfolio

    listener = await asyncpg.connect(DATABASE_URL)
    await listener.add_listener("market", eng.on_market)
    await listener.add_listener("strategies", lambda *_: eng.reload_needed.set())
    await listener.add_listener("backtests", lambda _c, _p, _ch, payload: eng.backtest_queue.put_nowait(int(payload)))
    await listener.add_listener("trend_reports", lambda _c, _p, _ch, payload: analysis.reports.put_nowait(int(payload)))

    tasks = [asyncio.create_task(t) for t in
             (eng.paper_resolver(), eng.backtest_worker(), eng.heartbeat(), eng.reloader(),
              analysis.sync_loop(), analysis.report_worker(), portfolio.loop())]
    await stop.wait()
    log.info("Arrêt demandé")
    for t in tasks:
        t.cancel()
    await listener.close()
    if eng.demo.client:
        await eng.demo.client.close()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
