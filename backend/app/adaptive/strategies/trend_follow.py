from __future__ import annotations

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import ema, sma


class TrendFollowStrategy(Strategy):
    spec = StrategySpec(
        key="trend_follow",
        display_name="Seguimento de tendência",
        description="Alinha posição com a direção da EMA relativamente à SMA, com filtro de força.",
        default_params={"ema_period": 21, "sma_period": 55, "min_slope": 0.0005},
        param_bounds={
            "ema_period": (10, 40, 5),
            "sma_period": (30, 100, 10),
            "min_slope": (0.0002, 0.002, 0.0002),
        },
        preferred_regimes=("tendencia_alta", "tendencia_baixa"),
        complexity=1.2,
    )

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        ep = int(self.params["ema_period"])
        sp = int(self.params["sma_period"])
        min_slope = float(self.params["min_slope"])
        if ep >= sp or len(closes) < sp + 2:
            return "HOLD"
        e = ema(closes, ep)
        s = sma(closes, sp)
        s_prev = sma(closes[:-5] if len(closes) > 5 else closes[:-1], sp)
        if e is None or s is None or s_prev is None or closes[-1] <= 0:
            return "HOLD"
        slope = (s - s_prev) / closes[-1]
        if e > s and slope >= min_slope:
            return "BUY"
        if e < s and slope <= -min_slope:
            return "SELL"
        return "HOLD"
