"""Variantes du détecteur comparées par le rapport (peu nombreuses à dessein :
plus on teste de variantes, plus l'une d'elles « gagne » par hasard)."""

from dataclasses import dataclass, field

from .trend import TrendParams

FAST = TrendParams(pivots="fractal", fractal_n=2, ema_fast=20, ema_slow=50, slope_bars=5, adx_rising_bars=3)


@dataclass(frozen=True)
class Variant:
    id: str
    name: str
    description: str
    tfs: tuple[int, ...]                       # de la plus grande à la plus petite
    params: TrendParams = field(default_factory=TrendParams)
    align_mode: str = "all"


VARIANTS = [
    Variant("A", "Actuel (v1)", "ZigZag 2 ATR, EMA 50/200, ADX ; H4/H1/M15 tous d'accord.", (14400, 3600, 900)),
    Variant("B", "Rapide", "Pivots fractals (2 bougies), EMA 20/50, ADX montant ; H4/H1/M15 tous d'accord.",
            (14400, 3600, 900), FAST),
    Variant("C", "Rôles : H4 biais", "Détecteur v1 ; H4 ne doit pas être contre, H1 et M15 d'accord.",
            (14400, 3600, 900), TrendParams(), "bias"),
    Variant("D", "Trio rapproché", "Détecteur v1 ; H1/M15/M5 tous d'accord.", (3600, 900, 300)),
    Variant("E", "Rapide + rôles", "Détecteur rapide ; H4 biais, H1 et M15 d'accord.", (14400, 3600, 900), FAST, "bias"),
]
BY_ID = {v.id: v for v in VARIANTS}
