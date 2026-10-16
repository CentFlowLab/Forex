"""Validação de ticks de mercado — nunca substitui por dados sintéticos."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.market.quote_types import FeedChannel, ValidatedTick
from app.risk.instruments import get_instrument


class TickValidationError(ValueError):
    """Tick inválido ou incompleto."""


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def validate_tick(
    *,
    ts: datetime,
    bid: float | None,
    ask: float | None,
    mid: float | None = None,
    channel: FeedChannel,
    volume: float | None = None,
    symbol: str = "EURUSD",
    max_future_skew_seconds: float = 5.0,
    max_age_seconds: float | None = None,
    now: datetime | None = None,
) -> ValidatedTick:
    """Valida e normaliza um tick. Falha em vez de fabricar preços."""
    if channel == "none":
        raise TickValidationError("Canal de cotação em falta.")
    ts = _aware(ts)
    now = _aware(now or datetime.now(timezone.utc))
    age = (now - ts).total_seconds()
    if age < -max_future_skew_seconds:
        raise TickValidationError(f"Timestamp no futuro ({age:.1f}s).")
    if max_age_seconds is not None and age > max_age_seconds:
        raise TickValidationError(f"Timestamp demasiado antigo ({age:.0f}s).")

    mid_only = False
    if bid is None and ask is None and mid is not None:
        mid_only = True
        bid = float(mid)
        ask = float(mid)
    if bid is None or ask is None:
        raise TickValidationError("Bid/ask em falta.")

    bid_f = float(bid)
    ask_f = float(ask)
    if bid_f <= 0 or ask_f <= 0:
        raise TickValidationError("Preços devem ser positivos.")
    if ask_f < bid_f:
        raise TickValidationError("Ask não pode ser inferior ao bid.")
    # Sanity por instrumento (JPY ≠ EURUSD).
    key = symbol.replace("/", "").upper()
    try:
        from app.robot.universe import pair_meta

        meta = pair_meta(key)
        lo, hi = meta.price_min, meta.price_max
    except Exception:  # noqa: BLE001
        lo, hi = 0.5, 2.5
    if bid_f < lo or ask_f > hi:
        raise TickValidationError(
            f"Preço fora do intervalo plausível {key}: {bid_f}/{ask_f} (esperado {lo}-{hi})"
        )

    mid_f = (bid_f + ask_f) / 2.0 if mid is None else float(mid)
    if mid_f <= 0:
        raise TickValidationError("Mid inválido.")

    spread_pips: float | None
    if mid_only or abs(ask_f - bid_f) < 1e-12:
        spread_pips = None
    else:
        pip = get_instrument(symbol).pip_size
        spread_pips = round((ask_f - bid_f) / pip, 2)
        if spread_pips < 0 or spread_pips > 50:
            raise TickValidationError(f"Spread implausível: {spread_pips} pips")

    return ValidatedTick(
        ts=ts,
        bid=bid_f,
        ask=ask_f,
        mid=mid_f,
        spread_pips=spread_pips,
        channel=channel,
        mid_only=mid_only,
        volume=volume,
    )


def is_duplicate_tick(prev: ValidatedTick | None, nxt: ValidatedTick, *, price_eps: float = 1e-8) -> bool:
    if prev is None:
        return False
    if prev.ts != nxt.ts:
        return False
    return (
        abs(prev.bid - nxt.bid) < price_eps
        and abs(prev.ask - nxt.ask) < price_eps
        and abs(prev.mid - nxt.mid) < price_eps
    )


def classify_status(
    lag_seconds: float | None,
    *,
    connected: bool,
    fresh_seconds: float,
    delayed_seconds: float,
) -> str:
    if not connected or lag_seconds is None:
        return "desligado"
    if lag_seconds <= fresh_seconds:
        return "atualizado"
    if lag_seconds <= delayed_seconds:
        return "com_atraso"
    return "obsoleto"


def assert_not_synthetic(payload: dict[str, Any]) -> None:
    if payload.get("is_synthetic") is True:
        raise TickValidationError("Substituição sintética não permitida no feed de cotações.")
