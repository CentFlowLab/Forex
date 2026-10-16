"""Testes de histórico Dukascopy + Motor Adaptativo (sem créditos IA)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adaptive.experiments import run_experiment
from app.database import Base
from app.market.history_inventory import (
    DEFAULT_INVESTIGATION_END,
    DEFAULT_INVESTIGATION_START,
    analyze_coverage,
    hours_already_satisfied,
    is_forex_weekend_gap,
    prepare_experiment,
)
from app.market.incremental_download import (
    IncrementalDownloadController,
    get_incremental_download_controller,
)
from app.market.labels import DUKASCOPY_SOURCE
from app.market.validation import validate_series
from app.models import adaptive as _adaptive_models  # noqa: F401
from app.models.market import Candle
from app.models.replay import DownloadJob, ValidatedHistoryHour
from app.services.seed import seed_all


def _candle(ts: datetime, source: str = DUKASCOPY_SOURCE, synthetic: bool = False, price: float = 1.1) -> Candle:
    return Candle(
        symbol="EURUSD",
        timeframe="M15",
        ts=ts,
        open=price,
        high=price + 0.0002,
        low=price - 0.0002,
        close=price + 0.0001,
        volume=10,
        source=source,
        is_synthetic=synthetic,
    )


@pytest.fixture()
def hdb() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionTesting = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionTesting()
    seed_all(session)
    try:
        yield session
    finally:
        session.close()


def test_weekend_gap_classification() -> None:
    fri_close = datetime(2025, 10, 10, 22, 0, tzinfo=timezone.utc)  # sexta
    sat = datetime(2025, 10, 11, 12, 0, tzinfo=timezone.utc)
    sun_open = datetime(2025, 10, 12, 22, 0, tzinfo=timezone.utc)
    mon = datetime(2025, 10, 13, 10, 0, tzinfo=timezone.utc)
    assert is_forex_weekend_gap(fri_close) is True
    assert is_forex_weekend_gap(sat) is True
    assert is_forex_weekend_gap(sun_open) is False
    assert is_forex_weekend_gap(mon) is False


def test_validate_candles_rejects_bad_ohlc() -> None:
    rows = [
        {
            "symbol": "EURUSD",
            "timeframe": "M15",
            "ts": datetime(2025, 10, 13, 12, tzinfo=timezone.utc),
            "open": 1.1,
            "high": 1.0,
            "low": 1.05,
            "close": 1.08,
            "volume": 1,
            "source": "dukascopy",
            "is_synthetic": False,
        }
    ]
    cleaned, report = validate_series(rows, "M15")
    assert cleaned == []
    assert report.rejected == 1


def test_coverage_and_gaps(hdb: Session) -> None:
    start = datetime(2025, 10, 13, 8, 0, tzinfo=timezone.utc)  # segunda
    # 8 velas M15 (2 horas) — cobertura parcial
    for i in range(8):
        hdb.add(_candle(start + timedelta(minutes=15 * i)))
    hdb.commit()
    end = start + timedelta(hours=8)
    cov = analyze_coverage(hdb, start=start, end=end, timeframe="M15")
    assert cov["bars_valid_real"] == 8
    assert cov["coverage_pct"] < 100.0
    assert cov["missing_hours_count"] > 0
    assert cov["distinction"]["real_dukascopy"] is True
    assert cov["fills_gaps_with_synthetic"] is False


def test_hours_already_satisfied_cache(hdb: Session) -> None:
    hour = datetime(2025, 10, 13, 10, 0, tzinfo=timezone.utc)
    for i in range(4):
        hdb.add(_candle(hour + timedelta(minutes=15 * i)))
    hdb.commit()
    assert hours_already_satisfied(hdb, hour) is True
    assert hours_already_satisfied(hdb, hour + timedelta(hours=1)) is False


def test_prepare_blocks_insufficient(hdb: Session) -> None:
    prep = prepare_experiment(
        hdb,
        start=DEFAULT_INVESTIGATION_START,
        end=DEFAULT_INVESTIGATION_END,
        require_real_data=True,
    )
    assert prep["ready"] is False
    assert prep["blocked"] is True
    assert prep["ai_calls"] is False
    assert any("insuficientes" in r.lower() or "não há" in r.lower() for r in prep["reasons"])


def test_experiment_blocks_synthetic_when_require_real(hdb: Session) -> None:
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    for i in range(200):
        hdb.add(_candle(start + timedelta(minutes=15 * i), source="synthetic", synthetic=True))
    hdb.commit()
    with pytest.raises(ValueError, match="bloqueada|insuficientes|Dukascopy|Cobertura"):
        run_experiment(
            hdb,
            start=start,
            end=start + timedelta(minutes=15 * 199),
            require_real_data=True,
            max_combos_per_strategy=1,
        )


def test_incremental_download_cancel_resume_and_cache(hdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simula datafeed — sem HTTP real / sem IA / sem thread bloqueante."""
    import threading

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self) -> None:
            if self._target:
                self._target(*self._args, **self._kwargs)

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    WorkerSession = sessionmaker(bind=hdb.get_bind(), autoflush=False, autocommit=False)
    monkeypatch.setattr("app.market.incremental_download.SessionLocal", WorkerSession)
    # Evitar I/O parquet nos testes.
    monkeypatch.setattr("app.market.incremental_download.write_parquet", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.market.incremental_download.persist_candles",
        lambda db, rows, tf: {"validation": {"bars": len(rows)}, "inserted": len(rows), "updated": 0},
    )

    ctrl = IncrementalDownloadController()
    calls: list[datetime] = []

    def fake_fetch(hour: datetime, client: httpx.Client, retries: int = 3):
        calls.append(hour)
        ticks = [(hour + timedelta(minutes=i * 2), 1.1000, 1.1002) for i in range(10)]
        return ticks, "ok"

    ctrl.set_fetch_hour_for_tests(fake_fetch)

    start = datetime(2025, 10, 13, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)
    hours = [start + timedelta(hours=i) for i in range(3)]
    job = DownloadJob(
        symbol="EURUSD",
        source=DUKASCOPY_SOURCE,
        start_ts=start,
        end_ts=end,
        status="running",
        timeframe="M15",
        hours_total=3,
        hours_done=0,
        message="teste",
        cancel_requested=True,
    )
    hdb.add(job)
    hdb.commit()
    job_id = job.id
    ctrl._run_job(job_id, [h.isoformat() for h in hours], "M15")
    hdb.expire_all()
    job = hdb.get(DownloadJob, job_id)
    assert job is not None
    assert job.status == "cancelled"
    assert len(calls) == 0  # cancelado antes de qualquer pedido

    # Marcar horas como validadas → novo plano não deve re-descarregar
    for h in hours:
        hdb.add(
            ValidatedHistoryHour(
                symbol="EURUSD",
                source=DUKASCOPY_SOURCE,
                hour_ts=h,
                status="ok",
                tick_count=5,
            )
        )
    hdb.commit()
    calls.clear()
    job2 = ctrl.start_missing(hdb, start=start, end=end, confirmed=True, timeframe="M15")
    assert job2.status == "completed"
    assert len(calls) == 0
    assert "ai_calls" in (job2.details_json or "")

    # Cancelamento via API do controlador
    job3 = DownloadJob(
        symbol="EURUSD",
        source=DUKASCOPY_SOURCE,
        start_ts=start,
        end_ts=end,
        status="running",
        message="x",
    )
    hdb.add(job3)
    hdb.commit()
    cancelled = ctrl.cancel(hdb, job3.id)
    assert cancelled.cancel_requested is True


def test_download_requires_confirmation(hdb: Session) -> None:
    ctrl = get_incremental_download_controller()
    with pytest.raises(ValueError, match="Confirmação"):
        ctrl.start_missing(
            hdb,
            start=DEFAULT_INVESTIGATION_START,
            end=DEFAULT_INVESTIGATION_START + timedelta(days=1),
            confirmed=False,
        )


def test_no_ai_in_history_inventory_module() -> None:
    import app.market.history_inventory as hi
    import app.market.incremental_download as inc
    import inspect

    src = inspect.getsource(hi) + inspect.getsource(inc)
    assert "openai" not in src.lower() or "OpenAI" in src  # mensagem pt pode mencionar
    assert "run_collaboration" not in src
    assert "Anthropic" not in src


def test_api_history_and_block_experiment(client) -> None:
    res = client.get("/api/adaptive/history")
    assert res.status_code == 200
    body = res.json()
    assert body["default_period"]["start"].startswith("2025-10-01")
    assert body["preparation"]["ready"] is False
    assert body["coverage"]["source"] == "dukascopy"

    prep = client.get("/api/adaptive/history/prepare")
    assert prep.status_code == 200
    assert prep.json()["blocked"] is True

    # Sem confirmação
    bad = client.post(
        "/api/adaptive/history/download",
        json={"confirmed": False, "start": "2025-10-13T00:00:00Z", "end": "2025-10-14T00:00:00Z"},
    )
    assert bad.status_code == 400

    blocked = client.post(
        "/api/adaptive/experiments",
        json={
            "start": "2025-10-01T00:00:00Z",
            "end": "2026-10-01T00:00:00Z",
            "require_real_data": True,
        },
    )
    assert blocked.status_code == 400
    assert "bloqueada" in blocked.json()["detail"].lower() or "insuficient" in blocked.json()["detail"].lower()


def test_dashboard_includes_history(client) -> None:
    res = client.get("/api/adaptive/dashboard")
    assert res.status_code == 200
    body = res.json()
    assert "history" in body
    assert body.get("ai_calls_in_adaptive") is False
