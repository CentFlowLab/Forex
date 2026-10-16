"""Esquema estruturado da proposta de decisão IA (único modelo, sem debate)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CandidateComparison(BaseModel):
    """Comparação explícita de um candidato do radar / motor direcional."""

    symbol: str = ""
    side_considered: Literal["BUY", "SELL", "BOTH", ""] = ""
    arguments_for: str = ""
    arguments_against: str = ""
    trading_costs: str = ""
    missing_for_entry: str = ""
    quantitative_probs_note: str = ""  # citar p_up/p_down/EV do motor — não inventar
    verdict: Literal["accept", "reject"] = "reject"
    verdict_reason: str = ""


class AITradeProposal(BaseModel):
    """Proposta mono-par (compatibilidade) ou campos partilhados."""

    decision: Literal["BUY", "SELL", "HOLD"] = "HOLD"
    symbol: str | None = None
    justification: str = ""
    favoring_scenario: str = ""
    invalidating_scenario: str = ""
    why_preferred: str = ""
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    time_horizon: str = ""
    risks: str = ""
    missing_information: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rejected_notes: str = ""
    entry_conditions: str = ""
    # Direção mais provável mesmo em HOLD (do motor quantitativo — não inventar).
    most_likely_direction: Literal["up", "down", "flat", ""] = ""
    cited_p_up: float | None = None
    cited_p_down: float | None = None
    cited_expected_net_usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AIRadarDecision(AITradeProposal):
    """Decisão multimeda — a IA escolhe o melhor par ou AGUARDAR."""

    compared_symbols: list[str] = Field(default_factory=list)
    top_candidates_comparison: list[CandidateComparison] = Field(default_factory=list)


DECISION_PT = {"BUY": "COMPRAR", "SELL": "VENDER", "HOLD": "AGUARDAR"}
