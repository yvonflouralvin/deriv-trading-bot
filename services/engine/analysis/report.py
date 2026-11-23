"""
Rapport d'évaluation du détecteur de tendance.

Question posée : quand les unités de temps sont alignées (toutes haussières ou
toutes baissières), le prix part-il ENSUITE dans ce sens plus souvent et plus
loin qu'à un instant quelconque (la référence) ?

Méthode (causale, sans regard vers le futur) :
  - grille de temps = clôtures de la plus petite unité de temps (ex. M15) ;
  - à chaque clôture t : état en vigueur de chaque unité de temps (dernière
    bougie clôturée) -> aligné haussier / aligné baissier / mixte ;
  - on mesure ce que fait le prix sur les N heures de marché suivantes, en ATR
    de l'unité de temps intermédiaire (ex. H1) : mouvement final, excursion
    favorable maximale (MFE) et défavorable maximale (MAE) ;
  - on compare chaque groupe à la référence (tous les instants).

Les instants voisins se chevauchent (deux clôtures M15 successives regardent
presque le même futur) : le nombre d'ÉPISODES (périodes d'alignement
distinctes) est la vraie taille d'échantillon à regarder pour juger de la
solidité d'un résultat.
"""

from statistics import median

from strategies import Bar

from . import trend

HORIZONS_H = (4, 24)
SPLIT = 0.7   # 70 % des données = « réglage », 30 % = « validation »


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _r(x, n=3):
    return round(x, n) if x is not None else None


def evaluate_symbol(symbol: str, bars_by_tf: dict[int, list[Bar]], tfs: list[int], start: int, end: int,
                    params: trend.TrendParams = trend.TrendParams()) -> dict:
    tfs = sorted(tfs, reverse=True)            # ex. [14400, 3600, 900]
    lo, mid = tfs[-1], tfs[len(tfs) // 2]
    states = {tf: trend.compute(bars_by_tf.get(tf, []), params) for tf in tfs}
    lookups = {tf: trend.StateLookup(states[tf], tf) for tf in tfs}
    base = bars_by_tf.get(lo, [])
    split_t = start + (end - start) * SPLIT

    samples = []   # un échantillon par clôture de la petite unité de temps
    for i, b in enumerate(base):
        t = b.time + lo
        if t < start or t > end:
            continue
        a = trend.align([lookups[tf] for tf in tfs], t)
        ref = lookups[mid].at(t)
        if a is None or ref is None or not ref.atr:
            continue
        s = {"t": t, "align": a, "tf": {tf: lookups[tf].at(t).state for tf in tfs}, "h": {}}
        for h in HORIZONS_H:
            k = h * 3600 // lo
            if i + k >= len(base):
                continue
            fut = base[i + 1:i + k + 1]
            s["h"][h] = (
                (base[i + k].close - b.close) / ref.atr,           # mouvement final
                (max(x.high for x in fut) - b.close) / ref.atr,    # excursion vers le haut
                (b.close - min(x.low for x in fut)) / ref.atr,     # excursion vers le bas
            )
        samples.append(s)

    return {
        "symbol": symbol,
        "samples": len(samples),
        "horizons": summarize(samples, tfs, split_t),
        "stability": {str(tf): stability(states[tf], tf, start, end) for tf in tfs},
    }


def summarize(samples, tfs, split_t) -> dict:
    out = {}
    for h in HORIZONS_H:
        hs = [s for s in samples if h in s["h"]]
        if not hs:
            continue
        base_up = _mean([s["h"][h][0] > 0 for s in hs])
        base_fwd = _mean([s["h"][h][0] for s in hs])
        groups = {
            "aligned_up": ([s for s in hs if s["align"] == 1], 1),
            "aligned_down": ([s for s in hs if s["align"] == -1], -1),
            "mixed": ([s for s in hs if s["align"] == 0], 0),
        }
        for tf in tfs:
            groups[f"{tf}_up"] = ([s for s in hs if s["tf"][tf] == 1], 1)
            groups[f"{tf}_down"] = ([s for s in hs if s["tf"][tf] == -1], -1)
        res = {"baseline": {"n": len(hs), "share": 1.0, "pct_up": _r(base_up), "mean_fwd": _r(base_fwd)}}
        for name, (grp, sign) in groups.items():
            res[name] = group_stats(grp, h, sign, len(hs), base_up, base_fwd)
        # stabilité dans le temps : réglage (70 %) vs validation (30 %)
        for name in ("aligned_up", "aligned_down"):
            grp, sign = groups[name]
            ins = [s for s in hs if s["t"] < split_t]
            oos = [s for s in hs if s["t"] >= split_t]
            res[name]["in_sample"] = group_stats([s for s in grp if s["t"] < split_t], h, sign, len(ins),
                                                 _mean([s["h"][h][0] > 0 for s in ins]),
                                                 _mean([s["h"][h][0] for s in ins]), brief=True)
            res[name]["out_of_sample"] = group_stats([s for s in grp if s["t"] >= split_t], h, sign, len(oos),
                                                     _mean([s["h"][h][0] > 0 for s in oos]),
                                                     _mean([s["h"][h][0] for s in oos]), brief=True)
        out[str(h)] = res
    return out


def group_stats(grp, h, sign, total, base_up, base_fwd, brief=False) -> dict:
    n = len(grp)
    d = {"n": n, "share": _r(n / total) if total else None}
    if not n:
        return d
    fwd = [s["h"][h][0] for s in grp]
    d["pct_up"] = _r(_mean([f > 0 for f in fwd]))
    d["mean_fwd"] = _r(_mean(fwd))
    if sign:
        hit = _mean([f * sign > 0 for f in fwd])
        base_hit = base_up if sign > 0 else (1 - base_up if base_up is not None else None)
        d["hit"] = _r(hit)
        d["edge_hit"] = _r(hit - base_hit) if base_hit is not None else None
        d["mean_dir"] = _r(_mean([f * sign for f in fwd]))
        d["edge_move"] = _r(d["mean_dir"] - base_fwd * sign) if base_fwd is not None else None
        if not brief:
            mfe = [s["h"][h][1] if sign > 0 else s["h"][h][2] for s in grp]
            mae = [s["h"][h][2] if sign > 0 else s["h"][h][1] for s in grp]
            d["mfe"], d["mae"] = _r(_mean(mfe)), _r(_mean(mae))
            d["mfe_mae"] = _r(d["mfe"] / d["mae"]) if d["mae"] else None
            # épisodes : début de chaque période d'alignement continue
            eps, prev_t = [], None
            for s in grp:
                if prev_t is None or s["t"] - prev_t > 3600 * 2:
                    eps.append(s)
                prev_t = s["t"]
            d["episodes"] = len(eps)
            d["episode_hit"] = _r(_mean([s["h"][h][0] * sign > 0 for s in eps]))
            d["episode_mean_dir"] = _r(_mean([s["h"][h][0] * sign for s in eps]))
    return d


def stability(states: list[trend.TrendState], tf: int, start: int, end: int) -> dict:
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


def pooled(results: list[dict], tfs) -> dict:
    """Agrégat de toutes les paires (les mesures en ATR sont comparables)."""
    # on agrège en recalculant à partir des moyennes pondérées des groupes
    out = {}
    for h in map(str, HORIZONS_H):
        per = [r["horizons"][h] for r in results if h in r["horizons"]]
        if not per:
            continue
        agg = {}
        for name in per[0]:
            rows = [p[name] for p in per if p.get(name) and p[name].get("n")]
            n = sum(r["n"] for r in rows)
            if not n:
                agg[name] = {"n": 0}
                continue
            row = {"n": n}
            for k in ("pct_up", "mean_fwd", "hit", "edge_hit", "mean_dir", "edge_move", "mfe", "mae",
                      "episode_hit", "episode_mean_dir"):
                vals = [(r[k], r["n"]) for r in rows if r.get(k) is not None]
                if vals:
                    row[k] = _r(sum(v * w for v, w in vals) / sum(w for _, w in vals))
            if row.get("mfe") and row.get("mae"):
                row["mfe_mae"] = _r(row["mfe"] / row["mae"])
            eps = [r["episodes"] for r in rows if "episodes" in r]
            if eps:
                row["episodes"] = sum(eps)
            tot = sum(p["baseline"]["n"] for p in per)
            row["share"] = _r(n / tot) if tot else None
            for split in ("in_sample", "out_of_sample"):
                parts = [r[split] for r in rows if r.get(split) and r[split].get("n")]
                if parts:
                    m = sum(x["n"] for x in parts)
                    row[split] = {"n": m}
                    for k in ("hit", "edge_hit", "mean_dir", "edge_move"):
                        vals = [(x[k], x["n"]) for x in parts if x.get(k) is not None]
                        if vals:
                            row[split][k] = _r(sum(v * w for v, w in vals) / sum(w for _, w in vals))
            agg[name] = row
        out[h] = agg
    return out
