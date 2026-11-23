"""
Rapport d'évaluation du détecteur de tendance (comparateur de variantes).

Pour une variante (paramètres + trio d'unités de temps + mode d'alignement) et
une paire, on mesure :

1. Valeur prédictive de l'alignement : quand la tendance est confirmée, le prix
   part-il ENSUITE dans ce sens plus souvent qu'à un instant quelconque ?
   (avantage vs référence, mouvement et MFE/MAE en ATR H1, épisodes, unités de
   temps seules, validation sur les 30 % les plus récents)
2. Par phase de la tendance au moment de la mesure (UT intermédiaire) :
   naissante / mature / étendue — l'hypothèse « détectée trop tard ».
3. Retard : sur les vrais grands mouvements (repérés APRÈS COUP par un ZigZag
   de 3 ATR H1, uniquement pour ce diagnostic), combien d'heures après le
   départ la tendance est confirmée et quelle part du mouvement est déjà faite.
4. Test du repli (la méthode visée) : quand le RSI de la petite UT fait un
   repli (passe sous 40 / au-dessus de 60), le prix repart-il dans le sens de
   la tendance confirmée plus souvent qu'un repli hors tendance ?

Tout ce qui sert de signal est causal ; les mesures « futures » ne servent qu'à
évaluer.
"""

from bisect import bisect_right
from collections import deque
from statistics import median

from strategies import Bar

from . import trend
from .variants import Variant

HORIZONS_H = (4, 24)
SPLIT = 0.7
NORM_TF = 3600            # toutes les mesures sont en ATR H1, quelle que soit la variante
YOUNG_BARS = 8            # tendance naissante : <= 8 bougies de l'UT intermédiaire…
YOUNG_MAX_EXT = 1.5       # … et pas encore loin de sa moyenne
EXTENDED_EXT = 2.5        # tendance étendue : prix à >= 2,5 ATR de l'EMA rapide
LEG_MIN_ATR = 4.0         # « vrai mouvement » pour la mesure du retard
PB_LOW, PB_HIGH = 40, 60  # seuils du repli (RSI 14 de la petite UT)


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _r(x, n=3):
    return round(x, n) if x is not None else None


def _fwd_extremes(vals, k, fn):
    """fn (max/min) de vals[i+1 .. i+k] pour chaque i, en O(n) (file monotone)."""
    n = len(vals)
    out = [None] * n
    dq: deque = deque()
    better = (lambda a, b: a >= b) if fn is max else (lambda a, b: a <= b)
    for j in range(n - 1, -1, -1):
        # fenêtre courante = indices j+1 .. j+k
        if j + 1 < n:
            v = vals[j + 1]
            while dq and better(v, vals[dq[-1]]):
                dq.pop()
            dq.append(j + 1)
        while dq and dq[0] > j + k:
            dq.popleft()
        out[j] = vals[dq[0]] if dq and j + k < n else None
    return out


def _rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = sum(max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)) / n
    l = sum(max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


# --------------------------------------------------------------------------- #
def evaluate_symbol(symbol: str, bars_by_tf: dict[int, list[Bar]], v: Variant, start: int, end: int) -> dict:
    tfs = list(v.tfs)
    lo, mid = tfs[-1], tfs[1]
    states = {tf: trend.compute(bars_by_tf.get(tf, []), v.params) for tf in tfs}
    lookups = [trend.StateLookup(states[tf], tf) for tf in tfs]
    mid_lk = lookups[1]
    norm_atr = trend.atr(bars_by_tf[NORM_TF], 14)
    norm_closes = [b.time + NORM_TF for b in bars_by_tf[NORM_TF]]
    base = bars_by_tf[lo]
    closes = [b.close for b in base]
    highs, lows = [b.high for b in base], [b.low for b in base]
    rsi = _rsi(closes)
    split_t = start + (end - start) * SPLIT
    ks = {h: h * 3600 // lo for h in HORIZONS_H}
    fmax = {h: _fwd_extremes(highs, k, max) for h, k in ks.items()}
    fmin = {h: _fwd_extremes(lows, k, min) for h, k in ks.items()}

    samples, grid = [], []        # grid : (t, alignement, prix) pour le diagnostic de retard
    last_pb = {}
    for i, b in enumerate(base):
        t = b.time + lo
        if t < start or t > end:
            continue
        a = trend.align(lookups, t, v.align_mode)
        a_ctx = trend.align(lookups[:-1], t, v.align_mode)   # contexte du repli : les 2 plus grandes UT
        j = bisect_right(norm_closes, t) - 1
        atr = norm_atr[j] if j >= 0 else None
        ms = mid_lk.at(t)
        if a is None or not atr or ms is None:
            continue
        grid.append((t, a, b.close))
        fut = {}
        for h, k in ks.items():
            if i + k < len(base) and fmax[h][i] is not None:
                fut[h] = ((closes[i + k] - b.close) / atr, (fmax[h][i] - b.close) / atr, (b.close - fmin[h][i]) / atr)
        if not fut:
            continue
        # phase de la tendance (UT intermédiaire), dans le sens de l'alignement
        phase = None
        if a:
            ext = ms.ext * a
            phase = "extended" if ext >= EXTENDED_EXT else "young" if ms.age <= YOUNG_BARS and ext < YOUNG_MAX_EXT else "mature"
        # repli : le RSI de la petite UT passe sous 40 (achat potentiel) ou au-dessus de 60 (vente potentielle)
        pb = None
        if i > 0 and rsi[i] is not None and rsi[i - 1] is not None:
            side = 1 if rsi[i - 1] >= PB_LOW > rsi[i] else -1 if rsi[i - 1] <= PB_HIGH < rsi[i] else 0
            if side:
                kind = "pb_neutral" if not a_ctx else "pb_with" if side == a_ctx else "pb_counter"
                if t - last_pb.get(kind, -10**9) >= 3600:      # au plus un repli par heure et par catégorie
                    pb = (kind, side)
                    last_pb[kind] = t
        samples.append({"t": t, "a": a, "tf": [lk.at(t).state for lk in lookups], "h": fut,
                        "phase": phase, "pb": pb})

    return {
        "symbol": symbol,
        "samples": len(samples),
        "horizons": summarize(samples, tfs, split_t),
        "stability": {str(tf): stability(states[tf], tf, start, end) for tf in tfs},
        "lag": lag_diagnostic(grid, bars_by_tf[NORM_TF], norm_atr, start, end),
    }


# --------------------------------------------------------------------------- #
def summarize(samples, tfs, split_t) -> dict:
    out = {}
    for h in HORIZONS_H:
        hs = [s for s in samples if h in s["h"]]
        if not hs:
            continue
        ref = _ref(hs, h)
        g: dict[str, list] = {
            "aligned": [(s, s["a"]) for s in hs if s["a"]],
            "aligned_up": [(s, 1) for s in hs if s["a"] == 1],
            "aligned_down": [(s, -1) for s in hs if s["a"] == -1],
        }
        for k, tf in enumerate(tfs):
            g[f"{tf}_up"] = [(s, 1) for s in hs if s["tf"][k] == 1]
            g[f"{tf}_down"] = [(s, -1) for s in hs if s["tf"][k] == -1]
        for ph in ("young", "mature", "extended"):
            g[f"phase_{ph}"] = [(s, s["a"]) for s in hs if s["phase"] == ph]
        for kind in ("pb_with", "pb_counter", "pb_neutral"):
            g[kind] = [(s, s["pb"][1]) for s in hs if s["pb"] and s["pb"][0] == kind]
        res = {"baseline": {"n": len(hs), "share": 1.0, "pct_up": _r(ref[0]), "mean_fwd": _r(ref[1])}}
        for name, items in g.items():
            res[name] = group_stats(items, h, ref, len(hs), episodes=not name.startswith("pb_"))
        ins, oos = [s for s in hs if s["t"] < split_t], [s for s in hs if s["t"] >= split_t]
        for name in ("aligned", "aligned_up", "aligned_down", "pb_with"):
            ep = not name.startswith("pb_")
            res[name]["in_sample"] = group_stats([x for x in g[name] if x[0]["t"] < split_t], h, _ref(ins, h),
                                                 len(ins), brief=True, episodes=ep)
            res[name]["out_of_sample"] = group_stats([x for x in g[name] if x[0]["t"] >= split_t], h, _ref(oos, h),
                                                     len(oos), brief=True, episodes=ep)
        out[str(h)] = res
    return out


def _ref(hs, h):
    return (_mean(s["h"][h][0] > 0 for s in hs), _mean(s["h"][h][0] for s in hs)) if hs else (None, None)


def group_stats(items, h, ref, total, brief=False, episodes=True) -> dict:
    """items = [(échantillon, sens prévu ±1)] ; l'avantage est mesuré contre la
    référence prise DANS LE MÊME SENS (ce qui neutralise la dérive de la période)."""
    n = len(items)
    d = {"n": n, "share": _r(n / total) if total else None}
    if not n or ref[0] is None:
        return d
    base_up, base_fwd = ref
    signed = [s["h"][h][0] * sg for s, sg in items]
    hit = _mean(x > 0 for x in signed)
    base_hit = _mean(base_up if sg > 0 else 1 - base_up for _, sg in items)
    d["hit"] = _r(hit)
    d["edge_hit"] = _r(hit - base_hit)
    d["mean_dir"] = _r(_mean(signed))
    d["edge_move"] = _r(d["mean_dir"] - _mean(base_fwd * sg for _, sg in items))
    if episodes:
        eps, prev = 0, None
        for s, sg in items:
            if prev is None or s["t"] - prev[0] > 2 * 3600 or sg != prev[1]:
                eps += 1
            prev = (s["t"], sg)
        d["episodes"] = eps
    else:
        d["episodes"] = n
    # marge d'erreur à 95 % sur le taux de réussite, calculée sur les épisodes
    # (les instants d'un même épisode ne sont pas indépendants)
    d["ci"] = _r(1.96 * (hit * (1 - hit) / d["episodes"]) ** 0.5) if d["episodes"] else None
    if brief:
        return d
    mfe = [s["h"][h][1] if sg > 0 else s["h"][h][2] for s, sg in items]
    mae = [s["h"][h][2] if sg > 0 else s["h"][h][1] for s, sg in items]
    d["mfe"], d["mae"] = _r(_mean(mfe)), _r(_mean(mae))
    d["mfe_mae"] = _r(d["mfe"] / d["mae"]) if d["mae"] else None
    return d


def stability(states, tf, start, end) -> dict:
    sel = [s for s in states if start <= s.time + tf <= end]
    if not sel:
        return {}
    flips = sum(1 for a, b in zip(sel, sel[1:]) if a.state != b.state)
    days = max((sel[-1].time - sel[0].time) / 86400, 1e-9)
    runs, cur = [], 1
    for a, b in zip(sel, sel[1:]):
        if a.state == b.state:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    n = len(sel)
    return {
        "bars": n,
        "share_up": _r(sum(s.state == 1 for s in sel) / n),
        "share_down": _r(sum(s.state == -1 for s in sel) / n),
        "share_neutral": _r(sum(s.state == 0 for s in sel) / n),
        "flips_per_day": _r(flips / days, 2),
        "median_run_hours": _r(median(runs) * tf / 3600, 1),
        "mean_strength": _r(sum(s.strength for s in sel) / n, 1),
    }


def lag_diagnostic(grid, h1_bars, h1_atr, start, end) -> dict:
    """Vrais mouvements repérés APRÈS COUP (ZigZag 3 ATR H1 sur les clôtures H1),
    puis : quand l'alignement l'a-t-il confirmé ?"""
    closes = [b.close for b in h1_bars]
    pivots, ext, dirn = [], None, 0
    for i, c in enumerate(closes):
        a = h1_atr[i]
        if a is None:
            continue
        if ext is None:
            ext = i
            continue
        thr = 3 * a
        if dirn == 0:
            if abs(c - closes[ext]) >= thr:
                pivots.append(ext)
                dirn, ext = (1 if c > closes[ext] else -1), i
        elif dirn == 1:
            if c > closes[ext]:
                ext = i
            elif closes[ext] - c >= thr:
                pivots.append(ext)
                dirn, ext = -1, i
        else:
            if c < closes[ext]:
                ext = i
            elif c - closes[ext] >= thr:
                pivots.append(ext)
                dirn, ext = 1, i
    legs = []
    for p0, p1 in zip(pivots, pivots[1:]):
        b0, b1 = h1_bars[p0], h1_bars[p1]
        t0, t1 = b0.time + 3600, b1.time + 3600
        size = abs(b1.close - b0.close) / (h1_atr[p0] or 1)
        if t0 >= start and t1 <= end and size >= LEG_MIN_ATR:
            legs.append((t0, t1, b0.close, b1.close, 1 if b1.close > b0.close else -1))
    if not legs:
        return {"legs": 0}
    times = [g[0] for g in grid]
    lags, done, missed = [], [], 0
    for t0, t1, p0, p1, sg in legs:
        i = bisect_right(times, t0)
        hit = None
        while i < len(grid) and grid[i][0] <= t1:
            if grid[i][1] == sg:
                hit = grid[i]
                break
            i += 1
        if hit is None:
            missed += 1
            continue
        lags.append((hit[0] - t0) / 3600)
        done.append((hit[2] - p0) / (p1 - p0))
    return {
        "legs": len(legs),
        "detected": len(lags),
        "missed_pct": _r(missed / len(legs)),
        "lag_hours_median": _r(median(lags), 1) if lags else None,
        "done_pct_median": _r(median(done)) if done else None,
    }


# --------------------------------------------------------------------------- #
def pooled(results: list[dict]) -> dict:
    """Agrégat de toutes les paires (mesures en ATR, donc comparables)."""
    out = {}
    for h in map(str, HORIZONS_H):
        per = [r["horizons"][h] for r in results if h in r["horizons"]]
        if not per:
            continue
        agg = {}
        tot = sum(p["baseline"]["n"] for p in per)
        for name in per[0]:
            rows = [p[name] for p in per if p.get(name) and p[name].get("n")]
            n = sum(r["n"] for r in rows)
            row = {"n": n, "share": _r(n / tot) if tot else None}
            for k in ("pct_up", "mean_fwd", "hit", "edge_hit", "mean_dir", "edge_move", "mfe", "mae"):
                vals = [(r[k], r["n"]) for r in rows if r.get(k) is not None]
                if vals:
                    row[k] = _r(sum(x * w for x, w in vals) / sum(w for _, w in vals))
            if row.get("mfe") and row.get("mae"):
                row["mfe_mae"] = _r(row["mfe"] / row["mae"])
            if any("episodes" in r for r in rows):
                row["episodes"] = sum(r.get("episodes", 0) for r in rows)
                if row.get("hit") is not None and row["episodes"]:
                    row["ci"] = _r(1.96 * (row["hit"] * (1 - row["hit"]) / row["episodes"]) ** 0.5)
            for split in ("in_sample", "out_of_sample"):
                parts = [r[split] for r in rows if r.get(split) and r[split].get("n")]
                if parts:
                    m = sum(x["n"] for x in parts)
                    row[split] = {"n": m, "episodes": sum(x.get("episodes", 0) for x in parts)}
                    for k in ("hit", "edge_hit", "mean_dir", "edge_move"):
                        vals = [(x[k], x["n"]) for x in parts if x.get(k) is not None]
                        if vals:
                            row[split][k] = _r(sum(a * w for a, w in vals) / sum(w for _, w in vals))
                    hs_, e_ = row[split].get("hit"), row[split]["episodes"]
                    if hs_ is not None and e_:
                        row[split]["ci"] = _r(1.96 * (hs_ * (1 - hs_) / e_) ** 0.5)
            agg[name] = row
        out[h] = agg
    return out


def pooled_lag(results: list[dict]) -> dict:
    lg = [r["lag"] for r in results if r.get("lag", {}).get("legs")]
    legs = sum(x["legs"] for x in lg)
    if not legs:
        return {"legs": 0}
    det = sum(x["detected"] for x in lg)

    def wavg(k):
        vals = [(x[k], x["detected"]) for x in lg if x.get(k) is not None and x["detected"]]
        return _r(sum(a * w for a, w in vals) / sum(w for _, w in vals), 2) if vals else None
    return {"legs": legs, "detected": det, "missed_pct": _r((legs - det) / legs),
            "lag_hours_median": wavg("lag_hours_median"), "done_pct_median": wavg("done_pct_median")}
