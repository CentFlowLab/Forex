"""Separação cronológica treino/validação/teste e walk-forward."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence


@dataclass(frozen=True)
class ChronoSplit:
    train: Sequence[Any]
    validation: Sequence[Any]
    test: Sequence[Any]
    train_ratio: float
    validation_ratio: float
    test_ratio: float

    def as_dict(self) -> dict:
        return {
            "train_bars": len(self.train),
            "validation_bars": len(self.validation),
            "test_bars": len(self.test),
            "train_ratio": self.train_ratio,
            "validation_ratio": self.validation_ratio,
            "test_ratio": self.test_ratio,
        }


def chronological_split(
    candles: Sequence[Any],
    *,
    train_ratio: float = 0.60,
    validation_ratio: float = 0.20,
    test_ratio: float = 0.20,
) -> ChronoSplit:
    """Divide estritamente por ordem temporal — sem misturar o futuro no treino."""
    total = train_ratio + validation_ratio + test_ratio
    if abs(total - 1.0) > 1e-6:
        raise ValueError("As proporções treino/validação/teste devem somar 1.0.")
    n = len(candles)
    if n < 90:
        raise ValueError("São necessárias pelo menos 90 velas para separação cronológica.")

    i_train = int(n * train_ratio)
    i_val = i_train + int(n * validation_ratio)
    # Garantir que o resto vai para teste.
    train = candles[:i_train]
    validation = candles[i_train:i_val]
    test = candles[i_val:]
    if not train or not validation or not test:
        raise ValueError("Separação inválida: um dos conjuntos ficou vazio.")
    # Integridade: último treino < primeiro val < … 
    if hasattr(train[-1], "ts") and hasattr(validation[0], "ts"):
        # Comparação tolerante a datetimes naive/aware (SQLite).
        def _ts(x: Any) -> datetime:
            t = x.ts
            from datetime import timezone

            if getattr(t, "tzinfo", None) is None:
                return t.replace(tzinfo=timezone.utc)
            return t

        if _ts(train[-1]) >= _ts(validation[0]):
            raise ValueError("Look-ahead: treino sobrepõe-se à validação.")
        if _ts(validation[-1]) >= _ts(test[0]):
            raise ValueError("Look-ahead: validação sobrepõe-se ao teste.")
    return ChronoSplit(train, validation, test, train_ratio, validation_ratio, test_ratio)


def walk_forward_windows(
    candles: Sequence[Any],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
) -> list[tuple[Sequence[Any], Sequence[Any]]]:
    """Janelas walk-forward: treino seguido de teste imediatamente posterior."""
    step = step or test_size
    windows: list[tuple[Sequence[Any], Sequence[Any]]] = []
    start = 0
    n = len(candles)
    while start + train_size + test_size <= n:
        train = candles[start : start + train_size]
        test = candles[start + train_size : start + train_size + test_size]
        if hasattr(train[-1], "ts") and train[-1].ts >= test[0].ts:
            raise ValueError("Look-ahead detetado numa janela walk-forward.")
        windows.append((train, test))
        start += step
    return windows
