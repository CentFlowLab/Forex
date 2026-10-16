"""Testes Fase 1 — painel transparente (sem OpenAI paga, sem order_send)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.robot.panel_view import (
    build_panel_overlay,
    build_why_not_trading,
    resolve_execution_state,
    resolve_mt5_state,
    resolve_openai_state,
    resolve_robot_state,
)


def test_robot_states():
    assert resolve_robot_state(running=False, thread_alive=False, block_reason="")["code"] == "stopped"
    assert resolve_robot_state(running=True, thread_alive=True, block_reason="")["code"] == "monitoring"
    assert resolve_robot_state(running=True, thread_alive=False, block_reason="")["code"] == "error"


def test_mt5_states_demo_real_stale():
    demo = resolve_mt5_state(
        {"terminal_connected": True, "trade_mode": 0, "trade_mode_label": "DEMO", "account_login": 1},
        {"feed_state": "recente", "update_age_s": 0.5},
    )
    assert demo["code"] == "demo_connected"
    real = resolve_mt5_state(
        {"terminal_connected": True, "trade_mode": 2, "trade_mode_label": "REAL", "account_login": 1},
        {"feed_state": "recente", "update_age_s": 0.5},
    )
    assert real["code"] == "account_not_authorized"
    stale = resolve_mt5_state(
        {"terminal_connected": True, "trade_mode": 0, "trade_mode_label": "DEMO", "account_login": 1},
        {"feed_state": "atrasado", "update_age_s": 120},
    )
    assert stale["code"] == "stale_data"
    off = resolve_mt5_state({"terminal_connected": False}, {})
    assert off["code"] == "disconnected"


def test_openai_and_execution_states():
    assert resolve_openai_state(auto={"ai_enabled": False}, openai_real={})["code"] == "disabled"
    assert (
        resolve_openai_state(
            auto={"ai_enabled": True, "openai_key_configured": True, "remaining_today_usd": 0},
            openai_real={},
        )["code"]
        == "budget_exhausted"
    )
    assert resolve_execution_state(demo_auth_active=False)["code"] == "blocked"
    assert resolve_execution_state(emergency=True)["code"] == "paused_safety"


def test_why_not_trading_wait_and_demo_blocked():
    why = build_why_not_trading(
        robot={"code": "monitoring", "label": "A monitorizar"},
        mt5={"code": "demo_connected", "label": "Ligado a DEMO"},
        openai={"code": "decision_received", "label": "Decisão"},
        execution={"code": "blocked", "label": "Bloqueada"},
        auto={},
        openai_real={"latest": {"available": True, "decision": "HOLD"}},
        quote={"feed_state": "recente"},
    )
    assert why["code"] == "demo_execution_disabled"

    why2 = build_why_not_trading(
        robot={"code": "monitoring", "label": "A monitorizar"},
        mt5={"code": "demo_connected", "label": "Ligado a DEMO"},
        openai={"code": "decision_received", "label": "Decisão"},
        execution={"code": "active", "label": "Ativa"},
        auto={},
        openai_real={"latest": {"available": True, "decision": "HOLD"}},
        quote={"feed_state": "recente"},
    )
    assert why2["code"] == "openai_wait"


def test_panel_overlay_separates_mt5_and_simulated():
    overlay = build_panel_overlay(
        running=True,
        thread_alive=True,
        block_reason="",
        mt5_snap={
            "terminal_connected": True,
            "trade_mode": 0,
            "trade_mode_label": "DEMO",
            "account_login": 5057430798,
            "account_server": "MetaQuotes-Demo",
            "balance": 12345.67,
            "equity": 12000.0,
            "margin": 100.0,
            "margin_free": 11900.0,
            "profit": -345.67,
            "currency": "USD",
            "trade_allowed": True,
        },
        quote={"feed_state": "recente", "update_age_s": 0.2, "received_at": datetime.now(timezone.utc).isoformat()},
        auto={
            "ai_enabled": True,
            "openai_key_configured": True,
            "confirmed": True,
            "scheduling_active": True,
            "remaining_today_usd": 0.09,
            "next_analysis_at": None,
        },
        openai_real={
            "available": True,
            "latest": {
                "available": True,
                "decision": "HOLD",
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        account_simulated={"balance": 10000.0, "equity": 10000.0, "is_simulated": True},
        demo_auth_active=False,
    )
    assert overlay["account_mt5"]["balance"] == 12345.67
    assert overlay["account_simulated"]["balance"] == 10000.0
    assert overlay["account_mt5"]["balance"] != overlay["account_simulated"]["balance"]
    assert overlay["execution_state"]["code"] == "blocked"
    assert overlay["panel_generated_at"]
    assert overlay["why_not_trading"]["code"]


def test_status_endpoint_includes_panel_fields(client):
    res = client.get("/api/robot/status")
    assert res.status_code == 200
    body = res.json()
    assert "robot_state" in body
    assert "mt5_state" in body
    assert "openai_state" in body
    assert "execution_state" in body
    assert "why_not_trading" in body
    assert "account_mt5" in body
    assert "account_simulated" in body or "capital" in body
    assert body["execution_state"]["code"] == "blocked"
    assert body["security"]["live_trading_locked"] is True


def test_start_stop_panel_states():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.post("/api/robot/stop")
    start = c.post("/api/robot/start")
    assert start.status_code == 200
    body = start.json()
    assert body.get("robot_state", {}).get("code") in {"monitoring", "error", "stopped"}
    stop = c.post("/api/robot/stop")
    assert stop.status_code == 200
    assert stop.json().get("running") is False
