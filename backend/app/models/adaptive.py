"""Modelos do Motor Adaptativo — experiências, estratégias e decisões simuladas."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdaptiveStrategyState(Base):
    """Estado de ciclo de vida de uma estratégia candidata."""

    __tablename__ = "adaptive_strategy_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="experimental", index=True)
    # experimental | validating | approved | suspended | retired
    regime_affinity_json: Mapped[str] = mapped_column(Text, default="[]")
    complexity_score: Mapped[float] = mapped_column(Float, default=1.0)
    experiment_count: Mapped[int] = mapped_column(Integer, default=0)
    in_sample_score: Mapped[float] = mapped_column(Float, default=0.0)
    validation_score: Mapped[float] = mapped_column(Float, default=0.0)
    out_of_sample_score: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    trading_costs: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    previous_status: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdaptiveExperiment(Base):
    """Experiência de investigação / validação (local por defeito, sem OpenAI)."""

    __tablename__ = "adaptive_experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="experiência")
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_source: Mapped[str] = mapped_column(String(32), default="unknown")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending | queued | running | completed | failed | cancelled
    use_openai: Mapped[bool] = mapped_column(Boolean, default=False)
    openai_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    bars_total: Mapped[int] = mapped_column(Integer, default=0)
    train_bars: Mapped[int] = mapped_column(Integer, default=0)
    validation_bars: Mapped[int] = mapped_column(Integer, default=0)
    test_bars: Mapped[int] = mapped_column(Integer, default=0)
    candidates_tested: Mapped[int] = mapped_column(Integer, default=0)
    candidates_total: Mapped[int] = mapped_column(Integer, default=0)
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    progress_message: Mapped[str] = mapped_column(Text, default="")
    selected_strategy_id: Mapped[int] = mapped_column(Integer, default=0)
    selected_action: Mapped[str] = mapped_column(String(16), default="HOLD")
    current_regime: Mapped[str] = mapped_column(String(32), default="desconhecido")
    results_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdaptiveDecisionLog(Base):
    """Registo de decisão simulada com proteção contra look-ahead no resultado."""

    __tablename__ = "adaptive_decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    strategy_state_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    decision_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_source: Mapped[str] = mapped_column(String(32), default="")
    data_age_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    regime: Mapped[str] = mapped_column(String(32), default="desconhecido")
    strategy_key: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    agent_arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    final_decision: Mapped[str] = mapped_column(String(8), default="HOLD")
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    spread_pips: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_pips: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_costs: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Resultado só disponível após outcome_available_at (anti look-ahead).
    outcome_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_label: Mapped[str] = mapped_column(String(32), default="")
    split_label: Mapped[str] = mapped_column(String(16), default="")  # train|validation|test
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdaptiveChangeLog(Base):
    """Histórico de aprovações, rejeições, suspensões e regressões."""

    __tablename__ = "adaptive_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_state_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    experiment_id: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(32), default="")
    from_status: Mapped[str] = mapped_column(String(32), default="")
    to_status: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdaptiveSchedule(Base):
    """Agendamento de experiências locais (sem OpenAI por defeito)."""

    __tablename__ = "adaptive_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    symbol: Mapped[str] = mapped_column(String(16), default="EURUSD")
    timeframe: Mapped[str] = mapped_column(String(8), default="M15")
    lookback_days: Mapped[int] = mapped_column(Integer, default=30)
    authorize_openai: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
