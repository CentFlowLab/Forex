"""Estado do ciclo autónomo e execução paper."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutonomyState(Base):
    __tablename__ = "autonomy_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    paper_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=180)
    lookback_days: Mapped[int] = mapped_column(Integer, default=365)
    max_combos: Mapped[int] = mapped_column(Integer, default=6)
    max_cpu_pct: Mapped[float] = mapped_column(Float, default=70.0)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    cycles_completed: Mapped[int] = mapped_column(Integer, default=0)
    ai_calls: Mapped[int] = mapped_column(Integer, default=0)  # deve permanecer 0
    # idle|executing|paused|error|off — espelho do último estado conhecido
    run_status: Mapped[str] = mapped_column(String(24), default="off")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AutonomyCycleLog(Base):
    """Registo de cada ciclo de investigação (início, fim, duração, resultado)."""

    __tablename__ = "autonomy_cycle_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running|ok|skipped|error
    message: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    experiment_id: Mapped[int] = mapped_column(Integer, default=0)
    strategies_count: Mapped[int] = mapped_column(Integer, default=0)


class ExperimentFingerprint(Base):
    """Evita repetir experiências equivalentes."""

    __tablename__ = "experiment_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    strategy_key: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    period_start: Mapped[str] = mapped_column(String(32), default="")
    period_end: Mapped[str] = mapped_column(String(32), default="")
    experiment_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AutonomyHypothesis(Base):
    __tablename__ = "autonomy_hypotheses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending|queued|done|skipped
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperTradeLog(Base):
    __tablename__ = "paper_trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_state_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    strategy_key: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|closed|blocked
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop: Mapped[float] = mapped_column(Float, default=0.0)
    take: Mapped[float] = mapped_column(Float, default=0.0)
    lots: Mapped[float] = mapped_column(Float, default=0.1)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    costs: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    quote_source: Mapped[str] = mapped_column(String(32), default="")
    quote_age_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    is_replay: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AutonomyAlert(Base):
    __tablename__ = "autonomy_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    category: Mapped[str] = mapped_column(String(32), default="autonomy")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
