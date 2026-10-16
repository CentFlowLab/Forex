"""
Adaptador de dados Forex para o pipeline multiagente.

Reutiliza a arquitetura de mercado existente (Dukascopy cache, sintético etiquetado).
Não usa Yahoo Finance para ticks Forex. Nunca fabrica velas em falta.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.market.data_store import market_bundle
from app.market.validation import TIMEFRAME_DELTA, ValidationReport, validate_series
from app.risk.instruments import get_instrument


SUPPORTED_TIMEFRAMES = ("M5", "M15", "H1", "H4")


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _freshness(
    market_ts: datetime | None,
    *,
    replay_mode: bool,
    environment: str,
    stale_seconds: int,
) -> tuple[str, bool, float | None]:
    """Devolve (etiqueta, is_stale, age_seconds)."""
    if market_ts is None:
        return "missing", True, None
    if replay_mode or environment == "BACKTEST":
        return "historical_point_in_time", False, None
    now = datetime.now(timezone.utc)
    age = (now - market_ts).total_seconds()
    if age > stale_seconds:
        return "stale", True, age
    # Dados com atraso / cache — nunca rotular como live.
    return "delayed_or_cached", False, age


def build_snapshot_id(symbol: str, timeframe: str, as_of: str, source: str, last_price: float | None) -> str:
    raw = f"{symbol}|{timeframe}|{as_of}|{source}|{last_price}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def forex_market_snapshot(
    db: Session,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    as_of: datetime | None = None,
    source: str | None = None,
    *,
    replay_mode: bool = False,
    bid: float | None = None,
    ask: float | None = None,
    spread_pips: float | None = None,
) -> dict[str, Any]:
    """
    Snapshot Forex verificado para agentes.

    Bloqueia novas entradas simuladas quando os dados estão em falta, inválidos
    ou obsoletos (exceto modo histórico/reprodução).
    """
    settings = get_settings()
    tf = timeframe.upper()
    if tf not in SUPPORTED_TIMEFRAMES:
        return {
            "ok": False,
            "block_entries": True,
            "reason": f"Intervalo temporal não suportado: {tf}. Utilize M5, M15, H1 ou H4.",
            "symbol": symbol,
            "timeframe": tf,
        }

    try:
        spec = get_instrument(symbol)
    except ValueError as exc:
        return {
            "ok": False,
            "block_entries": True,
            "reason": str(exc),
            "symbol": symbol,
            "timeframe": tf,
        }

    bundle = market_bundle(db, symbol, tf, as_of=as_of, source=source)
    last_ts = datetime.fromisoformat(bundle["as_of"]) if bundle.get("as_of") else None
    last_ts = _aware(last_ts)

    # Preferir cotação verificada do feed Dukascopy (se existir) para frescura/bloqueio.
    feed_snap = None
    if not replay_mode and settings.trading_environment != "BACKTEST":
        try:
            from app.market.quote_feed import get_quote_feed

            feed_snap = get_quote_feed().snapshot()
            if feed_snap.quote_ts is not None:
                last_ts = _aware(feed_snap.quote_ts)
            if bid is None and feed_snap.bid is not None:
                bid = feed_snap.bid
            if ask is None and feed_snap.ask is not None:
                ask = feed_snap.ask
            if spread_pips is None and feed_snap.spread_pips is not None:
                spread_pips = feed_snap.spread_pips
        except Exception:
            feed_snap = None

    if feed_snap is not None and feed_snap.quote_ts is not None:
        age = feed_snap.lag_seconds
        if feed_snap.status == "atualizado":
            freshness, is_stale = "delayed_or_cached", False
        elif feed_snap.status == "com_atraso":
            freshness, is_stale = "delayed_or_cached", False
        elif feed_snap.status == "obsoleto":
            freshness, is_stale = "stale", True
        else:
            freshness, is_stale = "missing", True
    else:
        freshness, is_stale, age = _freshness(
            last_ts,
            replay_mode=replay_mode,
            environment=settings.trading_environment,
            stale_seconds=settings.stale_data_seconds,
        )

    # Validação de qualidade da série primária (sem inventar gaps).
    primary_bars = bundle.get("bars", {}).get(tf) or []
    series_rows = []
    for b in primary_bars:
        series_rows.append(
            {
                "ts": datetime.fromtimestamp(b["time"], tz=timezone.utc),
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
            }
        )
    if not series_rows:
        validation = ValidationReport(
            valid=False,
            bars=0,
            notes=["Sem velas OHLC para o intervalo solicitado."],
        )
    else:
        _, validation = validate_series(series_rows, tf)
    validation_dict = validation.as_dict()
    validation_ok = bool(series_rows) and (
        bool(validation_dict.get("valid")) or int(validation_dict.get("bars") or 0) > 0
    )

    missing_price = bundle.get("last_price") is None and (
        feed_snap is None or feed_snap.mid is None
    )
    feed_blocks = bool(feed_snap.block_entries) if feed_snap is not None else False
    block_entries = bool(missing_price or is_stale or not validation_ok or feed_blocks)
    reasons: list[str] = []
    if missing_price:
        reasons.append("Preço de mercado em falta — novas entradas simuladas bloqueadas.")
    if is_stale or feed_blocks:
        reasons.append(
            f"Dados de mercado obsoletos ou em falta (frescura={freshness}"
            + (f", idade={age:.0f}s" if age is not None else "")
            + "). Não são dados em tempo real."
        )
        if feed_snap is not None and feed_snap.message:
            reasons.append(feed_snap.message)
    if not validation_ok:
        reasons.append("Qualidade dos dados OHLC insuficiente ou série vazia.")
    if validation_dict.get("missing_gaps"):
        reasons.append(
            f"Detetados {validation_dict['missing_gaps']} intervalos em falta — não foram fabricadas velas."
        )

    computed_spread = spread_pips
    if bid is not None and ask is not None and computed_spread is None:
        computed_spread = round((ask - bid) / spec.pip_size, 2)

    snapshot_id = build_snapshot_id(
        symbol,
        tf,
        bundle.get("as_of") or "",
        bundle.get("data_source") or "none",
        bundle.get("last_price"),
    )

    return {
        "ok": not missing_price,
        "block_entries": block_entries,
        "reasons": reasons,
        "reason": "; ".join(reasons) if reasons else "",
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "display": spec.display,
        "timeframe": tf,
        "as_of": bundle.get("as_of"),
        "last_bar_time": bundle.get("last_bar_time"),
        "last_price": (feed_snap.mid if feed_snap and feed_snap.mid is not None else bundle.get("last_price")),
        "bid": bid,
        "ask": ask,
        "spread_pips": computed_spread,
        "pip_size": spec.pip_size,
        "data_source": bundle.get("data_source"),
        "is_synthetic": bundle.get("is_synthetic"),
        "label": bundle.get("label"),
        "provenance": {
            "source": bundle.get("data_source"),
            "is_synthetic": bundle.get("is_synthetic"),
            "label": bundle.get("label"),
            "timestamps_utc": True,
            "live": False,
            "freshness": freshness,
            "quote_feed": feed_snap.as_dict() if feed_snap else None,
        },
        "freshness": freshness,
        "is_stale": is_stale,
        "age_seconds": age,
        "quote_feed": feed_snap.as_dict() if feed_snap else None,
        "validation": validation_dict,
        "indicators": bundle.get("indicators") or {},
        "primary_indicators": bundle.get("primary_indicators") or {},
        "bars_available": {k: len(v or []) for k, v in (bundle.get("bars") or {}).items()},
        "timeframe_delta_seconds": int(TIMEFRAME_DELTA[tf].total_seconds()),
        "instrument": spec.__dict__,
        "bundle": bundle,
    }
