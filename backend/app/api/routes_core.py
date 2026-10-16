"""HTTP routes for dashboard, trading, agents, backtest and settings."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.agents import analysis_control
from app.agents.base import load_agent_config
from app.agents.memory_store import list_memory, record_simulated_outcome
from app.agents.orchestrator import run_collaboration
from app.agents.prompts import ALL_CONFIGURABLE_ROLES, DISPLAY_NAMES
from app.i18n_pt import MSG
from app.ai.registry import list_provider_status
from app.backtest.engine import compare_workflows, run_ai_sample_backtest, run_baseline_backtest
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.database import get_db
from app.evaluation.comparison import compare_providers_on_history
from app.market.cache import count_source
from app.market.data_store import candles_for, market_bundle, serialize_candle
from app.market.importer import import_csv_text
from app.market.labels import DUKASCOPY_SOURCE, REAL_HISTORICAL_LABEL, SYNTHETIC_LABEL
from app.market.quote_feed import get_quote_feed
from app.market.source_resolve import resolve_market_source
from app.models.account import Account
from app.models.agent import AgentConfig, AgentReport, DecisionRecord
from app.models.audit import ApiUsage, AuditLog
from app.models.backtest import BacktestRun, EvaluationRun
from app.models.memory import AnalysisRun
from app.models.trading import ClosedTrade, Position
from app.risk.instruments import get_instrument
from app.services.runtime import get_flag, set_flag

router = APIRouter()


class AgentUpdate(BaseModel):
    provider: str
    model: str
    enabled: bool = True
    temperature: float = Field(ge=0.0, le=1.0, default=0.2)


class SettingsUpdate(BaseModel):
    trading_environment: str | None = None
    kill_switch: bool | None = None
    max_risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    max_spread_pips: float | None = None
    max_slippage_pips: float | None = None


class CollaborationRequest(BaseModel):
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    execute: bool = False
    workflow: str | None = None
    debate_rounds: int | None = Field(default=None, ge=0, le=4)
    source: str | None = None
    authorization_id: str | None = None


class AnalysisStartRequest(BaseModel):
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    execute: bool = False
    debate_rounds: int = Field(default=2, ge=0, le=4)
    enabled_roles: dict[str, bool] = Field(default_factory=dict)
    source: str | None = None
    authorization_id: str | None = None


class MemoryOutcomeRequest(BaseModel):
    realized_virtual_pnl: float
    outcome: dict[str, Any] = Field(default_factory=dict)
    lessons: list[str] = Field(default_factory=list)
    as_of: datetime | None = None


class BacktestRequest(BaseModel):
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    start: datetime
    end: datetime
    walk_forward: bool = False
    engine: str = "baseline"


class WorkflowCompareRequest(BaseModel):
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    start: datetime
    end: datetime
    max_bars: int = Field(default=6, ge=1, le=30)
    step: int = Field(default=20, ge=1, le=200)


class EvaluationRequest(BaseModel):
    symbol: str = "EURUSD"
    timeframe: str = "M15"
    start: datetime
    end: datetime
    providers: list[str] = Field(default_factory=lambda: ["xai", "openai", "anthropic"])
    max_bars: int = 3


def _account(db: Session) -> Account:
    row = db.scalar(select(Account).limit(1))
    if not row:
        raise HTTPException(500, MSG["account_missing"])
    return row


def _kill_switch(db: Session) -> bool:
    flag = get_flag(db, "kill_switch")
    if flag:
        return flag.lower() == "true"
    return get_settings().kill_switch


@router.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.trading_environment,
        "live_trading_enabled": False if not settings.live_authorized else settings.live_trading_enabled,
        "live_authorized": settings.live_authorized,
        "host": settings.app_host,
    }


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    last_lock: OperationalError | None = None
    for attempt in range(5):
        try:
            return _dashboard_payload(db)
        except OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_lock = exc
            db.rollback()
            time.sleep(0.25 * (attempt + 1))
    assert last_lock is not None
    raise last_lock


def _dashboard_payload(db: Session) -> dict[str, Any]:
    settings = get_settings()
    account = _account(db)
    broker = get_broker(db)
    if isinstance(broker, SimulatedBroker):
        broker.apply_stops()
        broker._mark_to_market()
        db.commit()
    positions = list(db.scalars(select(Position)))
    trades = list(db.scalars(select(ClosedTrade).order_by(ClosedTrade.closed_at.desc()).limit(50)))
    decisions = list(db.scalars(select(DecisionRecord).order_by(DecisionRecord.created_at.desc()).limit(20)))
    reports = list(db.scalars(select(AgentReport).order_by(AgentReport.created_at.desc()).limit(36)))
    usage_sum = db.execute(
        select(
            ApiUsage.provider,
            func.sum(ApiUsage.prompt_tokens),
            func.sum(ApiUsage.completion_tokens),
            func.sum(ApiUsage.estimated_cost_usd),
        ).group_by(ApiUsage.provider)
    ).all()
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    closed_all = list(db.scalars(select(ClosedTrade).where(ClosedTrade.backtest_run_id == 0)))
    equity_curve = []
    running = account.starting_equity
    for t in sorted(closed_all, key=lambda x: x.closed_at):
        running += t.pnl
        equity_curve.append({"time": t.closed_at.isoformat(), "equity": round(running, 2), "is_simulated": True})
    if not equity_curve:
        equity_curve.append(
            {"time": datetime.now(timezone.utc).isoformat(), "equity": account.equity, "is_simulated": True}
        )

    # Relatórios órfãos (ex.: fluxo clássico antigo) NÃO alimentam a «análise atual».
    historical_agents: dict[str, Any] = {}
    for r in reports:
        if r.role not in historical_agents and r.role in {
            "technical",
            "trend",
            "fundamental",
            "strategy",
            "risk_challenger",
            "coordinator",
        }:
            historical_agents[r.role] = {
                "role": r.role,
                "display_name": DISPLAY_NAMES.get(r.role, r.role),
                "status": r.status,
                "bias": r.bias,
                "confidence": r.confidence,
                "uncertainty": r.uncertainty,
                "summary": r.summary,
                "error": r.error,
                "provider": r.provider,
                "model": r.model,
                "created_at": r.created_at.isoformat(),
                "stale": True,
            }

    last_by_role: dict[str, Any] = {}
    latest_analysis = db.scalar(select(AnalysisRun).order_by(AnalysisRun.created_at.desc()).limit(1))
    analysis_payload = None
    if latest_analysis:
        try:
            analysis_payload = json.loads(latest_analysis.result_json or "{}")
        except json.JSONDecodeError:
            analysis_payload = {}
        raw_result = analysis_payload or {}
        analysis_payload = {
            "id": latest_analysis.id,
            "status": latest_analysis.status,
            "workflow": latest_analysis.workflow,
            "recommendation": latest_analysis.recommendation,
            "confidence": latest_analysis.confidence,
            "risk_veto": latest_analysis.risk_veto,
            "risk_veto_reason": latest_analysis.risk_veto_reason,
            "data_freshness": latest_analysis.data_freshness,
            "data_source": latest_analysis.data_source,
            "is_synthetic": latest_analysis.is_synthetic,
            "timeframe": latest_analysis.timeframe,
            "debate_rounds_requested": latest_analysis.debate_rounds_requested,
            "debate_rounds_completed": latest_analysis.debate_rounds_completed,
            "estimated_cost_usd": latest_analysis.estimated_cost_usd,
            "prompt_tokens": latest_analysis.prompt_tokens,
            "completion_tokens": latest_analysis.completion_tokens,
            "models_used": json.loads(latest_analysis.models_used_json or "[]"),
            "package": raw_result.get("package"),
            "debate": raw_result.get("debate"),
            "stages": raw_result.get("stages") or {},
            "diagnostics": raw_result.get("diagnostics") or {},
            "reports": raw_result.get("reports") or [],
            "created_at": latest_analysis.created_at.isoformat(),
            "duration_ms": latest_analysis.duration_ms,
            "error": latest_analysis.error,
            "market_time": latest_analysis.market_time.isoformat() if latest_analysis.market_time else None,
        }
        # Preferir relatórios da última análise (evita misturar histórico antigo em inglês).
        for rep in raw_result.get("reports") or []:
            role = rep.get("role")
            if role in DISPLAY_NAMES and role in {
                "technical",
                "trend",
                "fundamental",
                "strategy",
                "risk_challenger",
                "coordinator",
                "bull_researcher",
                "bear_researcher",
            }:
                last_by_role[role] = {
                    "role": role,
                    "display_name": DISPLAY_NAMES.get(role, role),
                    "status": rep.get("status"),
                    "bias": rep.get("bias"),
                    "confidence": rep.get("confidence") or 0,
                    "uncertainty": rep.get("uncertainty") or 1,
                    "summary": rep.get("summary") or "",
                    "error": rep.get("error") or "",
                    "provider": rep.get("provider") or "",
                    "model": rep.get("model") or "",
                    "created_at": latest_analysis.created_at.isoformat(),
                    "language_ok": rep.get("language_ok", True),
                    "language_warning": rep.get("language_warning") or "",
                }

    duk_bars = count_source(db, DUKASCOPY_SOURCE)
    active_market = resolve_market_source(db, "EURUSD", "M15", requested=None)
    quote_feed = get_quote_feed().snapshot().as_dict()
    return {
        "disclaimer": MSG["live_phase_disclaimer"],
        "environment": settings.trading_environment,
        "is_simulated": True,
        "live_authorized": False,
        "live_locked": settings.live_trading_locked,
        "kill_switch": _kill_switch(db),
        "synthetic_label": SYNTHETIC_LABEL,
        "real_historical_label": REAL_HISTORICAL_LABEL,
        "dukascopy_bars": duk_bars,
        "quote_feed": quote_feed,
        "account": {
            "name": account.name,
            "currency": account.currency,
            "balance": account.balance,
            "equity": account.equity,
            "unrealized_pnl": account.unrealized_pnl,
            "realized_pnl": account.realized_pnl,
            "max_drawdown_pct": account.max_drawdown_pct,
            "daily_realized_pnl": account.daily_realized_pnl,
            "is_simulated": account.is_simulated,
        },
        "metrics": {
            "closed_trades": len(closed_all),
            "win_rate": (len([t for t in closed_all if t.pnl > 0]) / len(closed_all) * 100.0) if closed_all else 0.0,
            "profit_factor": (gross_profit / gross_loss) if gross_loss else (gross_profit if gross_profit else 0.0),
            "net_pnl": sum(t.pnl for t in closed_all),
            "max_drawdown_pct": account.max_drawdown_pct,
        },
        "broker": {
            "adapter": broker.status().adapter,
            "connected": broker.status().connected,
            "message": broker.status().message,
            "is_simulated": broker.status().is_simulated,
        },
        "positions": [
            {
                "id": p.id,
                "symbol": p.symbol,
                "side": p.side,
                "volume_lots": p.volume_lots,
                "entry_price": p.entry_price,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "is_simulated": p.is_simulated,
            }
            for p in positions
        ],
        "closed_trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "volume_lots": t.volume_lots,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "pips": t.pips,
                "close_reason": t.close_reason,
                "is_simulated": t.is_simulated,
                "closed_at": t.closed_at.isoformat(),
            }
            for t in trades
        ],
        "equity_curve": equity_curve,
        "agents": last_by_role,
        "historical_agents": historical_agents,
        "has_tradingagents_analysis": latest_analysis is not None,
        "decisions": [
            {
                "id": d.id,
                "recommendation": d.recommendation,
                "risk_approved": d.risk_approved,
                "risk_reason": d.risk_reason,
                "executed": d.executed,
                "data_source": d.data_source,
                "is_simulated": d.is_simulated,
                "created_at": d.created_at.isoformat(),
                "rationale": d.coordinator_rationale,
            }
            for d in decisions
        ],
        "api_usage": [
            {
                "provider": row[0],
                "prompt_tokens": int(row[1] or 0),
                "completion_tokens": int(row[2] or 0),
                "estimated_cost_usd": float(row[3] or 0),
            }
            for row in usage_sum
        ],
        "latest_analysis": analysis_payload,
        "ai_budget": {
            "daily_usd": settings.ai_daily_budget_usd,
            "session_usd": settings.ai_session_budget_usd,
        },
        "max_debate_rounds": settings.max_debate_rounds,
        "active_market": active_market,
    }


@router.get("/market/quote-feed")
def market_quote_feed() -> dict[str, Any]:
    """Estado do feed automático Dukascopy (não dispara análises OpenAI)."""
    return get_quote_feed().snapshot().as_dict()


@router.get("/market/snapshot")
def market_snapshot(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if timeframe not in {"M5", "M15", "H1", "H4"}:
        raise HTTPException(400, MSG["unsupported_timeframe"])
    bundle = market_bundle(db, symbol, timeframe)
    broker = get_broker(db)
    try:
        quote = broker.quote(symbol)
        bundle["quote"] = {
            "bid": quote.bid,
            "ask": quote.ask,
            "spread_pips": quote.spread_pips,
            "is_synthetic": quote.is_synthetic,
            "source": quote.source,
            "ts": quote.ts.isoformat(),
        }
    except Exception as exc:
        bundle["quote_error"] = str(exc)
    return bundle


@router.get("/market/candles")
def market_candles(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    limit: int = Query(300, ge=20, le=2000),
    source: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.market.labels import MT5_LABEL, MT5_SOURCE
    from app.market.mt5_feed import get_mt5_feed

    # Velas MT5 em memória — só poll se ainda não houver cache (evita bloquear o painel).
    try:
        feed = get_mt5_feed()
        if not feed.candles(symbol, timeframe):
            feed.start()
            feed.poll_once()
    except Exception:  # noqa: BLE001
        pass

    resolved = resolve_market_source(db, symbol, timeframe, requested=source)
    locked = resolved.get("resolved_source")

    if locked == MT5_SOURCE or (source or "").lower() == MT5_SOURCE:
        mt5_rows = get_mt5_feed().candles(symbol, timeframe)[-limit:]
        candles_out = []
        for r in mt5_rows:
            ts_raw = str(r.get("ts") or "")
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                unix_t = int(dt.timestamp())
            except ValueError:
                unix_t = 0
            candles_out.append(
                {
                    "time": unix_t,
                    "open": r["o"],
                    "high": r["h"],
                    "low": r["l"],
                    "close": r["c"],
                    "volume": 0,
                    "source": MT5_SOURCE,
                    "is_synthetic": False,
                    "label": MT5_LABEL,
                }
            )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": MT5_SOURCE,
            "label": MT5_LABEL,
            "is_synthetic": False,
            "candles": candles_out,
            "source_resolution": resolved,
            "claims_realtime": False,
            "note": "Timestamps MT5 podem diferir de UTC (anomalia de relógio do servidor).",
        }

    rows = candles_for(db, symbol, timeframe, limit=limit, source=locked)
    if not rows and (source == DUKASCOPY_SOURCE or locked == DUKASCOPY_SOURCE):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": source,
            "label": REAL_HISTORICAL_LABEL,
            "is_synthetic": False,
            "candles": [],
            "message": MSG["no_dukascopy_candles"],
        }
    label = SYNTHETIC_LABEL
    is_synthetic = True
    src = "synthetic"
    if rows:
        is_synthetic = bool(rows[0].is_synthetic)
        src = rows[0].source
        label = REAL_HISTORICAL_LABEL if src == DUKASCOPY_SOURCE else (
            SYNTHETIC_LABEL if is_synthetic else f"DADOS HISTÓRICOS — {src}"
        )
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": src,
        "label": label,
        "is_synthetic": is_synthetic,
        "candles": [serialize_candle(c) for c in rows],
        "source_resolution": resolved,
    }


@router.post("/market/import")
async def import_market(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if timeframe not in {"M5", "M15", "H1", "H4"}:
        raise HTTPException(400, MSG["unsupported_timeframe"])
    text = (await file.read()).decode("utf-8")
    try:
        count = import_csv_text(db, text, symbol, timeframe, source="imported")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.add(AuditLog(category="market", message=f"Importadas {count} velas {symbol} {timeframe}"))
    db.commit()
    return {"imported": count, "symbol": symbol, "timeframe": timeframe, "is_synthetic": False}


@router.post("/collaboration/run")
def collaboration_run(body: CollaborationRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if _kill_switch(db) and body.execute:
        raise HTTPException(423, MSG["kill_active_short"])
    try:
        get_instrument(body.symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    source = None if not body.source or body.source == "auto" else body.source
    return run_collaboration(
        db,
        body.symbol.upper(),
        body.timeframe.upper(),
        execute=body.execute,
        workflow=body.workflow,
        debate_rounds=body.debate_rounds,
        source=source,
        authorization_id=body.authorization_id,
    )


def _serialize_analysis(row: AnalysisRun) -> dict[str, Any]:
    try:
        result = json.loads(row.result_json or "{}")
    except json.JSONDecodeError:
        result = {}
    return {
        "id": row.id,
        "decision_id": row.decision_id,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "workflow": row.workflow,
        "status": row.status,
        "cancel_requested": row.cancel_requested,
        "debate_rounds_requested": row.debate_rounds_requested,
        "debate_rounds_completed": row.debate_rounds_completed,
        "market_snapshot_id": row.market_snapshot_id,
        "market_time": row.market_time.isoformat() if row.market_time else None,
        "data_source": row.data_source,
        "data_freshness": row.data_freshness,
        "is_synthetic": row.is_synthetic,
        "recommendation": row.recommendation,
        "confidence": row.confidence,
        "risk_veto": row.risk_veto,
        "risk_veto_reason": row.risk_veto_reason,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "estimated_cost_usd": row.estimated_cost_usd,
        "duration_ms": row.duration_ms,
        "models_used": json.loads(row.models_used_json or "[]"),
        "package": result.get("package"),
        "debate": result.get("debate"),
        "reports": result.get("reports"),
        "stages": result.get("stages") or {},
        "diagnostics": result.get("diagnostics") or {},
        "error": row.error,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/analysis/start")
def analysis_start(body: AnalysisStartRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Trigger manual — não inicia análises repetidas automaticamente."""
    from app.ai.control import get_ai_control

    control = get_ai_control()
    if not control.is_enabled():
        raise HTTPException(
            403,
            "IA desligada (AI_ENABLED=false). Ative a IA e autorize a análise na página Consumo de IA.",
        )
    if not body.authorization_id:
        raise HTTPException(
            400,
            "Autorização de utilização única em falta. Confirme a estimativa de custo antes de iniciar.",
        )
    if _kill_switch(db) and body.execute:
        raise HTTPException(423, MSG["kill_active_short"])
    try:
        get_instrument(body.symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    active = analysis_control.active_run_id()
    if active is not None:
        existing = db.get(AnalysisRun, active)
        if existing and existing.status == "running":
            raise HTTPException(409, MSG.get("analysis_running", "Já existe uma análise em curso."))

    run = AnalysisRun(
        symbol=body.symbol.upper(),
        timeframe=body.timeframe.upper(),
        workflow="tradingagents",
        status="queued",
        debate_rounds_requested=body.debate_rounds,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    source = None if not body.source or body.source == "auto" else body.source
    result = run_collaboration(
        db,
        body.symbol.upper(),
        body.timeframe.upper(),
        execute=body.execute,
        workflow="tradingagents",
        debate_rounds=body.debate_rounds,
        analysis_run_id=run.id,
        enabled_roles=body.enabled_roles or None,
        source=source,
        authorization_id=body.authorization_id,
    )
    return result


@router.get("/analysis")
def analysis_list(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(AnalysisRun).order_by(AnalysisRun.created_at.desc()).limit(limit)))
    return [_serialize_analysis(r) for r in rows]


@router.get("/analysis/memory")
def analysis_memory(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return list_memory(db, limit=limit)


@router.post("/analysis/memory/{memory_id}/outcome")
def analysis_memory_outcome(
    memory_id: int,
    body: MemoryOutcomeRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        row = record_simulated_outcome(
            db,
            memory_id,
            realized_virtual_pnl=body.realized_virtual_pnl,
            outcome=body.outcome,
            lessons=body.lessons,
            as_of=body.as_of,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "id": row.id,
        "realized_virtual_pnl": row.realized_virtual_pnl,
        "outcome_recorded_at": row.outcome_recorded_at.isoformat() if row.outcome_recorded_at else None,
    }


@router.post("/analysis/{run_id}/stop")
def analysis_stop(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(AnalysisRun, run_id)
    if row is None:
        raise HTTPException(404, MSG.get("analysis_not_found", "Análise não encontrada."))
    row.cancel_requested = True
    analysis_control.request_cancel(run_id)
    if row.status in {"queued"}:
        row.status = "cancelled"
        row.finished_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": run_id, "cancel_requested": True, "status": row.status}


@router.get("/analysis/{run_id}")
def analysis_get(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(AnalysisRun, run_id)
    if row is None:
        raise HTTPException(404, MSG.get("analysis_not_found", "Análise não encontrada."))
    return _serialize_analysis(row)


@router.get("/decisions")
def list_decisions(limit: int = 50, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(DecisionRecord).order_by(DecisionRecord.created_at.desc()).limit(limit)))
    out = []
    for d in rows:
        reports = list(db.scalars(select(AgentReport).where(AgentReport.decision_id == d.id)))
        out.append(
            {
                "id": d.id,
                "recommendation": d.recommendation,
                "risk_approved": d.risk_approved,
                "risk_reason": d.risk_reason,
                "executed": d.executed,
                "data_source": d.data_source,
                "is_simulated": d.is_simulated,
                "created_at": d.created_at.isoformat(),
                "rationale": d.coordinator_rationale,
                "reports": [
                    {
                        "role": r.role,
                        "status": r.status,
                        "bias": r.bias,
                        "summary": r.summary,
                        "error": r.error,
                        "provider": r.provider,
                        "model": r.model,
                        "payload": json.loads(r.payload_json or "{}"),
                    }
                    for r in reports
                ],
            }
        )
    return out


@router.get("/positions")
def list_positions(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    broker = get_broker(db)
    if isinstance(broker, SimulatedBroker):
        broker._mark_to_market()
        db.commit()
    return [
        {
            "id": p.id,
            "symbol": p.symbol,
            "side": p.side,
            "volume_lots": p.volume_lots,
            "entry_price": p.entry_price,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "current_price": p.current_price,
            "unrealized_pnl": p.unrealized_pnl,
            "is_simulated": p.is_simulated,
        }
        for p in db.scalars(select(Position))
    ]


@router.post("/positions/{position_id}/close")
def close_position(position_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    broker = get_broker(db)
    if not isinstance(broker, SimulatedBroker):
        raise HTTPException(400, MSG["only_simulated_close"])
    trade = broker.close_position(position_id, "MANUAL")
    if not trade:
        raise HTTPException(404, MSG["position_not_found"])
    return {"closed": True, "pnl": trade.pnl, "is_simulated": True}


@router.get("/logs")
def logs(limit: int = 100, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)))
    return [
        {
            "id": r.id,
            "category": r.category,
            "level": r.level,
            "message": r.message,
            "details": r.details,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/settings")
def get_app_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    agents = []
    for role in ALL_CONFIGURABLE_ROLES:
        cfg = load_agent_config(db, role)
        agents.append(
            {
                "role": cfg.role,
                "display_name": DISPLAY_NAMES.get(cfg.role, cfg.display_name),
                "provider": cfg.provider,
                "model": cfg.model,
                "enabled": cfg.enabled,
                "temperature": cfg.temperature,
            }
        )
    return {
        "environment": settings.trading_environment,
        "live_trading_enabled": settings.live_trading_enabled,
        "live_authorized": settings.live_authorized,
        "kill_switch": _kill_switch(db),
        "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_concurrent_positions": settings.max_concurrent_positions,
        "max_spread_pips": settings.max_spread_pips,
        "max_slippage_pips": settings.max_slippage_pips,
        "providers": list_provider_status(),
        "agents": agents,
        "host_bound_to_localhost": settings.app_host in {"127.0.0.1", "localhost"},
    }


@router.put("/settings")
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.trading_environment:
        env = body.trading_environment.upper()
        if env not in {"BACKTEST", "PAPER", "LIVE"}:
            raise HTTPException(400, MSG["invalid_environment"])
        if env == "LIVE":
            raise HTTPException(403, MSG["live_permanently_disabled"])
        set_flag(db, "trading_environment", env)
        get_settings().trading_environment = env  # type: ignore[misc]
        account = _account(db)
        account.environment = env
        db.commit()
    if body.kill_switch is not None:
        set_flag(db, "kill_switch", "true" if body.kill_switch else "false")
    db.add(AuditLog(category="settings", message="Definições atualizadas", details=body.model_dump_json()))
    db.commit()
    return get_app_settings(db)


@router.put("/settings/agents/{role}")
def update_agent(role: str, body: AgentUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    if role not in ALL_CONFIGURABLE_ROLES:
        raise HTTPException(404, MSG["unknown_agent"])
    if body.provider not in {"xai", "openai", "anthropic"}:
        raise HTTPException(400, MSG["unknown_provider"])
    cfg = load_agent_config(db, role)
    cfg.provider = body.provider
    cfg.model = body.model
    cfg.enabled = body.enabled
    cfg.temperature = body.temperature
    cfg.display_name = DISPLAY_NAMES[role]
    db.commit()
    return {
        "role": cfg.role,
        "display_name": cfg.display_name,
        "provider": cfg.provider,
        "model": cfg.model,
        "enabled": cfg.enabled,
        "temperature": cfg.temperature,
    }


@router.post("/kill-switch")
def toggle_kill(active: bool = True, db: Session = Depends(get_db)) -> dict[str, Any]:
    set_flag(db, "kill_switch", "true" if active else "false")
    db.add(
        AuditLog(
            category="risk",
            level="WARN",
            message=f"Paragem de emergência definida para {active}",
        )
    )
    db.commit()
    return {"kill_switch": active}


@router.post("/backtest")
def backtest(body: BacktestRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.engine == "ai-sample":
        return run_ai_sample_backtest(db, body.symbol, body.timeframe, body.start, body.end)
    if body.engine == "compare-workflows":
        return compare_workflows(db, body.symbol, body.timeframe, body.start, body.end)
    run = run_baseline_backtest(
        db,
        body.symbol,
        body.timeframe,
        body.start,
        body.end,
        walk_forward=body.walk_forward,
    )
    return {
        "id": run.id,
        "engine": run.engine,
        "trades": run.trades,
        "win_rate": run.win_rate,
        "profit_factor": run.profit_factor,
        "max_drawdown_pct": run.max_drawdown_pct,
        "net_pnl": run.net_pnl,
        "ending_equity": run.ending_equity,
        "is_synthetic": run.is_synthetic,
        "notes": run.notes,
        "metrics": json.loads(run.metrics_json),
    }


@router.post("/backtest/compare-workflows")
def backtest_compare_workflows(body: WorkflowCompareRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    return compare_workflows(
        db,
        body.symbol,
        body.timeframe,
        body.start,
        body.end,
        max_bars=body.max_bars,
        step=body.step,
    )


@router.get("/backtest")
def list_backtests(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(20)))
    return [
        {
            "id": r.id,
            "name": r.name,
            "engine": r.engine,
            "trades": r.trades,
            "win_rate": r.win_rate,
            "profit_factor": r.profit_factor,
            "net_pnl": r.net_pnl,
            "is_synthetic": r.is_synthetic,
            "notes": r.notes,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/evaluation")
def evaluation(body: EvaluationRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = compare_providers_on_history(
        db, body.symbol, body.timeframe, body.start, body.end, body.providers, body.max_bars
    )
    return {
        "id": run.id,
        "results": json.loads(run.results_json),
        "notes": run.notes,
        "data_source": run.data_source,
    }


@router.get("/evaluation")
def list_evaluations(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc()).limit(20)))
    return [
        {
            "id": r.id,
            "name": r.name,
            "results": json.loads(r.results_json),
            "notes": r.notes,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
