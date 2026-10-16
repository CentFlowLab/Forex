"""Painel: Ordem recusada vs WAIT; filtrar stubs; sem order_send real."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.models.robot import AIDecisionLog
from app.robot.openai_panel import (
    is_real_paid_openai_decision,
    is_synthetic_or_test_decision,
    list_openai_decisions,
    openai_panel_payload,
)
from app.robot.panel_view import (
    build_why_not_trading,
    resolve_execution_state,
)


def test_execution_state_shows_reject_reason():
    # Recusa fresca (não mismatch resolvido) com auth ativa → active_with_rejection.
    st = resolve_execution_state(
        demo_auth_active=True,
        last_attempt={
            "status": "rejected",
            "reason": "Stop-loss ausente — nenhuma ordem.",
            "at": "2026-10-15T20:45:25+00:00",
            "decision_id": 10,
            "order_send_called": False,
        },
        auth_login="1",
        auth_server="Demo",
        live_login="1",
        live_server="Demo",
    )
    assert st["code"] == "active_with_rejection"
    assert "Stop-loss" in (st.get("reason") or st["label"])
    assert st["last_rejection"]["order_send_called"] is False


def test_why_distinguishes_wait_from_prior_rejection():
    why = build_why_not_trading(
        robot={"code": "monitoring", "label": "A monitorizar"},
        mt5={"code": "demo_connected", "label": "Ligado a DEMO"},
        openai={"code": "decision_received", "label": "Decisão"},
        execution={"code": "order_rejected", "label": "Ordem recusada"},
        auto={},
        openai_real={
            "latest": {
                "available": True,
                "decision": "HOLD",
                "id": 433,
                "analyzed_at": "2026-10-15T20:36:16+00:00",
            }
        },
        quote={"feed_state": "recente"},
        last_attempt={
            "status": "rejected",
            "reason": "Login/servidor diferem da autorização DEMO.",
            "at": "2026-10-15T20:45:25+00:00",
        },
    )
    assert why["code"] == "openai_wait"
    assert "AGUARDAR" in why["message"]
    assert "prior_order_rejection" in why
    assert "Login/servidor" in why["prior_order_rejection"]
    assert why["decision_id"] == 433


def test_why_prior_reject_when_buy_not_executed():
    why = build_why_not_trading(
        robot={"code": "monitoring", "label": "A monitorizar"},
        mt5={"code": "demo_connected", "label": "Ligado a DEMO"},
        openai={"code": "decision_received", "label": "Decisão"},
        execution={"code": "order_rejected", "label": "Ordem recusada"},
        auto={},
        openai_real={
            "latest": {
                "available": True,
                "decision": "BUY",
                "id": 99,
                "executed": False,
                "risk_approved": True,
                "analyzed_at": "2026-10-15T20:00:00+00:00",
            }
        },
        quote={"feed_state": "recente"},
        last_attempt={
            "status": "rejected",
            "reason": "Stop-loss ausente — nenhuma ordem.",
            "decision_id": 99,
            "at": "2026-10-15T20:00:01+00:00",
            "order_send_called": False,
        },
    )
    assert why["code"] == "prior_order_rejected"
    assert "Stop-loss" in why["message"]


def test_synthetic_decisions_excluded_from_openai_panel(db, tmp_path):
    real_file = tmp_path / "real.json"
    real_file.write_text(
        json.dumps({"model": "gpt-4o-mini", "estimated_cost_usd": 0.001, "text": "{}"}),
        encoding="utf-8",
    )
    real = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="Análise real com feed MT5 recente — mercado sem edge claro.",
        context_json=json.dumps(
            {
                "persisted_path": str(real_file),
                "authorization_id": "abc123real",
            }
        ),
        proposal_json='{"decision":"HOLD"}',
        estimated_cost_usd=0.0009,
        created_at=datetime.now(timezone.utc),
    )
    stub = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="BUY",
        justification="Assimetria teste BUY.",
        context_json=json.dumps(
            {
                "authorization_id": "test-auth-hold-path",
                "synthetic_test": True,
            }
        ),
        proposal_json='{"decision":"BUY","symbol":"EURUSD"}',
        estimated_cost_usd=0.0009,
        created_at=datetime.now(timezone.utc),
    )
    db.add_all([real, stub])
    db.commit()

    assert is_synthetic_or_test_decision(stub) is True
    assert is_real_paid_openai_decision(real) is True
    assert is_real_paid_openai_decision(stub) is False

    rows = list_openai_decisions(db, limit=5)
    assert all(r.id != stub.id for r in rows)
    assert any(r.id == real.id for r in rows)

    payload = openai_panel_payload(db, history_limit=5)
    assert payload["latest"]["id"] == real.id
    assert payload["latest"]["is_real_paid_openai"] is True
    assert all(h.get("decision") != "BUY" or h.get("id") == real.id for h in payload["recent"])


def test_buy_sell_hold_refusal_paths_isolated(db):
    """BUY/SELL/HOLD/recusa — mock executor, BD isolada, sem order_send real."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.brokers.mt5_demo_auth import activate_demo, pause_new_entries
    from app.brokers.mt5_demo_executor import MT5DemoExecutor
    from app.models.robot import RobotState

    st = RobotState(running=True, decision="HOLD")
    db.add(st)
    db.commit()
    activate_demo(db, confirmed=True, login=5057430798, server="MetaQuotes-Demo")
    pause_new_entries(db, paused=False)

    mt5 = MagicMock()
    mt5.ACCOUNT_TRADE_MODE_DEMO = 0
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_TIME_GTC = 0
    mt5.ORDER_FILLING_IOC = 1
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.account_info.return_value = SimpleNamespace(
        login=5057430798,
        server="MetaQuotes-Demo",
        trade_mode=0,
        trade_allowed=True,
        balance=100000.0,
        equity=100000.0,
        margin_free=90000.0,
        currency="EUR",
        profit=0.0,
    )
    mt5.terminal_info.return_value = SimpleNamespace(connected=True)
    mt5.symbol_info.return_value = SimpleNamespace(
        visible=True,
        ask=1.1002,
        bid=1.1000,
        point=0.00001,
        digits=5,
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=10,
        trade_contract_size=100_000,
    )
    mt5.symbol_info_tick.return_value = SimpleNamespace(ask=1.1002, bid=1.1000)
    mt5.order_calc_profit.return_value = -50.0
    mt5.order_calc_margin.return_value = 100.0
    mt5.positions_get.return_value = ()
    mt5.order_check.return_value = SimpleNamespace(retcode=0, comment="ok")
    mt5.order_send.return_value = SimpleNamespace(
        retcode=10009, order=777, deal=888, comment="done"
    )

    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)

    hold = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="wait",
        proposal_json='{"decision":"HOLD"}',
        context_json='{"synthetic_test":true}',
        estimated_cost_usd=0.0,
    )
    db.add(hold)
    db.commit()
    db.refresh(hold)
    out_h = ex.try_execute_decision(db, hold)
    assert out_h["ok"] is False
    assert "HOLD" in out_h["reason"] or "WAIT" in out_h["reason"]
    mt5.order_send.assert_not_called()

    no_sl = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="BUY",
        justification="buy sem sl",
        entry_price=1.1002,
        stop_loss=0.0,
        take_profit=1.11,
        proposal_json='{"decision":"BUY","symbol":"EURUSD","stop_loss":0}',
        context_json='{"synthetic_test":true}',
        estimated_cost_usd=0.0,
    )
    db.add(no_sl)
    db.commit()
    db.refresh(no_sl)
    out_sl = ex.try_execute_decision(db, no_sl)
    assert out_sl["ok"] is False
    assert "Stop-loss" in out_sl["reason"]
    assert out_sl.get("order_send_called") is False
    mt5.order_send.assert_not_called()

    buy = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="BUY",
        justification="buy ok",
        entry_price=1.1002,
        stop_loss=1.095,
        take_profit=1.11,
        proposal_json=json.dumps(
            {
                "decision": "BUY",
                "symbol": "EURUSD",
                "entry_price": 1.1002,
                "stop_loss": 1.095,
                "take_profit": 1.11,
            }
        ),
        context_json='{"synthetic_test":true}',
        estimated_cost_usd=0.0,
    )
    db.add(buy)
    db.commit()
    db.refresh(buy)
    out_b = ex.try_execute_decision(db, buy)
    assert out_b["ok"] is True
    assert mt5.order_send.call_count == 1

    sell = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="SELL",
        justification="sell ok",
        entry_price=1.1000,
        stop_loss=1.105,
        take_profit=1.09,
        proposal_json=json.dumps(
            {
                "decision": "SELL",
                "symbol": "EURUSD",
                "entry_price": 1.1000,
                "stop_loss": 1.105,
                "take_profit": 1.09,
            }
        ),
        context_json='{"synthetic_test":true}',
        estimated_cost_usd=0.0,
    )
    db.add(sell)
    db.commit()
    db.refresh(sell)
    out_s = ex.try_execute_decision(db, sell)
    assert out_s["ok"] is True
    assert mt5.order_send.call_count == 2
