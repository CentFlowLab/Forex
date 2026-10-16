"""Deterministic API spending limits. Enforced before agent calls."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.audit import ApiUsage


class BudgetExceeded(Exception):
    pass


def _spent_aicalls(db: Session, since: datetime) -> float:
    """Custos do robô/OpenAI compat — AICallLog status=ok."""
    from app.models.ai_control import AICallLog

    total = db.scalar(
        select(func.coalesce(func.sum(AICallLog.estimated_cost_usd), 0.0)).where(
            AICallLog.created_at >= since,
            AICallLog.status == "ok",
        )
    )
    return float(total or 0.0)


def spent_today_usd(db: Session) -> float:
    """Gasto do dia — max(ApiUsage, AICallLog) para não duplicar a mesma chamada."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    usage = db.scalar(
        select(func.coalesce(func.sum(ApiUsage.estimated_cost_usd), 0.0)).where(
            ApiUsage.created_at >= start
        )
    )
    return max(float(usage or 0.0), _spent_aicalls(db, start))


def spent_since(db: Session, since: datetime) -> float:
    usage = db.scalar(
        select(func.coalesce(func.sum(ApiUsage.estimated_cost_usd), 0.0)).where(
            ApiUsage.created_at >= since
        )
    )
    return max(float(usage or 0.0), _spent_aicalls(db, since))


def assert_budget_allows(db: Session, session_started: datetime | None = None) -> None:
    settings = get_settings()
    daily = spent_today_usd(db)
    if daily >= settings.ai_daily_budget_usd:
        raise BudgetExceeded(
            f"Orçamento diário de IA esgotado (${daily:.4f} / ${settings.ai_daily_budget_usd:.2f})."
        )
    if session_started is not None:
        session_spent = spent_since(db, session_started)
        if session_spent >= settings.ai_session_budget_usd:
            raise BudgetExceeded(
                f"Orçamento de sessão de IA esgotado (${session_spent:.4f} / ${settings.ai_session_budget_usd:.2f})."
            )
