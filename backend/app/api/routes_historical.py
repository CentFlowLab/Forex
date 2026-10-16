"""Historical Dukascopy download + replay API. No broker credentials required."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.market.cache import count_source, source_coverage
from app.market.download_service import run_download_job
from app.i18n_pt import MSG
from app.market.labels import DUKASCOPY_SOURCE, REAL_HISTORICAL_LABEL, SYNTHETIC_LABEL
from app.models.replay import DownloadJob, ReplaySession
from app.replay.engine import replay_controller

router = APIRouter(tags=["historical"])


class DownloadRequest(BaseModel):
    start: datetime
    end: datetime
    symbol: str = "EURUSD"


class ReplayCreateRequest(BaseModel):
    start: datetime
    end: datetime
    timeframe: str = "M15"
    speed: float = 1.0
    ai_every_n_bars: int = Field(default=12, ge=1, le=500)
    execute_trades: bool = True


class ReplaySpeedRequest(BaseModel):
    speed: float = Field(ge=0.1, le=50.0)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@router.get("/market/sources")
def market_sources(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    duk = count_source(db, DUKASCOPY_SOURCE)
    syn = count_source(db, "synthetic")
    return {
        "live_locked": settings.live_trading_locked,
        "sources": [
            {
                "name": DUKASCOPY_SOURCE,
                "label": REAL_HISTORICAL_LABEL,
                "is_synthetic": False,
                "bars": duk,
                "requires_credentials": False,
            },
            {
                "name": "synthetic",
                "label": SYNTHETIC_LABEL,
                "is_synthetic": True,
                "bars": syn,
                "requires_credentials": False,
            },
        ],
        "ai_daily_budget_usd": settings.ai_daily_budget_usd,
        "ai_session_budget_usd": settings.ai_session_budget_usd,
        "dukascopy_max_days": settings.dukascopy_max_days,
    }


@router.get("/market/coverage")
def market_coverage(
    start: datetime,
    end: datetime,
    timeframe: str = "M15",
    source: str = DUKASCOPY_SOURCE,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return source_coverage(db, source, "EURUSD", timeframe.upper(), _aware(start), _aware(end))


@router.post("/market/download/dukascopy")
def download_dukascopy(body: DownloadRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    start, end = _aware(body.start), _aware(body.end)
    if end <= start:
        raise HTTPException(400, "A data de fim tem de ser posterior ao início.")
    if (end - start) > timedelta(days=settings.dukascopy_max_days):
        raise HTTPException(
            400,
            f"O intervalo excede dukascopy_max_days={settings.dukascopy_max_days}. Reduza o período.",
        )
    job = run_download_job(db, start, end, symbol=body.symbol)
    return {
        "id": job.id,
        "status": job.status,
        "progress_pct": job.progress_pct,
        "message": job.message,
        "error": job.error,
        "bars": {"M5": job.bars_m5, "M15": job.bars_m15, "H1": job.bars_h1, "H4": job.bars_h4},
        "details": json.loads(job.details_json or "{}"),
        "parquet_paths": json.loads(job.parquet_paths or "[]"),
        "label": REAL_HISTORICAL_LABEL,
        "is_synthetic": False,
    }


@router.get("/market/download/{job_id}")
def download_status(job_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(DownloadJob, job_id)
    if not job:
        raise HTTPException(404, MSG["download_not_found"])
    return {
        "id": job.id,
        "status": job.status,
        "progress_pct": job.progress_pct,
        "message": job.message,
        "error": job.error,
        "start": job.start_ts.isoformat(),
        "end": job.end_ts.isoformat(),
        "bars": {"M5": job.bars_m5, "M15": job.bars_m15, "H1": job.bars_h1, "H4": job.bars_h4},
        "details": json.loads(job.details_json or "{}"),
        "label": REAL_HISTORICAL_LABEL,
    }


@router.get("/market/downloads")
def list_downloads(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(DownloadJob).order_by(DownloadJob.created_at.desc()).limit(20)))
    return [
        {
            "id": j.id,
            "status": j.status,
            "message": j.message,
            "error": j.error,
            "progress_pct": j.progress_pct,
            "start": j.start_ts.isoformat(),
            "end": j.end_ts.isoformat(),
            "bars": {"M5": j.bars_m5, "M15": j.bars_m15, "H1": j.bars_h1, "H4": j.bars_h4},
            "created_at": j.created_at.isoformat(),
        }
        for j in rows
    ]


@router.post("/replay")
def create_replay(body: ReplayCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        session = replay_controller.create_session(
            db,
            start=_aware(body.start),
            end=_aware(body.end),
            timeframe=body.timeframe.upper(),
            speed=body.speed,
            ai_every_n_bars=body.ai_every_n_bars,
            execute_trades=body.execute_trades,
            source=DUKASCOPY_SOURCE,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay_controller.status_payload(db, session.id)


@router.post("/replay/{session_id}/play")
def replay_play(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        replay_controller.play(db, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay_controller.status_payload(db, session_id)


@router.post("/replay/{session_id}/pause")
def replay_pause(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        replay_controller.pause(db, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay_controller.status_payload(db, session_id)


@router.post("/replay/{session_id}/restart")
def replay_restart(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        replay_controller.restart(db, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay_controller.status_payload(db, session_id)


@router.post("/replay/{session_id}/speed")
def replay_speed(session_id: int, body: ReplaySpeedRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        replay_controller.set_speed(db, session_id, body.speed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return replay_controller.status_payload(db, session_id)


@router.get("/replay/{session_id}")
def replay_status(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return replay_controller.status_payload(db, session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/replay/{session_id}/report")
def replay_report(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return replay_controller.export_report(db, session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/replay")
def list_replays(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(ReplaySession).order_by(ReplaySession.created_at.desc()).limit(20)))
    return [
        {
            "id": r.id,
            "status": r.status,
            "timeframe": r.timeframe,
            "source": r.source,
            "is_synthetic": r.is_synthetic,
            "start": r.start_ts.isoformat(),
            "end": r.end_ts.isoformat(),
            "bars_total": r.bars_total,
            "baseline_net_pnl": r.baseline_net_pnl,
            "ai_net_pnl": r.ai_net_pnl,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
