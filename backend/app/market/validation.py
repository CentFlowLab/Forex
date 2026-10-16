"""Validate candle integrity. Never silently invent missing bars."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

TIMEFRAME_DELTA = {
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
}


@dataclass
class ValidationReport:
    valid: bool
    bars: int
    rejected: int = 0
    duplicate_timestamps: int = 0
    missing_gaps: int = 0
    gap_examples: list[str] = field(default_factory=list)
    ohlc_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "bars": self.bars,
            "rejected": self.rejected,
            "duplicate_timestamps": self.duplicate_timestamps,
            "missing_gaps": self.missing_gaps,
            "gap_examples": self.gap_examples[:10],
            "ohlc_errors": self.ohlc_errors[:10],
            "notes": self.notes,
        }


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def validate_candle_row(row: dict[str, Any]) -> str | None:
    try:
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    except (KeyError, TypeError, ValueError):
        return "non-numeric OHLC"
    if min(o, h, l, c) <= 0:
        return "non-positive price"
    if h < max(o, c) or l > min(o, c) or h < l:
        return "inconsistent OHLC relationship"
    # Limite por instrumento (~500 pips por barra); JPY usa pip 0.01.
    symbol = str(row.get("symbol") or "EURUSD").replace("/", "").upper()
    try:
        from app.risk.instruments import get_instrument

        pip = get_instrument(symbol).pip_size
    except Exception:  # noqa: BLE001
        pip = 0.0001
    if abs(h - l) > pip * 500:
        return "implausible range"
    return None


def validate_series(rows: list[dict[str, Any]], timeframe: str) -> tuple[list[dict[str, Any]], ValidationReport]:
    report = ValidationReport(valid=True, bars=0)
    if timeframe not in TIMEFRAME_DELTA:
        report.valid = False
        report.notes.append(f"Unsupported timeframe: {timeframe}")
        return [], report

    cleaned: list[dict[str, Any]] = []
    seen: set[datetime] = set()
    for row in sorted(rows, key=lambda r: _aware(r["ts"])):
        err = validate_candle_row(row)
        if err:
            report.rejected += 1
            report.ohlc_errors.append(f"{row.get('ts')}: {err}")
            continue
        ts = _aware(row["ts"])
        if ts in seen:
            report.duplicate_timestamps += 1
            continue
        seen.add(ts)
        cleaned.append({**row, "ts": ts})

    delta = TIMEFRAME_DELTA[timeframe]
    for i in range(1, len(cleaned)):
        gap = cleaned[i]["ts"] - cleaned[i - 1]["ts"]
        # Forex has weekend gaps; flag only unexpected weekday gaps (> 2x TF and < 2 days)
        if gap > delta * 2 and gap < timedelta(days=2):
            # Allow up to ~1h missing around rollover; count larger holes
            expected = int(gap / delta) - 1
            if expected > 0:
                report.missing_gaps += expected
                if len(report.gap_examples) < 10:
                    report.gap_examples.append(
                        f"{cleaned[i-1]['ts'].isoformat()} -> {cleaned[i]['ts'].isoformat()} (~{expected} missing)"
                    )

    report.bars = len(cleaned)
    if report.bars == 0:
        report.valid = False
        report.notes.append("No valid candles after integrity checks.")
    else:
        report.notes.append(
            "Missing candles are reported but not fabricated. Gaps (incl. weekends) remain empty."
        )
    return cleaned, report
