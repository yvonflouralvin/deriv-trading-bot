from .base import Bar, Param, Side, Strategy, sma, stdev


class BollingerReversal(Strategy):
    kind = "bollinger_reversal"
    name = "Retour dans les bandes de Bollinger"
    description = ("PUT quand une bougie clôture sous la bande haute après l'avoir dépassée, "
                   "CALL quand elle clôture au-dessus de la bande basse après l'avoir cassée.")
    params = (
        Param("period", "Période", 20, 5, 200),
        Param("mult", "Écarts-types", 2, 0.5, 4, 0.1),
    )

    def warmup(self, p):
        return int(p["period"]) + 2

    def signals(self, bars: list[Bar], p) -> dict[int, Side]:
        closes = [b.close for b in bars]
        n = int(p["period"])
        mid, sd = sma(closes, n), stdev(closes, n)
        sig: dict[int, Side] = {}
        for i in range(1, len(bars)):
            if mid[i - 1] is None or mid[i] is None:
                continue
            up0, lo0 = mid[i - 1] + p["mult"] * sd[i - 1], mid[i - 1] - p["mult"] * sd[i - 1]
            up1, lo1 = mid[i] + p["mult"] * sd[i], mid[i] - p["mult"] * sd[i]
            if closes[i - 1] > up0 and closes[i] <= up1:
                sig[i] = "PUT"
            elif closes[i - 1] < lo0 and closes[i] >= lo1:
                sig[i] = "CALL"
        return sig
