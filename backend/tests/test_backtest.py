from __future__ import annotations

from datetime import datetime, timezone

from app.backtest.baseline import sma_crossover_backtest
from app.backtest.engine import run_baseline_backtest
from app.market.synthetic import generate_synthetic_candles
from app.models.market import Candle


def test_baseline_no_lookahead_uses_next_bar(db) -> None:
    rows = generate_synthetic_candles(bars=200, timeframe="M15", seed=7)
    candles = [Candle(**row) for row in rows]
    trades = sma_crossover_backtest(candles, symbol="EURUSD")
    for t in trades:
        assert t.exit_index > t.entry_index
        assert t.entry_index >= 1


def test_walk_forward_backtest_persists(db) -> None:
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    end = datetime(2024, 6, 1, tzinfo=timezone.utc)
    run = run_baseline_backtest(db, "EURUSD", "M15", start, end, walk_forward=True)
    assert run.engine == "baseline"
    assert run.is_synthetic is True
    assert "SINTÉTIC" in run.notes.upper() or "SINTETIC" in run.notes.upper()
    assert run.in_sample_bars > 0
    assert run.out_of_sample_bars > 0


def test_synthetic_label() -> None:
    from app.market.synthetic import SYNTHETIC_LABEL

    assert "síntético" in SYNTHETIC_LABEL.lower() or "sintetico" in SYNTHETIC_LABEL.lower().replace("é", "e")
