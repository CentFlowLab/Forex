"""Resolução explícita da fonte de dados — evita mistura silenciosa sintético/real."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.market.labels import DUKASCOPY_SOURCE, MT5_SOURCE, label_for
from app.models.market import Candle


def count_bars(db: Session, symbol: str, timeframe: str, source: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.source == source,
            )
        )
        or 0
    )


def resolve_market_source(
    db: Session,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    requested: str | None = None,
) -> dict[str, Any]:
    """
    Resolve a fonte ativa.

    - requested explícito (dukascopy|mt5|synthetic|...) é respeitado
    - auto/None: prefere MT5 em memória se disponível; senão Dukascopy; senão synthetic
    Nunca mistura fontes na mesma análise.
    """
    req = (requested or "").strip().lower() or None
    if req in {"auto", "any", ""}:
        req = None

    duk = count_bars(db, symbol, timeframe, DUKASCOPY_SOURCE)
    syn = count_bars(db, symbol, timeframe, "synthetic")
    mt5_bars = 0
    try:
        from app.market.mt5_feed import get_mt5_feed

        mt5_bars = len(get_mt5_feed().candles(symbol, timeframe))
    except Exception:  # noqa: BLE001
        mt5_bars = 0

    chosen: str | None = None
    reason = ""
    if req:
        if req == MT5_SOURCE:
            chosen = MT5_SOURCE
            reason = (
                f"Fonte pedida 'mt5' com {mt5_bars} velas {timeframe} em memória."
                if mt5_bars > 0
                else "Fonte pedida 'mt5' sem velas em memória."
            )
        else:
            n = count_bars(db, symbol, timeframe, req)
            chosen = req
            if n > 0:
                reason = f"Fonte pedida '{req}' com {n} velas {timeframe}."
            else:
                reason = (
                    f"Fonte pedida '{req}' sem velas {timeframe}. "
                    "Não foi feita substituição automática para evitar mistura de fontes."
                )
    else:
        if mt5_bars >= 10:
            chosen = MT5_SOURCE
            reason = f"Auto: MT5 preferido ({mt5_bars} velas {timeframe} em memória)."
        elif duk > 0:
            chosen = DUKASCOPY_SOURCE
            reason = f"Auto: Dukascopy complementar ({duk} velas {timeframe})."
        elif syn > 0:
            chosen = "synthetic"
            reason = f"Auto: apenas dados sintéticos disponíveis ({syn} velas {timeframe})."
        else:
            row = db.scalar(
                select(Candle.source)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.ts.desc())
                .limit(1)
            )
            chosen = row
            reason = f"Auto: fonte residual '{chosen}'." if chosen else "Sem velas disponíveis."

    is_synthetic = chosen == "synthetic"
    if chosen == DUKASCOPY_SOURCE or chosen == MT5_SOURCE:
        is_synthetic = False

    return {
        "requested": requested,
        "resolved_source": chosen,
        "is_synthetic": is_synthetic,
        "label": label_for(chosen or "none", is_synthetic),
        "bars_dukascopy": duk,
        "bars_synthetic": syn,
        "bars_mt5": mt5_bars,
        "reason": reason,
        "mixed_forbidden": True,
    }
