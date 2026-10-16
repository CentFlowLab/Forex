"""Persistência da memória de decisões — outcomes só após ocorrerem."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory import DecisionMemory


def save_decision_memory(
    db: Session,
    *,
    analysis_run_id: int,
    decision_id: int,
    market_snapshot_id: str,
    analysis_timestamp: datetime,
    symbol: str,
    timeframe: str,
    agent_reports: dict[str, Any],
    debate: dict[str, Any],
    final_decision: dict[str, Any],
    simulated_execution: dict[str, Any] | None = None,
) -> DecisionMemory:
    row = DecisionMemory(
        analysis_run_id=analysis_run_id,
        decision_id=decision_id,
        market_snapshot_id=market_snapshot_id,
        analysis_timestamp=analysis_timestamp,
        symbol=symbol,
        timeframe=timeframe,
        agent_reports_json=json.dumps(agent_reports, default=str),
        debate_json=json.dumps(debate, default=str),
        final_decision_json=json.dumps(final_decision, default=str),
        simulated_execution_json=json.dumps(simulated_execution or {}, default=str),
        # Outcomes e lições ficam vazios até existir resultado posterior.
        realized_virtual_pnl=None,
        outcome_json="",
        lessons_json="",
        outcome_recorded_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_simulated_outcome(
    db: Session,
    memory_id: int,
    *,
    realized_virtual_pnl: float,
    outcome: dict[str, Any],
    lessons: list[str] | None = None,
    as_of: datetime | None = None,
) -> DecisionMemory:
    """
    Regista o resultado simulado apenas depois de ocorrer.
    Recusa look-ahead: as_of deve ser >= analysis_timestamp.
    """
    row = db.get(DecisionMemory, memory_id)
    if row is None:
        raise ValueError("Memória de decisão não encontrada.")
    when = as_of or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    analysis_ts = row.analysis_timestamp
    if analysis_ts.tzinfo is None:
        analysis_ts = analysis_ts.replace(tzinfo=timezone.utc)
    if when < analysis_ts:
        raise ValueError(
            "Look-ahead recusado: não é permitido registar um outcome anterior à análise."
        )
    if row.outcome_recorded_at is not None:
        raise ValueError("Outcome já registado para esta memória.")

    row.realized_virtual_pnl = float(realized_virtual_pnl)
    row.outcome_json = json.dumps(outcome, default=str)
    row.lessons_json = json.dumps(lessons or [], default=str)
    row.outcome_recorded_at = when
    db.commit()
    db.refresh(row)
    return row


def list_memory(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(DecisionMemory).order_by(DecisionMemory.created_at.desc()).limit(limit)))
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r.id,
                "analysis_run_id": r.analysis_run_id,
                "decision_id": r.decision_id,
                "market_snapshot_id": r.market_snapshot_id,
                "analysis_timestamp": r.analysis_timestamp.isoformat(),
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "final_decision": json.loads(r.final_decision_json or "{}"),
                "has_outcome": r.outcome_recorded_at is not None,
                "realized_virtual_pnl": r.realized_virtual_pnl,
                "outcome_recorded_at": r.outcome_recorded_at.isoformat() if r.outcome_recorded_at else None,
                "lessons": json.loads(r.lessons_json or "[]") if r.lessons_json else [],
            }
        )
    return out
