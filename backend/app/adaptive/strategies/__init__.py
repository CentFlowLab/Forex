from __future__ import annotations

from typing import Any

from app.adaptive.strategies.base import Strategy, StrategySpec
from app.adaptive.strategies.filtered_breakout import FilteredBreakoutStrategy
from app.adaptive.strategies.ma_crossover import MACrossoverStrategy
from app.adaptive.strategies.mean_reversion import MeanReversionStrategy
from app.adaptive.strategies.regime_mean_reversion import RegimeMeanReversionStrategy
from app.adaptive.strategies.session_trend import SessionTrendStrategy
from app.adaptive.strategies.trend_follow import TrendFollowStrategy
from app.adaptive.strategies.volatility_breakout import VolatilityBreakoutStrategy

STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    MACrossoverStrategy.spec.key: MACrossoverStrategy,
    TrendFollowStrategy.spec.key: TrendFollowStrategy,
    MeanReversionStrategy.spec.key: MeanReversionStrategy,
    VolatilityBreakoutStrategy.spec.key: VolatilityBreakoutStrategy,
    SessionTrendStrategy.spec.key: SessionTrendStrategy,
    RegimeMeanReversionStrategy.spec.key: RegimeMeanReversionStrategy,
    FilteredBreakoutStrategy.spec.key: FilteredBreakoutStrategy,
}


def list_catalog() -> list[StrategySpec]:
    return [cls.spec for cls in STRATEGY_CLASSES.values()]


def build_strategy(key: str, params: dict[str, Any] | None = None) -> Strategy:
    cls = STRATEGY_CLASSES.get(key)
    if cls is None:
        raise KeyError(f"Estratégia desconhecida: {key}")
    return cls(params)


def param_grid(spec: StrategySpec, max_combos: int = 12) -> list[dict[str, Any]]:
    """Gera combinações limitadas dentro dos limites predefinidos."""
    keys = list(spec.param_bounds.keys())
    if not keys:
        return [dict(spec.default_params)]

    axes: list[list[tuple[str, Any]]] = []
    for key in keys:
        lo, hi, step = spec.param_bounds[key]
        values: list[Any] = []
        if isinstance(lo, float) or isinstance(step, float):
            v = float(lo)
            while v <= float(hi) + 1e-12:
                values.append(round(v, 6))
                v += float(step)
        else:
            v = int(lo)
            while v <= int(hi):
                values.append(v)
                v += int(step)
        default = spec.default_params.get(key)
        picked = []
        if values:
            picked.append(values[0])
            picked.append(values[len(values) // 2])
            picked.append(values[-1])
        if default is not None:
            picked.append(default)
        uniq = []
        for x in picked:
            if x not in uniq:
                uniq.append(x)
        axes.append([(key, x) for x in uniq])

    combos: list[dict[str, Any]] = []

    def _rec(i: int, cur: dict[str, Any]) -> None:
        if len(combos) >= max_combos:
            return
        if i >= len(axes):
            if "fast" in cur and "slow" in cur and cur["fast"] >= cur["slow"]:
                return
            if "ema_period" in cur and "sma_period" in cur and cur["ema_period"] >= cur["sma_period"]:
                return
            if "oversold" in cur and "overbought" in cur and cur["oversold"] >= cur["overbought"]:
                return
            if "atr_min_ratio" in cur and "atr_max_ratio" in cur and cur["atr_min_ratio"] >= cur["atr_max_ratio"]:
                return
            if (
                "session_start_utc" in cur
                and "session_end_utc" in cur
                and cur["session_start_utc"] == cur["session_end_utc"]
            ):
                return
            combos.append(dict(cur))
            return
        for k, v in axes[i]:
            cur[k] = v
            _rec(i + 1, cur)
        cur.pop(axes[i][0][0], None)

    _rec(0, {})
    if not combos:
        combos.append(dict(spec.default_params))
    return combos[:max_combos]
