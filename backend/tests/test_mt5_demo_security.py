"""Testes de segurança A–T do executor MT5 DEMO — mocks only, sem order_send real."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.brokers.mt5_demo_auth import (
    activate_demo,
    clear_auth_on_startup,
    clear_emergency,
    get_demo_auth_snapshot,
    pause_new_entries,
    reset_demo_auth_module_for_tests,
    trigger_emergency,
)
from app.brokers.mt5_demo_executor import MT5DemoExecutor, protected_order_send, set_mt5_demo_executor_for_tests
from app.database import Base
from app.models.demo_execution import DemoExecutionAttempt
from app.models.robot import AIDecisionLog, RobotState
from app.services.seed import seed_all


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    seed_all(s)
    reset_demo_auth_module_for_tests()
    clear_emergency(s)
    clear_auth_on_startup(s)
    # Robô a correr para preflight
    from sqlalchemy import select

    st = s.scalar(select(RobotState).limit(1))
    if st is None:
        st = RobotState(running=True, decision="HOLD")
        s.add(st)
    else:
        st.running = True
    s.commit()
    yield s
    set_mt5_demo_executor_for_tests(None)
    s.close()


def _mt5_mock(
    *,
    trade_mode: int = 0,
    login: int = 111,
    server: str = "Demo-Server",
    trade_allowed: bool = True,
    connected: bool = True,
    balance: float = 10000.0,
    equity: float = 10000.0,
    margin_free: float = 9000.0,
    order_send_result=None,
    order_send_raises: Exception | None = None,
    positions=None,
):
    mt5 = MagicMock()
    mt5.ACCOUNT_TRADE_MODE_DEMO = 0
    mt5.ACCOUNT_TRADE_MODE_CONTEST = 1
    mt5.ACCOUNT_TRADE_MODE_REAL = 2
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1
    mt5.ORDER_TIME_GTC = 0
    mt5.ORDER_FILLING_IOC = 1
    mt5.POSITION_TYPE_BUY = 0
    mt5.POSITION_TYPE_SELL = 1

    ai = SimpleNamespace(
        login=login,
        server=server,
        trade_mode=trade_mode,
        trade_allowed=trade_allowed,
        balance=balance,
        equity=equity,
        margin=100.0,
        margin_free=margin_free,
        profit=0.0,
        currency="USD",
    )
    mt5.account_info.return_value = ai
    mt5.terminal_info.return_value = SimpleNamespace(connected=connected)
    mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.1000, ask=1.1002)
    mt5.symbol_info.return_value = SimpleNamespace(
        volume_min=0.01,
        volume_max=10.0,
        volume_step=0.01,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value=1.0,
        trade_stops_level=10,
    )
    mt5.order_calc_profit.return_value = -50.0  # loss 50 per lot for SL distance
    mt5.order_calc_margin.return_value = 100.0
    mt5.order_check.return_value = SimpleNamespace(retcode=0, comment="ok")
    mt5.positions_get.return_value = positions or []

    if order_send_raises:
        mt5.order_send.side_effect = order_send_raises
    elif order_send_result is not None:
        mt5.order_send.return_value = order_send_result
    else:
        mt5.order_send.return_value = SimpleNamespace(
            retcode=10009, order=555, deal=666, comment="done"
        )
    return mt5


def _decision(db: Session, *, side: str = "BUY", sl: float = 1.0950, tp: float = 1.1100) -> AIDecisionLog:
    import json

    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision=side,
        justification="teste",
        entry_price=1.1002,
        stop_loss=sl,
        take_profit=tp,
        proposal_json=json.dumps(
            {
                "decision": side,
                "symbol": "EURUSD",
                "entry_price": 1.1002,
                "stop_loss": sl,
                "take_profit": tp,
            }
        ),
        context_json="{}",
        estimated_cost_usd=0.0,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def _auth_demo(db: Session, login: int = 111, server: str = "Demo-Server") -> None:
    activate_demo(db, confirmed=True, login=login, server=server)
    pause_new_entries(db, paused=False)


# --- A–T ---


def test_A_real_account_refused(db):
    mt5 = _mt5_mock(trade_mode=2)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    assert "DEMO" in out["reason"] or "não é" in out["reason"].lower()
    mt5.order_send.assert_not_called()


def test_B_contest_refused(db):
    mt5 = _mt5_mock(trade_mode=1)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_C_demo_not_authorized_refused(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    # sem activate
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    assert "autoriz" in out["reason"].lower()
    mt5.order_send.assert_not_called()


def test_D_demo_authorized_can_proceed(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is True
    assert out["status"] == "executed"
    mt5.order_send.assert_called_once()


def test_E_account_switch_blocks(db):
    mt5 = _mt5_mock(trade_mode=0, login=222)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db, login=111)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_F_account_info_none_blocks(db):
    mt5 = _mt5_mock(trade_mode=0)
    mt5.account_info.return_value = None
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_G_login_server_change_blocks(db):
    mt5 = _mt5_mock(trade_mode=0, server="Other")
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db, server="Demo-Server")
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False


def test_H_restart_clears_auth(db):
    _auth_demo(db)
    assert get_demo_auth_snapshot(db)["active"] is True
    clear_auth_on_startup(db)
    assert get_demo_auth_snapshot(db)["active"] is False


def test_I_emergency_blocks(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    trigger_emergency(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    assert "emergência" in out["reason"].lower()
    mt5.order_send.assert_not_called()


def test_J_wait_no_order(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    log = _decision(db, side="HOLD")
    out = ex.try_execute_decision(db, log)
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_K_missing_sl_blocks(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db, sl=0.0))
    assert out["ok"] is False
    assert "stop" in out["reason"].lower()
    mt5.order_send.assert_not_called()


def test_L_risk_too_high_blocks(db):
    mt5 = _mt5_mock(trade_mode=0, equity=100.0, balance=100.0, margin_free=100.0)
    # Loss per lot huge → volume below min
    mt5.order_calc_profit.return_value = -100000.0
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_M_duplicate_decision_one_attempt(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    log = _decision(db)
    assert ex.try_execute_decision(db, log)["ok"] is True
    out2 = ex.try_execute_decision(db, log)
    assert out2["ok"] is False
    assert "idempot" in out2["reason"].lower() or "já tem" in out2["reason"].lower()
    assert mt5.order_send.call_count == 1


def test_N_timeout_no_blind_retry(db):
    mt5 = _mt5_mock(trade_mode=0, order_send_raises=TimeoutError("timeout"))
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    assert out.get("status") == "uncertain" or "reenvio" in out["reason"].lower() or "exceção" in out["reason"].lower()
    assert mt5.order_send.call_count == 1


def test_O_manual_position_not_closed(db):
    manual = SimpleNamespace(
        magic=99999, symbol="EURUSD", ticket=1, volume=0.1, type=0
    )
    mt5 = _mt5_mock(trade_mode=0, positions=[manual])
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    res = ex.close_robot_positions(db)
    assert res.get("closed") == []
    mt5.order_send.assert_not_called()


def test_P_daily_loss_blocks(db):
    mt5 = _mt5_mock(trade_mode=0, equity=10000.0)
    mt5.account_info.return_value.profit = -500.0  # 5% > 2%
    # Re-bind because SimpleNamespace was replaced
    ai = SimpleNamespace(
        login=111,
        server="Demo-Server",
        trade_mode=0,
        trade_allowed=True,
        balance=10000.0,
        equity=10000.0,
        margin=100.0,
        margin_free=9000.0,
        profit=-500.0,
        currency="USD",
    )
    mt5.account_info.return_value = ai
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    assert "diári" in out["reason"].lower() or "perda" in out["reason"].lower()


def test_Q_db_failure_blocks(db):
    mt5 = _mt5_mock(trade_mode=0)
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    log = _decision(db)
    # Forçar falha no commit da tentativa: fechar sessão após criar decisão
    # Simular unique violation via tentativa pré-inserida
    db.add(
        DemoExecutionAttempt(
            decision_id=log.id,
            idempotency_key=f"decision:{log.id}",
            status="reserved",
        )
    )
    db.commit()
    out = ex.try_execute_decision(db, log)
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_R_stale_spread_blocks(db):
    mt5 = _mt5_mock(trade_mode=0)
    mt5.symbol_info_tick.return_value = SimpleNamespace(bid=1.10, ask=1.15)  # spread enorme
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    assert "spread" in out["reason"].lower()
    mt5.order_send.assert_not_called()


def test_S_account_changes_before_send(db):
    mt5 = _mt5_mock(trade_mode=0)
    calls = {"n": 0}
    ai_ok = SimpleNamespace(
        login=111,
        server="Demo-Server",
        trade_mode=0,
        trade_allowed=True,
        balance=10000.0,
        equity=10000.0,
        margin=100.0,
        margin_free=9000.0,
        profit=0.0,
        currency="USD",
    )
    ai_switched = SimpleNamespace(
        login=999,
        server="Demo-Server",
        trade_mode=0,
        trade_allowed=True,
        balance=10000.0,
        equity=10000.0,
        margin=100.0,
        margin_free=9000.0,
        profit=0.0,
        currency="USD",
    )

    def account_info():
        calls["n"] += 1
        # Últimas leituras antes do send (após status sending) → conta muda
        if calls["n"] >= 3:
            return ai_switched
        return ai_ok

    mt5.account_info.side_effect = account_info
    ex = MT5DemoExecutor(mt5_factory=lambda: mt5)
    _auth_demo(db)
    out = ex.try_execute_decision(db, _decision(db))
    assert out["ok"] is False
    mt5.order_send.assert_not_called()


def test_T_legacy_submit_order_protected():
    from app.brokers.base import OrderRequest
    from app.brokers.mt5 import MT5BrokerAdapter

    adapter = MT5BrokerAdapter()
    res = adapter.submit_order(
        OrderRequest(
            client_order_id="x",
            symbol="EURUSD",
            side="BUY",
            volume_lots=0.01,
            stop_loss=1.0,
            take_profit=1.1,
        )
    )
    assert res.accepted is False
    with pytest.raises(RuntimeError):
        protected_order_send({})
