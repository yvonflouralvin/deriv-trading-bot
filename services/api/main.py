"""
API de la plateforme (FastAPI) — fait le lien entre la base et le Monitor.

REST (préfixe /api) :
  GET    /health                      état (sans authentification)
  GET    /status                      état du collecteur
  GET    /timeframes                  unités de temps disponibles
  GET    /symbols?q=&limit=           catalogue des actifs Deriv
  GET    /watchlist                   actifs suivis (+ dernier prix)
  POST   /watchlist                   {symbol, backfill_days?}
  PATCH  /watchlist/{symbol}          {enabled?, backfill_days?}
  DELETE /watchlist/{symbol}
  GET    /candles?symbol&tf&limit&before
  GET    /drawings/{symbol}           dessins du Monitor
  PUT    /drawings/{symbol}

WebSocket /api/ws : le client envoie {"action":"subscribe","symbol":"R_100","tf":60}
et reçoit {"type":"candle",...} et {"type":"tick",...} en direct.

Authentification optionnelle : si API_TOKEN est défini, chaque appel doit
porter "Authorization: Bearer <token>" (ou ?token=<token> pour le WebSocket).
"""

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Literal

from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("api")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trading:trading@db:5432/trading")
API_TOKEN = os.getenv("API_TOKEN", "")
SCHEMA_FILE = Path(os.getenv("SCHEMA_FILE", Path(__file__).with_name("schema.sql")))
TIMEFRAMES = [int(x) for x in os.getenv("TIMEFRAMES", "60,300,900,3600,14400,86400").split(",")]
TF_LABELS = {60: "1m", 120: "2m", 180: "3m", 300: "5m", 600: "10m", 900: "15m", 1800: "30m",
             3600: "1h", 7200: "2h", 14400: "4h", 28800: "8h", 86400: "1D"}


# --------------------------------------------------------------------------- #
# Diffusion temps réel : LISTEN "market" -> clients WebSocket abonnés
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self):
        self.clients: dict[WebSocket, dict] = {}   # ws -> {"symbol", "tf"}
        self.conn: asyncpg.Connection | None = None

    async def start(self):
        self.conn = await asyncpg.connect(DATABASE_URL)
        await self.conn.add_listener("market", self._on_notify)
        await self.conn.add_listener("engine", self._on_engine)

    async def stop(self):
        if self.conn:
            await self.conn.close()

    def _on_notify(self, _conn, _pid, _channel, payload: str):
        if not self.clients:
            return
        try:
            m = json.loads(payload)
        except ValueError:
            return
        for ws, sub in list(self.clients.items()):
            if sub.get("symbol") != m["s"]:
                continue
            msgs = [{"type": "tick", "symbol": m["s"], "time": m["e"], "price": m["q"]}]
            c = m["c"].get(str(sub.get("tf")))
            if c:
                msgs.append({"type": "candle", "symbol": m["s"], "tf": sub["tf"],
                             "candle": {"time": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4]}})
            for msg in msgs:
                asyncio.create_task(self._send(ws, msg))

    def _on_engine(self, _conn, _pid, _channel, payload: str):
        try:
            ev = json.loads(payload)
        except ValueError:
            return
        for ws, sub in list(self.clients.items()):
            if sub.get("symbol") == ev.get("symbol"):
                asyncio.create_task(self._send(ws, ev))

    async def _send(self, ws: WebSocket, msg: dict):
        try:
            await ws.send_json(msg)
        except Exception:  # noqa: BLE001
            self.clients.pop(ws, None)


hub = Hub()
pool: asyncpg.Pool


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    delay = 2
    while True:
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
            break
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("Base indisponible (%s), nouvel essai dans %ds", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(424242)")
        try:
            await con.execute(SCHEMA_FILE.read_text())
        finally:
            await con.execute("SELECT pg_advisory_unlock(424242)")
    await hub.start()
    log.info("API prête")
    yield
    await hub.stop()
    await pool.close()


app = FastAPI(title="Trading platform API", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])


def _token_ok(value: str | None) -> bool:
    return not API_TOKEN or (value is not None and secrets.compare_digest(value, API_TOKEN))


def auth(request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    if not _token_ok(token):
        raise HTTPException(401, "Token invalide ou manquant")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health():
    async with pool.acquire() as con:
        await con.fetchval("SELECT 1")
    return {"ok": True, "auth_required": bool(API_TOKEN)}


@app.get("/api/status", dependencies=[Depends(auth)])
async def status():
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT name, last_seen, info, extract(epoch FROM now() - last_seen) AS age FROM service_status")
    return {r["name"]: {"last_seen": r["last_seen"].isoformat(), "age_s": int(r["age"]),
                        "alive": r["age"] < 60, **json.loads(r["info"])} for r in rows}


@app.get("/api/timeframes", dependencies=[Depends(auth)])
async def timeframes():
    return [{"tf": tf, "label": TF_LABELS.get(tf, f"{tf}s")} for tf in TIMEFRAMES]


@app.get("/api/symbols", dependencies=[Depends(auth)])
async def symbols(q: str = "", limit: int = Query(50, le=500)):
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT s.symbol, s.display_name, s.market_display, s.submarket_display, s.exchange_open,
                   (w.symbol IS NOT NULL) AS watched
            FROM symbols s LEFT JOIN watchlist w USING (symbol)
            WHERE $1 = '' OR s.symbol ILIKE '%' || $1 || '%' OR s.display_name ILIKE '%' || $1 || '%'
            ORDER BY s.market_display, s.display_name LIMIT $2""", q.strip(), limit)
    return [dict(r) for r in rows]


@app.get("/api/watchlist", dependencies=[Depends(auth)])
async def get_watchlist():
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT w.symbol, w.enabled, w.backfill_days, coalesce(s.display_name, w.symbol) AS display_name,
                   s.market_display, lt.quote AS last_price, extract(epoch FROM lt.ts)::bigint AS last_time
            FROM watchlist w
            LEFT JOIN symbols s USING (symbol)
            LEFT JOIN LATERAL (SELECT quote, ts FROM ticks t WHERE t.symbol = w.symbol
                               ORDER BY ts DESC LIMIT 1) lt ON true
            ORDER BY w.created_at""")
    return [dict(r) for r in rows]


class WatchIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    backfill_days: int = Field(7, ge=1, le=365)


class WatchPatch(BaseModel):
    enabled: bool | None = None
    backfill_days: int | None = Field(None, ge=1, le=365)


@app.post("/api/watchlist", dependencies=[Depends(auth)], status_code=201)
async def add_watch(body: WatchIn):
    async with pool.acquire() as con:
        await con.execute("""
            INSERT INTO watchlist(symbol, backfill_days) VALUES($1, $2)
            ON CONFLICT (symbol) DO UPDATE SET enabled = true, backfill_days = EXCLUDED.backfill_days""",
            body.symbol.strip(), body.backfill_days)
    return {"ok": True}


@app.patch("/api/watchlist/{symbol}", dependencies=[Depends(auth)])
async def patch_watch(symbol: str, body: WatchPatch):
    async with pool.acquire() as con:
        res = await con.execute("""
            UPDATE watchlist SET enabled = coalesce($2, enabled),
                                 backfill_days = coalesce($3, backfill_days)
            WHERE symbol = $1""", symbol, body.enabled, body.backfill_days)
    if res.endswith(" 0"):
        raise HTTPException(404, "Actif absent de la watchlist")
    return {"ok": True}


@app.delete("/api/watchlist/{symbol}", dependencies=[Depends(auth)])
async def delete_watch(symbol: str):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM watchlist WHERE symbol = $1", symbol)
    return {"ok": True}


@app.get("/api/candles", dependencies=[Depends(auth)])
async def candles(symbol: str, tf: int = 60, limit: int = Query(1000, ge=1, le=5000),
                  before: int | None = None):
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT extract(epoch FROM ts)::bigint AS time, open, high, low, close FROM candles
            WHERE symbol = $1 AND tf = $2 AND ($3::bigint IS NULL OR ts < to_timestamp($3))
            ORDER BY ts DESC LIMIT $4""", symbol, tf, before, limit)
    return [dict(r) for r in reversed(rows)]


@app.get("/api/drawings/{symbol}", dependencies=[Depends(auth)])
async def get_drawings(symbol: str):
    async with pool.acquire() as con:
        data = await con.fetchval("SELECT data FROM drawings WHERE symbol = $1", symbol)
    return json.loads(data) if data else []


@app.put("/api/drawings/{symbol}", dependencies=[Depends(auth)])
async def put_drawings(symbol: str, request: Request):
    body = await request.body()
    if len(body) > 2_000_000:
        raise HTTPException(413, "Trop de dessins")
    try:
        data = json.loads(body or b"[]")
    except ValueError:
        raise HTTPException(400, "JSON invalide") from None
    async with pool.acquire() as con:
        await con.execute("""
            INSERT INTO drawings(symbol, data, updated_at) VALUES($1, $2, now())
            ON CONFLICT (symbol) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
            symbol, json.dumps(data))
    return {"ok": True}


@app.websocket("/api/ws")
async def ws_endpoint(ws: WebSocket, token: str | None = None):
    if not _token_ok(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.clients[ws] = {}
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "subscribe":
                hub.clients[ws] = {"symbol": msg.get("symbol"), "tf": int(msg.get("tf", 60))}
                await ws.send_json({"type": "subscribed", **hub.clients[ws]})
            elif msg.get("action") == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        hub.clients.pop(ws, None)


# --------------------------------------------------------------------------- #
# Moteur de stratégie
# --------------------------------------------------------------------------- #
def _epoch(dt):
    return int(dt.timestamp()) if dt else None


def _js(v):
    return json.loads(v) if isinstance(v, str) else v


TODAY = "(date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


@app.get("/api/strategy-kinds", dependencies=[Depends(auth)])
async def strategy_kinds():
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT kind, name, description, params FROM strategy_kinds ORDER BY name")
    return [{**dict(r), "params": _js(r["params"])} for r in rows]


class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str
    symbol: str = Field(min_length=1, max_length=40)
    tf: int
    params: dict = {}
    mode: Literal["paper", "demo"] = "paper"
    enabled: bool = False
    stake: float = Field(1, gt=0, le=10000)
    duration: int = Field(5, ge=1, le=1440)
    duration_unit: Literal["t", "s", "m"] = "t"
    payout_ratio: float = Field(0.95, gt=0, le=10)
    max_trades_per_day: int = Field(20, ge=1, le=10000)
    max_daily_loss: float = Field(10, gt=0, le=1_000_000)


class StrategyPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    symbol: str | None = None
    tf: int | None = None
    params: dict | None = None
    mode: Literal["paper", "demo"] | None = None
    enabled: bool | None = None
    stake: float | None = Field(None, gt=0, le=10000)
    duration: int | None = Field(None, ge=1, le=1440)
    duration_unit: Literal["t", "s", "m"] | None = None
    payout_ratio: float | None = Field(None, gt=0, le=10)
    max_trades_per_day: int | None = Field(None, ge=1, le=10000)
    max_daily_loss: float | None = Field(None, gt=0, le=1_000_000)


async def _check_strategy(con, kind: str | None, tf: int | None):
    if kind is not None and not await con.fetchval("SELECT 1 FROM strategy_kinds WHERE kind=$1", kind):
        raise HTTPException(400, f"Type de stratégie inconnu : {kind} (le moteur est-il démarré ?)")
    if tf is not None and tf not in TIMEFRAMES:
        raise HTTPException(400, f"Unité de temps non collectée : {tf}")


async def _ensure_watched(con, symbol: str):
    """Une stratégie active a besoin des données de son actif."""
    await con.execute("""INSERT INTO watchlist(symbol) VALUES($1)
                         ON CONFLICT (symbol) DO UPDATE SET enabled = true""", symbol)


STRATEGY_SQL = f"""
    SELECT s.*, k.name AS kind_name,
      coalesce(d.n, 0) AS today_trades, coalesce(d.pnl, 0) AS today_pnl, coalesce(d.wins, 0) AS today_wins,
      coalesce(a.n, 0) AS total_trades, coalesce(a.pnl, 0) AS total_pnl, coalesce(a.wins, 0) AS total_wins,
      coalesce(a.open, 0) AS open_trades, coalesce(a.errors, 0) AS errors
    FROM strategies s
    LEFT JOIN strategy_kinds k ON k.kind = s.kind
    LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE status IN ('won','lost')) n, sum(profit) pnl,
                              count(*) FILTER (WHERE status='won') wins
                       FROM trades t WHERE t.strategy_id = s.id AND t.entry_time >= {TODAY}) d ON true
    LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE status IN ('won','lost')) n, sum(profit) pnl,
                              count(*) FILTER (WHERE status='won') wins,
                              count(*) FILTER (WHERE status='open') open,
                              count(*) FILTER (WHERE status='error') errors
                       FROM trades t WHERE t.strategy_id = s.id) a ON true
"""


def _strategy_out(r) -> dict:
    d = dict(r)
    d["params"] = _js(d["params"])
    d["created_at"], d["updated_at"] = _epoch(d["created_at"]), _epoch(d["updated_at"])
    return d


@app.get("/api/strategies", dependencies=[Depends(auth)])
async def list_strategies(symbol: str | None = None):
    async with pool.acquire() as con:
        rows = await con.fetch(STRATEGY_SQL + " WHERE $1::text IS NULL OR s.symbol = $1 ORDER BY s.id", symbol)
    return [_strategy_out(r) for r in rows]


@app.post("/api/strategies", dependencies=[Depends(auth)], status_code=201)
async def create_strategy(body: StrategyIn):
    async with pool.acquire() as con, con.transaction():
        await _check_strategy(con, body.kind, body.tf)
        sid = await con.fetchval("""
            INSERT INTO strategies(name, kind, symbol, tf, params, mode, enabled, stake, duration, duration_unit,
                                   payout_ratio, max_trades_per_day, max_daily_loss)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id""",
            body.name, body.kind, body.symbol, body.tf, json.dumps(body.params), body.mode, body.enabled,
            body.stake, body.duration, body.duration_unit, body.payout_ratio, body.max_trades_per_day,
            body.max_daily_loss)
        await _ensure_watched(con, body.symbol)
        row = await con.fetchrow(STRATEGY_SQL + " WHERE s.id = $1", sid)
    return _strategy_out(row)


@app.patch("/api/strategies/{sid}", dependencies=[Depends(auth)])
async def patch_strategy(sid: int, body: StrategyPatch):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Rien à modifier")
    async with pool.acquire() as con, con.transaction():
        await _check_strategy(con, None, fields.get("tf"))
        if "params" in fields:
            fields["params"] = json.dumps(fields["params"])
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
        row = await con.fetchrow(f"UPDATE strategies SET {sets}, updated_at = now() WHERE id = $1 RETURNING symbol, enabled",
                                 sid, *fields.values())
        if not row:
            raise HTTPException(404, "Stratégie introuvable")
        if row["enabled"]:
            await _ensure_watched(con, row["symbol"])
        out = await con.fetchrow(STRATEGY_SQL + " WHERE s.id = $1", sid)
    return _strategy_out(out)


@app.delete("/api/strategies/{sid}", dependencies=[Depends(auth)])
async def delete_strategy(sid: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM strategies WHERE id = $1", sid)
    return {"ok": True}


def _trade_out(r) -> dict:
    d = dict(r)
    for k in ("entry_time", "exit_time", "created_at"):
        d[k] = _epoch(d[k])
    return d


@app.get("/api/trades", dependencies=[Depends(auth)])
async def list_trades(symbol: str | None = None, strategy_id: int | None = None, since: int | None = None,
                      limit: int = Query(500, ge=1, le=5000)):
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT t.id, t.strategy_id, s.name AS strategy_name, t.mode, t.symbol, t.side, t.stake, t.payout,
                   t.duration, t.duration_unit, t.entry_time, t.entry_spot, t.exit_time, t.exit_spot, t.profit,
                   t.status, t.contract_id, t.error, t.created_at
            FROM trades t LEFT JOIN strategies s ON s.id = t.strategy_id
            WHERE ($1::text IS NULL OR t.symbol = $1) AND ($2::int IS NULL OR t.strategy_id = $2)
              AND ($3::bigint IS NULL OR t.entry_time >= to_timestamp($3))
            ORDER BY t.entry_time DESC LIMIT $4""", symbol, strategy_id, since, limit)
    return [_trade_out(r) for r in rows]


@app.get("/api/signals", dependencies=[Depends(auth)])
async def list_signals(strategy_id: int, limit: int = Query(100, ge=1, le=1000)):
    async with pool.acquire() as con:
        rows = await con.fetch("""SELECT id, strategy_id, symbol, tf, candle_ts, side, price, action, created_at
                                  FROM signals WHERE strategy_id = $1 ORDER BY id DESC LIMIT $2""", strategy_id, limit)
    return [{**dict(r), "candle_ts": _epoch(r["candle_ts"]), "created_at": _epoch(r["created_at"])} for r in rows]


class BacktestIn(BaseModel):
    strategy_id: int | None = None
    kind: str | None = None
    symbol: str | None = None
    tf: int | None = None
    params: dict | None = None
    stake: float | None = Field(None, gt=0)
    duration: int | None = Field(None, ge=1)
    duration_unit: Literal["t", "s", "m"] | None = None
    payout_ratio: float | None = Field(None, gt=0)
    days: float = Field(7, gt=0, le=365)
    date_to: int | None = None


@app.post("/api/backtests", dependencies=[Depends(auth)], status_code=201)
async def create_backtest(body: BacktestIn):
    """Les champs fournis remplacent ceux de la stratégie (permet de tester des variantes)."""
    cfg = {"stake": 1.0, "duration": 5, "duration_unit": "t", "payout_ratio": 0.95, "params": {}}
    async with pool.acquire() as con:
        if body.strategy_id is not None:
            st = await con.fetchrow("SELECT * FROM strategies WHERE id = $1", body.strategy_id)
            if not st:
                raise HTTPException(404, "Stratégie introuvable")
            cfg.update({k: st[k] for k in ("kind", "symbol", "tf", "stake", "duration", "duration_unit", "payout_ratio")})
            cfg["params"] = _js(st["params"])
        cfg.update({k: v for k, v in body.model_dump(exclude={"strategy_id", "days", "date_to"}).items() if v is not None})
        for k in ("kind", "symbol", "tf"):
            if k not in cfg:
                raise HTTPException(400, f"Champ manquant : {k}")
        await _check_strategy(con, cfg["kind"], cfg["tf"])
        end = body.date_to or int(time.time())
        bid = await con.fetchval("""
            INSERT INTO backtests(strategy_id, kind, symbol, tf, params, stake, duration, duration_unit, payout_ratio,
                                  date_from, date_to)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,to_timestamp($10),to_timestamp($11)) RETURNING id""",
            body.strategy_id, cfg["kind"], cfg["symbol"], cfg["tf"], json.dumps(cfg["params"]), cfg["stake"],
            cfg["duration"], cfg["duration_unit"], cfg["payout_ratio"], end - int(body.days * 86400), end)
    return {"id": bid}


def _backtest_out(r, with_trades: bool) -> dict:
    d = dict(r)
    d["params"] = _js(d["params"])
    d["stats"] = _js(d["stats"])
    d["trades"] = _js(d["trades"]) if with_trades else None
    for k in ("date_from", "date_to", "created_at", "finished_at"):
        d[k] = _epoch(d[k])
    return d


@app.get("/api/backtests", dependencies=[Depends(auth)])
async def list_backtests(strategy_id: int | None = None, limit: int = Query(20, ge=1, le=200)):
    async with pool.acquire() as con:
        rows = await con.fetch("""SELECT * FROM backtests WHERE $1::int IS NULL OR strategy_id = $1
                                  ORDER BY id DESC LIMIT $2""", strategy_id, limit)
    return [_backtest_out(r, False) for r in rows]


@app.get("/api/backtests/{bid}", dependencies=[Depends(auth)])
async def get_backtest(bid: int):
    async with pool.acquire() as con:
        r = await con.fetchrow("SELECT * FROM backtests WHERE id = $1", bid)
    if not r:
        raise HTTPException(404, "Backtest introuvable")
    return _backtest_out(r, True)


# --------------------------------------------------------------------------- #
# Analyse : tendance multi-timeframe
# --------------------------------------------------------------------------- #
TREND_TFS = [int(x) for x in os.getenv("TREND_TFS", "14400,3600,900").split(",")]


@app.get("/api/trend-config", dependencies=[Depends(auth)])
async def trend_config():
    return {"tfs": [{"tf": tf, "label": TF_LABELS.get(tf, f"{tf}s")} for tf in TREND_TFS]}


@app.get("/api/trends", dependencies=[Depends(auth)])
async def trends(symbol: str, since: int | None = None, limit: int = Query(20000, le=100000)):
    """États par unité de temps : [[début de bougie, état, force], ...] (ordre chronologique)."""
    out = {}
    async with pool.acquire() as con:
        for tf in TREND_TFS:
            rows = await con.fetch("""
                SELECT extract(epoch FROM ts)::bigint AS t, state, strength FROM trend_states
                WHERE symbol=$1 AND tf=$2 AND ($3::bigint IS NULL OR ts >= to_timestamp($3) - make_interval(secs => $2 * 2))
                ORDER BY ts DESC LIMIT $4""", symbol, tf, since, limit)
            out[str(tf)] = [[r["t"], r["state"], r["strength"]] for r in reversed(rows)]
    return {"tfs": TREND_TFS, "states": out}


@app.get("/api/trends/current", dependencies=[Depends(auth)])
async def trends_current():
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT w.symbol, coalesce(s.display_name, w.symbol) AS display_name, w.backfill_days,
                   ts.tf, ts.state, ts.strength, ts.adx, extract(epoch FROM ts.ts)::bigint AS t
            FROM watchlist w
            LEFT JOIN symbols s USING (symbol)
            LEFT JOIN LATERAL (
                SELECT DISTINCT ON (tf) tf, state, strength, adx, ts FROM trend_states x
                WHERE x.symbol = w.symbol AND x.tf = ANY($1::int[]) ORDER BY tf, ts DESC) ts ON true
            WHERE w.enabled ORDER BY w.symbol""", TREND_TFS)
    by = {}
    for r in rows:
        d = by.setdefault(r["symbol"], {"symbol": r["symbol"], "display_name": r["display_name"],
                                         "backfill_days": r["backfill_days"], "states": {}})
        if r["tf"] is not None:
            d["states"][str(r["tf"])] = {"state": r["state"], "strength": r["strength"], "adx": r["adx"],
                                         "time": r["t"]}
    for d in by.values():
        vals = [d["states"].get(str(tf), {}).get("state") for tf in TREND_TFS]
        d["align"] = None if None in vals else (1 if set(vals) == {1} else -1 if set(vals) == {-1} else 0)
        d["strength"] = round(sum(d["states"][str(tf)]["strength"] for tf in TREND_TFS) / len(TREND_TFS)) \
            if d["align"] is not None else None
    return list(by.values())


class TrendReportIn(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=20)
    days: int = Field(90, ge=7, le=365)
    variants: list[str] = []          # vide = toutes les variantes


@app.post("/api/trend-reports", dependencies=[Depends(auth)], status_code=201)
async def create_trend_report(body: TrendReportIn):
    async with pool.acquire() as con:
        rid = await con.fetchval("INSERT INTO trend_reports(symbols, tfs, days, variants) VALUES($1, $2, $3, $4) "
                                 "RETURNING id", body.symbols, TREND_TFS, body.days, body.variants)
    return {"id": rid}


def _report_out(r, full: bool) -> dict:
    d = dict(r)
    d["result"] = _js(d["result"]) if full else None
    d["created_at"], d["finished_at"] = _epoch(d["created_at"]), _epoch(d["finished_at"])
    return d


@app.get("/api/trend-reports", dependencies=[Depends(auth)])
async def list_trend_reports(limit: int = Query(10, ge=1, le=100)):
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT * FROM trend_reports ORDER BY id DESC LIMIT $1", limit)
    return [_report_out(r, False) for r in rows]


@app.get("/api/trend-reports/{rid}", dependencies=[Depends(auth)])
async def get_trend_report(rid: int):
    async with pool.acquire() as con:
        r = await con.fetchrow("SELECT * FROM trend_reports WHERE id = $1", rid)
    if not r:
        raise HTTPException(404, "Rapport introuvable")
    return _report_out(r, True)


# --------------------------------------------------------------------------- #
# Portefeuille diversifié en paper trading (calculé par le moteur, voir services/engine/portfolio)
# --------------------------------------------------------------------------- #
@app.get("/api/portfolio", dependencies=[Depends(auth)])
async def portfolio(events: int = Query(50, ge=0, le=1000)):
    async with pool.acquire() as con:
        book = await con.fetchval("SELECT value FROM pf_meta WHERE key='book'")
        navs = await con.fetch("SELECT d, sleeve, nav, exposure FROM pf_nav ORDER BY d, sleeve")
        evs = await con.fetch("SELECT d, kind, payload FROM pf_events ORDER BY id DESC LIMIT $1", events)
    book = _js(book) if book else None
    series: dict[str, list] = {}
    for r in navs:
        series.setdefault(r["sleeve"], []).append({"d": r["d"].isoformat(), "nav": r["nav"], "exposure": r["exposure"]})
    # rendements mensuels du total (dernière valeur de chaque mois vs dernière valeur du mois précédent)
    monthly, prev, last_by_month = [], None, {}
    for p in series.get("total", []):
        last_by_month[p["d"][:7]] = p["nav"]
    start = book["capital"] if book else None
    for m in sorted(last_by_month):
        base = prev if prev is not None else start
        monthly.append({"mois": m, "nav": last_by_month[m], "rendement": (last_by_month[m] / base - 1) if base else None})
        prev = last_by_month[m]
    positions = []
    if book:
        for sleeve, sl in book["sleeves"].items():
            for sym, p in sl["pos"].items():
                px = book["last_px"].get(sym, p["price"])
                positions.append({"poche": sleeve, "symbole": sym, "unites": p["units"], "prix_entree": p["price"],
                                  "dernier_prix": px, "valeur": p["units"] * px, "depuis": p["since"]})
    return {"mode": "paper", "capital_initial": start, "dernier_jour": book["last_d"] if book else None,
            "valeur": series["total"][-1]["nav"] if series.get("total") else start,
            "poches": {k: v[-1] for k, v in series.items() if k != "total" and v},
            "positions": positions, "ordres_en_attente": book["pending"] if book else [],
            "mensuel": monthly, "historique": series.get("total", []),
            "journal": [{"d": r["d"].isoformat(), "type": r["kind"], **_js(r["payload"])} for r in evs]}
