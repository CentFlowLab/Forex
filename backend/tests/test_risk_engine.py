from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.risk.engine import ProposedTrade, RiskEngine
from app.risk.instruments import get_instrument


def _ok(**overrides) -> ProposedTrade:
    data = dict(
        symbol="EURUSD",
        side="BUY",
        entry_price=1.08500,
        stop_loss=1.08350,
        take_profit=1.08800,
        spread_pips=1.2,
        expected_slippage_pips=0.3,
        market_ts=datetime.now(timezone.utc),
        broker_connected=True,
        data_is_stale=False,
    )
    data.update(overrides)
    return ProposedTrade(**data)


def test_eurusd_pip_value_differs_from_usdjpy() -> None:
    eurusd = get_instrument("EURUSD")
    usdjpy = get_instrument("USDJPY")
    assert eurusd.pip_size != usdjpy.pip_size
    assert eurusd.pip_value_account(0.1) != usdjpy.pip_value_account(0.1)


def test_approves_conservative_setup(db) -> None:
    decision = RiskEngine(db).evaluate(_ok())
    assert decision.approved
    assert decision.lots >= 0.01
    assert decision.risk_pct <= 0.25 + 1e-6


def test_rejects_missing_stop(db) -> None:
    decision = RiskEngine(db).evaluate(_ok(stop_loss=0))
    assert not decision.approved
    assert any("stop-loss" in r.lower() for r in decision.reasons)


def test_rejects_stale_and_disconnected(db) -> None:
    stale = RiskEngine(db).evaluate(_ok(data_is_stale=True))
    disc = RiskEngine(db).evaluate(_ok(broker_connected=False))
    assert not stale.approved
    assert not disc.approved


def test_rejects_wide_spread(db) -> None:
    decision = RiskEngine(db).evaluate(_ok(spread_pips=9.0))
    assert not decision.approved


def test_rejects_hold_side(db) -> None:
    decision = RiskEngine(db).evaluate(_ok(side="HOLD"))
    assert not decision.approved


def test_kill_switch(db) -> None:
    from app.models.account import AppSetting

    db.add(AppSetting(key="kill_switch", value="true"))
    db.commit()
    decision = RiskEngine(db).evaluate(_ok())
    assert not decision.approved
    assert any("emergência" in r.lower() or "paragem" in r.lower() for r in decision.reasons)


def test_position_size_uses_instrument_spec(db) -> None:
    decision = RiskEngine(db).evaluate(_ok())
    spec = get_instrument("EURUSD")
    sl_pips = spec.pips_from_price_delta(1.08500 - 1.08350)
    assert sl_pips == pytest.approx(15.0, abs=1e-6)
    assert decision.approved
    assert decision.lots <= spec.max_lot
    assert decision.risk_pct <= 0.25 + 1e-6
