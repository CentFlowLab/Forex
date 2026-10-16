"""API do Centro de Autonomia."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.autonomy.service import get_autonomy_service
from app.config import get_settings
from app.database import get_db

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


class AutonomyConfigBody(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    paper_enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=30, le=1440)
    lookback_days: int | None = Field(default=None, ge=30, le=400)
    max_combos: int | None = Field(default=None, ge=2, le=8)


@router.get("/dashboard")
def autonomy_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_autonomy_service().dashboard(db)


@router.put("/config")
def autonomy_config(body: AutonomyConfigBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    if settings.live_authorized or settings.trading_environment == "LIVE":
        raise HTTPException(403, "Negociação real bloqueada — autonomia apenas em simulação.")
    return get_autonomy_service().configure(
        db,
        enabled=body.enabled,
        paused=body.paused,
        paper_enabled=body.paper_enabled,
        interval_minutes=body.interval_minutes,
        lookback_days=body.lookback_days,
        max_combos=body.max_combos,
    )


@router.post("/cycle/start")
def autonomy_start(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_autonomy_service().configure(db, enabled=True, paused=False)


@router.post("/cycle/pause")
def autonomy_pause(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_autonomy_service().configure(db, paused=True)


@router.post("/cycle/resume")
def autonomy_resume(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_autonomy_service().configure(db, enabled=True, paused=False)


@router.post("/paper/enable")
def paper_enable(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_autonomy_service().configure(db, paper_enabled=True)


@router.post("/paper/disable")
def paper_disable(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_autonomy_service().configure(db, paper_enabled=False)
