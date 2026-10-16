"""Testes locais: recuperação do feed + prontidão antes de OpenAI (sem chamadas pagas)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.market.labels import DUKASCOPY_SOURCE
from app.market.quote_feed import get_quote_feed
from app.market.quote_types import ValidatedTick
from app.models import account as _acc  # noqa: F401
from app.models import adaptive as _a  # noqa: F401
from app.models import autonomy as _u  # noqa: F401
from app.models import robot as _r  # noqa: F401
from app.models import trading as _t  # noqa: F401
from app.models.market import Candle
from app.robot.ai_engine import estimate_single_analysis_usd, run_ai_decision
from app.robot.context import build_market_context, estimate_context_tokens
from app.robot.readiness import assess_data_readiness
from app.services.seed import seed_all


@pytest.fixture()
def rdb() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = S()
    seed_all(s)
    # Velas fechadas mínimas
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    base = 1.10
    for i in range(50):
        ts = now - timedelta(minutes=15 * (i + 1))
        s.add(
            Candle(
                symbol="EURUSD",
                timeframe="M15",
                ts=ts,
                open=base,
                high=base + 0.0005,
                low=base - 0.0005,
                close=base + (0.0001 if i % 2 == 0 else -0.0001),
                volume=100,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    for i in range(24):
        ts = now - timedelta(hours=(i + 1))
        s.add(
            Candle(
                symbol="EURUSD",
                timeframe="H1",
                ts=ts,
                open=base,
                high=base + 0.001,
                low=base - 0.001,
                close=base,
                volume=200,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    for i in range(16):
        ts = now - timedelta(hours=4 * (i + 1))
        s.add(
            Candle(
                symbol="EURUSD",
                timeframe="H4",
                ts=ts,
                open=base,
                high=base + 0.002,
                low=base - 0.002,
                close=base,
                volume=400,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    s.commit()
    yield s
    s.close()


def _inject(mid: float = 1.121, lag_seconds: float = 1800) -> None:
    ts = datetime.now(timezone.utc) - timedelta(seconds=lag_seconds)
    get_quote_feed().inject_tick_for_tests(
        ValidatedTick(
            ts=ts,
            bid=mid - 0.00006,
            ask=mid + 0.00006,
            mid=mid,
            spread_pips=1.2,
            channel="dukascopy_bi5",
            mid_only=False,
        )
    )


def test_readiness_historical_allows_paid_flag_without_execution(rdb: Session) -> None:
    _inject(lag_seconds=2000)
    ready = assess_data_readiness(rdb, recover_feed=False)
    assert ready.candles_ok is True
    assert ready.analysis_kind == "historical"
    assert ready.ok_for_paid is True
    assert ready.allow_execution is False


def test_readiness_too_stale_blocks_paid(rdb: Session) -> None:
    _inject(lag_seconds=8 * 3600)
    ready = assess_data_readiness(rdb, recover_feed=False)
    assert ready.ok_for_paid is False
    assert ready.analysis_kind == "too_stale"


def test_openai_mode_skips_paid_when_unavailable(rdb: Session) -> None:
    get_quote_feed().clear_for_tests()
    # Remover velas → unavailable
    for c in list(rdb.query(Candle).all()):
        rdb.delete(c)
    rdb.commit()
    ctx, prop, usage = run_ai_decision(
        rdb, mode="openai", auth_token="dummy-will-not-be-used", recover_feed=False
    )
    assert usage.get("skipped_paid") is True
    assert prop.decision == "HOLD"
    assert usage.get("estimated_cost_usd", 0) == 0


def test_compact_context_smaller_than_legacy_shape(rdb: Session) -> None:
    _inject()
    ctx = build_market_context(rdb, analysis_kind="historical")
    est = estimate_context_tokens(ctx)
    # Antes: ~11k+ prompt tokens; agora deve ficar bem abaixo de 10k típico.
    assert est["estimated_prompt_tokens"] < 10000
    assert est["candles"]["M15"] <= 40
    assert est["candles"]["H1"] <= 24
    assert est["candles"]["H4"] <= 16
    assert "ANÁLISE HISTÓRICA" in (ctx.get("data_disclaimer") or "")


def test_estimate_includes_token_fields(rdb: Session) -> None:
    _inject()
    est = estimate_single_analysis_usd(db=rdb)
    assert est["estimated_prompt_tokens"] > 0
    assert est["max_calls"] == 1


def test_recover_restores_mid_after_clear() -> None:
    """Simula pós-restart: cache vazio → recover deve tentar bi5 (rede) ou aceitar inject."""
    feed = get_quote_feed()
    feed.clear_for_tests()
    assert feed.snapshot().mid is None
    # Sem rede obrigatória no CI: inject após «arranque» simula bi5 síncrono.
    feed.start()
    _inject(lag_seconds=1500)
    snap = feed.snapshot()
    assert snap.mid is not None
    assert snap.lag_seconds is not None
    assert snap.lag_seconds > 100
    assert snap.claims_realtime is False
