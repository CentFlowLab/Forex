"""Interface comum das estratégias candidatas."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

Signal = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class StrategySpec:
    key: str
    display_name: str
    description: str
    default_params: dict[str, Any]
    param_bounds: dict[str, tuple[Any, Any, Any]]  # min, max, step
    preferred_regimes: tuple[str, ...]
    complexity: float  # penalização (1.0 = simples)


class Strategy(ABC):
    spec: StrategySpec

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = {**self.spec.default_params, **(params or {})}
        self._validate_params()

    def _validate_params(self) -> None:
        for key, (lo, hi, _step) in self.spec.param_bounds.items():
            if key not in self.params:
                continue
            val = self.params[key]
            if isinstance(lo, (int, float)) and isinstance(val, (int, float)):
                if val < lo or val > hi:
                    raise ValueError(
                        f"Parâmetro {key}={val} fora dos limites [{lo}, {hi}] "
                        f"da estratégia {self.spec.key}."
                    )

    @abstractmethod
    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        """Sinal com base apenas nos dados até ao índice atual (inclusive)."""

    def complexity_score(self) -> float:
        return float(self.spec.complexity)
