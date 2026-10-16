"""Testes locais do radar multimeda — sem chamadas OpenAI pagas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adaptive.costs import estimate_round_trip_costs
from app.database import Base
from app.market.labels import DUKASCOPY_SOURCE
from app.market.quote_types import ValidatedTick
from app.models import account as _acc  # noqa: F401
from app.models import adaptive as _a  # noqa: F401
from app.models import autonomy as _u  # noqa: F401
from app.models import robot as _r  # noqa: F401
from app.models import trading as _t  # noqa: F401
from app.models.market import Candle
from app.risk.correlation import correlation_conflict
from app.risk.instruments import get_instrument, supported_symbols
from app.robot.ai_engine import analyze_radar_simulated, estimate_radar_analysis_usd, run_radar_decision
from app.robot.ai_validate import validate_ai_proposal
from app.robot.pair_quotes import get_pair_quote_store
from app.robot.radar import scan_radar
from app.robot.universe import SYMBOLS
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
    yield s
    get_pair_quote_store().clear()
    s.close()


def _seed_pair(db: Session, symbol: str, mid: float, *, n_m15: int = 50) -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    pip = get_instrument(symbol).pip_size
    for i in range(n_m15):
        ts = now - timedelta(minutes=15 * (i + 1))
        drift = (0.5 if i % 3 == 0 else -0.3) * pip * 10
        db.add(
            Candle(
                symbol=symbol,
                timeframe="M15",
                ts=ts,
                open=mid,
                high=mid + 8 * pip,
                low=mid - 8 * pip,
                close=mid + drift,
                volume=100,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    for i in range(24):
        ts = now - timedelta(hours=i + 1)
        db.add(
            Candle(
                symbol=symbol,
                timeframe="H1",
                ts=ts,
                open=mid,
                high=mid + 15 * pip,
                low=mid - 15 * pip,
                close=mid,
                volume=200,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    for i in range(16):
        ts = now - timedelta(hours=4 * (i + 1))
        db.add(
            Candle(
                symbol=symbol,
                timeframe="H4",
                ts=ts,
                open=mid,
                high=mid + 30 * pip,
                low=mid - 30 * pip,
                close=mid,
                volume=400,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    db.commit()


def test_all_majors_have_instruments_and_costs() -> None:
    assert set(SYMBOLS).issubset(set(supported_symbols()))
    for sym in SYMBOLS:
        spec = get_instrument(sym)
        assert spec.pip_size > 0
        costs = estimate_round_trip_costs(sym, 0.10)
        assert costs.total > 0
    # JPY pip distinto
    assert get_instrument("USDJPY").pip_size == 0.01
    assert get_instrument("EURUSD").pip_size == 0.0001


def test_correlation_blocks_aligned_pairs() -> None:
    reasons = correlation_conflict("GBPUSD", ["EURUSD"])
    assert reasons
    assert not correlation_conflict("USDJPY", ["EURUSD"])


def test_radar_rejects_synthetic(rdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cotação sintética no store é rejeitada — MT5 do host é desligado neste teste."""
    _seed_pair(rdb, "GBPUSD", 1.27)

    class _NoMt5:
        def get_quote(self, _symbol: str):  # noqa: ANN001
            return None

        def candles(self, *_a, **_k):  # noqa: ANN001
            return []

    monkeypatch.setattr("app.robot.pair_snapshot.get_mt5_feed", lambda: _NoMt5())
    store = get_pair_quote_store()
    now = datetime.now(timezone.utc) - timedelta(seconds=400)
    store.set(
        "GBPUSD",
        ValidatedTick(
            ts=now,
            bid=1.2699,
            ask=1.2701,
            mid=1.27,
            spread_pips=2.0,
            channel="dukascopy_bi5",
            mid_only=False,
        ),
        is_synthetic=True,
        source="synthetic_test",
    )
    scan = scan_radar(rdb)
    syn = [r for r in scan.rejected if r["symbol"] == "GBPUSD"]
    assert syn
    assert "sintético" in syn[0]["reason"].lower() or syn[0].get("is_synthetic")


def test_radar_ranks_pairs_with_candles(rdb: Session) -> None:
    mids = {
        "EURUSD": 1.12,
        "GBPUSD": 1.27,
        "USDJPY": 150.2,
        "USDCHF": 0.88,
        "AUDUSD": 0.66,
        "USDCAD": 1.36,
        "NZDUSD": 0.61,
    }
    for sym, mid in mids.items():
        _seed_pair(rdb, sym, mid)
    scan = scan_radar(rdb)
    assert scan.monitored >= 7
    assert scan.with_valid_data >= 5
    # Candidatos são subset do universo (até 28)
    for c in scan.candidates:
        assert c.symbol in SYMBOLS
        assert c.score > 0
    assert len(scan.candidates) <= 5


def test_radar_decision_path_no_paid_and_no_hist_exec(rdb: Session) -> None:
    _seed_pair(rdb, "EURUSD", 1.12)
    _seed_pair(rdb, "GBPUSD", 1.28)
    _seed_pair(rdb, "USDJPY", 149.5)
    ctx, prop, scan, usage = run_radar_decision(rdb, mode="simulated")
    assert usage.get("estimated_cost_usd", 0) == 0
    assert "radar" in ctx
    assert prop.decision in {"BUY", "SELL", "HOLD"}
    # Validação: histórico não executa
    v = validate_ai_proposal(rdb, prop, ctx, allow_delayed=False)
    if prop.decision in {"BUY", "SELL"}:
        assert v.approved is False  # sem claims_realtime
    est = estimate_radar_analysis_usd(scan)
    assert est["max_calls"] == 1
    assert est["estimated_prompt_tokens"] > 0


def test_analyze_radar_compares_candidates(rdb: Session) -> None:
    for sym, mid in (("EURUSD", 1.1), ("GBPUSD", 1.25), ("AUDUSD", 0.65)):
        _seed_pair(rdb, sym, mid)
    scan = scan_radar(rdb)
    decision = analyze_radar_simulated(scan)
    assert decision.compared_symbols or decision.decision == "HOLD"
    if scan.candidates:
        assert decision.why_preferred
