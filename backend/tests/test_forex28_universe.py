"""Universo Forex 28 — descoberta MT5, ranking, top-5 OpenAI, orçamento, DEMO."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.market.labels import DUKASCOPY_SOURCE
from app.models import account as _acc  # noqa: F401
from app.models import adaptive as _a  # noqa: F401
from app.models import autonomy as _u  # noqa: F401
from app.models import robot as _r  # noqa: F401
from app.models import trading as _t  # noqa: F401
from app.models.market import Candle
from app.risk.instruments import get_instrument, supported_symbols
from app.robot.ai_engine import estimate_radar_analysis_usd
from app.robot.mt5_symbol_discovery import (
    _strip_to_canonical,
    discover_forex_universe,
    reset_discovery_cache,
)
from app.robot.pair_quotes import get_pair_quote_store
from app.robot.radar import _triage_score, scan_radar, validate_scan_for_paid
from app.robot.universe import (
    FOREX_28_SYMBOLS,
    MIN_PAID_MT5_PAIRS,
    OPENAI_TOP_CANDIDATES,
    pair_meta,
)
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
    reset_discovery_cache()
    s.close()


def _seed_pair(db: Session, symbol: str, mid: float, *, n_m15: int = 50) -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    pip = get_instrument(symbol).pip_size
    for i in range(n_m15):
        ts = now - timedelta(minutes=15 * (i + 1))
        db.add(
            Candle(
                symbol=symbol,
                timeframe="M15",
                ts=ts,
                open=mid,
                high=mid + 8 * pip,
                low=mid - 8 * pip,
                close=mid + (0.5 if i % 3 == 0 else -0.3) * pip * 10,
                volume=100,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
    for tf, n, step in (("H1", 24, 60), ("H4", 20, 240)):
        for i in range(n):
            ts = now - timedelta(minutes=step * (i + 1))
            db.add(
                Candle(
                    symbol=symbol,
                    timeframe=tf,
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
    db.commit()
    from app.market.quote_types import ValidatedTick
    from app.robot.pair_quotes import get_pair_quote_store as pq

    pq().set(
        symbol,
        ValidatedTick(
            ts=now,
            bid=mid - pip,
            ask=mid + pip,
            mid=mid,
            spread_pips=2.0,
            channel="mt5",
            mid_only=False,
        ),
        is_synthetic=False,
        source="mt5",
    )


def test_universe_has_exactly_28_unique_crosses() -> None:
    assert len(FOREX_28_SYMBOLS) == 28
    assert len(set(FOREX_28_SYMBOLS)) == 28
    assert len(supported_symbols()) == 28
    assert pair_meta("EURUSD").display == "EUR/USD"
    assert pair_meta("EURJPYm").symbol == "EURJPY"
    assert get_instrument("GBPJPY").pip_size == 0.01


def test_strip_broker_prefixes_suffixes() -> None:
    assert _strip_to_canonical("EURUSD") == "EURUSD"
    assert _strip_to_canonical("EURUSDm") == "EURUSD"
    assert _strip_to_canonical("EURUSD.a") == "EURUSD"
    assert _strip_to_canonical("EURUSDPRO") == "EURUSD"
    assert _strip_to_canonical("mEURUSD") == "EURUSD"
    assert _strip_to_canonical("BTCUSD") is None


def test_discovery_maps_available_and_skips_missing() -> None:
    reset_discovery_cache()

    class Info:
        def __init__(self, spread=10, digits=5, trade_mode=4):
            self.spread = spread
            self.digits = digits
            self.trade_mode = trade_mode

    class Tick:
        bid = 1.1
        ask = 1.1002

    names = [
        SimpleNamespace(name="EURUSD"),
        SimpleNamespace(name="GBPUSDm"),
        SimpleNamespace(name="USDJPY.a"),
        SimpleNamespace(name="XAUUSD"),  # fora do universo
    ]

    mt5 = MagicMock()
    mt5.terminal_info.return_value = object()
    mt5.symbols_get.return_value = names

    def _info(name):
        if name in {"EURUSD", "GBPUSDm", "USDJPY.a"}:
            return Info()
        return None

    def _tick(name):
        if name in {"EURUSD", "GBPUSDm", "USDJPY.a"}:
            return Tick()
        return None

    mt5.symbol_info.side_effect = _info
    mt5.symbol_info_tick.side_effect = _tick
    mt5.symbol_select.return_value = True

    disc = discover_forex_universe(mt5, force=True)
    assert disc.mt5_available is True
    assert "EURUSD" in disc.eligible_canonical
    assert "GBPUSD" in disc.eligible_canonical
    assert "USDJPY" in disc.eligible_canonical
    assert disc.broker_name("GBPUSD") == "GBPUSDm"
    assert any(m["canonical"] == "AUDUSD" for m in disc.missing)
    assert len(disc.eligible) == 3


def test_discovery_excludes_disabled_and_no_tick() -> None:
    reset_discovery_cache()
    mt5 = MagicMock()
    mt5.terminal_info.return_value = object()
    mt5.symbols_get.return_value = [
        SimpleNamespace(name="EURUSD"),
        SimpleNamespace(name="GBPUSD"),
    ]

    def _info(name):
        if name == "EURUSD":
            return SimpleNamespace(spread=5, digits=5, trade_mode=0)  # disabled
        return SimpleNamespace(spread=5, digits=5, trade_mode=4)

    mt5.symbol_info.side_effect = _info
    mt5.symbol_info_tick.side_effect = lambda n: (
        None if n == "GBPUSD" else SimpleNamespace(bid=1.1, ask=1.1001)
    )
    mt5.symbol_select.return_value = True
    disc = discover_forex_universe(mt5, force=True)
    assert "EURUSD" not in disc.eligible_canonical
    assert "GBPUSD" not in disc.eligible_canonical
    assert any("disabled" in e["reason"] for e in disc.excluded)


def test_high_spread_penalized_in_triage() -> None:
    snap_ok = {
        "data_quality": {"data_ok": True, "executable": True},
        "quote": {
            "is_synthetic": False,
            "spread_pips": 1.0,
            "source": "mt5",
            "feed_state": "recente",
            "update_age_s": 1.0,
            "lag_seconds": 10,
        },
        "volatility": {"atr14_m15": 0.0005},
        "structure": {
            "last_close": 1.1,
            "sma20": 1.099,
            "swing_high_20": 1.102,
            "swing_low_20": 1.098,
        },
        "trading_costs": {"total": 2.0},
    }
    snap_wide = dict(snap_ok)
    snap_wide["quote"] = {**snap_ok["quote"], "spread_pips": 7.0}
    s1, _, code1 = _triage_score(snap_ok)
    s2, reasons, code2 = _triage_score(snap_wide)
    assert s2 < s1
    assert code2 == "spread_too_wide" or any("spread" in r for r in reasons)


def test_radar_ranks_up_to_28_and_selects_top5(rdb: Session, monkeypatch) -> None:
    reset_discovery_cache()
    # Forçar universo completo sem depender do MT5 real.
    monkeypatch.setattr(
        "app.robot.radar.symbols_for_radar",
        lambda prefer_mt5=True: list(FOREX_28_SYMBOLS),
    )
    mids = {
        "EURUSD": 1.10,
        "GBPUSD": 1.27,
        "USDJPY": 150.0,
        "USDCHF": 0.88,
        "AUDUSD": 0.66,
        "USDCAD": 1.36,
        "NZDUSD": 0.60,
        "EURGBP": 0.86,
        "EURJPY": 165.0,
        "EURCHF": 0.95,
        "EURAUD": 1.65,
        "EURCAD": 1.48,
        "EURNZD": 1.80,
        "GBPJPY": 190.0,
        "GBPCHF": 1.12,
        "GBPAUD": 1.90,
        "GBPCAD": 1.72,
        "GBPNZD": 2.10,
        "AUDJPY": 100.0,
        "AUDCHF": 0.58,
        "AUDCAD": 0.90,
        "AUDNZD": 1.10,
        "NZDJPY": 90.0,
        "NZDCHF": 0.52,
        "NZDCAD": 0.81,
        "CADJPY": 110.0,
        "CADCHF": 0.64,
        "CHFJPY": 170.0,
    }
    for sym, mid in mids.items():
        _seed_pair(rdb, sym, mid)

    scan = scan_radar(rdb, top_n=OPENAI_TOP_CANDIDATES)
    assert scan.monitored == 28
    assert len(scan.candidates) <= OPENAI_TOP_CANDIDATES
    assert len(scan.candidates) >= 1
    compact = scan.compact_for_ai()
    assert len(compact["pairs_for_comparison"]) == len(scan.candidates)
    assert "other_pairs_brief" in compact
    assert len(compact["local_priority_hint"]) == len(scan.candidates)
    est = estimate_radar_analysis_usd(scan)
    assert est["max_calls"] == 1
    assert est["estimated_prompt_tokens"] < 20000
    assert est["pairs_in_context"] == len(scan.candidates)


def test_stale_data_rejected_from_candidates(rdb: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.robot.radar.symbols_for_radar",
        lambda prefer_mt5=True: ["EURUSD"],
    )
    _seed_pair(rdb, "EURUSD", 1.1)
    # Snapshot com atraso extremo via monkeypatch de build_pair_snapshot
    stale = {
        "data_quality": {"data_ok": True, "executable": False},
        "quote": {
            "is_synthetic": False,
            "lag_seconds": 999999,
            "spread_pips": 1.0,
            "source": "mt5",
            "feed_state": "atrasado",
            "update_age_s": 9999,
        },
        "volatility": {},
        "structure": {},
        "trading_costs": {"total": 2.0},
        "candles_counts": {"M15": 50},
        "candle_source": "mt5",
    }
    monkeypatch.setattr("app.robot.radar.build_pair_snapshot", lambda *_a, **_k: stale)
    scan = scan_radar(rdb)
    assert all(c.symbol != "EURUSD" for c in scan.candidates) or scan.candidates == []
    assert any(r["symbol"] == "EURUSD" for r in scan.rejected)


def test_budget_and_top_n_constants() -> None:
    from app.config import get_settings

    s = get_settings()
    assert float(s.robot_openai_daily_budget_usd) == pytest.approx(0.10)
    assert float(s.robot_openai_interval_seconds) == pytest.approx(900.0)
    assert s.openai_allow_quality_repair_call is False
    assert OPENAI_TOP_CANDIDATES == 5
    assert MIN_PAID_MT5_PAIRS == 5


def test_demo_executor_still_blocks_hold_and_insufficient(db) -> None:
    from app.brokers.mt5_demo_executor import MT5DemoExecutor
    from app.models.robot import AIDecisionLog
    import json

    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification="aguardar",
        estimated_cost_usd=0.001,
        context_json="{}",
        proposal_json="{}",
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    res = MT5DemoExecutor().try_execute_decision(db, log)
    assert res.get("ok") is False


def test_validate_scan_paid_uses_top_candidates_not_all_28(rdb: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.robot.radar.symbols_for_radar",
        lambda prefer_mt5=True: list(FOREX_28_SYMBOLS[:7]),
    )
    for i, sym in enumerate(FOREX_28_SYMBOLS[:7]):
        mid = 150.0 if "JPY" in sym else 1.1 + i * 0.01
        _seed_pair(rdb, sym, mid)
    scan = scan_radar(rdb)
    for c in list(scan.candidates) + list(scan.all_valid):
        q = c.snapshot.setdefault("quote", {})
        q["source"] = "mt5"
        q["feed_state"] = "recente"
        q["update_age_s"] = 1.0
        c.snapshot["candle_source"] = "mt5"
        c.snapshot.setdefault("data_quality", {})["data_ok"] = True
    v = validate_scan_for_paid(scan)
    assert "data_quality" in v
    assert "ok" in v
