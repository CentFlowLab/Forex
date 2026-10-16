"""Previsões direcionais quantitativas e avaliação pós-horizonte."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DirectionalPrediction(Base):
    """Uma previsão por (símbolo, lado, horizonte) com evidência e resolução futura."""

    __tablename__ = "directional_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8), default="")  # BUY|SELL|"" (só direção)
    horizon_min: Mapped[int] = mapped_column(Integer, default=30)
    most_likely: Mapped[str] = mapped_column(String(8), default="flat")  # up|down|flat
    p_up: Mapped[float] = mapped_column(Float, default=0.0)
    p_down: Mapped[float] = mapped_column(Float, default=0.0)
    p_flat: Mapped[float] = mapped_column(Float, default=0.0)
    expected_move_pips: Mapped[float] = mapped_column(Float, default=0.0)
    expected_volatility_pips: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainty: Mapped[float] = mapped_column(Float, default=0.0)
    calib_samples: Mapped[int] = mapped_column(Integer, default=0)
    calib_method: Mapped[str] = mapped_column(String(48), default="")
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    expected_net_usd: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_quality: Mapped[float] = mapped_column(Float, default=0.0)
    mid_at_prediction: Mapped[float] = mapped_column(Float, default=0.0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    forecast_json: Mapped[str] = mapped_column(Text, default="{}")
    scan_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    decision_id: Mapped[int] = mapped_column(Integer, default=0)  # AIDecisionLog opcional
    openai_decision: Mapped[str] = mapped_column(String(16), default="")
    executed_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str] = mapped_column(Text, default="")
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolve_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    mid_at_resolution: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_move_pips: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_direction: Mapped[str] = mapped_column(String(8), default="")  # up|down|flat
    direction_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    move_error_pips: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
