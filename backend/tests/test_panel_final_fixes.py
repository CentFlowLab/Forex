"""Regressão: histórico contaminado, recusa antiga, contexto, Dukascopy non-block."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from app.models.robot import AIDecisionLog
from app.robot.execution_state import build_execution_panel_state, is_stale_rejection
from app.robot.openai_panel import (
    is_real_paid_openai_decision,
    is_synthetic_or_test_decision,
    list_openai_decisions,
    openai_panel_payload,
)
from app.robot.panel_view import build_panel_overlay, resolve_execution_state


def test_contaminated_history_excludes_pytest_stubs(db, tmp_path):
    real_file = tmp_path / "real_paid.json"
    real_file.write_text(
        json.dumps(
            {
                "model": "gpt-4o-mini",
                "estimated_cost_usd": 0.0009,
                "status": "ok",
                "text": '{"decision":"HOLD"}',
            }
        ),
        encoding="utf-8",
    )
    stubs = [
        AIDecisionLog(
            mode="openai_radar_autonomous",
            decision="BUY",
            justification="Assimetria teste BUY.",
            estimated_cost_usd=0.0009,
            context_json=json.dumps(
                {"authorization_id": "test-auth-hold-path", "synthetic_test": True}
            ),
            proposal_json='{"decision":"BUY","symbol":"EURUSD"}',
        ),
        AIDecisionLog(
            mode="openai_radar_autonomous",
            decision="SELL",
            justification="Assimetria teste SELL.",
            estimated_cost_usd=0.0009,
            context_json=json.dumps({"authorization_id": "test-auth-hold-path"}),
            proposal_json='{"decision":"SELL","symbol":"EURUSD"}',
        ),
        AIDecisionLog(
            mode="openai_radar_autonomous",
            decision="HOLD",
            justification="aguardar",
            estimated_cost_usd=0.001,
            context_json="{}",
            proposal_json='{"decision":"HOLD"}',
        ),
        AIDecisionLog(
            mode="openai_radar_autonomous",
            decision="HOLD",
            justification="Justificação única de teste auditoria.",
            estimated_cost_usd=0.0008,
            context_json=json.dumps({"authorization_id": "audit-xyz"}),
            proposal_json='{"decision":"HOLD"}',
        ),
    ]
    real = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="Análise genuína com feed MT5 recente sem edge claro.",
        estimated_cost_usd=0.0009,
        context_json=json.dumps(
            {
                "persisted_path": str(real_file),
                "authorization_id": "a64c48cb0ae142118825961d616e4897",
            }
        ),
        proposal_json='{"decision":"HOLD"}',
        created_at=datetime.now(timezone.utc),
    )
    db.add_all(stubs + [real])
    db.commit()

    for s in stubs:
        assert is_synthetic_or_test_decision(s) is True
        assert is_real_paid_openai_decision(s) is False
    assert is_real_paid_openai_decision(real) is True

    rows = list_openai_decisions(db, limit=10)
    assert [r.id for r in rows] == [real.id]
    panel = openai_panel_payload(db, history_limit=5)
    assert panel["latest"]["id"] == real.id
    assert panel["latest"]["is_real_paid_openai"] is True
    assert all(h["decision"] != "BUY" for h in panel["recent"])
    assert all(h["decision"] != "SELL" for h in panel["recent"])


def test_stale_login_mismatch_not_current_execution_state():
    attempt = {
        "status": "rejected",
        "reason": "Login/servidor diferem da autorização DEMO.",
        "at": "2026-10-15T20:45:25.303792+00:00",
        "order_send_called": False,
    }
    assert (
        is_stale_rejection(
            attempt,
            demo_auth_active=True,
            auth_login="5057430798",
            auth_server="MetaQuotes-Demo",
            live_login=5057430798,
            live_server="MetaQuotes-Demo",
        )
        is True
    )
    st = build_execution_panel_state(
        demo_auth_active=True,
        last_attempt=attempt,
        auth_login="5057430798",
        auth_server="MetaQuotes-Demo",
        live_login=5057430798,
        live_server="MetaQuotes-Demo",
    )
    assert st["code"] == "active"
    assert st["label"] == "Ativa / pronta"
    assert st["last_rejection"] is not None
    assert st["last_rejection"]["stale"] is True
    assert st["last_rejection"]["reason"].startswith("Login/servidor")


def test_fresh_rejection_still_visible_when_auth_active():
    attempt = {
        "status": "rejected",
        "reason": "Stop-loss ausente — nenhuma ordem.",
        "at": datetime.now(timezone.utc).isoformat(),
        "decision_id": 99,
        "order_send_called": False,
    }
    st = resolve_execution_state(
        demo_auth_active=True,
        last_attempt=attempt,
        auth_login="1",
        auth_server="Demo",
        live_login="1",
        live_server="Demo",
    )
    assert st["code"] == "active_with_rejection"
    assert "Stop-loss" in st["label"]
    assert st["last_rejection"]["stale"] is False


def test_overlay_separates_wait_and_stale_rejection():
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
            "balance": 100000,
            "equity": 100000,
            "currency": "EUR",
        },
        quote={"feed_state": "recente", "update_age_s": 0.2, "source": "mt5"},
        auto={
            "ai_enabled": True,
            "openai_key_configured": True,
            "confirmed": True,
            "scheduling_active": True,
            "remaining_today_usd": 0.05,
        },
        openai_real={
            "available": True,
            "latest": {
                "available": True,
                "decision": "HOLD",
                "id": 490,
                "analyzed_at": "2026-10-15T21:01:13+00:00",
                "is_real_paid_openai": True,
            },
        },
        account_simulated={"balance": 10000, "is_simulated": True},
        demo_auth_active=True,
        last_execution_attempt={
            "status": "rejected",
            "reason": "Login/servidor diferem da autorização DEMO.",
            "at": "2026-10-15T20:45:25.303792+00:00",
        },
        auth_login="5057430798",
        auth_server="MetaQuotes-Demo",
    )
    assert overlay["execution_state"]["code"] == "active"
    assert overlay["last_rejection"]["stale"] is True
    assert overlay["why_not_trading"]["code"] == "openai_wait"
    assert "obsoleta" in overlay["why_not_trading"]["message"].lower() or "antiga" in (
        overlay["why_not_trading"]["message"].lower()
    )


def test_context_summary_marks_legacy_payload():
    """Contexto antigo (M15=30, sem local_regime_note) é detetável."""
    legacy = {
        "pairs_for_comparison": [
            {
                "candles_counts": {"M15": 30},
                "structure": {"sma50": None},
                "volatility": {"regime": "mercado_lateral"},
                "quote": {"tick_time_lag_s": -10800, "clock_anomaly": True},
            }
        ]
    }
    p0 = legacy["pairs_for_comparison"][0]
    assert (p0.get("candles_counts") or {}).get("M15", 0) < 50
    assert (p0.get("structure") or {}).get("sma50") is None
    assert "local_regime_note" not in (p0.get("volatility") or {})


def test_corrected_context_shape_from_radar_summary():
    from app.robot.radar import RadarCandidate

    m15 = [
        {
            "o": 1.1,
            "h": 1.11,
            "l": 1.09,
            "c": 1.1 + i * 0.00001,
            "ts": f"2026-10-15T10:{(i % 4) * 15:02d}:00+00:00",
        }
        for i in range(60)
    ]
    closes = [x["c"] for x in m15]
    c = RadarCandidate(
        symbol="EURUSD",
        display="EUR/USD",
        score=1.0,
        reasons=[],
        snapshot={
            "quote": {
                "mid": closes[-1],
                "bid": closes[-1],
                "ask": closes[-1],
                "update_age_s": 0.5,
                "feed_state": "recente",
                "source": "mt5",
                "clock_anomaly": True,
                "tick_time_lag_s": -10800,
                "claims_realtime": False,
                "block_entries": False,
                "is_synthetic": False,
            },
            "structure": {
                "sma20": 1.1,
                "sma50": sum(closes[-50:]) / 50,
                "above_sma20": True,
                "above_sma50": True,
            },
            "volatility": {
                "atr14_m15": 0.0009,
                "local_regime_hint": "mercado_lateral",
                "local_regime_hint_pt": "Mercado lateral",
                "trend_slope": 0.0,
                "atr_ratio": 1.0,
                "regime": "mercado_lateral",
            },
            "trading_costs": {"total": 1.0, "spread_pips": 0.2},
            "candles_closed": {"M15": m15},
            "candles_counts": {"M15": 60, "H1": 40, "H4": 20},
            "data_quality": {"executable": True},
            "candle_source": "mt5",
        },
    )
    s = c.summary()
    assert s["candles_counts"]["M15"] == 60
    assert s["structure"]["sma50"] is not None
    assert "tick_time_lag_s" not in s["quote"]
    assert "local_regime_note" in s["volatility"]
    assert "recent_m15_ohlc" in s


def test_dukascopy_timeout_does_not_block_panel_status():
    """Timeout Dukascopy não impede estado MT5 / execução no overlay."""
    mt5_snap = {
        "terminal_connected": True,
        "initialized": True,
        "trade_mode": 0,
        "trade_mode_label": "DEMO",
        "account_login": 5057430798,
        "account_server": "MetaQuotes-Demo",
        "balance": 100000.0,
        "equity": 100000.0,
        "currency": "EUR",
    }
    overlay = build_panel_overlay(
        running=True,
        thread_alive=True,
        block_reason="",  # service limpa TCP quando MT5 está recente
        mt5_snap=mt5_snap,
        quote={"source": "mt5", "feed_state": "recente", "update_age_s": 0.1},
        auto={
            "ai_enabled": True,
            "openai_key_configured": True,
            "remaining_today_usd": 0.1,
            "confirmed": True,
            "scheduling_active": True,
        },
        openai_real={
            "available": True,
            "latest": {"available": True, "decision": "HOLD", "id": 1},
        },
        account_simulated={"balance": 1},
        demo_auth_active=True,
        auth_login="5057430798",
        auth_server="MetaQuotes-Demo",
    )
    assert overlay["mt5_state"]["code"] == "demo_connected"
    assert overlay["execution_state"]["code"] == "active"
    assert "complementar" in overlay["dukascopy_note"].lower()
    assert overlay["dukascopy_note"]
