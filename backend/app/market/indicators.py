"""Deterministic technical indicators. No AI involvement."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class IndicatorSnapshot:
    rsi_14: float | None
    sma_20: float | None
    sma_50: float | None
    ema_12: float | None
    ema_26: float | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    atr_14: float | None
    support: float | None
    resistance: float | None
    last_close: float | None
    volatility_regime: str
    bars: int
    extras: dict[str, float | None] = field(default_factory=dict)


def _to_array(values: list[float]) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return float(np.mean(_to_array(values)[-period:]))


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    arr = _to_array(values)
    alpha = 2.0 / (period + 1.0)
    result = float(arr[0])
    for price in arr[1:]:
        result = alpha * float(price) + (1.0 - alpha) * result
    return result


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    deltas = np.diff(_to_array(values))
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[-period:]))
    avg_loss = float(np.mean(losses[-period:]))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(highs) < period + 1 or len(highs) != len(lows) or len(highs) != len(closes):
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    return float(np.mean(trs[-period:]))


def macd(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(values) < 35:
        return None, None, None
    line = None
    signal = None
    hist = None
    ema12_series: list[float] = []
    ema26_series: list[float] = []
    for i in range(1, len(values) + 1):
        e12 = ema(values[:i], 12)
        e26 = ema(values[:i], 26)
        if e12 is not None:
            ema12_series.append(e12)
        if e26 is not None:
            ema26_series.append(e26)
    if len(ema12_series) < 26 or len(ema26_series) < 9:
        return None, None, None
    macd_series = [
        ema12_series[i + (len(ema12_series) - len(ema26_series))] - ema26_series[i]
        for i in range(len(ema26_series))
    ]
    line = macd_series[-1]
    signal = ema(macd_series, 9)
    if signal is None:
        return line, None, None
    hist = line - signal
    return float(line), float(signal), float(hist)


def swing_levels(highs: list[float], lows: list[float], lookback: int = 40) -> tuple[float | None, float | None]:
    if not highs or not lows:
        return None, None
    window_h = highs[-lookback:]
    window_l = lows[-lookback:]
    return float(min(window_l)), float(max(window_h))


def volatility_regime(atr_value: float | None, close: float | None) -> str:
    if atr_value is None or not close:
        return "unknown"
    pct = atr_value / close
    if pct < 0.0006:
        return "low"
    if pct > 0.0018:
        return "high"
    return "normal"


def compute_snapshot(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> IndicatorSnapshot:
    rsi_v = rsi(closes, 14)
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line, macd_sig, macd_hist = macd(closes)
    atr_v = atr(highs, lows, closes, 14)
    support, resistance = swing_levels(highs, lows)
    last = float(closes[-1]) if closes else None
    return IndicatorSnapshot(
        rsi_14=rsi_v,
        sma_20=sma20,
        sma_50=sma50,
        ema_12=ema12,
        ema_26=ema26,
        macd=macd_line,
        macd_signal=macd_sig,
        macd_histogram=macd_hist,
        atr_14=atr_v,
        support=support,
        resistance=resistance,
        last_close=last,
        volatility_regime=volatility_regime(atr_v, last),
        bars=len(closes),
    )
