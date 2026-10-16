from __future__ import annotations

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import atr


class VolatilityBreakoutStrategy(Strategy):
    spec = StrategySpec(
        key="volatility_breakout",
        display_name="Breakout de volatilidade",
        description="Entra na direção do rompimento do máximo/mínimo recente ampliado pelo ATR.",
        default_params={"lookback": 20, "atr_mult": 1.5, "atr_period": 14},
        param_bounds={
            "lookback": (10, 40, 5),
            "atr_mult": (1.0, 2.5, 0.25),
            "atr_period": (10, 20, 2),
        },
        preferred_regimes=("volatilidade_elevada", "tendencia_alta", "tendencia_baixa"),
        complexity=1.4,
    )

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        lb = int(self.params["lookback"])
        mult = float(self.params["atr_mult"])
        ap = int(self.params["atr_period"])
        if len(closes) < max(lb, ap) + 2:
            return "HOLD"
        # Usa apenas barras anteriores ao fecho atual para definir o canal (sem look-ahead).
        prior_h = highs[-(lb + 1) : -1]
        prior_l = lows[-(lb + 1) : -1]
        if len(prior_h) < lb:
            return "HOLD"
        channel_high = max(prior_h)
        channel_low = min(prior_l)
        a = atr(highs, lows, closes, ap)
        if a is None:
            return "HOLD"
        buffer = a * mult
        if closes[-1] > channel_high + buffer * 0.15:
            return "BUY"
        if closes[-1] < channel_low - buffer * 0.15:
            return "SELL"
        return "HOLD"
