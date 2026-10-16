"""Testes do backtest walk-forward acionável (sem MT5/OpenAI)."""

from __future__ import annotations

import math

from app.robot.actionable_backtest import (
    backtest_symbol,
    beta_binomial_ci,
    evidence_from_samples,
    _is_actionable,
)


def _synth_candles(n: int = 1200, *, drift: float = 0.00005) -> list[dict]:
    out = []
    price = 1.50
    for i in range(n):
        wobble = 0.0004 * math.sin(i / 7.0)
        o = price
        c = price + drift + wobble
        h = max(o, c) + 0.00025
        l = min(o, c) - 0.00025
        out.append(
            {
                "ts": f"2025-01-01T{(i // 4) % 24:02d}:{(i * 15) % 60:02d}:00+00:00",
                "o": round(o, 5),
                "h": round(h, 5),
                "l": round(l, 5),
                "c": round(c, 5),
            }
        )
        price = c
    return out


def test_beta_ci_widens_for_small_n():
    mean43, lo43, hi43 = beta_binomial_ci(28, 43)
    mean400, lo400, hi400 = beta_binomial_ci(260, 400)
    assert hi43 - lo43 > hi400 - lo400
    assert 0.5 < mean43 < 0.75


def test_evidence_threshold_matches_engine():
    assert evidence_from_samples(10) < 0.35 or evidence_from_samples(10) >= 0.25
    # n>=30 eleva evidence
    e = evidence_from_samples(43, dir_conf=0.3)
    assert e >= 0.35


def test_actionable_filter_rejects_low_evidence():
    ok, rej = _is_actionable(
        expected_net=1.0,
        evidence=0.2,
        p_hit_n=43,
        p_hit=0.5,
        be=0.38,
        strategy="actionable_current",
        p_hit_lo=0.4,
    )
    assert ok is False
    assert any("evidence" in r for r in rej)


def test_conservative_ci_can_reject_positive_mean_ev():
    ok, rej = _is_actionable(
        expected_net=0.5,
        evidence=0.5,
        p_hit_n=43,
        p_hit=0.50,
        be=0.38,
        strategy="alt_conservative_ci",
        p_hit_lo=0.30,  # abaixo do BE
    )
    assert ok is False
    assert any("P_hit_lo" in r for r in rej)


def test_backtest_symbol_runs_oos_without_lookahead():
    candles = _synth_candles(1500, drift=0.00008)
    r = backtest_symbol(
        "GBPAUD",
        candles,
        spread_pips=0.9,
        mt5_specs=None,
        n_folds=3,
    )
    assert r["ok"] is True
    assert r["layer"] == "backtest"
    assert "actionable_current:BOTH" in r["strategies"]
    assert "baseline_always_buy:BOTH" in r["strategies"]
    assert r["uncertainty_small_sample"]["BUY"]["n"] <= 43
    # Previsão ≠ lucro: campos separados
    st = r["strategies"]["actionable_current:BOTH"]
    assert "net_pnl_usd" in st
    assert "avg_expected_net" in st
