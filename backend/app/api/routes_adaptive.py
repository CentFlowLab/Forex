"""API do Motor Adaptativo."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adaptive.decisions import decisions_for_agents_as_of, outcomes_visible_as_of
from app.adaptive.service import get_adaptive_service
from app.config import get_settings
from app.database import get_db
from app.market.history_inventory import (
    DEFAULT_INVESTIGATION_END,
    DEFAULT_INVESTIGATION_START,
    analyze_coverage,
    cache_bounds,
    prepare_experiment,
)
from app.market.incremental_download import get_incremental_download_controller
from app.market.labels import DUKASCOPY_SOURCE
from app.models.replay import DownloadJob

router = APIRouter(prefix="/adaptive", tags=["adaptive"])


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ExperimentBody(BaseModel):
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    start: datetime
    end: datetime
    name: str = "experiência-adaptativa"
    authorize_openai: bool = Field(
        default=False,
        description="Ignorado — o motor adaptativo é 100% local (sem OpenAI).",
    )
    require_real_data: bool = Field(
        default=True,
        description="Exige Dukascopy real com cobertura suficiente.",
    )


class ScheduleBody(BaseModel):
    enabled: bool = False
    interval_minutes: int = 1440
    lookback_days: int = 30
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    authorize_openai: bool = False


class HistoryDownloadBody(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    confirmed: bool = False
    resume_job_id: int | None = None


@router.get("/dashboard")
def adaptive_dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    svc = get_adaptive_service()
    # Tentativa oportunista de agenda local (sem OpenAI).
    try:
        svc.maybe_run_schedule(db)
    except Exception:
        pass
    return svc.dashboard(db)


@router.get("/regime")
def adaptive_regime(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return get_adaptive_service().current_regime(db, symbol, timeframe)


@router.get("/catalog")
def adaptive_catalog() -> dict[str, Any]:
    from app.adaptive.strategies import list_catalog

    return {
        "strategies": [
            {
                "key": s.key,
                "display_name": s.display_name,
                "description": s.description,
                "default_params": s.default_params,
                "preferred_regimes": list(s.preferred_regimes),
                "complexity": s.complexity,
            }
            for s in list_catalog()
        ]
    }


@router.get("/history")
def adaptive_history(
    start: datetime | None = None,
    end: datetime | None = None,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start = _aware(start or DEFAULT_INVESTIGATION_START)
    end = _aware(end or DEFAULT_INVESTIGATION_END)
    return get_adaptive_service().history_summary(
        db, start=start, end=end, symbol=symbol.upper(), timeframe=timeframe.upper()
    )


@router.get("/history/prepare")
def adaptive_history_prepare(
    start: datetime | None = None,
    end: datetime | None = None,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    require_real_data: bool = True,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start = _aware(start or DEFAULT_INVESTIGATION_START)
    end = _aware(end or DEFAULT_INVESTIGATION_END)
    return prepare_experiment(
        db,
        start=start,
        end=end,
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        require_real_data=require_real_data,
    )


@router.post("/history/download")
def adaptive_history_download(body: HistoryDownloadBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Descarregar histórico em falta (incremental). Não inicia sem confirmed=true."""
    start = _aware(body.start or DEFAULT_INVESTIGATION_START)
    end = _aware(body.end or DEFAULT_INVESTIGATION_END)
    ctrl = get_incremental_download_controller()
    try:
        job = ctrl.start_missing(
            db,
            start=start,
            end=end,
            symbol=body.symbol.upper(),
            timeframe=body.timeframe.upper(),
            confirmed=body.confirmed,
            resume_job_id=body.resume_job_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ctrl.job_payload(job)


@router.get("/history/download/{job_id}")
def adaptive_history_download_status(job_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(404, "Tarefa de descarregamento não encontrada.")
    return get_incremental_download_controller().job_payload(job)


@router.post("/history/download/{job_id}/cancel")
def adaptive_history_download_cancel(job_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    ctrl = get_incremental_download_controller()
    try:
        job = ctrl.cancel(db, job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ctrl.job_payload(job)


@router.get("/history/coverage")
def adaptive_history_coverage(
    start: datetime | None = None,
    end: datetime | None = None,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start = _aware(start or DEFAULT_INVESTIGATION_START)
    end = _aware(end or DEFAULT_INVESTIGATION_END)
    return {
        "coverage": analyze_coverage(
            db,
            start=start,
            end=end,
            symbol=symbol.upper(),
            timeframe=timeframe.upper(),
            source=DUKASCOPY_SOURCE,
        ),
        "cache": cache_bounds(db, symbol=symbol.upper(), source=DUKASCOPY_SOURCE),
    }


@router.post("/experiments")
def start_experiment(body: ExperimentBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    _ = settings  # orçamento IA irrelevante — motor local
    if body.end <= body.start:
        raise HTTPException(400, "A data de fim tem de ser posterior ao início.")
    try:
        return get_adaptive_service().start_experiment(
            db,
            start=_aware(body.start),
            end=_aware(body.end),
            symbol=body.symbol,
            timeframe=body.timeframe,
            name=body.name,
            authorize_openai=False,
            require_real_data=body.require_real_data,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/experiments")
def list_experiments(limit: int = 20, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    dash = get_adaptive_service().dashboard(db)
    return dash.get("experiments") or []


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.models.adaptive import AdaptiveExperiment

    row = db.get(AdaptiveExperiment, experiment_id)
    if row is None:
        raise HTTPException(404, "Experiência não encontrada.")
    return get_adaptive_service()._experiment_dict(row)


@router.get("/decisions/safe")
def safe_decisions(
    as_of: datetime,
    experiment_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Decisões visíveis aos agentes sem resultados futuros."""
    return {
        "as_of": as_of.isoformat(),
        "decisions": decisions_for_agents_as_of(db, as_of=as_of, experiment_id=experiment_id),
        "outcomes_visible": outcomes_visible_as_of(db, as_of=as_of, experiment_id=experiment_id),
    }


@router.put("/schedule")
def update_schedule(body: ScheduleBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return get_adaptive_service().update_schedule(
            db,
            enabled=body.enabled,
            interval_minutes=body.interval_minutes,
            lookback_days=body.lookback_days,
            authorize_openai=body.authorize_openai,
            symbol=body.symbol,
            timeframe=body.timeframe,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
