"""Universo Forex do radar — até 28 cruzamentos únicos (G8 currencies)."""

from __future__ import annotations

from dataclasses import dataclass

# Oito moedas → C(8,2)=28 cruzamentos únicos (convenção base/quote de mercado).
FOREX_CURRENCIES: tuple[str, ...] = (
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "AUD",
    "CAD",
    "NZD",
)

# Ordem estável: majors primeiro, depois crosses.
_PAIR_BASE_QUOTE: tuple[tuple[str, str], ...] = (
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "JPY"),
    ("USD", "CHF"),
    ("AUD", "USD"),
    ("USD", "CAD"),
    ("NZD", "USD"),
    ("EUR", "GBP"),
    ("EUR", "JPY"),
    ("EUR", "CHF"),
    ("EUR", "AUD"),
    ("EUR", "CAD"),
    ("EUR", "NZD"),
    ("GBP", "JPY"),
    ("GBP", "CHF"),
    ("GBP", "AUD"),
    ("GBP", "CAD"),
    ("GBP", "NZD"),
    ("AUD", "JPY"),
    ("AUD", "CHF"),
    ("AUD", "CAD"),
    ("AUD", "NZD"),
    ("NZD", "JPY"),
    ("NZD", "CHF"),
    ("NZD", "CAD"),
    ("CAD", "JPY"),
    ("CAD", "CHF"),
    ("CHF", "JPY"),
)

assert len(_PAIR_BASE_QUOTE) == 28, len(_PAIR_BASE_QUOTE)


@dataclass(frozen=True)
class PairMeta:
    symbol: str
    display: str
    base: str
    quote: str
    # Escala Dukascopy bi5 (POINT).
    bi5_point: int
    # Intervalo plausível de preço (bid/ask).
    price_min: float
    price_max: float
    # Spread típico simulado (pips) — conservador.
    default_spread_pips: float
    # Dados fiáveis modelados neste build?
    costs_modeled: bool = True


def _price_bounds(base: str, quote: str) -> tuple[float, float, int, float]:
    """(price_min, price_max, bi5_point, default_spread_pips)."""
    jpy = base == "JPY" or quote == "JPY"
    if jpy:
        return 20.0, 300.0, 1_000, 1.8
    # Pares próximos da paridade (EURGBP, AUDNZD, …)
    if {base, quote} <= {"EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "USD"}:
        if "GBP" in (base, quote) and "JPY" not in (base, quote):
            return 0.3, 3.5, 100_000, 1.6
        return 0.2, 3.5, 100_000, 1.5
    return 0.2, 3.5, 100_000, 1.6


def _build_pairs() -> tuple[PairMeta, ...]:
    out: list[PairMeta] = []
    for base, quote in _PAIR_BASE_QUOTE:
        sym = f"{base}{quote}"
        pmin, pmax, bi5, spr = _price_bounds(base, quote)
        out.append(
            PairMeta(
                symbol=sym,
                display=f"{base}/{quote}",
                base=base,
                quote=quote,
                bi5_point=bi5,
                price_min=pmin,
                price_max=pmax,
                default_spread_pips=spr,
                costs_modeled=True,
            )
        )
    return tuple(out)


FOREX_28_PAIRS: tuple[PairMeta, ...] = _build_pairs()
FOREX_28_SYMBOLS: tuple[str, ...] = tuple(p.symbol for p in FOREX_28_PAIRS)

# Alias históricos — majors continuam os primeiros 7.
MAJOR_PAIRS: tuple[PairMeta, ...] = FOREX_28_PAIRS[:7]
SYMBOLS: tuple[str, ...] = FOREX_28_SYMBOLS
META_BY_SYMBOL: dict[str, PairMeta] = {p.symbol: p for p in FOREX_28_PAIRS}

# Top-N enviado à OpenAI com detalhe completo.
OPENAI_TOP_CANDIDATES = 5
# Mínimo de pares MT5 válidos para autorizar análise paga.
MIN_PAID_MT5_PAIRS = 5


def normalize_symbol_key(symbol: str) -> str:
    return (
        (symbol or "")
        .replace("/", "")
        .replace(".", "")
        .replace("_", "")
        .replace("-", "")
        .upper()
    )


def pair_meta(symbol: str) -> PairMeta:
    key = normalize_symbol_key(symbol)
    if key in META_BY_SYMBOL:
        return META_BY_SYMBOL[key]
    # Prefixo/sufixo de broker: EURUSDm, mEURUSD, EURUSDPRO, …
    for canon, meta in META_BY_SYMBOL.items():
        if key.startswith(canon) or key.endswith(canon) or canon in key:
            # Exigir que as 6 letras do par apareçam de forma contígua.
            if canon in key:
                return meta
    raise ValueError(f"Par fora do universo do radar: {symbol}")


def canonical_symbol(symbol: str) -> str:
    return pair_meta(symbol).symbol


def all_pair_metas() -> tuple[PairMeta, ...]:
    return FOREX_28_PAIRS
