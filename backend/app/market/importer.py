"""Historical candle import. CSV columns: timestamp,open,high,low,close,volume."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.market import Candle

REQUIRED = ("timestamp", "open", "high", "low", "close")


def _parse_ts(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M",
        "%Y.%m.%d %H:%M",
        "%d/%m/%Y %H:%M",
    ):
        try:
            dt = datetime.strptime(raw.replace("Z", ""), fmt.replace("Z", ""))
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Unrecognized timestamp: {raw}") from exc


def import_csv_text(
    db: Session,
    text: str,
    symbol: str,
    timeframe: str,
    source: str = "imported",
) -> int:
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header")
    fields = {name.strip().lower(): name for name in reader.fieldnames}
    for col in REQUIRED:
        if col not in fields and col != "timestamp":
            raise ValueError(f"Missing required column: {col}")
    ts_key = fields.get("timestamp") or fields.get("time") or fields.get("date")
    if not ts_key:
        raise ValueError("CSV must include timestamp/time/date")

    count = 0
    for row in reader:
        ts = _parse_ts(row[ts_key])
        candle = Candle(
            symbol=symbol.replace("/", "").upper(),
            timeframe=timeframe.upper(),
            ts=ts,
            open=float(row[fields["open"]]),
            high=float(row[fields["high"]]),
            low=float(row[fields["low"]]),
            close=float(row[fields["close"]]),
            volume=float(row.get(fields.get("volume", ""), 0) or 0),
            source=source,
            is_synthetic=False,
        )
        db.merge(candle)
        count += 1
    db.commit()
    return count


def import_csv_file(db: Session, path: Path, symbol: str, timeframe: str, source: str = "imported") -> int:
    return import_csv_text(db, path.read_text(encoding="utf-8"), symbol, timeframe, source)
