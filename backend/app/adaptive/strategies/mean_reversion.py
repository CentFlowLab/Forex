from __future__ import annotations

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import rsi, sma


class MeanReversionStrategy(Strategy):
    spec = StrategySpec(
        key="mean_reversion",
        display_name="Reversão à média",
        description="Compra em sobrevenda e vende em sobrecompra relativamente à média, via RSI.",
        default_params={"rsi_period": 14, "oversold": 30, "overbought": 70, "sma_period": 40},
        param_bounds={
            "rsi_period": (7, 21, 2),
            "oversold": (20, 40, 5),
            "overbought": (60, 80, 5),
            "sma_period": (20, 60, 10),
        },
        preferred_regimes=("mercado_lateral", "volatilidade_reduzida"),
        complexity=1.3,
    )

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        rp = int(self.params["rsi_period"])
        os_level = float(self.params["oversold"])
        ob_level = float(self.params["overbought"])
        sp = int(self.params["sma_period"])
        if len(closes) < max(rp + 2, sp + 1):
            return "HOLD"
        r = rsi(closes, rp)
        mid = sma(closes, sp)
        if r is None or mid is None:
            return "HOLD"
        # Evitar operar contra tendência forte: preço longe da média.
        dist = abs(closes[-1] - mid) / mid if mid else 0.0
        if dist > 0.01:
            return "HOLD"
        if r <= os_level:
            return "BUY"
        if r >= ob_level:
            return "SELL"
        return "HOLD"
