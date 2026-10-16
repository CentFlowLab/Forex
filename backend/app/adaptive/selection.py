"""Seleção adaptativa com promoção, suspensão e regressão controladas."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.adaptive import AdaptiveChangeLog, AdaptiveStrategyState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def log_change(
    db: Session,
    *,
    strategy_state_id: int,
    experiment_id: int,
    action: str,
    from_status: str,
    to_status: str,
    reason: str,
) -> None:
    db.add(
        AdaptiveChangeLog(
            strategy_state_id=strategy_state_id,
            experiment_id=experiment_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
        )
    )


def promote_strategy(
    db: Session,
    state: AdaptiveStrategyState,
    *,
    experiment_id: int,
    reason: str,
) -> AdaptiveStrategyState:
    prev = state.status
    if prev not in {"experimental", "validating"}:
        return state
    state.previous_status = prev
    state.status = "approved"
    state.reason = reason
    state.updated_at = _utcnow()
    log_change(
        db,
        strategy_state_id=state.id,
        experiment_id=experiment_id,
        action="aprovar",
        from_status=prev,
        to_status="approved",
        reason=reason,
    )
    return state


def suspend_strategy(
    db: Session,
    state: AdaptiveStrategyState,
    *,
    experiment_id: int,
    reason: str,
) -> AdaptiveStrategyState:
    prev = state.status
    state.previous_status = prev
    state.status = "suspended"
    state.reason = reason
    state.updated_at = _utcnow()
    log_change(
        db,
        strategy_state_id=state.id,
        experiment_id=experiment_id,
        action="suspender",
        from_status=prev,
        to_status="suspended",
        reason=reason,
    )
    return state


def rollback_strategy(
    db: Session,
    state: AdaptiveStrategyState,
    *,
    experiment_id: int,
    reason: str,
) -> AdaptiveStrategyState:
    """Regressa ao estado anterior quando possível."""
    prev = state.status
    target = state.previous_status or "experimental"
    if target == prev:
        target = "experimental"
    state.previous_status = prev
    state.status = target
    state.reason = reason
    state.updated_at = _utcnow()
    log_change(
        db,
        strategy_state_id=state.id,
        experiment_id=experiment_id,
        action="regredir",
        from_status=prev,
        to_status=target,
        reason=reason,
    )
    return state


def select_for_regime(
    candidates: list[dict[str, Any]],
    current_regime: str,
    *,
    min_score: float = 0.5,
) -> dict[str, Any]:
    """Seleciona a melhor estratégia aprovada/compatível; senão AGUARDAR.

    Nunca aumenta limites de risco.
    """
    eligible = [
        c
        for c in candidates
        if c.get("status") in {"approved", "validating"}
        and float(c.get("score") or 0) >= min_score
        and not c.get("overfit_rejected")
    ]
    # Preferir afinidade de regime.
    regime_fit = [
        c
        for c in eligible
        if current_regime in (c.get("preferred_regimes") or [])
        or current_regime in json.loads(c.get("regime_affinity_json") or "[]")
    ]
    pool = regime_fit or eligible
    if not pool:
        return {
            "action": "HOLD",
            "strategy_key": None,
            "strategy_state_id": 0,
            "reason": (
                "Sem vantagem demonstrável no regime atual — AGUARDAR. "
                "Resultados históricos não são previsão de lucro futuro."
            ),
            "risk_limits_changed": False,
        }

    best = max(pool, key=lambda c: float(c.get("score") or 0))
    # Exigir score de validação/OOS positivo o suficiente.
    if float(best.get("score") or 0) < min_score:
        return {
            "action": "HOLD",
            "strategy_key": None,
            "strategy_state_id": 0,
            "reason": "Score insuficiente após validação — AGUARDAR.",
            "risk_limits_changed": False,
        }

    return {
        "action": "USE_STRATEGY",
        "strategy_key": best.get("strategy_key"),
        "strategy_state_id": best.get("id") or 0,
        "params": best.get("params") or {},
        "score": best.get("score"),
        "reason": (
            f"Selecionada {best.get('display_name')} para regime {current_regime} "
            f"com score validado {best.get('score')}. "
            "Não constitui garantia de rentabilidade futura."
        ),
        "risk_limits_changed": False,
    }


def detect_degradation(
    previous_oos_score: float,
    current_oos_score: float,
    *,
    drop_threshold: float = 3.0,
) -> bool:
    return (previous_oos_score - current_oos_score) >= drop_threshold
