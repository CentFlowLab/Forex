"""Reversão à média condicionada a mercado lateral / baixa volatilidade."""

from __future__ import annotations

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import atr, sma


class RegimeMeanReversionStrategy(Strategy):
    spec = StrategySpec(
        key="regime_mean_reversion",
        display_name="Reversão condicionada ao regime",
        description=(
            "Compra/vende desvios da média apenas quando ATR relativo está baixo "
            "(mercado mais lateral) — evita perseguir tendência."
        ),
        default_params={"period": 20, "z_entry": 1.5, "atr_max_ratio": 1.1},
        param_bounds={
            "period": (10, 40, 5),
            "z_entry": (1.0, 2.5, 0.25),
            "atr_max_ratio": (0.8, 1.4, 0.2),
        },
        preferred_regimes=("mercado_lateral", "volatilidade_reduzida"),
        complexity=1.5,
    )

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        period = int(self.params["period"])
        z_entry = float(self.params["z_entry"])
        atr_max = float(self.params["atr_max_ratio"])
        if len(closes) < period + 15:
            return "HOLD"
        window = closes[-period:]
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        std = var**0.5
        if std <= 1e-9:
            return "HOLD"
        z = (closes[-1] - mean) / std
        a_now = atr(highs, lows, closes, 14)
        a_ref = atr(highs[:-14] or highs, lows[:-14] or lows, closes[:-14] or closes, 14)
        if a_now is None or a_ref is None or a_ref <= 0:
            return "HOLD"
        if (a_now / a_ref) > atr_max:
            return "HOLD"  # volatilidade elevada — não reverter
        m = sma(closes, period)
        if m is None:
            return "HOLD"
        if z <= -z_entry and closes[-1] < m:
            return "BUY"
        if z >= z_entry and closes[-1] > m:
            return "SELL"
        return "HOLD"
