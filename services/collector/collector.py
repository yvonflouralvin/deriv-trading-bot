"""
Service 1 — Collecteur de ticks.

- lit la watchlist en base (modifiée depuis le Monitor) et réagit tout de suite
  aux changements (LISTEN/NOTIFY PostgreSQL)
- pour chaque actif : charge l'historique de bougies manquant (backfill
  incrémental depuis la dernière bougie connue), puis s'abonne aux ticks live
- enregistre les ticks, construit les bougies de toutes les unités de temps,
  et publie chaque mise à jour sur le canal PostgreSQL "market" (relayé au
  Monitor par l'API)
- rafraîchit le catalogue des actifs Deriv, écrit un battement de cœur,
  se reconnecte tout seul
"""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from deriv import DerivClient, DerivError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("collector")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trading:trading@db:5432/trading")
TIMEFRAMES = [int(x) for x in os.getenv("TIMEFRAMES", "60,300,900,3600,14400,86400").split(",")]
DEFAULT_SYMBOLS = [s for s in os.getenv("DEFAULT_SYMBOLS", "R_100,R_75,1HZ100V").split(",") if s]
CATALOG_REFRESH_S = int(os.getenv("CATALOG_REFRESH_HOURS", "6")) * 3600
RETRY_FAILED_S = 300
# Historique profond inutile (et coûteux) sur les petites unités de temps :
# au-delà de SHORT_TF_MAX_DAYS jours, seules les UT >= 5 min sont chargées (M1 plafonnée).
SHORT_TF_MAX_DAYS = int(os.getenv("SHORT_TF_MAX_DAYS", "30"))
HISTORY_PAGE = int(os.getenv("HISTORY_PAGE", "1000"))
SCHEMA_FILE = Path(os.getenv("SCHEMA_FILE", Path(__file__).with_name("schema.sql")))


def _pretty(code):
    """'synthetic_index' -> 'Synthetic Index' (la nouvelle API ne donne plus les libellés)."""
    return code.replace("_", " ").title() if code else None


def utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


async def apply_schema(pool: asyncpg.Pool):
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(424242)")
        try:
            await con.execute(SCHEMA_FILE.read_text())
        finally:
            await con.execute("SELECT pg_advisory_unlock(424242)")


# --------------------------------------------------------------------------- #
# Bougies en mémoire
# --------------------------------------------------------------------------- #
class CandleBook:
    """Bougie en cours pour chaque (actif, unité de temps)."""

    def __init__(self):
        self.cur: dict[tuple[str, int], list] = {}   # -> [ts, o, h, l, c]

    def seed(self, symbol: str, tf: int, c: list):
        self.cur[(symbol, tf)] = list(c)

    def update(self, symbol: str, epoch: int, q: float) -> dict[int, list]:
        changed = {}
        for tf in TIMEFRAMES:
            bucket = epoch - epoch % tf
            c = self.cur.get((symbol, tf))
            if c is None or bucket > c[0]:
                c = [bucket, q, q, q, q]
            elif bucket == c[0]:
                c[2], c[3], c[4] = max(c[2], q), min(c[3], q), q
            else:
                continue  # tick en retard sur une bougie déjà close : ignoré
            self.cur[(symbol, tf)] = c
            changed[tf] = c
        return changed


# --------------------------------------------------------------------------- #
# Collecteur
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.book = CandleBook()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
        self.client: DerivClient | None = None
        self.subs: dict[str, str] = {}               # actif -> id d'abonnement
        self.sub_symbol: dict[str, str] = {}         # id d'abonnement -> actif
        self.sub_days: dict[str, int] = {}           # profondeur d'historique déjà demandée
        self.deep_tried: set = set()                 # (actif, tf, jours) : historique ancien déjà tenté
        self.failed: dict[str, tuple[float, str]] = {}  # actif -> (réessai, raison)
        self.wake = asyncio.Event()
        self.stats = {"ticks": 0, "started": time.time()}
        self.catalog_at = 0.0

    # ---- flux entrant ---------------------------------------------------- #
    def on_stream(self, msg: dict):
        if msg.get("msg_type") == "tick" and "tick" in msg:
            t = msg["tick"]
            sym = (t.get("symbol") or t.get("underlying_symbol")
                   or self.sub_symbol.get((msg.get("subscription") or {}).get("id")))
            if not sym or t.get("epoch") is None or t.get("quote") is None:
                log.debug("Tick ignoré : %s", msg)
                return
            try:
                self.queue.put_nowait((sym, int(t["epoch"]), float(t["quote"])))
            except asyncio.QueueFull:
                log.error("File de ticks pleine : tick perdu")

    async def writer(self):
        """Écrit les ticks par lots (~1 s) et publie les mises à jour."""
        while True:
            batch = [await self.queue.get()]
            deadline = time.monotonic() + 1.0
            while len(batch) < 2000 and (left := deadline - time.monotonic()) > 0:
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), left))
                except asyncio.TimeoutError:
                    break
            ticks, candles, notes = [], {}, []
            for sym, epoch, q in batch:
                ticks.append((sym, utc(epoch), q))
                changed = self.book.update(sym, epoch, q)
                for tf, c in changed.items():
                    candles[(sym, tf, c[0])] = (sym, tf, utc(c[0]), c[1], c[2], c[3], c[4])
                notes.append(json.dumps({"s": sym, "e": epoch, "q": q,
                                         "c": {str(tf): c for tf, c in changed.items()}}))
            try:
                async with self.pool.acquire() as con, con.transaction():
                    await con.executemany(
                        "INSERT INTO ticks(symbol, ts, quote) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", ticks)
                    await upsert_candles(con, list(candles.values()))
                    await con.executemany("SELECT pg_notify('market', $1)", [(n,) for n in notes])
                self.stats["ticks"] += len(ticks)
            except Exception:  # noqa: BLE001
                log.exception("Écriture en base échouée (%d ticks)", len(ticks))

    # ---- catalogue -------------------------------------------------------- #
    async def refresh_catalog(self):
        try:
            resp = await self.client.send({"active_symbols": "brief"}, timeout=60)
        except (DerivError, asyncio.TimeoutError) as e:
            log.warning("Catalogue indisponible : %s", e)
            return
        rows = []
        for item in resp.get("active_symbols", []):
            sym = item.get("underlying_symbol") or item.get("symbol")
            if not sym:
                continue
            rows.append((
                sym,
                item.get("underlying_symbol_name") or item.get("display_name") or sym,
                item.get("market"),
                item.get("market_display_name") or _pretty(item.get("market")),
                item.get("submarket_display_name") or _pretty(item.get("submarket")),
                item.get("pip_size") or item.get("pip"),
                bool(item.get("exchange_is_open")),
            ))
        async with self.pool.acquire() as con:
            await con.executemany("""
                INSERT INTO symbols(symbol, display_name, market, market_display,
                                    submarket_display, pip, exchange_open, updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7, now())
                ON CONFLICT (symbol) DO UPDATE SET
                  display_name=EXCLUDED.display_name, market=EXCLUDED.market,
                  market_display=EXCLUDED.market_display,
                  submarket_display=EXCLUDED.submarket_display, pip=EXCLUDED.pip,
                  exchange_open=EXCLUDED.exchange_open, updated_at=now()""", rows)
        self.catalog_at = time.time()
        log.info("Catalogue : %d actifs", len(rows))

    # ---- historique ------------------------------------------------------- #
    async def backfill(self, symbol: str, days: int):
        now = int(time.time())
        for tf in TIMEFRAMES:
            async with self.pool.acquire() as con:
                last = await con.fetchval(
                    "SELECT extract(epoch FROM max(ts))::bigint FROM candles WHERE symbol=$1 AND tf=$2",
                    symbol, tf)
            tf_days = days if tf >= 300 else min(days, SHORT_TF_MAX_DAYS)
            wanted = now - max(tf_days * 86400, 300 * tf)
            got = 0
            if last:
                # incrémental : on repart de la dernière bougie connue
                got += await self.fetch_candles(symbol, tf, max(wanted, int(last)), now)
                # historique plus ancien demandé (ex. passage de 7 à 365 jours) : une seule tentative
                async with self.pool.acquire() as con:
                    first = await con.fetchval(
                        "SELECT extract(epoch FROM min(ts))::bigint FROM candles WHERE symbol=$1 AND tf=$2", symbol, tf)
                key = (symbol, tf, tf_days)
                if first and first > wanted + tf and key not in self.deep_tried:
                    self.deep_tried.add(key)
                    older = await self.fetch_candles(symbol, tf, wanted, int(first) - 1)
                    got += older
                    if older:
                        log.info("Historique ancien %s %ss : %d bougies", symbol, tf, older)
            else:
                got += await self.fetch_candles(symbol, tf, wanted, now)
            async with self.pool.acquire() as con:
                row = await con.fetchrow(
                    "SELECT extract(epoch FROM ts)::bigint AS ts, open, high, low, close FROM candles "
                    "WHERE symbol=$1 AND tf=$2 ORDER BY ts DESC LIMIT 1", symbol, tf)
            if row:
                self.book.seed(symbol, tf, [row["ts"], row["open"], row["high"], row["low"], row["close"]])
            log.info("Historique %s %ss : %d bougies", symbol, tf, got)

    async def fetch_candles(self, symbol, tf, start, end) -> int:
        total, cursor = 0, end
        while cursor > start:
            resp = await self.client.send({
                "ticks_history": symbol, "style": "candles", "granularity": tf,
                "start": start, "end": cursor, "count": HISTORY_PAGE, "adjust_start_time": 1,
            }, timeout=60)
            cs = resp.get("candles") or []
            if not cs:
                break
            rows = [(symbol, tf, utc(c["epoch"]), float(c["open"]), float(c["high"]),
                     float(c["low"]), float(c["close"]))
                    for c in cs if None not in (c.get("open"), c.get("high"), c.get("low"), c.get("close"))]
            async with self.pool.acquire() as con:
                await upsert_candles(con, rows)
            total += len(rows)
            oldest = int(cs[0]["epoch"])
            if oldest <= start or oldest >= cursor:
                break
            cursor = oldest - 1
        return total

    # ---- watchlist -------------------------------------------------------- #
    async def sync_watchlist(self):
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT symbol, backfill_days FROM watchlist WHERE enabled")
        desired = {r["symbol"]: r["backfill_days"] for r in rows}

        for sym in list(self.subs):
            if sym not in desired:
                try:
                    await self.client.send({"forget": self.subs[sym]})
                except DerivError as e:
                    log.warning("forget %s : %s", sym, e)
                del self.subs[sym]
                log.info("Arrêt de la collecte : %s", sym)
        for sym in list(self.failed):
            if sym not in desired:
                del self.failed[sym]

        for sym, days in desired.items():
            if sym in self.subs:
                if days > self.sub_days.get(sym, days):   # plus d'historique demandé depuis le Monitor
                    try:
                        await self.backfill(sym, days)
                        self.sub_days[sym] = days
                    except DerivError as e:
                        log.warning("Historique %s : %s", sym, e)
                continue
            retry_at, _ = self.failed.get(sym, (0, ""))
            if time.time() < retry_at:
                continue
            try:
                await self.backfill(sym, days)
                resp = await self.client.send({"ticks": sym, "subscribe": 1})
                self.subs[sym] = resp["subscription"]["id"]
                self.sub_symbol[self.subs[sym]] = sym
                self.sub_days[sym] = days
                self.failed.pop(sym, None)
                log.info("Collecte en direct : %s", sym)
            except DerivError as e:
                self.failed[sym] = (time.time() + RETRY_FAILED_S, str(e))
                log.warning("Actif %s indisponible (%s), nouvel essai dans %ds", sym, e, RETRY_FAILED_S)

    async def heartbeat(self):
        await asyncio.sleep(5)  # laisser la 1re connexion s'établir
        while True:
            info = {
                "connected": bool(self.client and not self.client.closed.is_set()),
                "symbols": sorted(self.subs),
                "failed": {k: v[1] for k, v in self.failed.items()},
                "ticks_written": self.stats["ticks"],
                "uptime_s": int(time.time() - self.stats["started"]),
            }
            try:
                async with self.pool.acquire() as con:
                    await con.execute("""
                        INSERT INTO service_status(name, last_seen, info) VALUES('collector', now(), $1)
                        ON CONFLICT (name) DO UPDATE SET last_seen=now(), info=EXCLUDED.info""",
                        json.dumps(info))
            except Exception as e:  # noqa: BLE001
                log.warning("Battement de cœur non écrit : %s", e)
            await asyncio.sleep(15)

    # ---- session ---------------------------------------------------------- #
    async def session(self):
        self.subs.clear()
        self.sub_symbol.clear()
        self.sub_days.clear()
        self.client = DerivClient(on_stream=self.on_stream)
        await self.client.connect()
        try:
            if time.time() - self.catalog_at > CATALOG_REFRESH_S:
                await self.refresh_catalog()
            while not self.client.closed.is_set():
                self.wake.clear()
                await self.sync_watchlist()
                waiters = [asyncio.create_task(self.wake.wait()),
                           asyncio.create_task(self.client.closed.wait())]
                await asyncio.wait(waiters, timeout=30, return_when=asyncio.FIRST_COMPLETED)
                for w in waiters:
                    w.cancel()
                if time.time() - self.catalog_at > CATALOG_REFRESH_S:
                    await self.refresh_catalog()
            raise ConnectionError("connexion Deriv perdue")
        finally:
            await self.client.close()


async def upsert_candles(con, rows):
    if rows:
        await con.executemany("""
            INSERT INTO candles(symbol, tf, ts, open, high, low, close) VALUES($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (symbol, tf, ts) DO UPDATE SET
              open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close""", rows)


async def seed_watchlist(pool):
    async with pool.acquire() as con:
        if await con.fetchval("SELECT count(*) FROM watchlist") == 0 and DEFAULT_SYMBOLS:
            await con.executemany("INSERT INTO watchlist(symbol) VALUES($1) ON CONFLICT DO NOTHING",
                                  [(s,) for s in DEFAULT_SYMBOLS])
            log.info("Watchlist initiale : %s", ", ".join(DEFAULT_SYMBOLS))


async def connect_db() -> asyncpg.Pool:
    delay = 2
    while True:
        try:
            return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("Base indisponible (%s), nouvel essai dans %ds", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    pool = await connect_db()
    await apply_schema(pool)
    await seed_watchlist(pool)
    col = Collector(pool)

    listener = await asyncpg.connect(DATABASE_URL)
    await listener.add_listener("watchlist", lambda *_: col.wake.set())

    background = [asyncio.create_task(col.writer()), asyncio.create_task(col.heartbeat())]

    async def run_sessions():
        delay = 5
        while True:
            started = time.monotonic()
            try:
                await col.session()
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, asyncio.TimeoutError, DerivError) as e:
                if time.monotonic() - started > 120:
                    delay = 5
                log.warning("Session Deriv terminée (%s). Reconnexion dans %ds", e, delay)
            except Exception:  # noqa: BLE001 — bug inattendu : on le journalise et on repart
                log.exception("Erreur inattendue dans la session Deriv. Reconnexion dans %ds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    runner = asyncio.create_task(run_sessions())
    await stop.wait()
    log.info("Arrêt demandé")
    runner.cancel()
    for t in background:
        t.cancel()
    await listener.close()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
