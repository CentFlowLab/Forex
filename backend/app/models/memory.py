"""Memória persistente de análises e decisões (inspirada no TradingAgents)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisRun(Base):
    """Execução manual ou amostrada do pipeline com debate Bull/Bear."""

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    workflow: Mapped[str] = mapped_column(String(32), default="tradingagents")
    status: Mapped[str] = mapped_column(String(24), default="queued")
    # queued | running | completed | cancelled | failed | blocked
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    debate_rounds_requested: Mapped[int] = mapped_column(Integer, default=2)
    debate_rounds_completed: Mapped[int] = mapped_column(Integer, default=0)
    market_snapshot_id: Mapped[str] = mapped_column(String(64), default="")
    market_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data_source: Mapped[str] = mapped_column(String(32), default="")
    data_freshness: Mapped[str] = mapped_column(String(32), default="unknown")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    recommendation: Mapped[str] = mapped_column(String(8), default="HOLD")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_veto: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_veto_reason: Mapped[str] = mapped_column(Text, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    models_used_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionMemory(Base):
    """
    Memória de decisão com factos de mercado separados de outcomes simulados.
    Outcomes só podem ser preenchidos depois de ocorrerem — sem look-ahead.
    """

    __tablename__ = "decision_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    decision_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    market_snapshot_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    analysis_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    agent_reports_json: Mapped[str] = mapped_column(Text, default="{}")
    debate_json: Mapped[str] = mapped_column(Text, default="{}")
    final_decision_json: Mapped[str] = mapped_column(Text, default="{}")
    simulated_execution_json: Mapped[str] = mapped_column(Text, default="{}")
    realized_virtual_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_json: Mapped[str] = mapped_column(Text, default="")
    lessons_json: Mapped[str] = mapped_column(Text, default="")
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
