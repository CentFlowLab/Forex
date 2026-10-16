"""Regressão: contexto OpenAI (defeitos HOLD) + BUY/SELL/HOLD → executor DEMO.

Sem chamadas OpenAI pagas nem order_send real.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.mt5_demo_auth import (
    activate_demo,
    pause_new_entries,
    reset_demo_auth_module_for_tests,
)
from app.brokers.mt5_demo_executor import MT5DemoExecutor, set_mt5_demo_executor_for_tests
from app.database import Base
from app.models.robot import AIDecisionLog, RobotState
from app.robot.ai_engine import RADAR_SYSTEM_PROMPT
from app.robot.ai_schema import AIRadarDecision
from app.robot.openai_autonomous import OpenAIAutonomousScheduler
from app.robot.pair_snapshot import _LIMITS
from app.robot.radar import RadarCandidate, RadarScan


@pytest.fixture()
def db():
    """BD em memória — NUNCA gravar stubs na sqlite de produção."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionTesting = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionTesting()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_demo():
    reset_demo_auth_module_for_tests()
    set_mt5_demo_executor_for_tests(None)
    yield
    reset_demo_auth_module_for_tests()
    set_mt5_demo_executor_for_tests(None)


def _mt5_mock(*, trade_mode: int = 0, login: int = 111, server: str = "Demo-Server"):
    mt5 = MagicMock()
    mt5.ACCOUNT_TRADE_MODE_DEMO = 0
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_TIME_GTC = 0
    mt5.ORDER_FILLING_IOC = 1
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.account_info.return_value = SimpleNamespace(
        login=login,
        server=server,
        trade_mode=trade_mode,
        trade_allowed=True,
        balance=10_000.0,
        equity=10_000.0,
        margin_free=9_000.0,
        currency="USD",
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
    mt5.order_send.return_value = SimpleNamespace(
        retcode=10009, order=555, deal=666, comment="done"
    )
    mt5.positions_get.return_value = ()
    return mt5


def _candidate(*, clock_anomaly: bool = True, n_m15: int = 60) -> RadarCandidate:
    m15 = [
        {
            "o": 1.1 + i * 0.00001,
            "h": 1.1005 + i * 0.00001,
            "l": 1.0995 + i * 0.00001,
            "c": 1.1 + i * 0.00001,
            "ts": f"2026-10-15T{10 + i // 4:02d}:{(i % 4) * 15:02d}:00+00:00",
        }
        for i in range(n_m15)
    ]
    closes = [x["c"] for x in m15]
    snap = {
        "quote": {
            "mid": closes[-1],
            "bid": closes[-1] - 0.00001,
            "ask": closes[-1] + 0.00001,
            "lag_seconds": 0.0,
            "tick_time_lag_s": -10800.0,
            "tick_msc_lag_s": -10800.0,
            "update_age_s": 0.5,
            "spread_pips": 0.2,
            "data_class": "mt5_atualizacao_recente_relogio_inconsistente",
            "claims_realtime": False,
            "block_entries": False,
            "is_synthetic": False,
            "channel": "mt5",
            "source": "mt5",
            "feed_state": "recente",
            "clock_anomaly": clock_anomaly,
            "ts": "2026-10-15T20:00:00+00:00",
        },
        "candle_source": "mt5",
        "structure": {
            "last_close": closes[-1],
            "swing_high_20": max(x["h"] for x in m15[-20:]),
            "swing_low_20": min(x["l"] for x in m15[-20:]),
            "sma20": sum(closes[-20:]) / 20,
            "sma50": sum(closes[-50:]) / 50 if len(closes) >= 50 else None,
            "above_sma20": True,
            "above_sma50": True if len(closes) >= 50 else False,
        },
        "volatility": {
            "atr14_m15": 0.0009,
            "local_regime_hint": "mercado_lateral",
            "local_regime_hint_pt": "Mercado lateral",
            "trend_slope": 0.0001,
            "atr_ratio": 1.0,
            "adx_proxy": 0.5,
            "bars_used": n_m15,
            "regime": "mercado_lateral",
            "regime_label_pt": "Mercado lateral",
        },
        "trading_costs": {"total": 1.4, "spread_pips": 0.2},
        "candles_closed": {"M15": m15, "H1": m15[-24:], "H4": m15[-16:]},
        "candles_counts": {"M15": n_m15, "H1": 24, "H4": 16},
        "data_quality": {
            "candles_ok": True,
            "quote_ok": True,
            "data_ok": True,
            "executable": True,
            "costs_modeled": True,
        },
    }
    return RadarCandidate(
        symbol="EURUSD",
        display="EUR/USD",
        score=1.0,
        reasons=["teste"],
        snapshot=snap,
    )


def test_m15_limit_allows_sma50():
    assert _LIMITS["M15"] >= 50


def test_ai_summary_omits_tick_lag_when_clock_anomaly():
    c = _candidate(clock_anomaly=True)
    s = c.summary()
    assert "tick_time_lag_s" not in s["quote"]
    assert "tick_msc_lag_s" not in s["quote"]
    assert s["quote"]["update_age_s"] == 0.5
    assert s["quote"]["feed_state"] == "recente"
    assert "lag_note" in s["quote"]
    assert s["structure"]["sma50"] is not None
    assert "local_regime_note" in s["volatility"]
    assert s["volatility"]["local_regime_hint"] == "mercado_lateral"
    assert len(s["recent_m15_ohlc"]) == 12
    assert "h" in s["recent_m15_ohlc"][0]


def test_ai_summary_keeps_tick_lag_without_clock_anomaly():
    c = _candidate(clock_anomaly=False)
    s = c.summary()
    assert s["quote"]["tick_time_lag_s"] == -10800.0


def test_radar_prompt_does_not_prefer_hold_by_default():
    low = RADAR_SYSTEM_PROMPT.lower()
    assert "preferido por defeito" in low or "não é preferido por defeito" in low
    assert "local_regime_hint" in RADAR_SYSTEM_PROMPT
    assert "cotações atrasadas" in RADAR_SYSTEM_PROMPT
    assert "5" in RADAR_SYSTEM_PROMPT  # top-5 candidatos


def test_compact_instruction_warns_against_copying_regime_hint():
    scan = RadarScan(
        monitored=7,
        with_valid_data=1,
        candidates=[_candidate()],
        rejected=[],
        pairs=[],
        all_valid=[_candidate()],
    )
    compact = scan.compact_for_ai()
    assert "local_regime_hint" in compact["instruction"]
    assert "não é o resultado preferido" in compact["instruction"].lower() or (
        "HOLD não é o resultado preferido" in compact["instruction"]
    )
    pair = compact["pairs_for_comparison"][0]
    assert "tick_time_lag_s" not in pair["quote"]


def _proposal(decision: str) -> AIRadarDecision:
    if decision == "HOLD":
        return AIRadarDecision(
            decision="HOLD",
            symbol=None,
            justification="Sem edge claro (teste).",
            favoring_scenario="—",
            invalidating_scenario="—",
            risks="nenhum",
            missing_information="",
            confidence=0.5,
            compared_symbols=["EURUSD"],
        )
    side_entry = 1.1002
    sl = 1.0950 if decision == "BUY" else 1.1050
    tp = 1.1100 if decision == "BUY" else 1.0900
    return AIRadarDecision(
        decision=decision,
        symbol="EURUSD",
        justification=f"Assimetria teste {decision}.",
        favoring_scenario="continuidade",
        invalidating_scenario="reversão",
        entry_price=side_entry,
        stop_loss=sl,
        take_profit=tp,
        risks="teste",
        missing_information="",
        confidence=0.7,
        compared_symbols=["EURUSD"],
    )


def _ensure_robot_running(db):
    st = db.query(RobotState).first()
    if st is None:
        st = RobotState(running=True, decision="HOLD")
        db.add(st)
    else:
        st.running = True
    db.commit()


@pytest.mark.parametrize("decision", ["BUY", "SELL", "HOLD"])
def test_simulated_decision_path_to_demo_executor(db, tmp_path, monkeypatch, decision):
    """BUY/SELL preservados → DEMO (mock); HOLD sem order_send."""
    monkeypatch.setattr("app.ai.response_store._STORE_DIR", tmp_path / "ai_responses")
    _ensure_robot_running(db)
    activate_demo(db, confirmed=True, login=111, server="Demo-Server")
    pause_new_entries(db, paused=False)

    mt5 = _mt5_mock()
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    set_mt5_demo_executor_for_tests(ex)

    prop = _proposal(decision)
    scan = RadarScan(
        monitored=7,
        with_valid_data=7,
        candidates=[_candidate()],
        rejected=[],
        pairs=[],
        all_valid=[_candidate() for _ in range(7)],
    )
    for i, c in enumerate(scan.all_valid):
        c.symbol = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"][i]
        c.display = c.symbol

    usage = {
        "estimated_cost_usd": 0.0009,
        "persisted_path": "",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "skipped_paid": False,
    }
    ctx = {
        "radar": scan.compact_for_ai(),
        "quote": scan.candidates[0].snapshot["quote"],
        "symbol": "EURUSD" if decision != "HOLD" else None,
    }

    sched = OpenAIAutonomousScheduler()
    sched._dry_run = False
    sched._interval_s = 0.0

    mt5_ok = {
        "ok": True,
        "reason": "ok",
        "terminal_connected": True,
        "pairs_ok": 7,
        "clock_anomaly": True,
        "claims_realtime": False,
    }

    with patch("app.robot.openai_autonomous.assess_mt5_ready_for_paid", return_value=mt5_ok):
        with patch("app.robot.openai_autonomous.scan_radar", return_value=scan):
            with patch(
                "app.robot.openai_autonomous.validate_scan_for_paid",
                return_value={"ok": True, "data_quality": {}},
            ):
                with patch(
                    "app.robot.openai_autonomous.run_radar_decision",
                    return_value=(ctx, prop, scan, usage),
                ):
                    with patch.object(
                        sched,
                        "_claim_schedule_slot",
                        return_value=True,
                    ):
                        # Autorização OpenAI local (sem rede).
                        control = MagicMock()
                        control.is_enabled.return_value = True
                        control.create_authorization.return_value = {
                            "authorization_id": "test-auth-hold-path"
                        }
                        control._lock = MagicMock()
                        control._lock.__enter__ = lambda s: s
                        control._lock.__exit__ = lambda *a: False
                        control._auths = {}
                        control.end_analysis_context = MagicMock()
                        with patch(
                            "app.robot.openai_autonomous.get_ai_control",
                            return_value=control,
                        ):
                            with patch(
                                "app.robot.openai_autonomous.get_settings"
                            ) as gs:
                                settings = MagicMock()
                                settings.provider_configured.return_value = True
                                settings.openai_default_model = "gpt-4o-mini"
                                settings.robot_openai_daily_budget_usd = 1.0
                                settings.live_authorized = False
                                settings.trading_environment = "DEMO"
                                settings.live_trading_locked = True
                                gs.return_value = settings
                                with patch(
                                    "app.robot.openai_autonomous.autonomous_spent_today_usd",
                                    return_value=0.0,
                                ):
                                    with patch(
                                        "app.robot.openai_autonomous.estimate_radar_analysis_usd",
                                        return_value={"estimated_cost_usd": 0.001},
                                    ):
                                        result = sched._run_once_locked(db, dry_run=False)

    assert result.get("ok") is True
    assert result["decision"] == decision

    log = (
        db.query(AIDecisionLog)
        .filter(AIDecisionLog.mode == "openai_radar_autonomous")
        .order_by(AIDecisionLog.id.desc())
        .first()
    )
    assert log is not None
    assert log.decision == decision  # sem conversão BUY/SELL → HOLD

    ctx_saved = json.loads(log.context_json or "{}")
    demo_ex = result.get("demo_execution") or {}
    if decision == "HOLD":
        assert mt5.order_send.call_count == 0
        assert not result.get("executed")
        assert demo_ex.get("attempted") in (False, None)
        assert "HOLD" in (log.risk_reasons or "")
    else:
        assert demo_ex.get("attempted") is True
        assert mt5.order_send.call_count == 1
        assert demo_ex.get("ok") is True
        assert log.executed is True
        assert ctx_saved.get("demo_execution_armed") is True
        assert ctx_saved.get("execution_disabled") is False


def test_hold_never_calls_order_send_even_with_demo_auth(db):
    mt5 = _mt5_mock()
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _ensure_robot_running(db)
    activate_demo(db, confirmed=True, login=111, server="Demo-Server")
    pause_new_entries(db, paused=False)
    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="aguardar",
        entry_price=0,
        stop_loss=0,
        take_profit=0,
        proposal_json='{"decision":"HOLD","symbol":null}',
        context_json="{}",
        estimated_cost_usd=0.001,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    out = ex.try_execute_decision(db, log)
    assert out["ok"] is False
    mt5.order_send.assert_not_called()
