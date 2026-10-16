"""Snapshot por par: velas fechadas, estrutura, volatilidade, custos, qualidade dos dados."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptive.costs import estimate_round_trip_costs
from app.adaptive.regimes import classify_regime
from app.autonomy.timeutil import iso_utc, utcnow
from app.market.indicators import atr, sma
from app.market.labels import DUKASCOPY_SOURCE
from app.market.mt5_feed import get_mt5_feed
from app.market.quote_feed import get_quote_feed
from app.models.market import Candle
from app.risk.instruments import get_instrument
from app.robot.pair_quotes import get_pair_quote_store
from app.robot.universe import pair_meta

# M15 ≥ 50 para SMA50; 120 melhora calibração direcional walk-forward (sem look-ahead).
_LIMITS = {"M15": 120, "H1": 60, "H4": 40}
_BAR_MIN = {"M15": 15, "H1": 60, "H4": 240}


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def closed_candles(
    db: Session,
    symbol: str,
    timeframe: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    key = symbol.replace("/", "").upper()
    bar_minutes = _BAR_MIN[timeframe]
    now = utcnow()
    rows = list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == key,
                Candle.timeframe == timeframe,
                Candle.source == DUKASCOPY_SOURCE,
                Candle.is_synthetic == False,  # noqa: E712
            )
            .order_by(Candle.ts.desc())
            .limit(limit + 5)
        )
    )
    closed: list[Candle] = []
    for c in rows:
        end = _aware(c.ts) + timedelta(minutes=bar_minutes)
        if end <= now:
            closed.append(c)
        if len(closed) >= limit:
            break
    closed.reverse()
    return [
        {
            "ts": iso_utc(c.ts),
            "o": round(c.open, 5 if key != "USDJPY" else 3),
            "h": round(c.high, 5 if key != "USDJPY" else 3),
            "l": round(c.low, 5 if key != "USDJPY" else 3),
            "c": round(c.close, 5 if key != "USDJPY" else 3),
        }
        for c in closed
    ]


def _candles_for_pair(db: Session, key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    """MT5 primeiro; Dukascopy bi5 como histórico complementar."""
    mt5 = get_mt5_feed()
    m15_m = mt5.candles(key, "M15")
    h1_m = mt5.candles(key, "H1")
    h4_m = mt5.candles(key, "H4")
    if len(m15_m) >= 20 and len(h1_m) >= 10 and len(h4_m) >= 5:
        return m15_m[-_LIMITS["M15"] :], h1_m[-_LIMITS["H1"] :], h4_m[-_LIMITS["H4"] :], "mt5"
    m15 = closed_candles(db, key, "M15", limit=_LIMITS["M15"])
    h1 = closed_candles(db, key, "H1", limit=_LIMITS["H1"])
    h4 = closed_candles(db, key, "H4", limit=_LIMITS["H4"])
    # Preferir MT5 parcial se existir; completar com Dukascopy.
    if m15_m:
        m15 = m15_m[-_LIMITS["M15"] :]
    if h1_m:
        h1 = h1_m[-_LIMITS["H1"] :]
    if h4_m:
        h4 = h4_m[-_LIMITS["H4"] :]
    source = "mt5" if (m15_m or h1_m or h4_m) else "dukascopy"
    return m15, h1, h4, source


def build_pair_snapshot(db: Session, symbol: str) -> dict[str, Any]:
    key = symbol.replace("/", "").upper()
    meta = pair_meta(key)
    spec = get_instrument(key)
    m15, h1, h4, candle_source = _candles_for_pair(db, key)

    closes = [x["c"] for x in m15]
    highs = [x["h"] for x in m15]
    lows = [x["l"] for x in m15]
    regime = classify_regime(closes, highs, lows) if len(closes) >= 30 else None
    atr_now = atr(highs, lows, closes, 14) if len(closes) >= 20 else None
    sma20 = sma(closes, 20) if len(closes) >= 20 else None
    sma50 = sma(closes, 50) if len(closes) >= 50 else None

    structure = {
        "last_close": closes[-1] if closes else None,
        "swing_high_20": max(highs[-20:]) if len(highs) >= 20 else None,
        "swing_low_20": min(lows[-20:]) if len(lows) >= 20 else None,
        "sma20": round(sma20, spec.digits) if sma20 else None,
        "sma50": round(sma50, spec.digits) if sma50 else None,
        "above_sma20": bool(sma20 and closes and closes[-1] > sma20),
        "above_sma50": bool(sma50 and closes and closes[-1] > sma50),
    }

    # Cotação: MT5 (principal) → cache par → Dukascopy EURUSD → vela fechada.
    quote: dict[str, Any] | None = None
    mt5_q = get_mt5_feed().get_quote(key)
    if mt5_q is not None:
        quote = mt5_q.as_dict()
        quote["spread_pips"] = quote.get("spread_pips") or meta.default_spread_pips
    if quote is None:
        cached = get_pair_quote_store().get(key)
        if cached is not None and cached.source == "mt5":
            quote = {
                "bid": cached.tick.bid,
                "ask": cached.tick.ask,
                "mid": cached.tick.mid,
                "spread_pips": cached.tick.spread_pips or meta.default_spread_pips,
                "ts": cached.tick.ts.isoformat(),
                "lag_seconds": cached.lag_seconds,
                "tick_time_lag_s": cached.tick_time_lag_s,
                "tick_msc_lag_s": cached.tick_msc_lag_s,
                "update_age_s": cached.update_age_s,
                "status": "desligado" if cached.feed_state == "ligacao_perdida" else (
                    "com_atraso" if cached.feed_state == "atrasado" else "atualizado"
                ),
                "feed_state": cached.feed_state,
                "channel": "mt5",
                "data_class": "mt5",
                "claims_realtime": cached.claims_realtime,
                "block_entries": cached.block_entries,
                "is_synthetic": False,
                "source": "mt5",
                "clock_anomaly": cached.clock_anomaly,
                "message": cached.message,
            }
        elif cached is not None:
            lag = cached.lag_seconds
            quote = {
                "bid": cached.tick.bid,
                "ask": cached.tick.ask,
                "mid": cached.tick.mid,
                "spread_pips": cached.tick.spread_pips or meta.default_spread_pips,
                "ts": cached.tick.ts.isoformat(),
                "lag_seconds": lag,
                "status": "com_atraso",
                "channel": cached.tick.channel,
                "data_class": "historico_horario_atrasado",
                "claims_realtime": False,
                "block_entries": cached.is_synthetic or lag > 7200,
                "is_synthetic": cached.is_synthetic,
                "source": cached.source,
            }
    if quote is None and key == "EURUSD":
        feed = get_quote_feed().snapshot()
        if feed.mid is not None:
            quote = {
                "bid": feed.bid,
                "ask": feed.ask,
                "mid": feed.mid,
                "spread_pips": feed.spread_pips or meta.default_spread_pips,
                "ts": iso_utc(feed.quote_ts) if feed.quote_ts else None,
                "lag_seconds": feed.lag_seconds,
                "status": feed.status,
                "channel": feed.channel,
                "data_class": feed.data_class,
                "claims_realtime": feed.claims_realtime,
                "block_entries": feed.block_entries,
                "is_synthetic": feed.is_synthetic,
                "source": feed.source_label,
            }
    if quote is None and closes:
        # Referência histórica da última vela fechada — NÃO é cotação executável.
        last_ts = m15[-1]["ts"]
        try:
            ts_dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            lag = (utcnow() - ts_dt).total_seconds()
        except ValueError:
            lag = 99999.0
        mid = float(closes[-1])
        half = meta.default_spread_pips / 2.0 * spec.pip_size
        quote = {
            "bid": mid - half,
            "ask": mid + half,
            "mid": mid,
            "spread_pips": meta.default_spread_pips,
            "ts": last_ts,
            "lag_seconds": lag,
            "status": "com_atraso",
            "channel": "candle_derived",
            "data_class": "derivado_vela_fechada",
            "claims_realtime": False,
            "block_entries": True,
            "is_synthetic": False,
            "source": "ultima_vela_m15_fechada",
        }
    if quote is None:
        quote = {
            "bid": None,
            "ask": None,
            "mid": None,
            "spread_pips": None,
            "ts": None,
            "lag_seconds": None,
            "status": "desligado",
            "channel": "none",
            "data_class": "indisponivel",
            "claims_realtime": False,
            "block_entries": True,
            "is_synthetic": False,
            "source": "nenhuma",
        }

    costs = estimate_round_trip_costs(
        key, 0.10, spread_pips=float(quote.get("spread_pips") or meta.default_spread_pips)
    )

    candles_ok = len(m15) >= 20 and len(h1) >= 10 and len(h4) >= 5
    quote_ok = quote.get("mid") is not None
    lag = quote.get("lag_seconds")
    data_ok = candles_ok and quote_ok

    return {
        "symbol": key,
        "display": meta.display,
        "quote": quote,
        "candles_closed": {"M15": m15, "H1": h1, "H4": h4},
        "candles_counts": {"M15": len(m15), "H1": len(h1), "H4": len(h4)},
        "candle_source": candle_source,
        "structure": structure,
        "volatility": {
            "atr14_m15": round(atr_now, 6) if atr_now else None,
            # Heurística local — NÃO é sinal nem veto de trading; a IA deve avaliar sozinha.
            "local_regime_hint": regime.label if regime else "desconhecido",
            "local_regime_hint_pt": regime.label_pt if regime else "Desconhecido",
            "trend_slope": regime.trend_slope if regime else None,
            "atr_ratio": regime.atr_ratio if regime else None,
            "adx_proxy": regime.adx_proxy if regime else None,
            "bars_used": regime.bars_used if regime else len(closes),
            # Alias legado (testes / painéis); mesmo valor que local_regime_hint.
            "regime": regime.label if regime else "desconhecido",
            "regime_label_pt": regime.label_pt if regime else "Desconhecido",
        },
        "trading_costs": costs.as_dict(),
        "instrument": {
            "pip_size": spec.pip_size,
            "digits": spec.digits,
            "quote_currency": spec.quote,
            "quote_to_account_fx": spec.quote_to_account_fx,
        },
        "data_quality": {
            "candles_ok": candles_ok,
            "quote_ok": quote_ok,
            "data_ok": data_ok,
            # Executável em simulação se MT5 recente (mesmo sem claims_realtime
            # quando o relógio do tick é inconsistente com UTC — sem correção fixa).
            "executable": bool(
                quote_ok
                and not quote.get("block_entries")
                and not quote.get("is_synthetic")
                and (
                    quote.get("claims_realtime")
                    or (
                        quote.get("source") == "mt5"
                        and quote.get("feed_state") == "recente"
                    )
                )
            ),
            "costs_modeled": meta.costs_modeled,
        },
    }
