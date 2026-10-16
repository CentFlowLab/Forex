"""Testes obrigatórios da auditoria — sem chamadas OpenAI pagas."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.ai.response_store import persist_raw_provider_response
from app.database import SessionLocal, init_db
from app.models.ai_control import AICallLog
from app.models.robot import AIDecisionLog
from app.robot.openai_autonomous import (
    FLAG_REQUIRES_RECONFIRM,
    OpenAIAutonomousScheduler,
    _set_flag,
    autonomous_spent_today_usd,
)


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def test_autonomous_budget_counts_aicall_even_without_decision(db):
    """Chamada cobrada sem AIDecisionLog ainda conta para o teto 0,10."""
    # Usar auth realista (não audit-*) para validar a conta; limpar no finally.
    row = AICallLog(
        provider="openai",
        model="gpt-4o-mini",
        role="robot_radar",
        status="ok",
        estimated_cost_usd=0.05,
        authorized=True,
        authorization_id="tmp-budget-proof-delete-me",
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    try:
        spent = autonomous_spent_today_usd(db)
        assert spent >= 0.05
    finally:
        db.delete(row)
        db.commit()


def test_autonomous_budget_reconciles_by_auth_id(db):
    """Mesmo authorization_id: conta uma vez (max); órfão AICallLog ainda conta."""
    auth = "tmp-budget-reconcile-delete-me"
    call = AICallLog(
        provider="openai",
        model="gpt-4o-mini",
        role="robot_radar_autonomous",
        status="ok",
        estimated_cost_usd=0.07,
        authorized=True,
        authorization_id=auth,
        prompt_tokens=10,
        completion_tokens=5,
    )
    dup = AICallLog(
        provider="openai",
        model="gpt-4o-mini",
        role="robot_radar_autonomous",
        status="ok",
        estimated_cost_usd=0.07,
        authorized=True,
        authorization_id=auth,
        prompt_tokens=10,
        completion_tokens=5,
    )
    dec = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        estimated_cost_usd=0.01,
        justification="tmp-budget-max-dec",
        context_json=json.dumps({"authorization_id": auth}),
    )
    orphan = AICallLog(
        provider="openai",
        model="gpt-4o-mini",
        role="robot_radar",
        status="ok",
        estimated_cost_usd=0.03,
        authorized=True,
        authorization_id="tmp-budget-orphan-delete-me",
    )
    incomplete = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        estimated_cost_usd=0.02,
        justification="tmp-budget-incomplete-dec",
        context_json="{}",
    )
    before = autonomous_spent_today_usd(db)
    db.add_all([call, dup, dec, orphan, incomplete])
    db.commit()
    try:
        spent = autonomous_spent_today_usd(db)
        # 0.07 (auth reconciliado, sem double-count) + 0.03 (órfão) + 0.02 (decisão sem auth)
        assert spent - before == pytest.approx(0.12, abs=1e-6)
    finally:
        for row in (call, dup, dec, orphan, incomplete):
            db.delete(row)
        db.commit()


def test_autonomous_budget_concurrent_reads_stable(db):
    """Leituras concorrentes do orçamento devolvem o mesmo total."""
    auth = "tmp-budget-concurrent-delete-me"
    row = AICallLog(
        provider="openai",
        model="gpt-4o-mini",
        role="robot_radar_autonomous",
        status="ok",
        estimated_cost_usd=0.04,
        authorized=True,
        authorization_id=auth,
    )
    db.add(row)
    db.commit()
    try:
        results: list[float] = []

        def _read() -> None:
            s = SessionLocal()
            try:
                results.append(autonomous_spent_today_usd(s))
            finally:
                s.close()

        threads = [threading.Thread(target=_read) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(results) == 4
        assert all(r == pytest.approx(results[0], abs=1e-9) for r in results)
        assert results[0] >= 0.04
    finally:
        db.delete(row)
        db.commit()


def test_response_store_keeps_request_messages(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ai.response_store._STORE_DIR", tmp_path / "ai_responses")
    path = persist_raw_provider_response(
        provider="openai",
        model="gpt-4o-mini",
        raw_http={"choices": [{"message": {"content": '{"decision":"HOLD"}'}}]},
        text='{"decision":"HOLD"}',
        request_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "contexto MT5 update_age_s=0.01"},
        ],
    )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["request_messages"][1]["content"].startswith("contexto MT5")
    assert data["text"]


def test_simulated_broker_commission_once(db):
    from app.brokers.base import OrderRequest
    from app.brokers.simulated import SimulatedBroker
    from app.config import get_settings
    from app.models.trading import ClosedTrade, Position
    from sqlalchemy import select

    settings = get_settings()
    broker = SimulatedBroker(db)
    broker.connect()
    bal_before = broker.account().balance
    cid = f"TEST-AUDIT-{datetime.now(timezone.utc).timestamp()}"
    fake_quote = MagicMock(bid=1.1000, ask=1.1002, mid=1.1001, spread_pips=2.0)
    with patch.object(broker, "quote", return_value=fake_quote):
        res = broker.submit_order(
            OrderRequest(
                client_order_id=cid,
                symbol="EURUSD",
                side="BUY",
                volume_lots=0.10,
                stop_loss=1.0900,
                take_profit=1.1100,
            )
        )
        assert res.accepted, res.message
        db.expire_all()
        # Sem comissão na abertura
        assert broker.account().balance == pytest.approx(bal_before, abs=0.02)
        pid = db.scalar(select(Position.id).where(Position.client_order_id == cid).limit(1))
        assert pid is not None
        trade = broker.close_position(int(pid), "TEST")
        assert trade is not None
        commission = float(settings.sim_commission_per_lot) * 0.10
        assert trade.commission == pytest.approx(commission, abs=1e-6)
        assert broker.account().balance == pytest.approx(bal_before + trade.pnl, abs=0.05)
        closed = list(db.scalars(select(ClosedTrade).where(ClosedTrade.client_order_id == cid)))
        assert len(closed) == 1


def test_requires_reconfirm_blocks_schedule(db):
    sched = OpenAIAutonomousScheduler()
    _set_flag(db, FLAG_REQUIRES_RECONFIRM, "true")
    from app.robot.openai_autonomous import FLAG_CONFIRMED, FLAG_ARMED

    _set_flag(db, FLAG_CONFIRMED, "true")
    _set_flag(db, FLAG_ARMED, "true")
    assert sched.is_confirmed(db) is False


def test_claim_after_precheck_not_before_mt5_fail(db, monkeypatch):
    sched = OpenAIAutonomousScheduler()
    sched._interval_s = 900.0
    claimed = {"n": 0}

    def fake_claim(_db):
        claimed["n"] += 1
        return True

    monkeypatch.setattr(sched, "_claim_schedule_slot", fake_claim)
    with patch(
        "app.robot.openai_autonomous.assess_mt5_ready_for_paid",
        return_value={"ok": False, "reason": "MT5 down"},
    ):
        result = sched.run_once(db, dry_run=False)
    assert result.get("skipped") is True
    assert claimed["n"] == 0  # não reserva intervalo se MT5 falha


def test_panel_openai_matches_persisted_decision(db, tmp_path, monkeypatch):
    from app.robot.openai_panel import openai_panel_payload

    store = tmp_path / "ai_responses"
    store.mkdir()
    monkeypatch.setenv("GROK_AI_RESPONSES_DIR", str(store))
    path = store / "audit_real.json"
    path.write_text(
        json.dumps({"model": "gpt-4o-mini", "estimated_cost_usd": 0.0008, "text": "{}"}),
        encoding="utf-8",
    )
    just = "Análise genuína: mercado sem assimetria clara entre os sete pares."
    db.add(
        AIDecisionLog(
            mode="openai_radar_autonomous",
            decision="HOLD",
            justification=just,
            risks="Risco X",
            invalidating_scenario="Inválida se Y",
            confidence=0.55,
            estimated_cost_usd=0.0008,
            proposal_json=json.dumps(
                {
                    "decision": "HOLD",
                    "symbol": None,
                    "justification": just,
                    "confidence": 0.55,
                    "risks": "Risco X",
                    "invalidating_scenario": "Inválida se Y",
                }
            ),
            context_json=json.dumps(
                {
                    "persisted_path": str(path),
                    "authorization_id": "a64c48cb0ae142118825961d616e4897",
                }
            ),
        )
    )
    db.commit()
    panel = openai_panel_payload(db, history_limit=1)
    assert panel["latest"]["justification"] == just
    assert panel["latest"]["decision_pt"] == "AGUARDAR"
    assert panel["latest"]["is_real_paid_openai"] is True


def test_live_locked_default():
    from app.config import get_settings

    assert get_settings().live_trading_locked is True
