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
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
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
