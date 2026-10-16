"""Botões do painel Robô — HTTP (INICIAR/PARAR/Preparar/Confirmar/Desativar).

Usa a app real (SessionLocal), como o UI. Sem chamadas OpenAI pagas.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.robot.openai_autonomous import (
    FLAG_ARMED,
    FLAG_CONFIRMED,
    FLAG_REQUIRES_RECONFIRM,
    _get_flag,
    _set_flag,
)

client = TestClient(app)


def test_robot_buttons_http_flow() -> None:
    # Garantir estado limpo no arranque (BD partilhada do processo).
    client.post("/api/robot/stop")

    start = client.post("/api/robot/start")
    assert start.status_code == 200, start.text
    body = start.json()
    assert body.get("running") is True, body.get("block_reason")
    assert body.get("security", {}).get("live_trading_locked") is True
    assert body.get("security", {}).get("environment") != "LIVE"

    status = client.get("/api/robot/status")
    assert status.status_code == 200
    assert status.json().get("running") is True

    stop = client.post("/api/robot/stop")
    assert stop.status_code == 200
    assert stop.json().get("running") is False

    db = SessionLocal()
    prev = {
        FLAG_ARMED: _get_flag(db, FLAG_ARMED),
        FLAG_CONFIRMED: _get_flag(db, FLAG_CONFIRMED),
        FLAG_REQUIRES_RECONFIRM: _get_flag(db, FLAG_REQUIRES_RECONFIRM),
    }
    try:
        _set_flag(db, FLAG_CONFIRMED, "false")
        _set_flag(db, FLAG_ARMED, "false")
        _set_flag(db, FLAG_REQUIRES_RECONFIRM, "false")

        arm = client.post("/api/robot/ai/autonomous/arm")
        assert arm.status_code == 200, arm.text
        armed = arm.json()
        assert armed.get("armed") is True
        assert armed.get("confirmed") is False
        assert armed.get("paid_calls_blocked_until_confirm") is True

        # Sem confirmed=true → erro de autorização (não inicia chamadas pagas)
        bad = client.post(
            "/api/robot/ai/autonomous/activate",
            json={"confirmed": False},
        )
        assert bad.status_code in {400, 403}, bad.text

        good = client.post(
            "/api/robot/ai/autonomous/activate",
            json={"confirmed": True},
        )
        # 200 se AI/chave OK; 403 se pré-requisitos falham — nunca chamada paga aqui
        assert good.status_code in {200, 403}, good.text
        if good.status_code == 200:
            activated = good.json()
            assert activated.get("confirmed") is True
            assert activated.get("execution_enabled") is False

        off = client.post("/api/robot/ai/autonomous/deactivate")
        assert off.status_code == 200, off.text
        deactivated = off.json()
        assert deactivated.get("confirmed") is False or deactivated.get("armed") is False
    finally:
        for k, v in prev.items():
            _set_flag(db, k, v or "false")
        db.close()
        client.post("/api/robot/stop")
