"""Custos de negociação simulada (spread, slippage, comissão)."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.risk.instruments import get_instrument


@dataclass(frozen=True)
class TradeCosts:
    spread_pips: float
    slippage_pips: float
    commission: float
    spread_cash: float
    slippage_cash: float
    total: float

    def as_dict(self) -> dict:
        return {
            "spread_pips": self.spread_pips,
            "slippage_pips": self.slippage_pips,
            "commission": self.commission,
            "spread_cash": self.spread_cash,
            "slippage_cash": self.slippage_cash,
            "total": self.total,
        }


def estimate_round_trip_costs(
    symbol: str = "EURUSD",
    lots: float = 0.10,
    *,
    spread_pips: float | None = None,
    slippage_pips: float | None = None,
    commission_per_lot: float | None = None,
) -> TradeCosts:
    settings = get_settings()
    spec = get_instrument(symbol)
    sp = spread_pips if spread_pips is not None else settings.sim_spread_pips
    sl = slippage_pips if slippage_pips is not None else settings.sim_slippage_pips
    comm_rate = commission_per_lot if commission_per_lot is not None else settings.sim_commission_per_lot
    pip_value = spec.pip_value_account(lots, settings.account_currency)
    # Spread cobrado na entrada (ida); slippage ida+volta aproximado.
    spread_cash = sp * pip_value
    slippage_cash = sl * 2.0 * pip_value
    commission = comm_rate * lots
    total = spread_cash + slippage_cash + commission
    return TradeCosts(
        spread_pips=sp,
        slippage_pips=sl,
        commission=round(commission, 4),
        spread_cash=round(spread_cash, 4),
        slippage_cash=round(slippage_cash, 4),
        total=round(total, 4),
    )


def apply_entry_price(side: str, mid: float, spread_pips: float, slippage_pips: float, symbol: str) -> float:
    spec = get_instrument(symbol)
    half = (spread_pips / 2.0) * spec.pip_size
    slip = slippage_pips * spec.pip_size
    if side.upper() == "BUY":
        return mid + half + slip
    return mid - half - slip
