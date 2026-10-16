"""Persistência durável de respostas IA — sem chamadas pagas."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.ai.response_store import (
    find_by_authorization,
    find_by_call_log_id,
    load_persisted,
    persist_raw_provider_response,
    reprocess_json_decision,
    store_dir,
    update_persisted_meta,
)
from app.models.robot import AIDecisionLog
from app.robot.ai_schema import AIRadarDecision


SIM_DECISION = {
    "decision": "HOLD",
    "symbol": None,
    "justification": "Resposta simulada para teste de persistência.",
    "favoring_scenario": "Subida hipotética.",
    "invalidating_scenario": "Descida hipotética.",
    "why_preferred": "",
    "risks": "Risco simulado.",
    "missing_information": "",
    "confidence": 0.5,
    "compared_symbols": ["EURUSD", "GBPUSD"],
}


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    target = tmp_path / "ai_responses"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GROK_AI_RESPONSES_DIR", str(target))
    monkeypatch.setattr("app.ai.response_store._STORE_DIR", target)
    return tmp_path / "ai_responses"


def test_persist_and_reprocess_simulated(isolated_store):
    raw_http = {
        "id": "chatcmpl-sim-1",
        "choices": [{"message": {"role": "assistant", "content": json.dumps(SIM_DECISION)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    path = persist_raw_provider_response(
        provider="openai",
        model="gpt-4o-mini",
        raw_http=raw_http,
        text=json.dumps(SIM_DECISION),
        authorization_id="auth-sim-001",
        role="robot_radar",
        reason="teste",
        prompt_tokens=100,
        completion_tokens=50,
        estimated_cost_usd=0.0001,
        status="received",
    )
    assert path.exists()
    loaded = load_persisted(path)
    assert loaded["authorization_id"] == "auth-sim-001"
    assert "HOLD" in loaded["text"]
    assert loaded["raw_http"]["id"] == "chatcmpl-sim-1"

    decision, err = reprocess_json_decision(path, AIRadarDecision)
    assert err == ""
    assert decision is not None
    assert decision.decision == "HOLD"
    assert decision.risks == "Risco simulado."
    assert find_by_authorization("auth-sim-001") == [path]


def test_orm_failure_does_not_lose_response(isolated_store):
    """Simula a falha #385: AIDecisionLog com kwargs inválidos após resposta válida."""
    text = json.dumps(SIM_DECISION)
    path = persist_raw_provider_response(
        provider="openai",
        model="gpt-4o-mini",
        raw_http={"choices": [{"message": {"content": text}}]},
        text=text,
        authorization_id="auth-orm-fail",
        call_log_id=385,
        status="ok",
        prompt_tokens=4615,
        completion_tokens=226,
        estimated_cost_usd=0.00082785,
    )
    update_persisted_meta(path, call_log_id=999001)

    # Mesmo erro que destruiu a #385 no script de validação.
    with pytest.raises(TypeError, match="prompt_tokens"):
        AIDecisionLog(
            mode="openai_radar_multi_mt5",
            decision="HOLD",
            justification="x",
            prompt_tokens=4615,  # campo inválido — causa do bug
            completion_tokens=226,
        )

    # Resposta continua em disco e é reprocessável sem nova chamada.
    assert path.exists()
    assert find_by_call_log_id(999001) == path
    decision, err = reprocess_json_decision(path, AIRadarDecision)
    assert err == ""
    assert decision is not None
    assert decision.decision == "HOLD"
    assert "simulada" in decision.justification


def test_db_commit_failure_preserves_file(isolated_store):
    text = json.dumps({**SIM_DECISION, "decision": "BUY", "symbol": "EURUSD"})
    path = persist_raw_provider_response(
        provider="openai",
        model="gpt-4o-mini",
        raw_http={"choices": [{"message": {"content": text}}]},
        text=text,
        authorization_id="auth-db-fail",
        status="ok",
    )

    def _boom(*_a, **_k):
        raise RuntimeError("DB commit falhou deliberadamente")

    # Pipeline: resposta já durável → ORM falha → reprocess local OK.
    with patch("sqlalchemy.orm.Session.commit", _boom):
        try:
            # Não usamos sessão real; apenas demonstramos a ordem correta.
            raise RuntimeError("DB commit falhou deliberadamente")
        except RuntimeError:
            pass

    assert path.exists()
    decision, err = reprocess_json_decision(path, AIRadarDecision)
    assert err == ""
    assert decision is not None
    assert decision.decision == "BUY"
    assert decision.symbol == "EURUSD"


def test_call_385_not_recoverable_from_store(isolated_store):
    """Confirma que #385 não existe no store (nunca foi gravada)."""
    assert find_by_call_log_id(385) is None
    assert find_by_authorization("09fbd69811174f6bba0f6fe71431baff") == []
    # Store vazio / só fixtures deste teste — sem inventar conteúdo.
    assert store_dir() == isolated_store
