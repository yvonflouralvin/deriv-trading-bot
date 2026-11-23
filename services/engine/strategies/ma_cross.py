from .base import Bar, Param, Side, Strategy, crosses, ema, sma


class SmaCross(Strategy):
    kind = "sma_cross"
    name = "Croisement SMA"
    description = ("CALL quand la moyenne mobile rapide passe au-dessus de la lente, "
                   "PUT quand elle passe en dessous (logique du premier bot).")
    params = (Param("fast", "Moyenne rapide", 5, 2, 200), Param("slow", "Moyenne lente", 20, 3, 400))

    def validate(self, p):
        if p["fast"] >= p["slow"]:
            p["fast"] = max(2, p["slow"] - 1)
        return p

    def warmup(self, p):
        return int(p["slow"]) + 2

    def signals(self, bars: list[Bar], p) -> dict[int, Side]:
        closes = [b.close for b in bars]
        return crosses(sma(closes, int(p["fast"])), sma(closes, int(p["slow"])))


class EmaCross(SmaCross):
    kind = "ema_cross"
    name = "Croisement EMA"
    description = "Même logique avec des moyennes exponentielles, plus réactives aux derniers prix."
    params = (Param("fast", "EMA rapide", 9, 2, 200), Param("slow", "EMA lente", 21, 3, 400))

    def warmup(self, p):
        return int(p["slow"]) * 3  # l'EMA a besoin d'un peu d'historique pour se stabiliser

    def signals(self, bars: list[Bar], p) -> dict[int, Side]:
        closes = [b.close for b in bars]
        return crosses(ema(closes, int(p["fast"])), ema(closes, int(p["slow"])))
