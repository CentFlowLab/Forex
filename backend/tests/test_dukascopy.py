from __future__ import annotations

import lzma
import struct
from datetime import datetime, timezone

import pytest

from app.market.dukascopy import (
    DukascopyError,
    aggregate_ticks_to_candles,
    parse_bi5_ticks,
)
from app.market.validation import validate_series


def _make_bi5(hour_start: datetime, n: int = 120) -> bytes:
    """Build a tiny valid bi5 payload for unit tests (not market fabrication for trading)."""
    records = bytearray()
    for i in range(n):
        ms = i * 30_000  # every 30s
        mid = 1.08500 + (i % 10) * 0.00001
        ask = int(round((mid + 0.00005) * 100_000))
        bid = int(round((mid - 0.00005) * 100_000))
        records.extend(struct.pack(">IIIff", ms, ask, bid, 1.0, 1.0))
    return lzma.compress(bytes(records))


def test_parse_bi5_and_aggregate_m5() -> None:
    hour = datetime(2025, 10, 13, 12, tzinfo=timezone.utc)
    payload = _make_bi5(hour)
    ticks = parse_bi5_ticks(payload, hour)
    assert len(ticks) == 120
    assert ticks[0][1] < ticks[0][2]  # bid < ask
    candles = aggregate_ticks_to_candles(ticks, "M5")
    assert len(candles) >= 1
    assert all(c["source"] == "dukascopy" and c["is_synthetic"] is False for c in candles)
    cleaned, report = validate_series(candles, "M5")
    assert report.valid
    assert len(cleaned) == len(candles)


def test_reject_html_payload() -> None:
    hour = datetime(2025, 10, 13, 12, tzinfo=timezone.utc)
    with pytest.raises(DukascopyError):
        parse_bi5_ticks(b"<html>503</html>", hour)


def test_validation_rejects_bad_ohlc() -> None:
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
