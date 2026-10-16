"""Six-agent collaboration workflow. Agents never execute orders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agents.base import run_agent
from app.brokers.factory import get_broker
from app.config import get_settings
from app.market.data_store import market_bundle
from app.models.agent import AgentReport, DecisionRecord
from app.models.audit import AuditLog
from app.models.market import EconomicEvent
from app.risk.engine import ProposedTrade, RiskEngine
from app.risk.instruments import get_instrument
from app.schemas.agents import (
    CoordinatorOutput,
    FundamentalOutput,
    RiskChallengerOutput,
    StrategyOutput,
    TechnicalOutput,
    TrendOutput,
)
from sqlalchemy import select


def _compact_indicators(raw: dict[str, Any]) -> dict[str, Any]:
    skip = {"extras"}
    return {k: v for k, v in raw.items() if k not in skip}


def _fundamental_payload(db: Session, as_of: datetime) -> dict[str, Any]:
    settings = get_settings()
    events = list(
        db.scalars(
            select(EconomicEvent)
            .where(EconomicEvent.event_time <= as_of)
            .order_by(EconomicEvent.event_time.desc())
            .limit(20)
        )
    )
    calendar_available = bool(events)
    news_available = bool(settings.news_api_key)
    return {
        "calendar_available": calendar_available,
        "news_available": news_available,
        "events": [
            {
                "time": e.event_time.isoformat(),
                "currency": e.currency,
                "title": e.title,
                "impact": e.impact,
                "actual": e.actual,
                "forecast": e.forecast,
                "previous": e.previous,
                "source": e.source,
            }
            for e in events
        ],
        "news": [],
        "instruction": (
            "Fontes indisponíveis. Não inventes notícias nem eventos de calendário."
            if not (calendar_available or news_available)
            else "Utiliza apenas os eventos listados. Não acrescentes afirmações sem fonte."
        ),
    }


def _persist_report(db: Session, decision_id: int, report: dict[str, Any]) -> AgentReport:
    row = AgentReport(
        decision_id=decision_id,
        role=report["role"],
        provider=report["provider"],
        model=report["model"],
        status=report["status"],
        bias=report.get("bias", "NEUTRAL"),
        confidence=float(report.get("confidence") or 0),
        uncertainty=float(report.get("uncertainty") or 1),
        summary=report.get("summary") or "",
        payload_json=json.dumps(report.get("payload") or {}, default=str),
        error=report.get("error") or "",
        prompt_tokens=report.get("prompt_tokens") or 0,
        completion_tokens=report.get("completion_tokens") or 0,
        estimated_cost_usd=report.get("estimated_cost_usd") or 0.0,
        latency_ms=report.get("latency_ms") or 0,
    )
    db.add(row)
    return row


def _is_stale(market_ts: datetime | None, stale_seconds: int) -> bool:
    if market_ts is None:
        return True
    now = datetime.now(timezone.utc)
    if market_ts.tzinfo is None:
        market_ts = market_ts.replace(tzinfo=timezone.utc)
    return (now - market_ts).total_seconds() > stale_seconds


def run_collaboration(
    db: Session,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    as_of: datetime | None = None,
    execute: bool = False,
    backtest_run_id: int = 0,
    source: str | None = None,
    replay_mode: bool = False,
    workflow: str | None = None,
    debate_rounds: int | None = None,
    analysis_run_id: int | None = None,
    enabled_roles: dict[str, bool] | None = None,
    authorization_id: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    from app.ai.control import get_ai_control

    control = get_ai_control()
    # Gate antecipado: sem IA ligada / autorização, não entrar no pipeline pago.
    if not control.is_enabled():
        return {
            "recommendation": "HOLD",
            "risk_approved": False,
            "executed": False,
            "status": "blocked",
            "error": "IA desligada (AI_ENABLED=false). Nenhuma chamada foi efetuada.",
            "ai_disabled": True,
        }
    if settings.ai_require_manual_approval and not authorization_id:
        return {
            "recommendation": "HOLD",
            "risk_approved": False,
            "executed": False,
            "status": "blocked",
            "error": "Autorização de análise em falta. Confirme o custo estimado antes de iniciar.",
            "ai_auth_required": True,
        }

    # Predefinição e botões do painel: sempre TradingAgents (com debate).
    # O fluxo clássico só corre se workflow="classic" for pedido explicitamente.
    selected = (workflow or settings.default_analysis_workflow or "tradingagents").lower()
    if selected not in {"classic", "legacy", "six_only"}:
        selected = "tradingagents"
    if selected in {"tradingagents", "debate", "ta"}:
        from app.agents.tradingagents_pipeline import run_tradingagents_collaboration

        return run_tradingagents_collaboration(
            db,
            symbol=symbol,
            timeframe=timeframe,
            as_of=as_of,
            execute=execute,
            backtest_run_id=backtest_run_id,
            source=source,
            replay_mode=replay_mode,
            debate_rounds=debate_rounds,
            analysis_run_id=analysis_run_id,
            enabled_roles=enabled_roles,
            authorization_id=authorization_id,
        )

    if settings.live_trading_locked and settings.trading_environment == "LIVE":
        settings.trading_environment = "PAPER"  # type: ignore[misc]

    from app.ai.budget import BudgetExceeded, assert_budget_allows

    try:
        assert_budget_allows(db)
    except BudgetExceeded as exc:
        return {
            "decision_id": 0,
            "symbol": symbol,
            "timeframe": timeframe,
            "recommendation": "HOLD",
            "risk_approved": False,
            "risk": {"approved": False, "reasons": [str(exc)], "lots": 0.0},
            "executed": False,
            "execution": None,
            "data_source": source or "unknown",
            "is_synthetic": source != "dukascopy",
            "label": str(exc),
            "market": {},
            "reports": [],
            "budget_blocked": True,
        }

    bundle = market_bundle(db, symbol, timeframe, as_of=as_of, source=source)
    last_ts = datetime.fromisoformat(bundle["as_of"]) if bundle.get("as_of") else None
    broker = get_broker(db)
    broker_status = broker.status()
    try:
        quote = broker.quote(symbol)
        spread = quote.spread_pips
    except Exception:
        spread = settings.sim_spread_pips
        quote = None

    spec = get_instrument(symbol)
    fund = _fundamental_payload(db, last_ts or datetime.now(timezone.utc))

    decision = DecisionRecord(
        symbol=symbol,
        timeframe=timeframe,
        environment=settings.trading_environment,
        recommendation="HOLD",
        market_time=last_ts or datetime.now(timezone.utc),
        data_source=bundle["data_source"],
        close_price=bundle.get("last_price") or 0.0,
        is_simulated=True,
        backtest_run_id=backtest_run_id,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)

    common = {
        "symbol": symbol,
        "display": spec.display,
        "timeframe": timeframe,
        "instrument": spec.__dict__,
        "market": {
            "last_price": bundle.get("last_price"),
            "as_of": bundle.get("as_of"),
            "data_source": bundle.get("data_source"),
            "is_synthetic": bundle.get("is_synthetic"),
            "label": bundle.get("label"),
            "spread_pips": spread,
        },
        "indicators": {tf: _compact_indicators(v) for tf, v in bundle["indicators"].items()},
        "environment": settings.trading_environment,
    }

    technical = run_agent(db, "technical", TechnicalOutput, common)
    trend = run_agent(db, "trend", TrendOutput, common)
    fundamental = run_agent(db, "fundamental", FundamentalOutput, {**common, "fundamentals": fund})
    strategy = run_agent(
        db,
        "strategy",
        StrategyOutput,
        {
            **common,
            "peer_notes": {
                "technical": {k: technical[k] for k in ("status", "bias", "recommendation", "summary", "payload")},
                "trend": {k: trend[k] for k in ("status", "bias", "recommendation", "summary", "payload")},
                "fundamental": {
                    k: fundamental[k] for k in ("status", "bias", "recommendation", "summary", "payload")
                },
            },
        },
    )
    challenger = run_agent(
        db,
        "risk_challenger",
        RiskChallengerOutput,
        {
            **common,
            "proposed": strategy.get("payload") or {},
            "reports": {
                "technical": technical,
                "trend": trend,
                "fundamental": fundamental,
                "strategy": strategy,
            },
        },
    )
    coordinator = run_agent(
        db,
        "coordinator",
        CoordinatorOutput,
        {
            **common,
            "reports": {
                "technical": technical,
                "trend": trend,
                "fundamental": fundamental,
                "strategy": strategy,
                "risk_challenger": challenger,
            },
        },
    )

    reports = [technical, trend, fundamental, strategy, challenger, coordinator]
    for report in reports:
        _persist_report(db, decision.id, report)

    payload = coordinator.get("payload") or {}
    recommendation = payload.get("final_recommendation") or coordinator.get("recommendation") or "HOLD"
    if coordinator.get("status") != "ok":
        recommendation = "HOLD"

    veto = bool((challenger.get("payload") or {}).get("veto"))
    if veto:
        recommendation = "HOLD"

    # Historical replay / BACKTEST: never treat bars as "stale wall-clock".
    stale = False
    if not replay_mode and settings.trading_environment not in {"BACKTEST"}:
        try:
            from app.market.quote_feed import get_quote_feed

            feed = get_quote_feed().snapshot()
            if feed.quote_ts is not None:
                stale = bool(feed.block_entries)
            else:
                stale = _is_stale(last_ts, settings.stale_data_seconds)
        except Exception:
            stale = _is_stale(last_ts, settings.stale_data_seconds)
    strat_payload = strategy.get("payload") or {}
    entry = float(strat_payload.get("entry_price") or bundle.get("last_price") or 0.0)
    stop = float(strat_payload.get("stop_loss") or 0.0)
    take = strat_payload.get("take_profit")
    take_f = float(take) if take else None

    risk = RiskEngine(db, settings).evaluate(
        ProposedTrade(
            symbol=symbol,
            side=recommendation if recommendation in {"BUY", "SELL"} else "HOLD",
            entry_price=entry,
            stop_loss=stop,
            take_profit=take_f,
            spread_pips=spread,
            expected_slippage_pips=settings.sim_slippage_pips,
            market_ts=last_ts or datetime.now(timezone.utc),
            broker_connected=broker_status.connected,
            data_is_stale=stale,
        )
    )

    approved = bool(risk.approved and recommendation in {"BUY", "SELL"} and not veto)
    decision.recommendation = recommendation
    decision.coordinator_rationale = coordinator.get("summary") or ""
    decision.risk_approved = approved
    decision.risk_reason = "; ".join(risk.reasons)
    db.add(
        AuditLog(
            category="decision",
            level="INFO" if approved else "WARN",
            message=f"{recommendation} risk_approved={approved}",
            details=json.dumps(
                {
                    "decision_id": decision.id,
                    "reasons": risk.reasons,
                    "lots": risk.lots,
                    "data_source": bundle["data_source"],
                    "is_synthetic": bundle["is_synthetic"],
                    "replay_mode": replay_mode,
                }
            ),
        )
    )
    db.commit()

    execution: dict[str, Any] | None = None
    # LIVE is permanently locked this phase. Replay may execute on the simulated broker.
    if execute and approved and not settings.live_authorized:
        if replay_mode or settings.trading_environment != "LIVE":
            from app.execution.engine import execute_approved_decision

            execution = execute_approved_decision(db, decision, risk, symbol, recommendation, stop, take_f)

    return {
        "decision_id": decision.id,
        "symbol": symbol,
        "timeframe": timeframe,
        "recommendation": recommendation,
        "risk_approved": approved,
        "risk": {
            "approved": risk.approved,
            "reasons": risk.reasons,
            "lots": risk.lots,
            "risk_amount": risk.risk_amount,
            "risk_pct": risk.risk_pct,
            "risk_reward": risk.risk_reward,
        },
        "executed": bool(execution and execution.get("accepted")),
        "execution": execution,
        "data_source": bundle["data_source"],
        "is_synthetic": bundle["is_synthetic"],
        "label": bundle["label"],
        "market": common["market"],
        "reports": reports,
        "workflow": "classic",
        "debate": {"rounds_completed": 0, "rounds": []},
        "package": None,
        "status": "completed",
    }
