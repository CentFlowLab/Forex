from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    source: Mapped[str] = mapped_column(String(32), default="dukascopy")
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    parquet_paths: Mapped[str] = mapped_column(Text, default="[]")
    bars_m5: Mapped[int] = mapped_column(Integer, default=0)
    bars_m15: Mapped[int] = mapped_column(Integer, default=0)
    bars_h1: Mapped[int] = mapped_column(Integer, default=0)
    bars_h4: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    hours_total: Mapped[int] = mapped_column(Integer, default=0)
    hours_done: Mapped[int] = mapped_column(Integer, default=0)
    resume_hour: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ValidatedHistoryHour(Base):
    """Horas já consultadas ao datafeed — evita re-descarregar períodos validados."""

    __tablename__ = "validated_history_hours"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    source: Mapped[str] = mapped_column(String(32), default="dukascopy")
    hour_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(24), default="ok")  # ok|empty|unavailable
    tick_count: Mapped[int] = mapped_column(Integer, default=0)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReplaySession(Base):
    __tablename__ = "replay_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    source: Mapped[str] = mapped_column(String(32), default="dukascopy")
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cursor_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="idle")  # idle|playing|paused|finished
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    ai_every_n_bars: Mapped[int] = mapped_column(Integer, default=12)
    execute_trades: Mapped[bool] = mapped_column(Boolean, default=True)
    bars_total: Mapped[int] = mapped_column(Integer, default=0)
    decisions: Mapped[int] = mapped_column(Integer, default=0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    starting_equity: Mapped[float] = mapped_column(Float, default=10_000.0)
    ending_equity: Mapped[float] = mapped_column(Float, default=10_000.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    api_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    ai_net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
