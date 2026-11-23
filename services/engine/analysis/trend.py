"""
Détecteur de tendance (étape 1).

Pour chaque bougie CLÔTURÉE d'une unité de temps, on calcule un état
haussier (+1) / baissier (-1) / neutre (0) et une force de 0 à 100, à partir de
trois lectures indépendantes :

  1. Structure de marché (poids 0,5) — sommets/creux confirmés par un ZigZag
     en ATR : sommets ET creux plus hauts => +1 ; plus bas => -1 ; mixte => 0.
     Une clôture au-delà du dernier sommet (ou sous le dernier creux) confirmé
     fait basculer la structure immédiatement (cassure).
  2. Moyennes (poids 0,3) — clôture au-dessus de l'EMA lente ET pente positive
     de l'EMA rapide => +1 ; l'inverse => -1 ; sinon 0.
  3. Directionnel (poids 0,2) — +DI > -DI avec ADX >= seuil => +1 ; l'inverse
     => -1 ; ADX sous le seuil => 0.

score = 0,5·S + 0,3·E + 0,2·D (entre -1 et +1)
état  = +1 si score >= 0,5 ; -1 si score <= -0,5 ; 0 sinon
force = |score| × min(1, ADX / 40) × 100

Tout est CAUSAL : l'état d'une bougie n'utilise que les bougies passées et
elle-même (aucune information du futur), condition indispensable pour que
l'évaluation soit honnête.

La confirmation multi-timeframe (alignement des 3 unités de temps) est faite
par `align()` : à un instant t, on prend pour chaque unité de temps l'état de
la dernière bougie déjà clôturée.
"""

from bisect import bisect_right
from dataclasses import dataclass

VERSION = 2


@dataclass(frozen=True)
class TrendParams:
    atr_period: int = 14
    pivots: str = "zigzag"        # "zigzag" (retournement de N ATR) ou "fractal" (N bougies de chaque côté)
    zigzag_atr: float = 2.0       # retournement du ZigZag : 2 ATR
    fractal_n: int = 2
    ema_fast: int = 50
    ema_slow: int = 200
    slope_bars: int = 10
    adx_period: int = 14
    adx_min: float = 20.0
    adx_rising_bars: int = 0      # > 0 : le directionnel ne compte que si l'ADX monte sur N bougies
    threshold: float = 0.5

    @property
    def warmup(self) -> int:
        return self.ema_slow + 2 * self.adx_period


@dataclass(frozen=True)
class TrendState:
    time: int          # début de la bougie
    state: int         # -1 / 0 / +1
    score: float
    strength: int      # 0..100
    adx: float
    structure: int
    ema: int
    di: int
    atr: float
    age: int = 0          # nombre de bougies depuis le début de l'état actuel
    ext: float = 0.0      # écart clôture - EMA rapide, en ATR (signé)


# --------------------------------------------------------------------------- #
# Indicateurs (Wilder)
# --------------------------------------------------------------------------- #
def _ema(values, n):
    out, k, prev = [None] * len(values), 2 / (n + 1), None
    for i, v in enumerate(values):
        if i == n - 1:
            prev = sum(values[:n]) / n
        elif i >= n:
            prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _true_ranges(bars):
    tr = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        tr.append(max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc)))
    return tr


def _wilder(values, n):
    out, prev = [None] * len(values), None
    for i, v in enumerate(values):
        if i == n - 1:
            prev = sum(values[:n]) / n
        elif i >= n:
            prev = (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def atr(bars, n=14):
    return _wilder(_true_ranges(bars), n)


def adx_di(bars, n=14):
    """Renvoie (ADX, +DI, -DI) par bougie."""
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        dn = bars[i - 1].low - bars[i].low
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr_s, p_s, m_s = _wilder(_true_ranges(bars), n), _wilder(pdm, n), _wilder(mdm, n)
    pdi, mdi, dx = [None] * len(bars), [None] * len(bars), [None] * len(bars)
    for i in range(len(bars)):
        if tr_s[i]:
            pdi[i] = 100 * p_s[i] / tr_s[i]
            mdi[i] = 100 * m_s[i] / tr_s[i]
            s = pdi[i] + mdi[i]
            dx[i] = 100 * abs(pdi[i] - mdi[i]) / s if s else 0.0
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    adx = [None] * len(bars)
    if first is not None:
        sm = _wilder([v for v in dx[first:]], n)
        adx[first:] = sm
    return adx, pdi, mdi


# --------------------------------------------------------------------------- #
# Structure de marché (ZigZag ATR, causal)
# --------------------------------------------------------------------------- #
def structure_series(bars, atr_vals, mult):
    """Pour chaque bougie : +1 / -1 / 0 selon les 2 derniers sommets et creux
    confirmés à cet instant, avec bascule immédiate sur cassure."""
    out = [0] * len(bars)
    highs: list[float] = []
    lows: list[float] = []
    direction = 0          # +1 : on suit une jambe haussière, -1 : baissière
    ext_hi = ext_lo = None
    for i, b in enumerate(bars):
        a = atr_vals[i]
        if a is None:
            continue
        if direction == 0:
            ext_hi, ext_lo, direction = b.high, b.low, 1
        if direction == 1:
            if b.high > ext_hi:
                ext_hi = b.high
            elif ext_hi - b.low >= mult * a:       # retournement : le sommet est confirmé
                highs.append(ext_hi)
                direction, ext_lo = -1, b.low
        else:
            if b.low < ext_lo:
                ext_lo = b.low
            elif b.high - ext_lo >= mult * a:      # le creux est confirmé
                lows.append(ext_lo)
                direction, ext_hi = 1, b.high
        s = 0
        if len(highs) >= 2 and len(lows) >= 2:
            hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
            s = 1 if hh and hl else -1 if not hh and not hl else 0
        if highs and b.close > highs[-1]:
            s = 1
        elif lows and b.close < lows[-1]:
            s = -1
        out[i] = s
    return out


def fractal_structure(bars, n):
    """Variante rapide : pivots fractals (un sommet est confirmé quand les n
    bougies suivantes sont plus basses), même logique sommets/creux + cassure."""
    out = [0] * len(bars)
    highs: list[float] = []
    lows: list[float] = []
    for i, b in enumerate(bars):
        j = i - n                                   # candidat pivot, confirmé à la bougie i
        if j >= n:
            win = bars[j - n:i + 1]
            if bars[j].high == max(x.high for x in win) and bars[j].high > max(x.high for x in bars[j + 1:i + 1]):
                highs.append(bars[j].high)
            if bars[j].low == min(x.low for x in win) and bars[j].low < min(x.low for x in bars[j + 1:i + 1]):
                lows.append(bars[j].low)
        s = 0
        if len(highs) >= 2 and len(lows) >= 2:
            hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
            s = 1 if hh and hl else -1 if not hh and not hl else 0
        if highs and b.close > highs[-1]:
            s = 1
        elif lows and b.close < lows[-1]:
            s = -1
        out[i] = s
    return out


# --------------------------------------------------------------------------- #
# États
# --------------------------------------------------------------------------- #
def compute(bars, p: TrendParams = TrendParams()) -> list[TrendState]:
    """États pour chaque bougie après la période de chauffe."""
    if len(bars) <= p.warmup:
        return []
    closes = [b.close for b in bars]
    atr_v = atr(bars, p.atr_period)
    ef, es = _ema(closes, p.ema_fast), _ema(closes, p.ema_slow)
    adx, pdi, mdi = adx_di(bars, p.adx_period)
    struct = (fractal_structure(bars, p.fractal_n) if p.pivots == "fractal"
              else structure_series(bars, atr_v, p.zigzag_atr))
    out = []
    prev_state, age = None, 0
    k = p.adx_rising_bars
    for i in range(p.warmup, len(bars)):
        if None in (es[i], ef[i], ef[i - p.slope_bars], adx[i], pdi[i], mdi[i], atr_v[i]):
            continue
        slope = ef[i] - ef[i - p.slope_bars]
        e = 1 if closes[i] > es[i] and slope > 0 else -1 if closes[i] < es[i] and slope < 0 else 0
        rising = not k or (adx[i - k] is not None and adx[i] > adx[i - k])
        d = 0 if adx[i] < p.adx_min or not rising else (1 if pdi[i] > mdi[i] else -1 if mdi[i] > pdi[i] else 0)
        score = 0.5 * struct[i] + 0.3 * e + 0.2 * d
        state = 1 if score >= p.threshold else -1 if score <= -p.threshold else 0
        age = age + 1 if state == prev_state else 1
        prev_state = state
        strength = round(abs(score) * min(1.0, adx[i] / 40) * 100)
        out.append(TrendState(bars[i].time, state, round(score, 3), strength, round(adx[i], 2),
                              struct[i], e, d, atr_v[i], age, round((closes[i] - ef[i]) / atr_v[i], 3)))
    return out


class StateLookup:
    """État en vigueur à un instant t = état de la dernière bougie clôturée."""

    def __init__(self, states: list[TrendState], tf: int):
        self.tf = tf
        self.closes = [s.time + tf for s in states]
        self.states = states

    def at(self, t: int) -> TrendState | None:
        i = bisect_right(self.closes, t) - 1
        return self.states[i] if i >= 0 else None


def align(lookups: list[StateLookup], t: int, mode: str = "all") -> int | None:
    """Confirmation multi-timeframe (lookups de la plus grande à la plus petite UT).
    mode "all"  : +1 / -1 si TOUTES les unités de temps sont d'accord, 0 sinon ;
    mode "bias" : la plus grande UT sert de biais (elle ne doit pas être CONTRE,
                  neutre accepté) et les autres doivent être d'accord.
    None si une unité de temps n'a pas encore d'état."""
    states = [lk.at(t) for lk in lookups]
    if any(s is None for s in states):
        return None
    if mode == "bias":
        rest = {s.state for s in states[1:]}
        if len(rest) == 1 and 0 not in rest:
            d = rest.pop()
            return d if states[0].state != -d else 0
        return 0
    vals = {s.state for s in states}
    if vals == {1}:
        return 1
    if vals == {-1}:
        return -1
    return 0
