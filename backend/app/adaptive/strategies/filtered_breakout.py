"""Breakout com filtro de volatilidade mínima e máxima."""

from __future__ import annotations

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import atr


class FilteredBreakoutStrategy(Strategy):
    spec = StrategySpec(
        key="filtered_breakout",
        display_name="Breakout com filtro de volatilidade",
        description=(
            "Rompe canal recente só se ATR estiver numa banda saudável "
            "(evita mercados mortos e explosões extremas)."
        ),
        default_params={
            "lookback": 24,
            "atr_period": 14,
            "atr_min_ratio": 0.9,
            "atr_max_ratio": 2.0,
        },
        param_bounds={
            "lookback": (12, 36, 6),
            "atr_period": (10, 20, 2),
            "atr_min_ratio": (0.7, 1.2, 0.1),
            "atr_max_ratio": (1.5, 2.5, 0.25),
        },
        preferred_regimes=("volatilidade_elevada", "tendencia_alta", "tendencia_baixa"),
        complexity=1.6,
    )

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        lb = int(self.params["lookback"])
        ap = int(self.params["atr_period"])
        amin = float(self.params["atr_min_ratio"])
        amax = float(self.params["atr_max_ratio"])
        if len(closes) < max(lb, ap) + 20:
            return "HOLD"
        prior_h = highs[-(lb + 1) : -1]
        prior_l = lows[-(lb + 1) : -1]
        if len(prior_h) < lb:
            return "HOLD"
        a_now = atr(highs, lows, closes, ap)
        a_ref = atr(highs[:-ap], lows[:-ap], closes[:-ap], ap)
        if a_now is None or a_ref is None or a_ref <= 0:
            return "HOLD"
        ratio = a_now / a_ref
        if ratio < amin or ratio > amax:
            return "HOLD"
        ch = max(prior_h)
        cl = min(prior_l)
        if closes[-1] > ch:
            return "BUY"
        if closes[-1] < cl:
            return "SELL"
        return "HOLD"
