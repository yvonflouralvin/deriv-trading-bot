"""
Service « portefeuille paper » dans le moteur.

Une fois par jour (après la clôture américaine, PORTFOLIO_RUN_HOUR_UTC), le service :
  1. télécharge les cours journaliers (Yahoo Finance, API publique) des ETF et du BTC ;
  2. les enregistre (pf_prices) ;
  3. fait avancer le portefeuille virtuel (portfolio.book.Book) jour par jour depuis le dernier jour traité :
     exécution des ordres à l'ouverture, valorisation à la clôture, nouveaux signaux ;
  4. enregistre l'état (pf_meta), la valeur par poche (pf_nav) et le journal (pf_events).

PAPER UNIQUEMENT : aucun ordre n'est transmis à un courtier, aucun compte (démo ou réel) n'est utilisé.
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

import asyncpg

from .book import ALL_SYMBOLS, WEIGHTS, Bar, Book

log = logging.getLogger("portfolio")

CAPITAL = float(os.getenv("PORTFOLIO_CAPITAL", "500"))
RUN_HOUR_UTC = int(os.getenv("PORTFOLIO_RUN_HOUR_UTC", "22"))
ENABLED = os.getenv("PORTFOLIO_PAPER", "1") == "1"
HISTORY_DAYS = 420               # historique nécessaire aux signaux (MM200, 7 fins de mois)
UA = "Mozilla/5.0 (paper-portfolio)"


def _fetch(symbol: str, days: int) -> list[dict]:
    now = int(time.time())
    q = urllib.parse.urlencode({"period1": now - days * 86400, "period2": now + 86400, "interval": "1d", "events": "div,split"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    quote = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, c, a = quote["open"][i], quote["close"][i], adj[i]
        if o is None or c is None or a is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        out.append({"d": d, "open": float(o), "close": float(c), "adj": float(a)})
    return out


class PortfolioService:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.status = {"enabled": ENABLED, "last_run": None, "last_day": None, "nav": None, "error": None}

    async def loop(self):
        if not ENABLED:
            log.info("Portefeuille paper désactivé (PORTFOLIO_PAPER=0)")
            return
        while True:
            try:
                await self.run_if_due()
            except Exception as e:  # noqa: BLE001
                self.status["error"] = str(e)[:300]
                log.exception("Portefeuille paper")
            await asyncio.sleep(600)

    async def run_if_due(self, force: bool = False):
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as con:
            last = await con.fetchval("SELECT value->>'run_date' FROM pf_meta WHERE key='run'")
        if not force and (now.hour < RUN_HOUR_UTC or last == now.date().isoformat()):
            return
        await self.run()
        async with self.pool.acquire() as con:
            await con.execute("""INSERT INTO pf_meta(key, value) VALUES('run', $1)
                                 ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                              json.dumps({"run_date": now.date().isoformat()}))

    async def run(self):
        t0 = time.monotonic()
        # 1) cours
        rows = []
        for sym in ALL_SYMBOLS:
            try:
                for b in await asyncio.to_thread(_fetch, sym, HISTORY_DAYS):
                    rows.append((sym, date.fromisoformat(b["d"]), b["open"], b["close"], b["adj"]))
            except Exception as e:  # noqa: BLE001
                log.warning("Cours %s indisponibles : %s", sym, e)
            await asyncio.sleep(1.2)
        today_utc = datetime.now(timezone.utc).date()
        async with self.pool.acquire() as con:
            # on ne garde que les jours clos : la barre BTC du jour UTC en cours n'est jamais définitive ;
            # celle des ETF l'est après la clôture américaine (RUN_HOUR_UTC)
            closed = lambda r: r[1] < today_utc or (r[0] != "BTC-USD" and datetime.now(timezone.utc).hour >= RUN_HOUR_UTC)  # noqa: E731
            await con.executemany("""
                INSERT INTO pf_prices(symbol, d, open, close, adj) VALUES($1, $2, $3, $4, $5)
                ON CONFLICT (symbol, d) DO UPDATE SET open=EXCLUDED.open, close=EXCLUDED.close, adj=EXCLUDED.adj""",
                [r for r in rows if closed(r)])
            state = await con.fetchval("SELECT value FROM pf_meta WHERE key='book'")
            prices = await con.fetch("""SELECT symbol, d::text AS d, open, close, adj FROM pf_prices
                                        WHERE d >= current_date - $1::int ORDER BY d, symbol""", HISTORY_DAYS)
        book = Book(CAPITAL, json.loads(state) if state else None)
        hist: dict[str, list[Bar]] = {s: [] for s in ALL_SYMBOLS}
        by_day: dict[str, dict[str, Bar]] = {}
        for r in prices:
            b = Bar(r["d"], r["open"], r["close"], r["adj"])
            by_day.setdefault(r["d"], {})[r["symbol"]] = b
        last = book.s["last_d"]
        if last is None:                       # démarrage : on commence au dernier jour disponible
            days_all = sorted(by_day)
            start = days_all[-1] if days_all else None
        else:
            start = None
        events, navs = [], []
        for d in sorted(by_day):
            today = by_day[d]
            for s, b in today.items():
                hist[s].append(b)
            if (last is not None and d <= last) or (last is None and d != start):
                continue
            book.step(d, today, hist)
            events += book.events
            navs.append((d, {k: book.sleeve_nav(k) for k in WEIGHTS}, {k: book.exposure(k) for k in WEIGHTS}, book.nav()))
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("""INSERT INTO pf_meta(key, value) VALUES('book', $1)
                                     ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                                  json.dumps(book.s))
                for d, sl, ex, total in navs:
                    await con.executemany("""INSERT INTO pf_nav(d, sleeve, nav, exposure) VALUES($1, $2, $3, $4)
                                             ON CONFLICT (d, sleeve) DO UPDATE SET nav=EXCLUDED.nav, exposure=EXCLUDED.exposure""",
                                          [(date.fromisoformat(d), k, sl[k], ex[k]) for k in WEIGHTS] + [(date.fromisoformat(d), "total", total, None)])
                await con.executemany("INSERT INTO pf_events(d, kind, payload) VALUES($1, $2, $3)",
                                      [(date.fromisoformat(e["d"]), e["type"], json.dumps(e)) for e in events])
                await con.execute("SELECT pg_notify('engine', $1)", json.dumps({"type": "portfolio", "nav": book.nav()}))
        self.status.update(last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"), last_day=book.s["last_d"],
                           nav=round(book.nav(), 2), error=None)
        log.info("Portefeuille paper : %d jour(s) traité(s), %d événement(s), valeur %.2f $ (%.1f s)",
                 len(navs), len(events), book.nav(), time.monotonic() - t0)
