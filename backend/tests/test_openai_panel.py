"""Apresentação de decisões OpenAI reais — só leitura de registos (sem API paga)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.models.robot import AIDecisionLog
from app.robot.openai_panel import (
    NOT_INDICATED,
    authorization_label,
    empty_openai_decision,
    openai_panel_payload,
    serialize_openai_decision,
)


def test_serialize_uses_persisted_fields_not_radar(db, tmp_path, monkeypatch):
    store = tmp_path / "ai_responses"
    store.mkdir()
    path = store / "sample.json"
    path.write_text(
        json.dumps(
            {
                "model": "gpt-4o-mini",
                "text": '{"decision":"HOLD","symbol":null,"confidence":0.6}',
            }
        ),
        encoding="utf-8",
    )
    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="Motivo real da OpenAI: aguardar por atraso de dados.",
        favoring_scenario="Recuperação acima da MM20.",
        invalidating_scenario="Quebra abaixo do mínimo.",
        risks="Dados atrasados.",
        confidence=0.6,
        estimated_cost_usd=0.000831,
        context_json=json.dumps({"symbol": None, "persisted_path": str(path)}),
        proposal_json=json.dumps(
            {
                "decision": "HOLD",
                "symbol": None,
                "justification": "Motivo real da OpenAI: aguardar por atraso de dados.",
                "confidence": 0.6,
                "risks": "Dados atrasados.",
                "invalidating_scenario": "Quebra abaixo do mínimo.",
            }
        ),
        created_at=datetime(2026, 10, 15, 13, 39, 43, tzinfo=timezone.utc),
    )
    db.add(log)
    db.commit()

    payload = openai_panel_payload(db, history_limit=5)
    assert payload["available"] is True
    latest = payload["latest"]
    assert latest["decision"] == "HOLD"
    assert latest["decision_pt"] == "AGUARDAR"
    assert "atraso" in latest["justification"].lower()
    assert latest["symbol"] == NOT_INDICATED
    assert latest["confidence"] == "60%"
    assert latest["model"] == "gpt-4o-mini"
    assert latest["risks"] == "Dados atrasados."
    assert latest["invalidating_scenario"] == "Quebra abaixo do mínimo."
    assert latest["executed"] is False
    assert len(payload["recent"]) == 1


def test_empty_when_only_simulated(db):
    db.add(
        AIDecisionLog(
            mode="simulated_radar",
            decision="HOLD",
            justification="Triagem local AUD/USD",
            confidence=0.6,
        )
    )
    db.commit()
    payload = openai_panel_payload(db)
    assert payload["available"] is False
    assert "não existe" in payload["latest"]["justification"].lower() or payload["message"]


def test_confidence_zero_is_not_indicated():
    log = AIDecisionLog(
        mode="openai",
        decision="HOLD",
        justification="Sem confiança",
        confidence=0.0,
        proposal_json=json.dumps({"decision": "HOLD", "confidence": 0.0}),
        context_json="{}",
    )
    out = serialize_openai_decision(log)
    assert out["confidence"] == NOT_INDICATED


def test_authorization_label_states():
    assert (
        authorization_label(
            {
                "active": True,
                "confirmed": True,
                "robot_running": True,
                "scheduling_active": True,
                "suspend_reason": "",
            }
        )
        == "OpenAI autónoma ativa"
    )
    assert (
        authorization_label({"active": False, "confirmed": False})
        == "A aguardar autorização"
    )
    assert "Desativada" in authorization_label(
        {
            "active": False,
            "confirmed": False,
            "suspend_reason": "Desativado pelo utilizador.",
            "robot_running": False,
            "scheduling_active": False,
        }
    )
    assert empty_openai_decision()["available"] is False
