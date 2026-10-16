"""Estado persistente do robô de simulação (painel único)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RobotState(Base):
    __tablename__ = "robot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    decision: Mapped[str] = mapped_column(String(16), default="HOLD")  # BUY|SELL|HOLD
    block_reason: Mapped[str] = mapped_column(Text, default="")
    last_message: Mapped[str] = mapped_column(Text, default="")
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ticks_completed: Mapped[int] = mapped_column(Integer, default=0)
    quote_source: Mapped[str] = mapped_column(String(64), default="")
    quote_age_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    strategy_key: Mapped[str] = mapped_column(String(64), default="")
    ai_calls: Mapped[int] = mapped_column(Integer, default=0)
    # simulated | openai — openai nunca automático sem autorização
    decision_mode: Mapped[str] = mapped_column(String(24), default="simulated")
    last_justification: Mapped[str] = mapped_column(Text, default="")
    last_ai_decision_id: Mapped[int] = mapped_column(Integer, default=0)
    paid_ai_calls: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIDecisionLog(Base):
    """Registo de cada análise/decisão IA (simulada ou paga)."""

    __tablename__ = "ai_decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(24), default="simulated")  # simulated|openai
    decision: Mapped[str] = mapped_column(String(16), default="HOLD")
    justification: Mapped[str] = mapped_column(Text, default="")
    favoring_scenario: Mapped[str] = mapped_column(Text, default="")
    invalidating_scenario: Mapped[str] = mapped_column(Text, default="")
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    time_horizon: Mapped[str] = mapped_column(String(64), default="")
    risks: Mapped[str] = mapped_column(Text, default="")
    missing_information: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    proposal_json: Mapped[str] = mapped_column(Text, default="{}")
    risk_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_reasons: Mapped[str] = mapped_column(Text, default="")
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    client_order_id: Mapped[str] = mapped_column(String(64), default="")
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    quote_lag_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    outcome_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
