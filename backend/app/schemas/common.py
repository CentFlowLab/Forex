from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Recommendation = Literal["BUY", "SELL", "HOLD"]
Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]
Timeframe = Literal["M5", "M15", "H1", "H4"]
Environment = Literal["BACKTEST", "PAPER", "LIVE"]
ProviderName = Literal["xai", "openai", "anthropic"]


class EvidenceItem(BaseModel):
    claim: str
    source: str
    strength: float = Field(ge=0.0, le=1.0)


class AgentBaseOutput(BaseModel):
    bias: Bias = "NEUTRAL"
    recommendation: Recommendation = "HOLD"
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    uncertainty: float = Field(ge=0.0, le=1.0, default=1.0)
    rationale: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    insufficient_information: bool = False
    timestamp_utc: str
