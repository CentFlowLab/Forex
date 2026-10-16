"""Clearly labeled synthetic EUR/USD series for interface and engine testing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from app.market.labels import SYNTHETIC_LABEL

TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}
SYNTHETIC_SOURCE = "synthetic"


def generate_synthetic_candles(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    bars: int = 800,
    start: datetime | None = None,
    seed: int = 42,
    start_price: float = 1.08500,
) -> list[dict]:
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    rng = np.random.default_rng(seed)
    minutes = TIMEFRAME_MINUTES[timeframe]
    if start is None:
        start = datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc)

    price = start_price
    candles: list[dict] = []
    for i in range(bars):
        ts = start + timedelta(minutes=minutes * i)
        # Mild drift + mean reversion + occasional shock. Deterministic via seed.
        drift = 0.000002 * np.sin(i / 35.0)
        shock = float(rng.normal(0, 0.00028))
        if i % 97 == 0:
            shock += float(rng.choice([-1, 1]) * 0.0012)
        open_px = price
        close_px = max(1.02, min(1.18, open_px + drift + shock))
        wick_high = abs(float(rng.normal(0, 0.00018)))
        wick_low = abs(float(rng.normal(0, 0.00018)))
        high = max(open_px, close_px) + wick_high
        low = min(open_px, close_px) - wick_low
        volume = float(max(10.0, rng.lognormal(5.2, 0.35)))
        candles.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "ts": ts,
                "open": round(float(open_px), 5),
                "high": round(float(high), 5),
                "low": round(float(low), 5),
                "close": round(float(close_px), 5),
                "volume": round(volume, 2),
                "source": SYNTHETIC_SOURCE,
                "is_synthetic": True,
            }
        )
        price = close_px
    return candles
