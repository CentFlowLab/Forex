"""Tentativas de execução DEMO — separadas da decisão OpenAI."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DemoExecutionAttempt(Base):
    __tablename__ = "demo_execution_attempts"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_demo_exec_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(96), default="")
    account_login: Mapped[str] = mapped_column(String(32), default="")
    account_server: Mapped[str] = mapped_column(String(128), default="")
    symbol: Mapped[str] = mapped_column(String(16), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    volume_lots: Mapped[float] = mapped_column(Float, default=0.0)
    price_requested: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    order_check_json: Mapped[str] = mapped_column(Text, default="")
    order_send_json: Mapped[str] = mapped_column(Text, default="")
    order_ticket: Mapped[str] = mapped_column(String(64), default="")
    deal_ticket: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
