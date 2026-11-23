"""
Télécharge l'historique d'un marché Deriv dans un CSV (pour backtester).

Exemples :
  python fetch_history.py --symbol R_100 --granularity 60 --days 7     # bougies 1 min sur 7 jours
  python fetch_history.py --symbol R_100 --ticks --count 5000           # 5000 derniers ticks
  python fetch_history.py --symbol frxEURUSD --granularity 3600 --days 90

Granularités acceptées (secondes) : 60, 120, 180, 300, 600, 900, 1800,
3600, 7200, 14400, 28800, 86400. L'API renvoie au plus 5000 points par
requête : le script pagine en remontant dans le temps.
"""

import argparse
import asyncio
import csv
import os
import time
from datetime import datetime, timezone

import deriv_bot as db

MAX_PER_CALL = 5000


async def connect() -> db.DerivClient:
    client = db.DerivClient(db.WS_URL)
    if db.USE_NEW_API and not os.getenv("DERIV_WS_URL"):
        client.url = db.get_otp_ws_url()
    await client.connect()
    return client


async def fetch_candles(client, symbol, granularity, start, end):
    rows, cursor = {}, end
    while cursor > start:
        resp = await client.send({
            "ticks_history": symbol, "style": "candles", "granularity": granularity,
            "start": start, "end": cursor, "count": MAX_PER_CALL, "adjust_start_time": 1,
        }, timeout=60)
        candles = resp.get("candles") or []
        if not candles:
            break
        for c in candles:
            rows[c["epoch"]] = c
        oldest = candles[0]["epoch"]
        db.log.info("%d bougies récupérées (jusqu'à %s)", len(rows), _fmt(oldest))
        if oldest <= start or len(candles) < MAX_PER_CALL:
            break
        cursor = oldest - 1
    return [rows[k] for k in sorted(rows)]


async def fetch_ticks(client, symbol, count):
    resp = await client.send({
        "ticks_history": symbol, "style": "ticks", "end": "latest", "count": min(count, MAX_PER_CALL),
    }, timeout=60)
    h = resp.get("history", {})
    return [{"epoch": t, "quote": p} for t, p in zip(h.get("times", []), h.get("prices", []))]


def _fmt(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M")


def save(rows, path):
    if not rows:
        db.log.warning("Aucune donnée reçue.")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        fields = ["time_utc"] + list(rows[0].keys())
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({"time_utc": _fmt(int(r["epoch"])), **r})
    db.log.info("%d lignes écrites dans %s", len(rows), path)


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="R_100")
    ap.add_argument("--granularity", type=int, default=60, help="taille d'une bougie en secondes")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--ticks", action="store_true", help="ticks bruts au lieu de bougies")
    ap.add_argument("--count", type=int, default=5000, help="nombre de ticks (max 5000)")
    ap.add_argument("--out", help="fichier CSV de sortie")
    a = ap.parse_args()

    client = await connect()
    try:
        if a.ticks:
            rows = await fetch_ticks(client, a.symbol, a.count)
            out = a.out or f"history_{a.symbol}_ticks.csv"
        else:
            end = int(time.time())
            rows = await fetch_candles(client, a.symbol, a.granularity, end - int(a.days * 86400), end)
            out = a.out or f"history_{a.symbol}_{a.granularity}s_{a.days:g}d.csv"
        save(rows, out)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
