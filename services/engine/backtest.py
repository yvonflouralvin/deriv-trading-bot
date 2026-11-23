"""
Backtest : rejoue une stratégie sur l'historique stocké, avec les mêmes règles
que le moteur live :
  - signal évalué à la clôture de chaque bougie ;
  - entrée au premier prix après la clôture ;
  - contrat Rise/Fall : CALL gagne si le prix de sortie > prix d'entrée,
    PUT gagne si < ; égalité = perdu (comme chez Deriv) ;
  - un seul contrat ouvert à la fois par stratégie ;
  - gain = mise × taux de paiement, perte = mise.

Résolution des prix :
  - « ticks » si les ticks bruts couvrent la période (exact) ;
  - sinon « bougies 1m » : prix approché par la clôture de la bougie d'une
    minute qui contient l'instant voulu (approximation, surtout pour les
    contrats de quelques ticks).
"""

from bisect import bisect_left
from dataclasses import dataclass

from strategies import Bar, Strategy

UNRESOLVED = object()   # le prix de sortie ne peut pas être distingué de l'entrée


def tick_interval(symbol: str) -> int:
    """Secondes entre deux ticks : 1 s pour les indices « (1s) », ~2 s sinon."""
    return 1 if symbol.upper().startswith("1HZ") else 2


def duration_seconds(duration: int, unit: str, symbol: str) -> int:
    return {"t": duration * tick_interval(symbol), "s": duration, "m": duration * 60}[unit]


class TickSource:
    resolution = "ticks"

    def __init__(self, times: list[int], prices: list[float]):
        self.t, self.p = times, prices

    def entry(self, at: int):
        i = bisect_left(self.t, at)
        return (i, self.t[i], self.p[i]) if i < len(self.t) else None

    def exit(self, entry, duration: int, unit: str, symbol: str):
        i, t0, _ = entry
        if unit == "t":
            j = i + duration
        else:
            j = bisect_left(self.t, t0 + duration_seconds(duration, unit, symbol))
        return (self.t[j], self.p[j]) if j < len(self.t) else None


class CandleSource:
    resolution = "bougies 1m (approximation)"

    def __init__(self, times: list[int], closes: list[float]):
        self.t, self.c = times, closes   # début de chaque bougie 1m

    def _at(self, ts: int):
        i = bisect_left(self.t, ts - ts % 60)
        if i < len(self.t) and self.t[i] == ts - ts % 60:
            return self.c[i]
        return None

    def entry(self, at: int):
        # prix juste après la clôture = clôture de la bougie 1m précédente
        p = self._at(at - 1)
        return (None, at, p) if p is not None else None

    def exit(self, entry, duration: int, unit: str, symbol: str):
        _, t0, _ = entry
        te = t0 + duration_seconds(duration, unit, symbol)
        if (te - te % 60) == ((t0 - 1) - (t0 - 1) % 60):
            # entrée et sortie lues dans la même bougie 1m : égalité artificielle -> on n'en tient pas compte
            return UNRESOLVED
        p = self._at(te)
        return (te, p) if p is not None else None


@dataclass
class Config:
    symbol: str
    tf: int
    stake: float
    payout_ratio: float
    duration: int
    duration_unit: str


def run(strategy: Strategy, params: dict, bars: list[Bar], source, cfg: Config, start_at: int):
    """bars inclut l'historique de chauffe ; seuls les signaux >= start_at comptent."""
    signals = strategy.signals(bars, params)
    trades, busy_until, skipped, unresolved = [], -1, 0, 0
    for i in sorted(signals):
        close_time = bars[i].time + cfg.tf
        if close_time < start_at:
            continue
        if close_time < busy_until:
            skipped += 1
            continue
        side = signals[i]
        ent = source.entry(close_time)
        if ent is None:
            continue
        ext = source.exit(ent, cfg.duration, cfg.duration_unit, cfg.symbol)
        if ext is UNRESOLVED:
            unresolved += 1
            continue
        if ext is None:
            break  # plus de données après cette date
        entry_spot, (exit_time, exit_spot) = ent[2], ext
        won = exit_spot > entry_spot if side == "CALL" else exit_spot < entry_spot
        profit = round(cfg.stake * cfg.payout_ratio if won else -cfg.stake, 4)
        trades.append({"time": close_time, "side": side, "entry": entry_spot, "exit_time": exit_time,
                       "exit": exit_spot, "won": won, "profit": profit})
        busy_until = exit_time
    st = stats(trades, cfg, skipped, source.resolution)
    st["unresolved_signals"] = unresolved
    return st, trades


def stats(trades: list[dict], cfg: Config, skipped: int, resolution: str) -> dict:
    n = len(trades)
    wins = sum(t["won"] for t in trades)
    pnl, peak, max_dd, equity = 0.0, 0.0, 0.0, []
    gross_win = sum(t["profit"] for t in trades if t["profit"] > 0)
    gross_loss = -sum(t["profit"] for t in trades if t["profit"] < 0)
    best_streak = worst_streak = cur = 0
    for t in trades:
        pnl += t["profit"]
        peak = max(peak, pnl)
        max_dd = max(max_dd, peak - pnl)
        equity.append([t["exit_time"], round(pnl, 4)])
        cur = (cur + 1 if cur >= 0 else 1) if t["won"] else (cur - 1 if cur <= 0 else -1)
        best_streak, worst_streak = max(best_streak, cur), min(worst_streak, cur)
    step = max(1, len(equity) // 500)
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "breakeven_win_rate": round(1 / (1 + cfg.payout_ratio), 4),
        "pnl": round(pnl, 4),
        "max_drawdown": round(max_dd, 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "best_win_streak": best_streak,
        "worst_loss_streak": -worst_streak,
        "skipped_signals": skipped,
        "resolution": resolution,
        "equity": equity[::step] + ([equity[-1]] if equity and (len(equity) - 1) % step else []),
    }
