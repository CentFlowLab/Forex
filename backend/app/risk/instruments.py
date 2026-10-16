"""Instrument specifications. Never assume identical pip values across pairs."""

from __future__ import annotations

from dataclasses import dataclass

from app.robot.universe import FOREX_28_PAIRS, pair_meta


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    display: str
    base: str
    quote: str
    pip_size: float
    pip_value_per_standard_lot_quote: float
    contract_size: float
    min_lot: float
    lot_step: float
    max_lot: float
    quote_to_account_fx: float
    digits: int

    def pips_from_price_delta(self, delta: float) -> float:
        return delta / self.pip_size

    def price_from_pips(self, pips: float) -> float:
        return pips * self.pip_size

    def pip_value_account(self, lots: float, account_currency: str = "USD") -> float:
        """Cash value of 1 pip for the given lot size in account currency."""
        quote_value = self.pip_value_per_standard_lot_quote * lots
        if account_currency == self.quote:
            return quote_value
        return quote_value * self.quote_to_account_fx

    def pnl(self, side: str, entry: float, exit_price: float, lots: float, account_currency: str = "USD") -> float:
        direction = 1.0 if side.upper() == "BUY" else -1.0
        pips = self.pips_from_price_delta((exit_price - entry) * direction)
        return pips * self.pip_value_account(lots, account_currency)

    def normalize_lots(self, lots: float) -> float:
        steps = int(lots / self.lot_step)
        normalized = max(0.0, min(self.max_lot, steps * self.lot_step))
        return round(normalized, 2)


# Conversão aproximada quote→USD (simulação). LIVE deve sobrescrever via corretora.
_QUOTE_FX_USD: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.10,
    "GBP": 1.27,
    "JPY": 0.0067,
    "CHF": 1.12,
    "AUD": 0.66,
    "CAD": 0.74,
    "NZD": 0.60,
}


def _build_instruments() -> dict[str, InstrumentSpec]:
    out: dict[str, InstrumentSpec] = {}
    for meta in FOREX_28_PAIRS:
        jpy_quote = meta.quote == "JPY"
        pip_size = 0.01 if jpy_quote else 0.0001
        # Pip value em unidades da moeda quote por lote standard.
        pip_value = 1000.0 if jpy_quote else 10.0
        digits = 3 if jpy_quote else 5
        out[meta.symbol] = InstrumentSpec(
            symbol=meta.symbol,
            display=meta.display,
            base=meta.base,
            quote=meta.quote,
            pip_size=pip_size,
            pip_value_per_standard_lot_quote=pip_value,
            contract_size=100_000,
            min_lot=0.01,
            lot_step=0.01,
            max_lot=10.0,
            quote_to_account_fx=float(_QUOTE_FX_USD.get(meta.quote, 1.0)),
            digits=digits,
        )
    return out


INSTRUMENTS: dict[str, InstrumentSpec] = _build_instruments()


def get_instrument(symbol: str) -> InstrumentSpec:
    try:
        key = pair_meta(symbol).symbol
    except ValueError as exc:
        raise ValueError(f"Unsupported instrument: {symbol}") from exc
    if key not in INSTRUMENTS:
        raise ValueError(f"Unsupported instrument: {symbol}")
    return INSTRUMENTS[key]


def supported_symbols() -> list[str]:
    return sorted(INSTRUMENTS.keys())
