from __future__ import annotations

from app.market.indicators import atr, compute_snapshot, rsi, sma


def test_sma_window() -> None:
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 5) is None


def test_rsi_bounds() -> None:
    up = [float(i) for i in range(1, 30)]
    down = [float(i) for i in range(30, 0, -1)]
    assert rsi(up) > 70
    assert rsi(down) < 30


def test_atr_positive() -> None:
    highs = [1.10 + i * 0.0002 for i in range(20)]
    lows = [h - 0.0008 for h in highs]
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    value = atr(highs, lows, closes)
    assert value is not None and value > 0


def test_snapshot_from_short_series() -> None:
    prices = [1.08 + i * 0.0001 for i in range(10)]
    snap = compute_snapshot(prices, [p + 0.0002 for p in prices], [p - 0.0002 for p in prices], prices)
    assert snap.bars == 10
    assert snap.rsi_14 is None
