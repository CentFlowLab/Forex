"""Testes do agendador OpenAI autónomo — sem chamadas pagas reais."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from app.ai.budget import spent_today_usd
from app.database import SessionLocal, init_db
from app.robot.openai_autonomous import OpenAIAutonomousScheduler, get_openai_autonomous


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def sched(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ai.response_store._STORE_DIR", tmp_path / "ai_responses")
    s = OpenAIAutonomousScheduler()
    s._dry_run = True
    s._interval_s = 900.0
    return s


def test_no_paid_calls_until_confirm(db, sched):
    from app.robot.openai_autonomous import (
        FLAG_ARMED,
        FLAG_CONFIRMED,
        FLAG_SUSPEND_REASON,
        _get_flag,
        _set_flag,
    )

    prev_c, prev_a = _get_flag(db, FLAG_CONFIRMED), _get_flag(db, FLAG_ARMED)
    prev_s = _get_flag(db, FLAG_SUSPEND_REASON)
    try:
        _set_flag(db, FLAG_CONFIRMED, "false")
        _set_flag(db, FLAG_ARMED, "false")
        _set_flag(db, FLAG_SUSPEND_REASON, "")
        sched._suspend_reason = ""
        prep = sched.preparation_status(db)
        assert prep["paid_calls_blocked_until_confirm"] is True
        assert prep["confirmed"] is False
        assert prep["execution_enabled"] is False
        label = (prep.get("authorization_label") or "").lower()
        note = (prep.get("note") or "").lower()
        assert "aguardar" in label or "pendente" in label or prep.get("authorization_state") == "pending"
        assert "nenhuma chamada paga" in note or "até confirmar" in note
    finally:
        _set_flag(db, FLAG_CONFIRMED, prev_c or "false")
        _set_flag(db, FLAG_ARMED, prev_a or "false")
        _set_flag(db, FLAG_SUSPEND_REASON, prev_s or "")


def test_dry_run_persists_and_no_orders(db, sched, tmp_path, monkeypatch):
    monkeypatch.setattr("app.ai.response_store._STORE_DIR", tmp_path / "ai_responses")
    mt5_ok = {
        "ok": True,
        "reason": "ok",
        "terminal_connected": True,
        "pairs_ok": 7,
        "clock_anomaly": True,
        "claims_realtime": False,
    }
    with patch("app.robot.openai_autonomous.assess_mt5_ready_for_paid", return_value=mt5_ok):
        with patch("app.robot.openai_autonomous.scan_radar") as scan_m:
            scan = MagicMock()
            scan.monitored = 7
            scan.with_valid_data = 7
            scan.all_valid = []
            scan.candidates = []
            scan_m.return_value = scan
            with patch(
                "app.robot.openai_autonomous.estimate_radar_analysis_usd",
                return_value={"estimated_cost_usd": 0.001},
            ):
                with patch("app.robot.openai_autonomous.get_settings") as gs:
                    settings = gs.return_value
                    settings.openai_default_model = "gpt-4o-mini"
                    settings.provider_configured.return_value = True
                    settings.robot_openai_daily_budget_usd = 0.10
                    settings.ai_enabled = True
                    with patch("app.robot.openai_autonomous.get_ai_control") as gc:
                        gc.return_value.is_enabled.return_value = True
                        with patch(
                            "app.robot.openai_autonomous.autonomous_spent_today_usd",
                            return_value=0.0,
                        ):
                            result = sched.run_once(db, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["executed"] is False
    assert result["decision"] == "HOLD"
    assert result["persisted_path"]
    from pathlib import Path

    assert Path(result["persisted_path"]).exists()


def test_daily_budget_blocks(db, sched, monkeypatch):
    mt5_ok = {
        "ok": True,
        "reason": "ok",
        "terminal_connected": True,
        "pairs_ok": 7,
        "clock_anomaly": False,
    }
    with patch("app.robot.openai_autonomous.assess_mt5_ready_for_paid", return_value=mt5_ok):
        with patch("app.robot.openai_autonomous.scan_radar") as scan_m:
            scan = MagicMock()
            scan.monitored = 7
            scan.with_valid_data = 7
            scan.all_valid = []
            scan.candidates = []
            scan_m.return_value = scan
            with patch(
                "app.robot.openai_autonomous.validate_scan_for_paid",
                return_value={"ok": True, "reason": "ok", "data_quality": {}},
            ):
                with patch(
                    "app.robot.openai_autonomous.estimate_radar_analysis_usd",
                    return_value={"estimated_cost_usd": 0.05},
                ):
                    with patch(
                        "app.robot.openai_autonomous.autonomous_spent_today_usd",
                        return_value=0.09,
                    ):
                        with patch("app.robot.openai_autonomous.get_settings") as gs:
                            settings = gs.return_value
                            settings.provider_configured.return_value = True
                            settings.robot_openai_daily_budget_usd = 0.10
                            settings.openai_default_model = "gpt-4o-mini"
                            with patch("app.robot.openai_autonomous.get_ai_control") as gc:
                                gc.return_value.is_enabled.return_value = True
                                # Evitar claim de intervalo neste teste de orçamento.
                                with patch.object(sched, "_claim_schedule_slot", return_value=True):
                                    result = sched.run_once(db, dry_run=False)
    assert result["skipped"] is True
    assert "Orçamento" in result["reason"]


def test_concurrent_calls_blocked(db, sched, monkeypatch):
    mt5_ok = {
        "ok": True,
        "reason": "ok",
        "terminal_connected": True,
        "pairs_ok": 7,
        "clock_anomaly": False,
    }
    entered = threading.Event()
    release = threading.Event()

    def slow_run(db, dry_run=False):
        entered.set()
        release.wait(timeout=5)
        return {"ok": True, "dry_run": True, "executed": False}

    with patch.object(sched, "_run_once_locked", side_effect=slow_run):
        t = threading.Thread(target=lambda: sched.run_once(db, dry_run=True))
        t.start()
        assert entered.wait(timeout=2)
        second = sched.run_once(db, dry_run=True)
        assert second.get("skipped") is True
        assert "concorrência" in second.get("reason", "").lower() or "curso" in second.get(
            "reason", ""
        ).lower()
        release.set()
        t.join(timeout=3)


def test_stop_clears_schedule(db, sched):
    from datetime import datetime, timezone

    sched._next_at = datetime.now(timezone.utc)
    sched.on_robot_stop()
    assert "parado" in (sched._suspend_reason or "").lower()


def test_claim_slot_blocks_second_within_interval(tmp_path, monkeypatch):
    """Reserva atómica na BD: 2.ª tentativa no mesmo intervalo falha."""
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.database import Base
    from app.models.account import AppSetting  # noqa: F401
    from app.models.robot import AIDecisionLog  # noqa: F401
    from app.robot.openai_autonomous import (
        FLAG_LAST_RUN_AT,
        FLAG_NEXT_AT,
        OpenAIAutonomousScheduler,
        _set_flag,
        atomic_claim_autonomous_slot,
    )

    db_path = tmp_path / "claim_unit.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 60.0},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):  # noqa: ANN001
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
        c.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    _set_flag(db, FLAG_LAST_RUN_AT, "")
    _set_flag(db, FLAG_NEXT_AT, "")

    now = datetime.now(timezone.utc)
    assert atomic_claim_autonomous_slot(db, interval_s=900.0, now=now) is True
    assert atomic_claim_autonomous_slot(db, interval_s=900.0, now=now) is False

    sched = OpenAIAutonomousScheduler()
    sched._interval_s = 900.0
    sched._last_run_at = None
    sched._next_at = None
    assert sched._claim_schedule_slot(db) is False
    db.close()
    engine.dispose()


def test_earliest_eligible_respects_last_run(db, sched, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    sched._last_run_at = now - timedelta(seconds=60)
    sched._next_at = None
    sched._interval_s = 900.0
    monkeypatch.setattr(sched, "_hydrate_schedule_from_db", lambda _db: None)

    # Não consultar AIDecisionLog real.
    orig = sched._earliest_eligible_at.__func__  # type: ignore[attr-defined]

    def earliest_only_memory(self, db_):
        last = self._last_run_at
        if last is None:
            return datetime.now(timezone.utc)
        earliest = last + timedelta(seconds=self._interval_s)
        now2 = datetime.now(timezone.utc)
        return earliest if earliest > now2 else now2

    monkeypatch.setattr(
        type(sched),
        "_earliest_eligible_at",
        earliest_only_memory,
    )
    eligible = sched._earliest_eligible_at(db)
    assert eligible >= now + timedelta(seconds=800)
