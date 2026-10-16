"""Orchestrate Dukascopy downloads into SQLite + Parquet cache."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.market.cache import persist_candles, write_parquet, parquet_path
from app.market.dukascopy import DukascopyError, download_eurusd_range
from app.i18n_pt import MSG
from app.market.labels import DUKASCOPY_SOURCE, REAL_HISTORICAL_LABEL
from app.models.audit import AuditLog
from app.models.replay import DownloadJob


def run_download_job(
    db: Session,
    start: datetime,
    end: datetime,
    symbol: str = "EURUSD",
    job_id: int | None = None,
) -> DownloadJob:
    if symbol.replace("/", "").upper() != "EURUSD":
        raise ValueError("Only EURUSD is supported in this phase.")

    job = db.get(DownloadJob, job_id) if job_id else None
    if job is None:
        job = DownloadJob(
            symbol="EURUSD",
            source=DUKASCOPY_SOURCE,
            start_ts=start,
            end_ts=end,
            status="running",
            message=MSG["download_connecting"],
        )
        db.add(job)
        db.commit()
        db.refresh(job)

    job.status = "running"
    job.error = ""
    job.updated_at = datetime.now(timezone.utc)
    db.commit()

    def on_progress(info: dict[str, Any]) -> None:
        total = max(1, int(info.get("total") or 1))
        current = int(info.get("current") or 0)
        job.progress_pct = round(100.0 * current / total, 2)
        job.message = (
            f"Hora {current}/{total} · {info.get('status')} · ticks={info.get('ticks_so_far')}"
        )
        job.details_json = json.dumps(info, default=str)
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

    try:
        result = download_eurusd_range(start, end, progress=on_progress)
    except DukascopyError as exc:
        job.status = "failed"
        job.error = str(exc)
        job.message = MSG["download_failed"]
        job.updated_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
                category="market",
                level="ERROR",
                message="Descarregamento Dukascopy falhou",
                details=str(exc),
            )
        )
        db.commit()
        db.refresh(job)
        return job

    parquet_paths: list[str] = []
    persist_summary: dict[str, Any] = {}
    for tf, rows in result["candles"].items():
        if not rows:
            persist_summary[tf] = {"inserted": 0, "updated": 0, "validation": {"bars": 0}}
            continue
        path = parquet_path("EURUSD", tf, start, end)
        write_parquet(rows, path)
        parquet_paths.append(str(path))
        persist_summary[tf] = persist_candles(db, rows, tf)

    job.bars_m5 = persist_summary.get("M5", {}).get("validation", {}).get("bars", 0) or len(
        result["candles"].get("M5", [])
    )
    job.bars_m15 = len(result["candles"].get("M15", []))
    job.bars_h1 = len(result["candles"].get("H1", []))
    job.bars_h4 = len(result["candles"].get("H4", []))
    job.parquet_paths = json.dumps(parquet_paths)
    job.details_json = json.dumps(
        {
            "hour_stats": result["hour_stats"],
            "tick_count": result["tick_count"],
            "persist": persist_summary,
            "errors": result["errors"],
            "label": REAL_HISTORICAL_LABEL,
        },
        default=str,
    )
    job.progress_pct = 100.0
    job.status = "completed"
    job.message = MSG["download_ok"].format(
        m5=job.bars_m5,
        m15=job.bars_m15,
        h1=job.bars_h1,
        h4=job.bars_h4,
        ticks=result["tick_count"],
    )
    job.updated_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            category="market",
            level="INFO",
            message=job.message,
            details=job.details_json[:2000],
        )
    )
    db.commit()
    db.refresh(job)
    return job
