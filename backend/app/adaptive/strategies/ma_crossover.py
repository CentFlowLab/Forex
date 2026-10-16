from __future__ import annotations

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import sma


class MACrossoverStrategy(Strategy):
    spec = StrategySpec(
        key="ma_crossover",
        display_name="Cruzamento de médias móveis",
        description="Compra quando a média rápida cruza acima da lenta; vende no cruzamento inverso.",
        default_params={"fast": 20, "slow": 50},
        param_bounds={"fast": (5, 40, 5), "slow": (20, 100, 10)},
        preferred_regimes=("tendencia_alta", "tendencia_baixa"),
        complexity=1.0,
    )

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        fast = int(self.params["fast"])
        slow = int(self.params["slow"])
        if fast >= slow or len(closes) < slow + 1:
            return "HOLD"
        f_now = sma(closes, fast)
        s_now = sma(closes, slow)
        f_prev = sma(closes[:-1], fast)
        s_prev = sma(closes[:-1], slow)
        if None in (f_now, s_now, f_prev, s_prev):
            return "HOLD"
        if f_prev <= s_prev and f_now > s_now:
            return "BUY"
        if f_prev >= s_prev and f_now < s_now:
            return "SELL"
        return "HOLD"
