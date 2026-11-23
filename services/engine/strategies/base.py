from dataclasses import asdict, dataclass
from typing import Literal

Side = Literal["CALL", "PUT"]


@dataclass(frozen=True)
class Bar:
    time: int       # début de la bougie (epoch, secondes)
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    default: float
    min: float
    max: float
    step: float = 1

    def to_dict(self):
        return asdict(self)


class Strategy:
    kind: str = ""
    name: str = ""
    description: str = ""
    params: tuple[Param, ...] = ()

    def resolve_params(self, raw: dict | None) -> dict[str, float]:
        """Paramètres de l'utilisateur, complétés et bornés."""
        raw = raw or {}
        out = {}
        for p in self.params:
            try:
                v = float(raw.get(p.key, p.default))
            except (TypeError, ValueError):
                v = p.default
            v = min(max(v, p.min), p.max)
            out[p.key] = int(v) if p.step >= 1 and float(v).is_integer() else v
        return self.validate(out)

    def validate(self, params: dict) -> dict:
        return params

    def warmup(self, params: dict) -> int:
        """Nombre de bougies nécessaires avant le premier signal possible."""
        return 50

    def signals(self, bars: list[Bar], params: dict) -> dict[int, Side]:
        raise NotImplementedError

    def signal_on_last(self, bars: list[Bar], params: dict) -> Side | None:
        """Signal éventuel sur la dernière bougie clôturée (utilisé en live)."""
        if not bars:
            return None
        return self.signals(bars, params).get(len(bars) - 1)

    def describe(self) -> dict:
        return {"kind": self.kind, "name": self.name, "description": self.description,
                "params": [p.to_dict() for p in self.params]}


# --------------------------------------------------------------------------- #
# Indicateurs
# --------------------------------------------------------------------------- #
def sma(values: list[float], n: int) -> list[float | None]:
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    k, prev = 2 / (n + 1), None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        prev = sum(values[:n]) / n if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: list[float], n: int) -> list[float | None]:
    """RSI de Wilder."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def stdev(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i < n - 1:
            out.append(None)
            continue
        w = values[i - n + 1:i + 1]
        m = sum(w) / n
        out.append((sum((x - m) ** 2 for x in w) / n) ** 0.5)
    return out


def crosses(fast: list[float | None], slow: list[float | None]) -> dict[int, Side]:
    sig: dict[int, Side] = {}
    for i in range(1, len(fast)):
        f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
        if None in (f0, s0, f1, s1):
            continue
        d0, d1 = f0 - s0, f1 - s1
        if d0 <= 0 < d1:
            sig[i] = "CALL"
        elif d0 >= 0 > d1:
            sig[i] = "PUT"
    return sig
