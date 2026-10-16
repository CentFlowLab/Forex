"""Pydantic schemas."""

from app.schemas.agents import (
    AgentAssignment,
    AgentReportOut,
    CoordinatorOutput,
    FundamentalOutput,
    RiskChallengerOutput,
    StrategyOutput,
    TechnicalOutput,
    TrendOutput,
)
from app.schemas.common import Recommendation

__all__ = [
    "AgentAssignment",
    "AgentReportOut",
    "CoordinatorOutput",
    "FundamentalOutput",
    "RiskChallengerOutput",
    "StrategyOutput",
    "TechnicalOutput",
    "TrendOutput",
    "Recommendation",
]
