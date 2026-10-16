"""API de controlo e auditoria de consumo de IA."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control
from app.database import get_db
from app.models.ai_control import AICallLog

router = APIRouter(prefix="/ai", tags=["ai-control"])


class AuthorizeBody(BaseModel):
    confirmed: bool = False
    workflow: str = "tradingagents"
    debate_rounds: int = Field(default=2, ge=0, le=4)
    model: str | None = None


class EnableBody(BaseModel):
    enabled: bool


@router.get("/status")
def ai_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_ai_control().status(db)


@router.get("/estimate")
def ai_estimate(
    debate_rounds: int = Query(2, ge=0, le=4),
    model: str | None = None,
) -> dict[str, Any]:
    try:
        return get_ai_control().estimate_analysis(debate_rounds=debate_rounds, model=model)
    except AIAuthorizationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/authorize")
def ai_authorize(body: AuthorizeBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    control = get_ai_control()
    try:
        return control.create_authorization(
            db,
            confirmed=body.confirmed,
            workflow=body.workflow,
            debate_rounds=body.debate_rounds,
            model=body.model,
        )
    except AIDisabled as exc:
        raise HTTPException(403, str(exc)) from exc
    except (AIAuthorizationError, BudgetExceeded) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/block")
def ai_block() -> dict[str, Any]:
    """Bloqueio de emergência — impede novas chamadas e cancela autorizações pendentes.

    Pedidos já enviados ao fornecedor podem continuar a ser faturados.
    """
    control = get_ai_control()
    control.block_emergency()
    return {
        "ai_enabled": False,
        "emergency_block": True,
        "message": (
            "IA bloqueada no backend. Novas chamadas e retries estão impedidos. "
            "Pedidos já enviados ao fornecedor podem ainda ser faturados."
        ),
    }


@router.put("/enabled")
def ai_set_enabled(body: EnableBody) -> dict[str, Any]:
    control = get_ai_control()
    if not body.enabled:
        control.block_emergency()
    else:
        control.set_enabled(True, emergency=False)
    return {
        "ai_enabled": control.is_enabled(),
        "emergency_block": not body.enabled,
        "message": "IA ativada." if body.enabled else "IA desligada / bloqueada.",
    }


@router.get("/calls")
def ai_calls(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(AICallLog).order_by(AICallLog.created_at.desc()).limit(limit)))
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "provider": r.provider,
            "model": r.model,
            "role": r.role,
            "analysis_id": r.analysis_id,
            "reason": r.reason,
            "authorized": r.authorized,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "estimated_cost_usd": r.estimated_cost_usd,
            "status": r.status,
            "error": r.error,
            "attempt": r.attempt,
        }
        for r in rows
    ]
