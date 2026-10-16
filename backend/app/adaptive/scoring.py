"""Pontuação de estratégias — não seleciona apenas por rentabilidade histórica."""

from __future__ import annotations

from typing import Any


def score_candidate(
    *,
    train_metrics: dict[str, Any],
    validation_metrics: dict[str, Any],
    test_metrics: dict[str, Any] | None,
    complexity: float,
    experiment_count: int,
    regime_match_ratio: float,
) -> dict[str, float]:
    """Devolve componentes e score final.

    Penaliza: drawdown alto, poucas operações, complexidade, muitas experiências (sobreajuste),
    divergência forte treino vs validação.
    """
    def _safe_pf(m: dict[str, Any]) -> float:
        return float(m.get("profit_factor") or 0.0)

    def _safe_dd(m: dict[str, Any]) -> float:
        return float(m.get("max_drawdown_pct") or 0.0)

    def _safe_trades(m: dict[str, Any]) -> int:
        return int(m.get("trades") or 0)

    def _safe_pnl(m: dict[str, Any]) -> float:
        return float(m.get("net_pnl") or 0.0)

    val_pf = _safe_pf(validation_metrics)
    val_dd = _safe_dd(validation_metrics)
    val_trades = _safe_trades(validation_metrics)
    train_pf = _safe_pf(train_metrics)

    # Base na validação (não no treino).
    base = (val_pf - 1.0) * 10.0 + min(_safe_pnl(validation_metrics) / 100.0, 5.0)
    dd_penalty = min(val_dd, 40.0) * 0.35
    complexity_penalty = max(0.0, complexity - 1.0) * 2.5
    sparse_penalty = 3.0 if val_trades < 5 else 0.0
    # Sobreajuste: treino muito melhor que validação.
    overfit_gap = max(0.0, train_pf - val_pf)
    overfit_penalty = overfit_gap * 4.0
    experiment_penalty = min(experiment_count, 30) * 0.08
    regime_bonus = regime_match_ratio * 2.0

    oos_component = 0.0
    if test_metrics is not None:
        oos_pf = _safe_pf(test_metrics)
        oos_component = (oos_pf - 1.0) * 8.0 - min(_safe_dd(test_metrics), 40.0) * 0.25

    total = (
        base
        - dd_penalty
        - complexity_penalty
        - sparse_penalty
        - overfit_penalty
        - experiment_penalty
        + regime_bonus
        + oos_component
    )

    return {
        "base": round(base, 4),
        "dd_penalty": round(dd_penalty, 4),
        "complexity_penalty": round(complexity_penalty, 4),
        "sparse_penalty": round(sparse_penalty, 4),
        "overfit_penalty": round(overfit_penalty, 4),
        "experiment_penalty": round(experiment_penalty, 4),
        "regime_bonus": round(regime_bonus, 4),
        "oos_component": round(oos_component, 4),
        "total": round(total, 4),
    }


def reject_for_overfitting(
    train_metrics: dict[str, Any],
    validation_metrics: dict[str, Any],
    *,
    max_pf_gap: float = 1.25,
    min_val_trades: int = 3,
) -> tuple[bool, str]:
    train_pf = float(train_metrics.get("profit_factor") or 0.0)
    val_pf = float(validation_metrics.get("profit_factor") or 0.0)
    val_trades = int(validation_metrics.get("trades") or 0)
    if val_trades < min_val_trades:
        return True, "Rejeitada: poucas operações na validação (risco de sobreajustamento)."
    if train_pf - val_pf >= max_pf_gap and train_pf > 1.2:
        return True, (
            f"Rejeitada por sobreajustamento: PF treino={train_pf:.2f} vs validação={val_pf:.2f}."
        )
    if float(validation_metrics.get("max_drawdown_pct") or 0) > 25.0:
        return True, "Rejeitada: drawdown de validação excessivo (>25%)."
    return False, ""
