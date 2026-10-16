from __future__ import annotations

from app.agents.structured import normalize_agent_payload, validate_agent_payload
from app.schemas.agents import TrendOutput


def test_unclear_h4_trend_normalized_to_neutral() -> None:
    raw = {
        "bias": "BEARISH",
        "recommendation": "HOLD",
        "confidence": 0.4,
        "uncertainty": 0.6,
        "rationale": "Tendência pouco clara no H4; no M15 predomina pressão vendedora.",
        "evidence": [{"claim": "RSI abaixo de 50", "source": "multi-timeframe", "strength": 0.5}],
        "risks": ["Volatilidade"],
        "insufficient_information": False,
        "timestamp_utc": "2025-10-13T12:00:00+00:00",
        "m5_trend": "BEARISH",
        "m15_trend": "BEARISH",
        "h1_trend": "NEUTRAL",
        "h4_trend": "unclear",  # causa histórica do invalid_json
        "structure": "range",
        "momentum": "unclear",
        "alignment_score": 0.3,
    }
    normalized = normalize_agent_payload(raw)
    assert normalized["h4_trend"] == "NEUTRAL"
    assert normalized["momentum"] == "unclear"  # permitido
    payload, err = validate_agent_payload(TrendOutput, raw)
    assert err == ""
    assert payload is not None
    assert payload["h4_trend"] == "NEUTRAL"


def test_invalid_bias_still_fails() -> None:
    raw = {
        "bias": "SIDEWAYS_CRAZY",
        "recommendation": "HOLD",
        "confidence": 0.1,
        "uncertainty": 0.9,
        "rationale": "Teste",
        "timestamp_utc": "2025-10-13T12:00:00+00:00",
        "m5_trend": "NEUTRAL",
        "m15_trend": "NEUTRAL",
        "h1_trend": "NEUTRAL",
        "h4_trend": "NEUTRAL",
    }
    payload, err = validate_agent_payload(TrendOutput, raw)
    assert payload is None
    assert err


def test_pipeline_exposes_failed_agents_and_round_semantics(db) -> None:
    from tests.ai_test_helpers import enable_ai_offline_no_approval
    from app.agents.tradingagents_pipeline import run_tradingagents_collaboration

    enable_ai_offline_no_approval()
    result = run_tradingagents_collaboration(db, execute=False, debate_rounds=1, source="synthetic")
    assert "diagnostics" in result
    assert "response_rounds_requested" in result["diagnostics"]
    assert "opening_round" in result["diagnostics"]
    assert "rounds_label" in result["diagnostics"]
    debate = result["debate"]
    assert "opening_round" in debate
    assert "response_rounds_completed" in debate
    # Contagem de resposta ≠ abertura
    assert debate["response_rounds_requested"] == 1
    assert "failed_agents" in result["diagnostics"]
