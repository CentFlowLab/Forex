"""Regressão: modelo ecoa envelope JSON Schema em vez do payload plano (#573)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.agents.structured import (
    normalize_agent_payload,
    unwrap_schema_envelope,
    validate_agent_payload,
)
from app.models.robot import AIDecisionLog
from app.robot.ai_schema import AIRadarDecision
from app.robot.openai_panel import NOT_INDICATED, serialize_openai_decision

# Resposta real #573 (abreviada mas com a mesma forma envelope).
ENVELOPE_573 = {
    "description": "Decisão multimeda — a IA escolhe o melhor par ou AGUARDAR.",
    "properties": {
        "decision": "HOLD",
        "symbol": None,
        "justification": (
            "Não há uma assimetria clara entre os pares analisados, "
            "todos apresentam características de mercado lateral e custos de trading semelhantes."
        ),
        "favoring_scenario": "Aumento de volatilidade ou mudança de tendência que possa favorecer um dos pares.",
        "invalidating_scenario": "Continuação do mercado lateral sem sinais claros de tendência.",
        "risks": "Risco de movimentos inesperados no mercado que possam afetar a estrutura atual dos preços.",
        "missing_information": "Informação sobre eventos económicos futuros que possam impactar os pares.",
        "compared_symbols": [
            "GBP/USD",
            "USD/CHF",
            "NZD/USD",
            "AUD/USD",
            "USD/CAD",
            "EUR/USD",
            "USD/JPY",
        ],
        "confidence": 0.7,
        "rejected_notes": "Nenhum par apresenta uma vantagem clara para uma posição de compra ou venda.",
    },
}


def test_unwrap_schema_envelope_extracts_properties() -> None:
    flat = unwrap_schema_envelope(ENVELOPE_573)
    assert flat.get("decision") == "HOLD"
    assert "assimetria" in (flat.get("justification") or "")
    assert flat.get("confidence") == 0.7
    assert len(flat.get("compared_symbols") or []) == 7


def test_validate_agent_payload_recovers_envelope() -> None:
    # Sem unwrap, Pydantic aceitaria defaults vazios (bug histórico).
    bare = AIRadarDecision.model_validate(ENVELOPE_573)
    assert bare.justification == ""
    assert bare.confidence == 0.0

    parsed, err = validate_agent_payload(AIRadarDecision, ENVELOPE_573)
    assert err == ""
    assert parsed is not None
    assert "assimetria" in parsed["justification"]
    assert parsed["confidence"] == 0.7
    assert parsed["compared_symbols"] == ENVELOPE_573["properties"]["compared_symbols"]


def test_normalize_does_not_unwrap_flat_payload() -> None:
    flat = {
        "decision": "BUY",
        "symbol": "EURUSD",
        "justification": "Tendência alinhada.",
        "confidence": 0.8,
    }
    out = normalize_agent_payload(flat)
    assert out["decision"] == "BUY"
    assert out["symbol"] == "EURUSD"


def test_panel_hydrates_empty_orm_from_response_store(tmp_path: Path) -> None:
    store = tmp_path / "ai_responses"
    store.mkdir()
    path = store / "envelope573.json"
    path.write_text(
        json.dumps(
            {
                "model": "gpt-4o-mini",
                "estimated_cost_usd": 0.00143565,
                "text": json.dumps(ENVELOPE_573, ensure_ascii=False),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # ORM como ficou após o bug: HOLD + campos vazios.
    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="",
        favoring_scenario="",
        invalidating_scenario="",
        risks="",
        confidence=0.0,
        estimated_cost_usd=0.00143565,
        context_json=json.dumps({"symbol": None, "persisted_path": str(path)}),
        proposal_json=json.dumps(
            {
                "decision": "HOLD",
                "symbol": None,
                "justification": "",
                "confidence": 0.0,
                "compared_symbols": [],
            }
        ),
        created_at=datetime(2026, 10, 15, 21, 31, 20, tzinfo=timezone.utc),
    )
    out = serialize_openai_decision(log)
    assert out["decision"] == "HOLD"
    assert out["decision_pt"] == "AGUARDAR"
    assert "assimetria" in out["justification"]
    assert out["justification"] != NOT_INDICATED
    assert out["confidence"] == "70%"
    assert "inesperados" in out["risks"]
    assert out["hydrated_from_response_store"] is True
    assert len(out.get("compared_symbols") or []) == 7
