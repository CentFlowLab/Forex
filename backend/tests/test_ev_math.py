"""Testes numéricos da fórmula EV e barreiras."""

from __future__ import annotations

from app.robot.directional_audit import _numeric_ev_self_checks
from app.robot.ev_math import (
    breakeven_p_hit,
    compute_ev,
    pip_value_from_mt5_specs,
    simulate_barrier_hit,
)


def test_numeric_self_checks_pass():
    checks = _numeric_ev_self_checks()
    failed = [c for c in checks if not c.get("ok")]
    assert not failed, failed


def test_breakeven_formula():
    assert abs(breakeven_p_hit(10, 16) - 10 / 26) < 1e-9


def test_barrier_buy_tp_first():
    # high reaches TP before low reaches SL
    out = simulate_barrier_hit(
        side="BUY",
        entry=1.10,
        stop_loss=1.09,
        take_profit=1.12,
        future_highs=[1.105, 1.121],
        future_lows=[1.098, 1.100],
    )
    assert out == "tp"


def test_barrier_buy_sl_conservative_same_bar():
    out = simulate_barrier_hit(
        side="BUY",
        entry=1.10,
        stop_loss=1.09,
        take_profit=1.12,
        future_highs=[1.13],
        future_lows=[1.08],
    )
    assert out == "sl"


def test_jpy_pip_from_mt5_ticks():
    pv = pip_value_from_mt5_specs(
        trade_tick_size=0.001,
        trade_tick_value=0.67,
        pip_size=0.01,
        lots=0.10,
    )
    assert pv is not None
    assert 0.6 < pv < 0.75


def test_ev_negative_when_p_hit_below_be_after_costs():
    be = breakeven_p_hit(10, 16)
    ev = compute_ev(
        symbol="EURUSD",
        side="BUY",
        mid=1.1,
        atr_price=0.001,
        p_hit_tp_before_sl=be - 0.05,
        p_hit_source="test",
        p_direction=0.6,
        spread_pips=1.2,
    )
    assert ev.expected_net_usd < 0
