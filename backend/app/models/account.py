from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="SIMULATED")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    environment: Mapped[str] = mapped_column(String(16), default="PAPER")
    starting_equity: Mapped[float] = mapped_column(Float, default=10_000.0)
    balance: Mapped[float] = mapped_column(Float, default=10_000.0)
    equity: Mapped[float] = mapped_column(Float, default=10_000.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    peak_equity: Mapped[float] = mapped_column(Float, default=10_000.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    daily_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_date: Mapped[str] = mapped_column(String(16), default="")
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
