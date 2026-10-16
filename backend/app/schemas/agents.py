from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import AgentBaseOutput, Bias, ProviderName, Recommendation


class TechnicalOutput(AgentBaseOutput):
    rsi_14: float | None = None
    macd_histogram: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None
    support: float | None = None
    resistance: float | None = None
    atr_14: float | None = None
    volatility_regime: Literal["low", "normal", "high", "unknown"] = "unknown"


class TrendOutput(AgentBaseOutput):
    m5_trend: Bias = "NEUTRAL"
    m15_trend: Bias = "NEUTRAL"
    h1_trend: Bias = "NEUTRAL"
    h4_trend: Bias = "NEUTRAL"
    structure: Literal["uptrend", "downtrend", "range", "unclear"] = "unclear"
    momentum: Literal["accelerating", "stable", "fading", "unclear"] = "unclear"
    alignment_score: float = Field(ge=0.0, le=1.0, default=0.0)


class FundamentalOutput(AgentBaseOutput):
    events_considered: list[str] = Field(default_factory=list)
    news_available: bool = False
    calendar_available: bool = False
    disclaimer: str = (
        "Não foram fabricados dados de notícias nem de calendário. "
        "Se as fontes estiverem indisponíveis, este relatório deve marcar insufficient_information."
    )


class StrategyOutput(AgentBaseOutput):
    setup_valid: bool = False
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    suggested_lots: float | None = None
    invalidation: str = ""


class RiskChallengerOutput(AgentBaseOutput):
    challenge_accepted: bool = False
    contradictions: list[str] = Field(default_factory=list)
    overconfidence_flags: list[str] = Field(default_factory=list)
    execution_risks: list[str] = Field(default_factory=list)
    veto: bool = False
    veto_reason: str = ""


class CoordinatorOutput(AgentBaseOutput):
    final_recommendation: Recommendation = "HOLD"
    dissenting_views: list[str] = Field(default_factory=list)
    agreement_score: float = Field(ge=0.0, le=1.0, default=0.0)
    follow_challenger_veto: bool = False


class DebateResearcherOutput(AgentBaseOutput):
    """Saída estruturada dos papéis Bull/Bear (debate TradingAgents)."""

    argument: str = ""
    counterpoints_addressed: list[str] = Field(default_factory=list)
    key_evidence: list[str] = Field(default_factory=list)
    round_index: int = 0


class TradingDecisionPackage(BaseModel):
    """Pacote final obrigatório do pipeline com debate."""

    instrument: str
    market_data_timestamp: str
    timeframe: str
    direction: Recommendation = "HOLD"
    confidence_score: float = Field(ge=0.0, le=1.0, default=0.0)
    bull_arguments: list[str] = Field(default_factory=list)
    bear_arguments: list[str] = Field(default_factory=list)
    key_evidence: list[str] = Field(default_factory=list)
    risk_objections: list[str] = Field(default_factory=list)
    entry_proposal: float | None = None
    stop_loss_proposal: float | None = None
    take_profit_proposal: float | None = None
    final_explanation: str = ""
    agent_disagreement_summary: str = ""
    risk_veto: bool = False
    risk_veto_reason: str = ""
    snapshot_id: str = ""
    data_freshness: str = "unknown"
    data_source: str = ""
    is_synthetic: bool = True
    models_used: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    debate_rounds: int = 0
    debate_executed: bool = False
    debate_complete: bool = False
    workflow: str = "tradingagents"


class AgentAssignment(BaseModel):
    role: str
    display_name: str
    provider: ProviderName
    model: str
    enabled: bool = True
    temperature: float = 0.2


class AgentReportOut(BaseModel):
    id: int
    decision_id: int
    role: str
    provider: str
    model: str
    status: str
    bias: str
    confidence: float
    uncertainty: float
    summary: str
    payload: dict[str, Any]
    error: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    created_at: datetime


class DecisionOut(BaseModel):
    id: int
    symbol: str
    timeframe: str
    environment: str
    recommendation: str
    coordinator_rationale: str
    risk_approved: bool
    risk_reason: str
    executed: bool
    market_time: datetime
    data_source: str
    close_price: float
    is_simulated: bool
    created_at: datetime
    reports: list[AgentReportOut] = Field(default_factory=list)
