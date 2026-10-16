"""Motor de avaliação — registo de decisões com proteção anti look-ahead."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.adaptive import AdaptiveDecisionLog


def record_decision(db: Session, payload: dict[str, Any]) -> AdaptiveDecisionLog:
    row = AdaptiveDecisionLog(
        experiment_id=int(payload.get("experiment_id") or 0),
        strategy_state_id=int(payload.get("strategy_state_id") or 0),
        decision_ts=payload["decision_ts"],
        data_source=str(payload.get("data_source") or ""),
        data_age_seconds=float(payload.get("data_age_seconds") or 0.0),
        regime=str(payload.get("regime") or "desconhecido"),
        strategy_key=str(payload.get("strategy_key") or ""),
        params_json=json.dumps(payload.get("params") or {}, ensure_ascii=False),
        agent_arguments_json=json.dumps(payload.get("agent_arguments") or {}, ensure_ascii=False),
        final_decision=str(payload.get("final_decision") or "HOLD"),
        entry_price=float(payload.get("entry_price") or 0.0),
        exit_price=float(payload.get("exit_price") or 0.0),
        spread_pips=float(payload.get("spread_pips") or 0.0),
        slippage_pips=float(payload.get("slippage_pips") or 0.0),
        commission=float(payload.get("commission") or 0.0),
        estimated_costs=float(payload.get("estimated_costs") or 0.0),
        pnl=float(payload.get("pnl") or 0.0),
        drawdown_pct=float(payload.get("drawdown_pct") or 0.0),
        outcome_available_at=payload.get("outcome_available_at"),
        outcome_label=str(payload.get("outcome_label") or ""),
        split_label=str(payload.get("split_label") or ""),
        is_simulated=True,
    )
    db.add(row)
    return row


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def outcomes_visible_as_of(
    db: Session,
    *,
    as_of: datetime,
    experiment_id: int | None = None,
) -> list[dict[str, Any]]:
    """Devolve apenas resultados cujo outcome_available_at <= as_of.

    Garante que o resultado futuro nunca fica disponível antes do momento correto.
    """
    as_of_aware = _aware(as_of)
    assert as_of_aware is not None
    q = select(AdaptiveDecisionLog).where(AdaptiveDecisionLog.outcome_available_at.is_not(None))
    if experiment_id is not None:
        q = q.where(AdaptiveDecisionLog.experiment_id == experiment_id)
    rows = list(db.scalars(q.order_by(AdaptiveDecisionLog.outcome_available_at.asc())))
    visible = [r for r in rows if _aware(r.outcome_available_at) is not None and _aware(r.outcome_available_at) <= as_of_aware]
    return [_serialize(r, include_outcome=True) for r in visible]


def decisions_for_agents_as_of(
    db: Session,
    *,
    as_of: datetime,
    experiment_id: int | None = None,
) -> list[dict[str, Any]]:
    """Vista segura para agentes: decisões até as_of, sem PnL futuro."""
    as_of_aware = _aware(as_of)
    assert as_of_aware is not None
    q = select(AdaptiveDecisionLog)
    if experiment_id is not None:
        q = q.where(AdaptiveDecisionLog.experiment_id == experiment_id)
    rows = list(db.scalars(q.order_by(AdaptiveDecisionLog.decision_ts.asc())))
    out: list[dict[str, Any]] = []
    for r in rows:
        dts = _aware(r.decision_ts)
        if dts is None or dts > as_of_aware:
            continue
        outcome_ts = _aware(r.outcome_available_at)
        include = bool(outcome_ts and outcome_ts <= as_of_aware)
        out.append(_serialize(r, include_outcome=include))
    return out


def _serialize(r: AdaptiveDecisionLog, *, include_outcome: bool) -> dict[str, Any]:
    data = {
        "id": r.id,
        "experiment_id": r.experiment_id,
        "decision_ts": r.decision_ts.isoformat() if r.decision_ts else None,
        "data_source": r.data_source,
        "data_age_seconds": r.data_age_seconds,
        "regime": r.regime,
        "strategy_key": r.strategy_key,
        "params": json.loads(r.params_json or "{}"),
        "agent_arguments": json.loads(r.agent_arguments_json or "{}"),
        "final_decision": r.final_decision,
        "entry_price": r.entry_price,
        "spread_pips": r.spread_pips,
        "estimated_costs": r.estimated_costs,
        "split_label": r.split_label,
        "is_simulated": r.is_simulated,
        "outcome_available": include_outcome,
    }
    if include_outcome:
        data.update(
            {
                "exit_price": r.exit_price,
                "pnl": r.pnl,
                "drawdown_pct": r.drawdown_pct,
                "outcome_label": r.outcome_label or ("lucro" if r.pnl > 0 else "prejuízo" if r.pnl < 0 else "nulo"),
                "outcome_available_at": r.outcome_available_at.isoformat() if r.outcome_available_at else None,
            }
        )
    else:
        data.update(
            {
                "exit_price": None,
                "pnl": None,
                "drawdown_pct": None,
                "outcome_label": "ainda_indisponível",
                "outcome_available_at": None,
            }
        )
    return data
