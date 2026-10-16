"""Testes do ciclo autónomo e paper trading (sem IA paga)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adaptive.validation import chronological_split
from app.autonomy.fingerprint import experiment_fingerprint
from app.autonomy.paper import PaperTradingService
from app.autonomy.service import (
    STATUS_ERROR,
    STATUS_EXECUTING,
    STATUS_IDLE,
    STATUS_PAUSED,
    AutonomyService,
)
from app.autonomy.timeutil import as_utc, iso_utc, utcnow
from app.database import Base
from app.models import adaptive as _a  # noqa: F401
from app.models import autonomy as _u  # noqa: F401
from app.models.adaptive import AdaptiveStrategyState
from app.models.autonomy import AutonomyCycleLog, AutonomyState, ExperimentFingerprint, PaperTradeLog
from app.models.market import Candle
from app.services.seed import seed_all


@pytest.fixture()
def udb() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    seed_all(s)
    yield s
    s.close()


def test_fingerprint_stable() -> None:
    a = experiment_fingerprint(
        strategy_key="ma_crossover",
        params={"fast": 20, "slow": 50},
        start_iso="2025-10-01T00:00:00+00:00",
        end_iso="2026-10-01T00:00:00+00:00",
        timeframe="M15",
    )
    b = experiment_fingerprint(
        strategy_key="ma_crossover",
        params={"slow": 50, "fast": 20},
        start_iso="2025-10-01T00:00:00+00:00",
        end_iso="2026-10-01T00:00:00+00:00",
        timeframe="M15",
    )
    assert a == b


def test_no_duplicate_fingerprint(udb: Session) -> None:
    fp = experiment_fingerprint(
        strategy_key="x",
        params={"a": 1},
        start_iso="2025-01-01T00:00:00Z",
        end_iso="2025-02-01T00:00:00Z",
        timeframe="M15",
    )
    udb.add(ExperimentFingerprint(fingerprint=fp, strategy_key="x"))
    udb.commit()
    again = udb.scalar(select(ExperimentFingerprint).where(ExperimentFingerprint.fingerprint == fp))
    assert again is not None


def test_as_utc_naive_assumed_utc() -> None:
    naive = datetime(2026, 3, 1, 12, 0, 0)  # sem tz — política: UTC
    aware = as_utc(naive)
    assert aware is not None
    assert aware.tzinfo is not None
    assert aware.utcoffset() == timedelta(0)
    assert aware.hour == 12


def test_as_utc_aware_converted() -> None:
    eastern = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    utc = as_utc(eastern)
    assert utc is not None
    assert utc.hour == 17
    assert "+00:00" in (iso_utc(utc) or "")


def test_sqlite_naive_next_cycle_comparable(udb: Session) -> None:
    """Datas antigas SQLite (naive) devem comparar com now aware sem TypeError."""
    svc = AutonomyService()
    st = svc._state(udb)
    # Simular leitura SQLite: escrever via SQL raw sem timezone.
    udb.execute(
        text(
            "UPDATE autonomy_state SET enabled=1, paused=0, "
            "next_cycle_at='2026-01-01 00:00:00', last_error='' WHERE id=:id"
        ),
        {"id": st.id},
    )
    udb.commit()
    udb.expire_all()
    st2 = svc._state(udb)
    assert st2.next_cycle_at is not None
    # Valor pode voltar naive do SQLite
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    next_at = as_utc(st2.next_cycle_at)
    assert next_at is not None
    assert next_at < now  # não lança


def test_maybe_tick_naive_vs_aware_no_crash(udb: Session) -> None:
    svc = AutonomyService()
    st = svc._state(udb)
    st.enabled = True
    st.paused = False
    st.interval_minutes = 60
    # next_cycle no passado, naive (como SQLite)
    st.next_cycle_at = datetime(2020, 1, 1, 0, 0, 0)  # naive
    udb.commit()

    with patch.object(svc, "_run_research_cycle", return_value={"skipped": True, "reason": "test"}):
        with patch("app.autonomy.service.get_experiment_runner") as gr:
            gr.return_value.active_id.return_value = None
            out = svc.maybe_tick(udb, now=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    assert out is not None
    assert out.get("skipped") is True
    logs = list(udb.scalars(select(AutonomyCycleLog)))
    assert len(logs) >= 1
    assert logs[-1].status == "skipped"


def test_two_consecutive_cycles_simulated_clock(udb: Session) -> None:
    svc = AutonomyService()
    clock = {"t": datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)}
    svc.set_clock(lambda: clock["t"])
    calls: list[datetime] = []

    def fake_cycle(db, st, *, clock):  # noqa: ANN001
        calls.append(clock)
        return {"skipped": True, "reason": f"cycle-{len(calls)}", "strategies": ["a"]}

    st = svc._state(udb)
    st.enabled = True
    st.paused = False
    st.interval_minutes = 60
    st.next_cycle_at = clock["t"]
    udb.commit()

    with patch.object(svc, "_run_research_cycle", side_effect=fake_cycle):
        with patch("app.autonomy.service.get_experiment_runner") as gr:
            gr.return_value.active_id.return_value = None
            r1 = svc.maybe_tick(udb)
            assert r1 is not None
            assert len(calls) == 1
            # Antes do intervalo — não corre
            clock["t"] = datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc)
            r2 = svc.maybe_tick(udb)
            assert r2 is None
            assert len(calls) == 1
            # Após intervalo — segundo ciclo
            clock["t"] = datetime(2026, 6, 1, 11, 1, tzinfo=timezone.utc)
            r3 = svc.maybe_tick(udb)
            assert r3 is not None
            assert len(calls) == 2

    st2 = svc._state(udb)
    assert st2.cycles_completed == 2
    assert as_utc(st2.next_cycle_at) == datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)
    logs = list(udb.scalars(select(AutonomyCycleLog).order_by(AutonomyCycleLog.id)))
    assert len(logs) == 2
    assert all(x.status == "skipped" for x in logs)


def test_display_status_never_executing_on_error(udb: Session) -> None:
    svc = AutonomyService()
    st = svc._state(udb)
    st.enabled = True
    st.paused = False
    st.last_error = "can't compare offset-naive and offset-aware datetimes"
    udb.commit()
    assert svc.compute_display_status(st, active_experiment_id=None) == STATUS_ERROR
    assert svc.compute_display_status(st, active_experiment_id=None) != STATUS_EXECUTING
    st.paused = True
    assert svc.compute_display_status(st) == STATUS_PAUSED
    st.paused = False
    st.last_error = ""
    assert svc.compute_display_status(st) == STATUS_IDLE


def test_dashboard_status_matches_backend_after_error(udb: Session) -> None:
    svc = AutonomyService()
    st = svc._state(udb)
    st.enabled = True
    st.paused = False
    st.next_cycle_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    st.last_error = "falha de teste"
    udb.commit()
    with patch.object(svc, "maybe_tick", return_value=None):
        dash = svc.dashboard(udb)
    assert dash["engine"]["display_status"] == STATUS_ERROR
    assert "A trabalhar" not in (dash["engine"].get("last_message") or "")


def test_research_cycle_includes_seven_strategies(udb: Session) -> None:
    from app.adaptive.strategies import list_catalog

    svc = AutonomyService()
    st = svc._state(udb)
    st.enabled = True
    st.paused = False
    st.max_combos = 2
    st.lookback_days = 365
    udb.commit()
    catalog = list_catalog()
    assert len(catalog) == 7

    with patch("app.autonomy.service.prepare_experiment", return_value={"ready": True, "message": "ok"}):
        with patch("app.autonomy.service.get_adaptive_service") as gas:
            gas.return_value.start_experiment.return_value = {"id": 99, "status": "queued", "async": True}
            with patch("app.autonomy.service.get_experiment_runner") as gr:
                gr.return_value.active_id.return_value = None
                out = svc._run_research_cycle(
                    udb, st, clock=datetime(2026, 6, 1, tzinfo=timezone.utc)
                )
    assert out.get("started", {}).get("id") == 99
    assert set(out["strategies"]) == {s.key for s in catalog}
    assert len(out["strategies"]) == 7
    # Fingerprint guardado — segunda chamada é duplicado
    with patch("app.autonomy.service.prepare_experiment", return_value={"ready": True, "message": "ok"}):
        out2 = svc._run_research_cycle(udb, st, clock=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert out2.get("skipped") is True
    assert out2.get("reason") == "duplicate_batch"


def test_local_experiment_start_and_cycle_log(udb: Session) -> None:
    svc = AutonomyService()
    st = svc._state(udb)
    st.enabled = True
    st.paused = False
    st.interval_minutes = 60
    st.next_cycle_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    udb.commit()

    with patch("app.autonomy.service.prepare_experiment", return_value={"ready": True, "message": "ok"}):
        with patch("app.autonomy.service.get_adaptive_service") as gas:
            gas.return_value.start_experiment.return_value = {
                "id": 7,
                "status": "queued",
                "async": True,
            }
            with patch("app.autonomy.service.get_experiment_runner") as gr:
                gr.return_value.active_id.return_value = None
                result = svc.maybe_tick(udb, now=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))

    assert result is not None
    assert result.get("started", {}).get("id") == 7
    log = udb.scalar(select(AutonomyCycleLog).order_by(AutonomyCycleLog.id.desc()).limit(1))
    assert log is not None
    assert log.status == "ok"
    assert log.experiment_id == 7
    assert log.finished_at is not None
    assert log.strategies_count == 7
    st2 = svc._state(udb)
    assert st2.last_error == ""
    assert st2.cycles_completed == 1


def test_chrono_split_no_lookahead(udb: Session) -> None:
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    rows = []
    price = 1.1
    for i in range(120):
        rows.append(
            Candle(
                symbol="EURUSD",
                timeframe="M15",
                ts=start + timedelta(minutes=15 * i),
                open=price,
                high=price + 0.0002,
                low=price - 0.0002,
                close=price + 0.00005,
                volume=1,
                source="dukascopy",
                is_synthetic=False,
            )
        )
        price += 0.00005
    for r in rows:
        udb.add(r)
    udb.commit()
    split = chronological_split(rows)
    assert split.train[-1].ts < split.validation[0].ts < split.test[0].ts


def test_paper_blocks_stale_quote(udb: Session) -> None:
    svc = PaperTradingService()
    snap = MagicMock()
    snap.mid = 1.1
    snap.lag_seconds = 99999.0
    snap.block_entries = True
    snap.source_label = "test"
    with patch("app.autonomy.paper.get_quote_feed") as gf:
        gf.return_value.snapshot.return_value = snap
        out = svc.tick(udb)
    assert out["blocked"] is True
    assert out["reason"] == "cotação_obsoleta"


def test_paper_only_approved_and_simulated(udb: Session) -> None:
    svc = PaperTradingService()
    snap = MagicMock()
    snap.mid = 1.105
    snap.lag_seconds = 5.0
    snap.block_entries = False
    snap.source_label = "dukascopy_bi5"
    with patch("app.autonomy.paper.get_quote_feed") as gf:
        gf.return_value.snapshot.return_value = snap
        with patch("app.autonomy.paper.get_broker") as gb:
            broker = MagicMock()
            broker._quote_mid = None
            gb.return_value = broker
            out = svc.tick(udb)
    assert out.get("reason") == "Sem estratégias aprovadas." or out.get("action") == "HOLD"


def test_autonomy_pause_resume(udb: Session) -> None:
    svc = AutonomyService()
    with patch.object(svc, "maybe_tick", return_value=None):
        eng = svc.configure(udb, enabled=True, paused=False, interval_minutes=60, max_combos=2)
    assert eng["enabled"] is True
    assert eng["paused"] is False
    eng2 = svc.configure(udb, paused=True)
    assert eng2["paused"] is True
    assert eng2["ai_calls"] == 0
    assert eng2["display_status"] == STATUS_PAUSED


def test_autonomy_api_dashboard(client) -> None:
    res = client.get("/api/autonomy/dashboard")
    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "Centro de Autonomia"
    assert body["security"]["live_trading_locked"] is True
    assert body["engine"]["ai_calls"] == 0
    assert "display_status" in body["engine"]


def test_catalog_includes_new_strategies() -> None:
    from app.adaptive.strategies import list_catalog

    keys = {s.key for s in list_catalog()}
    assert len(keys) == 7
    assert "session_trend" in keys
    assert "regime_mean_reversion" in keys
    assert "filtered_breakout" in keys


def test_suspend_strategy(udb: Session) -> None:
    from app.adaptive.selection import suspend_strategy

    st = AdaptiveStrategyState(strategy_key="ma_crossover", display_name="t", status="approved")
    udb.add(st)
    udb.commit()
    udb.refresh(st)
    suspend_strategy(udb, st, experiment_id=1, reason="teste")
    udb.commit()
    assert st.status == "suspended"


def test_utcnow_is_aware() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
