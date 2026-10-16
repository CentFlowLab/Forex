"""Contexto de mercado estruturado para o motor IA — só dados fechados / verificados."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptive.costs import estimate_round_trip_costs
from app.adaptive.regimes import classify_regime
from app.autonomy.timeutil import iso_utc, utcnow
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.market.indicators import atr, sma
from app.market.labels import DUKASCOPY_SOURCE
from app.market.quote_feed import get_quote_feed
from app.models.market import Candle, EconomicEvent
from app.models.robot import AIDecisionLog
from app.models.trading import ClosedTrade, Position

# Contexto compacto — essencial para análise, sem inflar tokens.
_LIMITS = {"M15": 40, "H1": 24, "H4": 16}


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _closed_candles(
    db: Session,
    timeframe: str,
    *,
    limit: int,
    bar_minutes: int,
) -> list[dict[str, Any]]:
    """Devolve apenas velas cujo período já terminou (sem barra em formação)."""
    now = utcnow()
    rows = list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == "EURUSD",
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
            "o": round(c.open, 5),
            "h": round(c.high, 5),
            "l": round(c.low, 5),
            "c": round(c.close, 5),
        }
        for c in closed
    ]


def count_closed_candles(db: Session) -> tuple[dict[str, int], datetime | None]:
    """Contagens de velas fechadas + timestamp da última M15."""
    m15 = _closed_candles(db, "M15", limit=_LIMITS["M15"], bar_minutes=15)
    h1 = _closed_candles(db, "H1", limit=_LIMITS["H1"], bar_minutes=60)
    h4 = _closed_candles(db, "H4", limit=_LIMITS["H4"], bar_minutes=240)
    last_ts: datetime | None = None
    if m15 and m15[-1].get("ts"):
        try:
            last_ts = datetime.fromisoformat(str(m15[-1]["ts"]).replace("Z", "+00:00"))
        except ValueError:
            last_ts = None
    return {"M15": len(m15), "H1": len(h1), "H4": len(h4)}, last_ts


def build_market_context(
    db: Session,
    *,
    analysis_kind: str = "historical",
) -> dict[str, Any]:
    settings = get_settings()
    feed = get_quote_feed().snapshot()
    m15 = _closed_candles(db, "M15", limit=_LIMITS["M15"], bar_minutes=15)
    h1 = _closed_candles(db, "H1", limit=_LIMITS["H1"], bar_minutes=60)
    h4 = _closed_candles(db, "H4", limit=_LIMITS["H4"], bar_minutes=240)

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
        "sma20": round(sma20, 5) if sma20 else None,
        "sma50": round(sma50, 5) if sma50 else None,
        "above_sma20": bool(sma20 and closes and closes[-1] > sma20),
        "above_sma50": bool(sma50 and closes and closes[-1] > sma50),
    }

    costs = estimate_round_trip_costs("EURUSD", 0.10)
    broker = get_broker(db)
    if isinstance(broker, SimulatedBroker):
        broker.connect()
        broker._mark_to_market()
    acct = broker.account()
    positions = list(db.scalars(select(Position).limit(5)))
    recent_closed = list(
        db.scalars(select(ClosedTrade).order_by(ClosedTrade.id.desc()).limit(5))
    )
    recent_ai = list(
        db.scalars(select(AIDecisionLog).order_by(AIDecisionLog.id.desc()).limit(3))
    )

    news_cutoff = utcnow() - timedelta(hours=48)
    events = list(
        db.scalars(
            select(EconomicEvent)
            .where(EconomicEvent.event_time >= news_cutoff)
            .order_by(EconomicEvent.event_time.desc())
            .limit(5)
        )
    )
    news = [
        {
            "ts": iso_utc(e.event_time),
            "title": e.title,
            "impact": e.impact or "",
            "currency": e.currency or "",
        }
        for e in events
        if e.title
    ]

    is_historical = analysis_kind in {"historical", "too_stale"} or (
        not feed.claims_realtime and (feed.lag_seconds or 0) > 120
    )

    return {
        "generated_at": iso_utc(utcnow()),
        "symbol": "EURUSD",
        "analysis_kind": analysis_kind,
        "data_disclaimer": (
            "ANÁLISE HISTÓRICA / COM ATRASO — cotações não são tempo real; "
            "não executar operações com base neste contexto."
            if is_historical
            else "Contexto de mercado para decisão livre COMPRAR/VENDER/AGUARDAR."
        ),
        "quote": {
            "bid": feed.bid,
            "ask": feed.ask,
            "mid": feed.mid,
            "spread_pips": feed.spread_pips,
            "ts": iso_utc(feed.quote_ts) if feed.quote_ts else None,
            "lag_seconds": feed.lag_seconds,
            "status": feed.status,
            "status_label": feed.status_label,
            "channel": feed.channel,
            "data_class": feed.data_class,
            "claims_realtime": feed.claims_realtime,
            "block_entries": feed.block_entries,
            "is_synthetic": feed.is_synthetic,
        },
        "candles_closed": {"M15": m15, "H1": h1, "H4": h4},
        "structure": structure,
        "volatility": {
            "atr14_m15": round(atr_now, 6) if atr_now else None,
            "regime": regime.label if regime else "desconhecido",
            "regime_label_pt": regime.label_pt if regime else "Desconhecido",
        },
        "trading_costs": {
            "lots_reference": 0.10,
            "spread_pips": costs.spread_pips,
            "slippage_pips": costs.slippage_pips,
            "commission": costs.commission,
            "total_round_trip_usd": costs.total,
        },
        "news_verified": news,
        "news_note": (
            "Eventos locais <48h — não inventar."
            if news
            else "Sem notícias verificadas atuais — não inventar."
        ),
        "account": {
            "balance": round(acct.balance, 2),
            "equity": round(acct.equity, 2),
            "currency": acct.currency,
            "is_simulated": True,
        },
        "open_positions": [
            {
                "id": p.id,
                "side": p.side,
                "entry": p.entry_price,
                "stop": p.stop_loss,
                "take": p.take_profit,
                "lots": p.volume_lots,
            }
            for p in positions
        ],
        "recent_closed_trades": [
            {"id": t.id, "side": t.side, "pnl": round(t.pnl, 2)}
            for t in recent_closed
        ],
        "recent_ai_decisions": [
            {
                "decision": d.decision,
                "executed": d.executed,
                "summary": (d.justification or "")[:120],
            }
            for d in recent_ai
        ],
        "constraints": {
            "live_trading_locked": True,
            "execution_allowed": False if is_historical else None,
            "instruction": (
                "Decidir COMPRAR, VENDER ou AGUARDAR livremente com base apenas nestes dados. "
                "Não inventar cotações, notícias ou velas. Não usar informação futura. "
                "Se os dados forem históricos/atrasados, preferir AGUARDAR para execução."
            ),
        },
    }


def compact_context_for_ai(context: dict[str, Any]) -> dict[str, Any]:
    """Reduz ruído mantendo o essencial para a decisão."""
    candles = context.get("candles_closed") or {}
    return {
        "generated_at": context.get("generated_at"),
        "symbol": context.get("symbol"),
        "analysis_kind": context.get("analysis_kind"),
        "data_disclaimer": context.get("data_disclaimer"),
        "quote": context.get("quote"),
        "structure": context.get("structure"),
        "volatility": context.get("volatility"),
        "trading_costs": context.get("trading_costs"),
        "candles_closed": {
            "M15": candles.get("M15") or [],
            "H1": candles.get("H1") or [],
            "H4": candles.get("H4") or [],
        },
        "news_note": context.get("news_note"),
        "news_verified": context.get("news_verified") or [],
        "account": context.get("account"),
        "open_positions": context.get("open_positions") or [],
        "recent_closed_trades": context.get("recent_closed_trades") or [],
        "recent_ai_decisions": context.get("recent_ai_decisions") or [],
        "constraints": context.get("constraints"),
    }


def estimate_context_tokens(context: dict[str, Any]) -> dict[str, Any]:
    """Estimativa local de tokens (≈ chars/4) para o contexto compacto."""
    compact = compact_context_for_ai(context)
    payload = json.dumps(compact, ensure_ascii=False, default=str)
    # Overhead do system prompt + schema ≈ 400–600 tokens.
    context_tokens = max(1, len(payload) // 4)
    system_overhead = 500
    max_completion = min(1200, int(get_settings().ai_max_tokens))
    total_prompt = context_tokens + system_overhead
    from app.ai.costs import estimate_cost

    model = get_settings().openai_default_model
    cost = estimate_cost(model, total_prompt, max_completion)
    return {
        "context_chars": len(payload),
        "estimated_prompt_tokens": total_prompt,
        "estimated_completion_tokens_cap": max_completion,
        "estimated_total_tokens_cap": total_prompt + max_completion,
        "estimated_cost_usd": round(cost, 6),
        "model": model,
        "candles": {
            tf: len((compact.get("candles_closed") or {}).get(tf) or [])
            for tf in ("M15", "H1", "H4")
        },
        "note": "Estimativa local (chars/4). Não é faturação OpenAI. Sem chamada paga.",
    }
