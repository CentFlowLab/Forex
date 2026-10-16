"""Integração HTTP dos endpoints do robô — sem OpenAI paga."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.robot.openai_autonomous import (
    FLAG_REQUIRES_RECONFIRM,
    FLAG_SUSPEND_REASON,
    _set_flag,
)
from app.database import SessionLocal


client = TestClient(app)


def test_robot_status_ok_shape():
    r = client.get("/api/robot/status")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body
    assert "radar" in body
    assert "openai_real" in body
    assert "openai_autonomous" in body
    assert "security" in body
    assert body["security"]["live_trading_locked"] is True
    assert body["openai_autonomous"].get("execution_enabled") is False


def test_robot_start_stop_cycle():
    r = client.post("/api/robot/start")
    assert r.status_code == 200
    body = r.json()
    # Em LIVE bloqueado pode não arrancar; em simulação deve marcar running ou block_reason.
    assert "running" in body
    r2 = client.post("/api/robot/stop")
    assert r2.status_code == 200
    assert r2.json()["running"] is False


def test_autonomous_arm_activate_deactivate_no_paid():
    db = SessionLocal()
    try:
        _set_flag(db, FLAG_REQUIRES_RECONFIRM, "true")
        _set_flag(db, FLAG_SUSPEND_REASON, "teste integração")
    finally:
        db.close()
    arm = client.post("/api/robot/ai/autonomous/arm")
    assert arm.status_code == 200
    # Activate pode falhar se AI_ENABLED=false — aceitável sem gastar.
    act = client.post("/api/robot/ai/autonomous/activate", json={"confirmed": True})
    assert act.status_code in {200, 403}
    deact = client.post("/api/robot/ai/autonomous/deactivate")
    assert deact.status_code == 200
    body = deact.json()
    assert body.get("confirmed") is False or body.get("active") is False


def test_market_candles_endpoint():
    r = client.get("/api/market/candles", params={"timeframe": "M15", "limit": 50})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "candles" in body
    assert "source" in body or "label" in body
    # Painel usa limit>=20 (validação FastAPI).
    assert isinstance(body["candles"], list)
