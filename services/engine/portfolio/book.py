"""
Portefeuille diversifié en paper trading (logique pure, sans base de données).

Trois poches, rééquilibrées entre elles au début de chaque mois :
  * rotation (40 %) : fin de mois, les 3 ETF de ROTATION au meilleur rendement sur 6 mois
    (s'il est positif) à parts égales, sinon liquidités ;
  * creux RSI2 (35 %, levier 2) : ETF au-dessus de sa moyenne 200 j avec RSI(2) < 10 -> achat,
    sortie à la première clôture au-dessus de la moyenne 5 j (ou après 20 jours) ; 5 positions max,
    chacune = 2/5 de la poche ;
  * tendance BTC (25 %) : BTC au-dessus de sa moyenne 50 j -> investi, sinon liquidités.

Règles d'exécution (causales) :
  * les signaux sont calculés à la clôture d'un jour J et exécutés à l'ouverture du jour de cotation suivant ;
  * la rotation, décidée sur la clôture de fin de mois, est exécutée à l'ouverture du 1er jour du mois ;
  * coûts : 0,05 % par transaction sur les ETF, 0,10 % sur le BTC ; financement du levier sur le solde négatif.

Paper uniquement : aucun ordre n'est envoyé à un courtier.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

ROTATION = ["SPY", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC"]
RSI2_UNIV = ["SPY", "QQQ", "IWM", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB"]
BTC = "BTC-USD"
ALL_SYMBOLS = sorted(set(ROTATION) | set(RSI2_UNIV) | {BTC})

WEIGHTS = {"rotation": 0.40, "rsi2": 0.35, "btc": 0.25}
RSI2_LEVERAGE = 2.0
RSI2_MAX_POS = 5
RSI2_MAX_DAYS = 20
COST = {"etf": 0.0005, "btc": 0.0010}
FIN_ANNUAL = 0.065                     # coût du levier (taux court + marge du courtier), par an


@dataclass
class Bar:
    d: str            # date ISO AAAA-MM-JJ
    open: float
    close: float      # cours brut (valorisation)
    adj: float        # cours ajusté des dividendes (signaux)


# --------------------------------------------------------------------------- #
# Indicateurs (identiques à ceux du backtest)
# --------------------------------------------------------------------------- #
def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi_last(xs: list[float], n: int) -> float | None:
    """RSI de Wilder (moyenne exponentielle alpha = 1/n, initialisée sur la 1re variation) — comme pandas ewm(adjust=False)."""
    if len(xs) < n + 2:
        return None
    a = 1.0 / n
    g = l = None
    for p, q in zip(xs[:-1], xs[1:]):
        up, dn = max(q - p, 0.0), max(p - q, 0.0)
        g = up if g is None else a * up + (1 - a) * g
        l = dn if l is None else a * dn + (1 - a) * l
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + g / l)


# --------------------------------------------------------------------------- #
class Book:
    def __init__(self, capital: float = 500.0, state: dict | None = None):
        if state:
            self.s = copy.deepcopy(state)
        else:
            self.s = {
                "capital": capital,
                "sleeves": {k: {"cash": capital * w, "pos": {}} for k, w in WEIGHTS.items()},
                "rsi2_days": {},          # symbole -> jours détenus
                "pending": [],            # ordres à exécuter à la prochaine ouverture
                "last_d": None,           # dernier jour traité
                "last_etf_d": None,       # dernier jour de cotation des ETF traité
                "last_px": {},            # dernier cours brut connu par symbole
            }
        self.events: list[dict] = []      # journal du dernier step (ordres exécutés, signaux)

    # ---- valorisation ------------------------------------------------------ #
    def sleeve_nav(self, name: str) -> float:
        sl = self.s["sleeves"][name]
        return sl["cash"] + sum(p["units"] * self.s["last_px"].get(sym, p["price"]) for sym, p in sl["pos"].items())

    def nav(self) -> float:
        return sum(self.sleeve_nav(k) for k in WEIGHTS)

    def exposure(self, name: str) -> float:
        sl = self.s["sleeves"][name]
        nv = self.sleeve_nav(name)
        gross = sum(abs(p["units"]) * self.s["last_px"].get(sym, p["price"]) for sym, p in sl["pos"].items())
        return gross / nv if nv > 0 else 0.0

    # ---- exécution --------------------------------------------------------- #
    def _trade(self, d: str, sleeve: str, sym: str, target_units: float, px: float, reason: str):
        sl = self.s["sleeves"][sleeve]
        cur = sl["pos"].get(sym, {"units": 0.0, "price": px, "since": d})
        delta = target_units - cur["units"]
        if abs(delta) * px < 0.01:
            return
        cost = abs(delta) * px * (COST["btc"] if sym == BTC else COST["etf"])
        sl["cash"] -= delta * px + cost
        if abs(target_units) < 1e-12:
            sl["pos"].pop(sym, None)
        else:
            if cur["units"] == 0:
                cur = {"units": 0.0, "price": px, "since": d}
            cur["units"] = target_units
            cur["price"] = px
            sl["pos"][sym] = cur
        self.events.append({"type": "ordre", "d": d, "poche": sleeve, "symbole": sym, "unites": round(delta, 6),
                            "prix": px, "cout": round(cost, 4), "raison": reason})

    def _order(self, d: str, sleeve: str, sym: str, weight: float, ref_px: float, reason: str):
        """ordre « poids cible » à exécuter à la prochaine ouverture du symbole"""
        self.s["pending"] = [o for o in self.s["pending"] if not (o["sleeve"] == sleeve and o["sym"] == sym)]
        self.s["pending"].append({"created": d, "sleeve": sleeve, "sym": sym, "weight": weight, "ref_px": ref_px, "reason": reason})

    # ---- un jour --------------------------------------------------------------- #
    def step(self, d: str, today: dict[str, Bar], hist: dict[str, list[Bar]]):
        """today : barres du jour d (symboles qui cotent ce jour) ; hist : historique jusqu'à d INCLUS."""
        self.events = []
        last = self.s["last_d"]
        if last is not None and d <= last:
            return
        # financement du levier (jours calendaires écoulés)
        if last is not None:
            from datetime import date
            days = (date.fromisoformat(d) - date.fromisoformat(last)).days
            for sl in self.s["sleeves"].values():
                if sl["cash"] < 0:
                    sl["cash"] += sl["cash"] * FIN_ANNUAL / 365 * days
        etf_day = any(s in today for s in RSI2_UNIV)
        new_month = etf_day and self.s["last_etf_d"] is not None and d[:7] != self.s["last_etf_d"][:7]

        # 1) début de mois : rééquilibrage des poches puis rotation, à l'ouverture
        if new_month:
            for sym, b in today.items():
                self.s["last_px"].setdefault(sym, b.open)
            total = self.nav()
            for k, w in WEIGHTS.items():
                diff = total * w - self.sleeve_nav(k)
                self.s["sleeves"][k]["cash"] += diff
            self._rotation(d, today, hist)
            # la poche BTC garde son exposition à 100 % si elle est investie
            if BTC in self.s["sleeves"]["btc"]["pos"] and BTC in today:
                nv = self.sleeve_nav("btc")
                self._trade(d, "btc", BTC, nv / today[BTC].open, today[BTC].open, "rééquilibrage mensuel")

        # 1 bis) tout premier jour de cotation des ETF : la poche rotation s'investit tout de suite
        #        (décision sur les fins de mois passées, exécution à l'ouverture du jour : même règle qu'en début de mois)
        if etf_day and self.s["last_etf_d"] is None and not self.s["sleeves"]["rotation"]["pos"]:
            self._rotation(d, today, hist)

        # 2) ordres en attente -> ouverture du jour
        keep = []
        for o in self.s["pending"]:
            b = today.get(o["sym"])
            if b is None:
                keep.append(o)
                continue
            nv = self.sleeve_nav(o["sleeve"])
            self._trade(d, o["sleeve"], o["sym"], o["weight"] * nv / b.open, b.open, o["reason"])
        self.s["pending"] = keep

        # 3) valorisation à la clôture
        for sym, b in today.items():
            self.s["last_px"][sym] = b.close

        # 4) signaux à la clôture -> ordres pour la prochaine ouverture
        self._rsi2(d, today, hist)
        self._btc(d, today, hist)

        self.s["last_d"] = d
        if etf_day:
            self.s["last_etf_d"] = d

    # ---- stratégies ---------------------------------------------------------- #
    def _rotation(self, d, today, hist):
        """décision sur les clôtures de fin de mois (historique AVANT d), exécution à l'ouverture de d"""
        scores = {}
        for sym in ROTATION:
            h = [b for b in hist.get(sym, []) if b.d < d]
            month_ends = {}
            for b in h:
                month_ends[b.d[:7]] = b.adj            # dernière clôture de chaque mois
            ms = sorted(month_ends)
            if len(ms) < 7:
                continue
            scores[sym] = month_ends[ms[-1]] / month_ends[ms[-7]] - 1
        top = [s for s, r in sorted(scores.items(), key=lambda x: -x[1])[:3] if r > 0]
        sl = self.s["sleeves"]["rotation"]
        nv = self.sleeve_nav("rotation")
        for sym in list(sl["pos"]):
            if sym not in top and sym in today:
                self._trade(d, "rotation", sym, 0.0, today[sym].open, "rotation : sort du top 3")
        for sym in top:
            if sym in today:
                self._trade(d, "rotation", sym, nv / 3 / today[sym].open, today[sym].open, f"rotation : top 3 (6 mois {scores[sym]:+.1%})")
        self.events.append({"type": "signal", "d": d, "poche": "rotation", "top3": top,
                            "scores": {k: round(v, 4) for k, v in scores.items()}})

    def _rsi2(self, d, today, hist):
        sl = self.s["sleeves"]["rsi2"]
        days = self.s["rsi2_days"]
        held = set(sl["pos"]) | {o["sym"] for o in self.s["pending"] if o["sleeve"] == "rsi2" and o["weight"] > 0}
        # sorties
        for sym in list(sl["pos"]):
            if sym not in today:
                continue
            days[sym] = days.get(sym, 0) + 1
            adj = [b.adj for b in hist[sym]]
            m5 = sma(adj, 5)
            if (m5 is not None and adj[-1] > m5) or days[sym] >= RSI2_MAX_DAYS:
                self._order(d, "rsi2", sym, 0.0, today[sym].close, "RSI2 : sortie (clôture > MM5 ou 20 j)")
                held.discard(sym)
                days.pop(sym, None)
        # entrées (priorité = ordre de la liste)
        for sym in RSI2_UNIV:
            if len(held) >= RSI2_MAX_POS or sym in held or sym not in today:
                continue
            adj = [b.adj for b in hist[sym]]
            m200, r2 = sma(adj, 200), rsi_last(adj[-260:], 2)
            if m200 is not None and r2 is not None and adj[-1] > m200 and r2 < 10:
                self._order(d, "rsi2", sym, RSI2_LEVERAGE / RSI2_MAX_POS, today[sym].close, f"RSI2 : creux (RSI2 {r2:.1f})")
                held.add(sym)
                days[sym] = 0

    def _btc(self, d, today, hist):
        if BTC not in today:
            return
        adj = [b.adj for b in hist[BTC]]
        m50 = sma(adj, 50)
        if m50 is None:
            return
        want = adj[-1] > m50
        have = BTC in self.s["sleeves"]["btc"]["pos"]
        if want and not have:
            self._order(d, "btc", BTC, 1.0, today[BTC].close, "BTC : au-dessus de la MM50")
        elif not want and have:
            self._order(d, "btc", BTC, 0.0, today[BTC].close, "BTC : sous la MM50")
