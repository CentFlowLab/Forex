"""Candle persistence and snapshot assembly."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.market.indicators import IndicatorSnapshot, compute_snapshot
from app.market.labels import label_for
from app.market.synthetic import generate_synthetic_candles
from app.models.market import Candle


def seed_synthetic_if_empty(db: Session, symbol: str = "EURUSD") -> int:
    existing = db.scalar(select(Candle.id).where(Candle.symbol == symbol).limit(1))
    if existing:
        return 0
    inserted = 0
    for tf, bars, seed in (("M5", 1200, 11), ("M15", 800, 42), ("H1", 500, 77), ("H4", 300, 99)):
        for row in generate_synthetic_candles(symbol=symbol, timeframe=tf, bars=bars, seed=seed):
            db.add(Candle(**row))
            inserted += 1
    db.commit()
    return inserted


def candles_for(
    db: Session,
    symbol: str,
    timeframe: str,
    limit: int = 300,
    before: datetime | None = None,
    source: str | None = None,
) -> list[Candle]:
    stmt = select(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
    if before is not None:
        stmt = stmt.where(Candle.ts <= before)
    if source:
        stmt = stmt.where(Candle.source == source)
    stmt = stmt.order_by(Candle.ts.desc()).limit(limit)
    rows = list(db.scalars(stmt))
    rows.reverse()
    return rows


def latest_candle(db: Session, symbol: str, timeframe: str) -> Candle | None:
    stmt = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        .order_by(Candle.ts.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def serialize_candle(c: Candle) -> dict[str, Any]:
    return {
        "time": int(c.ts.replace(tzinfo=c.ts.tzinfo or timezone.utc).timestamp()),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
        "source": c.source,
        "is_synthetic": c.is_synthetic,
        "label": label_for(c.source, c.is_synthetic),
    }


def snapshot_from_candles(rows: list[Candle]) -> IndicatorSnapshot:
    return compute_snapshot(
        [c.open for c in rows],
        [c.high for c in rows],
        [c.low for c in rows],
        [c.close for c in rows],
    )


def market_bundle(
    db: Session,
    symbol: str = "EURUSD",
    primary_tf: str = "M15",
    as_of: datetime | None = None,
    lookback: int = 200,
    source: str | None = None,
) -> dict[str, Any]:
    from app.market.source_resolve import resolve_market_source

    tfs = ("M5", "M15", "H1", "H4")
    resolved = resolve_market_source(db, symbol, primary_tf, requested=source)
    locked_source = resolved.get("resolved_source")

    series: dict[str, list[Candle]] = {}
    indicators: dict[str, dict[str, Any]] = {}
    for tf in tfs:
        # Mesma fonte em todos os timeframes da análise — sem mistura.
        rows = candles_for(db, symbol, tf, limit=lookback, before=as_of, source=locked_source)
        series[tf] = rows
        snap = snapshot_from_candles(rows) if rows else None
        indicators[tf] = snap.__dict__ if snap else {}

    primary = series.get(primary_tf) or []
    last = primary[-1] if primary else None
    used_source = locked_source or (last.source if last else "none")
    is_synthetic = bool(last.is_synthetic) if last else bool(resolved.get("is_synthetic"))
    if used_source == "synthetic":
        is_synthetic = True
    return {
        "symbol": symbol,
        "primary_timeframe": primary_tf,
        "as_of": (as_of or (last.ts if last else datetime.now(timezone.utc))).isoformat(),
        "data_source": used_source,
        "is_synthetic": is_synthetic,
        "label": label_for(used_source, is_synthetic),
        "last_price": last.close if last else None,
        "last_bar_time": last.ts.isoformat() if last else None,
        "bars": {tf: [serialize_candle(c) for c in series[tf]] for tf in tfs},
        "indicators": indicators,
        "primary_indicators": indicators.get(primary_tf, {}),
        "source_resolution": resolved,
    }
