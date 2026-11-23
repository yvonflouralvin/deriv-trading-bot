"""
Service d'analyse dans le moteur :
  - calcule et stocke en continu l'état de tendance de chaque actif suivi,
    pour chaque unité de temps de TREND_TFS (bougies clôturées uniquement) ;
  - exécute les rapports d'évaluation demandés depuis le Monitor.
"""

import asyncio
import json
import logging
import os
import time

import asyncpg

from strategies import Bar

from . import report, trend

log = logging.getLogger("analysis")

TREND_TFS = [int(x) for x in os.getenv("TREND_TFS", "14400,3600,900").split(",")]
SYNC_EVERY_S = 30
INCREMENTAL_BARS = 1500


def _bars(rows):
    return [Bar(r["t"], r["open"], r["high"], r["low"], r["close"]) for r in rows]


class AnalysisService:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.reports: asyncio.Queue = asyncio.Queue()

    # ---- états de tendance ------------------------------------------------- #
    async def sync_loop(self):
        while True:
            try:
                async with self.pool.acquire() as con:
                    symbols = [r["symbol"] for r in await con.fetch("SELECT symbol FROM watchlist WHERE enabled")]
                for sym in symbols:
                    for tf in TREND_TFS:
                        await self.sync(sym, tf)
            except Exception:  # noqa: BLE001
                log.exception("Synchronisation des tendances")
            await asyncio.sleep(SYNC_EVERY_S)

    async def sync(self, symbol: str, tf: int) -> int:
        now = int(time.time())
        async with self.pool.acquire() as con:
            row = await con.fetchrow("""
                SELECT extract(epoch FROM max(ts))::bigint AS last, min(version) AS vmin, max(version) AS vmax
                FROM trend_states WHERE symbol=$1 AND tf=$2""", symbol, tf)
            full = row["last"] is None or row["vmin"] != trend.VERSION or row["vmax"] != trend.VERSION
            last = None if full else row["last"]
            rows = await con.fetch("""
                SELECT extract(epoch FROM ts)::bigint AS t, open, high, low, close FROM candles
                WHERE symbol=$1 AND tf=$2 AND ts + make_interval(secs => $2) <= to_timestamp($3)
                  AND ($4::bigint IS NULL OR ts >= to_timestamp($4))
                ORDER BY ts""", symbol, tf, now, None if full else last - INCREMENTAL_BARS * tf)
            if not rows or (last is not None and rows[-1]["t"] <= last):
                return 0
            states = await asyncio.to_thread(trend.compute, _bars(rows))
            new = [s for s in states if last is None or s.time > last]
            if not new:
                return 0
            async with con.transaction():
                if full:
                    await con.execute("DELETE FROM trend_states WHERE symbol=$1 AND tf=$2", symbol, tf)
                await con.copy_records_to_table(
                    "trend_states",
                    columns=["symbol", "tf", "ts", "state", "score", "strength", "adx", "structure", "ema", "di", "version"],
                    records=[(symbol, tf, _ts(s.time), s.state, s.score, s.strength, s.adx, s.structure, s.ema, s.di,
                              trend.VERSION) for s in new])
        if full:
            log.info("Tendances %s %ss : %d états calculés", symbol, tf, len(new))
        return len(new)

    # ---- rapports ---------------------------------------------------------- #
    async def report_worker(self):
        async with self.pool.acquire() as con:
            for r in await con.fetch("SELECT id FROM trend_reports WHERE status IN ('pending','running') ORDER BY id"):
                self.reports.put_nowait(r["id"])
        while True:
            rid = await self.reports.get()
            try:
                await self.run_report(rid)
            except Exception as e:  # noqa: BLE001
                log.exception("Rapport %s en erreur", rid)
                async with self.pool.acquire() as con:
                    await con.execute("UPDATE trend_reports SET status='error', error=$2, finished_at=now() WHERE id=$1",
                                      rid, str(e)[:500])

    async def run_report(self, rid: int):
        async with self.pool.acquire() as con:
            rep = await con.fetchrow("UPDATE trend_reports SET status='running' WHERE id=$1 "
                                     "AND status IN ('pending','running') RETURNING *", rid)
            if not rep:
                return
        t0 = time.monotonic()
        end = int(time.time())
        start = end - rep["days"] * 86400
        params = trend.TrendParams()
        results, missing = [], []
        for sym in rep["symbols"]:
            data = {}
            async with self.pool.acquire() as con:
                for tf in rep["tfs"]:
                    warm = (params.warmup + 50) * tf
                    rows = await con.fetch("""
                        SELECT extract(epoch FROM ts)::bigint AS t, open, high, low, close FROM candles
                        WHERE symbol=$1 AND tf=$2 AND ts >= to_timestamp($3)
                          AND ts + make_interval(secs => $2) <= to_timestamp($4) ORDER BY ts""",
                        sym, tf, start - warm, end)
                    data[tf] = _bars(rows)
            if any(len(v) <= params.warmup for v in data.values()):
                missing.append(sym)
                continue
            first = min(v[0].time for v in data.values())
            res = await asyncio.to_thread(report.evaluate_symbol, sym, data, list(rep["tfs"]), max(start, first), end, params)
            res["first_candle"] = first
            results.append(res)
        result = {
            "symbols": results,
            "pooled": report.pooled(results, rep["tfs"]) if results else {},
            "missing": missing,
            "horizons": list(report.HORIZONS_H),
            "params": params.__dict__,
            "version": trend.VERSION,
            "seconds": round(time.monotonic() - t0, 2),
        }
        async with self.pool.acquire() as con:
            await con.execute("UPDATE trend_reports SET status='done', result=$2, finished_at=now() WHERE id=$1",
                              rid, json.dumps(result))
        log.info("Rapport de tendance %s : %d paire(s), %s s", rid, len(results), result["seconds"])


def _ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
