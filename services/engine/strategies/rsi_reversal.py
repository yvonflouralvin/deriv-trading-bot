from .base import Bar, Param, Side, Strategy, rsi


class RsiReversal(Strategy):
    kind = "rsi_reversal"
    name = "Retournement RSI"
    description = ("CALL quand le RSI ressort de la zone de survente (repasse au-dessus du seuil bas), "
                   "PUT quand il ressort de la zone de surachat (repasse sous le seuil haut).")
    params = (
        Param("period", "Période RSI", 14, 2, 100),
        Param("low", "Seuil de survente", 30, 5, 50),
        Param("high", "Seuil de surachat", 70, 50, 95),
    )

    def warmup(self, p):
        return int(p["period"]) * 4

    def signals(self, bars: list[Bar], p) -> dict[int, Side]:
        r = rsi([b.close for b in bars], int(p["period"]))
        sig: dict[int, Side] = {}
        for i in range(1, len(r)):
            a, b = r[i - 1], r[i]
            if a is None or b is None:
                continue
            if a < p["low"] <= b:
                sig[i] = "CALL"
            elif a > p["high"] >= b:
                sig[i] = "PUT"
        return sig
