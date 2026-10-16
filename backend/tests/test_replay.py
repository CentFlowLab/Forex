from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market.labels import DUKASCOPY_SOURCE
from app.models.market import Candle
from app.replay.engine import ReplayController


def _seed_dukascopy(db, bars: int = 80) -> tuple[datetime, datetime]:
    start = datetime(2025, 10, 13, 8, 0, tzinfo=timezone.utc)
    price = 1.085
    for i in range(bars):
        ts = start + timedelta(minutes=15 * i)
        o = price
        c = price + (0.0001 if i % 2 == 0 else -0.00008)
        db.add(
            Candle(
                symbol="EURUSD",
                timeframe="M15",
                ts=ts,
                open=o,
                high=max(o, c) + 0.0002,
                low=min(o, c) - 0.0002,
                close=c,
                volume=100,
                source=DUKASCOPY_SOURCE,
                is_synthetic=False,
            )
        )
        price = c
    db.commit()
    return start, start + timedelta(minutes=15 * (bars - 1))


def test_replay_rejects_synthetic(db) -> None:
    ctrl = ReplayController()
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="sintéticos|sinteticos|históricos|historicos"):
        ctrl.create_session(db, start, end, source="synthetic")


def test_replay_no_lookahead_visible_candles(db) -> None:
    start, end = _seed_dukascopy(db, 60)
    ctrl = ReplayController()
    session = ctrl.create_session(db, start, end, timeframe="M15", ai_every_n_bars=100, speed=50)
    assert session.is_synthetic is False
    assert session.bars_total == 60
    # Advance cursor manually and ensure status only exposes past bars
    session.cursor_index = 10
    session.cursor_ts = start + timedelta(minutes=15 * 10)
    db.commit()
    payload = ctrl.status_payload(db, session.id)
    assert len(payload["candles"]) == 11
    last_time = payload["candles"][-1]["time"]
    assert last_time == int((start + timedelta(minutes=15 * 10)).timestamp())
    # Baseline comparison present
    assert "baseline" in (session.report_json or "")


def test_market_bundle_respects_as_of_source(db) -> None:
    from app.market.data_store import market_bundle

    start, end = _seed_dukascopy(db, 50)
    mid = start + timedelta(minutes=15 * 20)
    bundle = market_bundle(db, "EURUSD", "M15", as_of=mid, source=DUKASCOPY_SOURCE)
    assert bundle["is_synthetic"] is False
    assert bundle["data_source"] == DUKASCOPY_SOURCE
    assert datetime.fromisoformat(bundle["as_of"]) <= mid
    for bar in bundle["bars"]["M15"]:
        assert bar["time"] <= int(mid.timestamp())
