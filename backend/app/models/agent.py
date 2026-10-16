from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(16), default="xai")
    model: Mapped[str] = mapped_column(String(64), default="grok-4-latest")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentReport(Base):
    __tablename__ = "agent_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    role: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="ok")
    bias: Mapped[str] = mapped_column(String(16), default="HOLD")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainty: Mapped[float] = mapped_column(Float, default=1.0)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionRecord(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    environment: Mapped[str] = mapped_column(String(16), default="PAPER")
    recommendation: Mapped[str] = mapped_column(String(8), default="HOLD")
    coordinator_rationale: Mapped[str] = mapped_column(Text, default="")
    risk_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_reason: Mapped[str] = mapped_column(Text, default="")
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    client_order_id: Mapped[str] = mapped_column(String(64), default="")
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data_source: Mapped[str] = mapped_column(String(32), default="synthetic")
    close_price: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    backtest_run_id: Mapped[int] = mapped_column(Integer, default=0)
