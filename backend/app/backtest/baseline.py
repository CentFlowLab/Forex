"""Deterministic SMA-crossover baseline. No AI. Used for comparison only."""

from __future__ import annotations

from dataclasses import dataclass

from app.market.indicators import sma
from app.models.market import Candle
from app.risk.instruments import get_instrument


@dataclass
class BaselineTrade:
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    pnl: float
    pips: float


def sma_crossover_backtest(
    candles: list[Candle],
    symbol: str = "EURUSD",
    fast: int = 20,
    slow: int = 50,
    stop_pips: float = 15.0,
    take_pips: float = 25.0,
    spread_pips: float = 1.2,
    commission_per_lot: float = 7.0,
    lots: float = 0.10,
    account_currency: str = "USD",
) -> list[BaselineTrade]:
    """Walk forward bar-by-bar. Signal on close[i] is executed at open[i+1]."""
    if len(candles) < slow + 2:
        return []
    spec = get_instrument(symbol)
    closes = [c.close for c in candles]
    trades: list[BaselineTrade] = []
    position: dict | None = None

    for i in range(slow, len(candles) - 1):
        # Indicators use closes up to i inclusive — no look-ahead.
        fast_now = sma(closes[: i + 1], fast)
        slow_now = sma(closes[: i + 1], slow)
        fast_prev = sma(closes[:i], fast)
        slow_prev = sma(closes[:i], slow)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            continue

        if position:
            bar = candles[i + 1]
            stop = position["stop"]
            take = position["take"]
            hit = None
            if position["side"] == "BUY":
                if bar.low <= stop:
                    hit = ("SL", stop)
                elif bar.high >= take:
                    hit = ("TP", take)
            else:
                if bar.high >= stop:
                    hit = ("SL", stop)
                elif bar.low <= take:
                    hit = ("TP", take)
            if hit:
                exit_px = hit[1]
                pnl = spec.pnl(position["side"], position["entry"], exit_px, lots, account_currency)
                pnl -= commission_per_lot * lots
                trades.append(
                    BaselineTrade(
                        side=position["side"],
                        entry_index=position["index"],
                        exit_index=i + 1,
                        entry_price=position["entry"],
                        exit_price=exit_px,
                        pnl=pnl,
                        pips=spec.pips_from_price_delta(
                            (exit_px - position["entry"]) * (1 if position["side"] == "BUY" else -1)
                        ),
                    )
                )
                position = None

        if position:
            continue
        cross_up = fast_prev <= slow_prev and fast_now > slow_now
        cross_down = fast_prev >= slow_prev and fast_now < slow_now
        if not (cross_up or cross_down):
            continue
        # Execute next bar open + spread (realistic delay, no look-ahead).
        next_open = candles[i + 1].open
        half = (spread_pips / 2.0) * spec.pip_size
        if cross_up:
            entry = next_open + half
            side = "BUY"
            stop = entry - spec.price_from_pips(stop_pips)
            take = entry + spec.price_from_pips(take_pips)
        else:
            entry = next_open - half
            side = "SELL"
            stop = entry + spec.price_from_pips(stop_pips)
            take = entry - spec.price_from_pips(take_pips)
        position = {"side": side, "entry": entry, "stop": stop, "take": take, "index": i + 1}
    return trades
