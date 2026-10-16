"""Local cache for historical candles (SQLite via ORM + Parquet files)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.market.labels import DUKASCOPY_SOURCE, label_for
from app.market.validation import validate_series
from app.models.market import Candle

PARQUET_DIR = DATA_DIR / "historical" / "parquet"


def parquet_path(symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    name = (
        f"{symbol}_{timeframe}_{start.strftime('%Y%m%d%H')}_{end.strftime('%Y%m%d%H')}.parquet"
    )
    return PARQUET_DIR / name


def write_parquet(rows: list[dict[str, Any]], path: Path) -> Path:
    if not rows:
        raise ValueError("No rows to cache")
    frame = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def read_parquet(path: Path) -> list[dict[str, Any]]:
    frame = pd.read_parquet(path)
    rows: list[dict[str, Any]] = []
    for rec in frame.to_dict(orient="records"):
        ts = rec["ts"]
        if not isinstance(ts, datetime):
            ts = pd.Timestamp(ts).to_pydatetime()
        rows.append({**rec, "ts": ts})
    return rows


def persist_candles(db: Session, rows: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    cleaned, report = validate_series(rows, timeframe)
    inserted = 0
    updated = 0
    for row in cleaned:
        existing = db.scalar(
            select(Candle).where(
                and_(
                    Candle.symbol == row["symbol"],
                    Candle.timeframe == row["timeframe"],
                    Candle.ts == row["ts"],
                    Candle.source == row["source"],
                )
            )
        )
        if existing:
            existing.open = row["open"]
            existing.high = row["high"]
            existing.low = row["low"]
            existing.close = row["close"]
            existing.volume = row["volume"]
            existing.is_synthetic = False
            updated += 1
        else:
            db.add(Candle(**row))
            inserted += 1
    db.commit()
    return {
        "inserted": inserted,
        "updated": updated,
        "validation": report.as_dict(),
        "label": label_for(DUKASCOPY_SOURCE, False),
    }


def count_source(db: Session, source: str, symbol: str = "EURUSD") -> dict[str, int]:
    from sqlalchemy import func

    out: dict[str, int] = {}
    for tf in ("M5", "M15", "H1", "H4"):
        out[tf] = int(
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol, Candle.timeframe == tf, Candle.source == source
                )
            )
            or 0
        )
    return out


def source_coverage(
    db: Session,
    source: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    from sqlalchemy import func

    rows = list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.source == source,
                Candle.ts >= start,
                Candle.ts <= end,
            )
            .order_by(Candle.ts.asc())
        )
    )
    first = rows[0].ts if rows else None
    last = rows[-1].ts if rows else None
    return {
        "source": source,
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(rows),
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "is_synthetic": False if source != "synthetic" else True,
        "label": label_for(source, source == "synthetic"),
        "count_total": int(
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol, Candle.source == source
                )
            )
            or 0
        ),
    }
