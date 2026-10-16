"""
Pipeline multiagente inspirado no TradingAgents, adaptado a Forex EUR/USD.

Preserva os seis agentes do GROK FOREX AI e acrescenta debate Bull/Bear
estruturado antes do Analista de Risco e do Coordenador Principal.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agents import analysis_control
from app.agents.base import is_valid_analysis, run_agent
from app.agents.memory_store import save_decision_memory
from app.agents.prompts import DISPLAY_NAMES
from app.ai.budget import BudgetExceeded, assert_budget_allows
from app.brokers.factory import get_broker
from app.config import get_settings
from app.i18n_pt import MSG
from app.market.forex_adapter import forex_market_snapshot
from app.models.agent import AgentReport, DecisionRecord
from app.models.audit import AuditLog
from app.models.market import EconomicEvent
from app.models.memory import AnalysisRun
from app.risk.engine import ProposedTrade, RiskEngine
from app.schemas.agents import (
    CoordinatorOutput,
    DebateResearcherOutput,
    FundamentalOutput,
    RiskChallengerOutput,
    StrategyOutput,
    TechnicalOutput,
    TradingDecisionPackage,
    TrendOutput,
)
from sqlalchemy import select


def _compact_indicators(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k != "extras"}


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
        "macro_status": "available" if (calendar_available or news_available) else "UNKNOWN",
        "instruction": (
            "Fontes indisponíveis. Não inventes notícias nem eventos. "
            "Se não houver dados macro, reporta UNKNOWN / insufficient_information=true."
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


def _token_stats(reports: list[dict[str, Any]]) -> tuple[int, int, float, list[str]]:
    pt = sum(int(r.get("prompt_tokens") or 0) for r in reports)
    ct = sum(int(r.get("completion_tokens") or 0) for r in reports)
    cost = sum(float(r.get("estimated_cost_usd") or 0) for r in reports)
    models = sorted({f"{r.get('provider')}:{r.get('model')}" for r in reports if r.get("model")})
    return pt, ct, cost, models


def _extract_arguments(report: dict[str, Any]) -> list[str]:
    payload = report.get("payload") or {}
    args: list[str] = []
    if payload.get("argument"):
        args.append(str(payload["argument"]))
    for item in payload.get("key_evidence") or []:
        args.append(str(item))
    for ev in payload.get("evidence") or []:
        if isinstance(ev, dict) and ev.get("claim"):
            args.append(str(ev["claim"]))
        elif isinstance(ev, str):
            args.append(ev)
    if report.get("summary") and not args:
        args.append(str(report["summary"]))
    return args


def _check_limits(run: AnalysisRun, started: float) -> str | None:
    settings = get_settings()
    if analysis_control.is_cancelled(run.id) or run.cancel_requested:
        return "cancelled"
    if (time.perf_counter() - started) > settings.max_analysis_duration_seconds:
        return "timeout"
    return None


def _slim(report: dict[str, Any]) -> dict[str, Any]:
    return {
        k: report[k]
        for k in (
            "role",
            "display_name",
            "status",
            "bias",
            "recommendation",
            "confidence",
            "uncertainty",
            "summary",
            "payload",
            "error",
            "provider",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "estimated_cost_usd",
            "language_ok",
            "language_warning",
            "valid_for_analysis",
            "error_code",
        )
        if k in report
    }


def _failed_agent_entries(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in reports:
        role = str(r.get("role") or "")
        if not role or role in seen:
            continue
        if is_valid_analysis(r):
            continue
        seen.add(role)
        out.append(
            {
                "role": role,
                "display_name": r.get("display_name") or DISPLAY_NAMES.get(role, role),
                "status": r.get("status"),
                "error_code": r.get("error_code") or "",
                "error": (r.get("error") or "")[:500],
            }
        )
    return out


def run_tradingagents_collaboration(
    db: Session,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    as_of: datetime | None = None,
    execute: bool = False,
    backtest_run_id: int = 0,
    source: str | None = None,
    replay_mode: bool = False,
    debate_rounds: int | None = None,
    analysis_run_id: int | None = None,
    enabled_roles: dict[str, bool] | None = None,
    authorization_id: str | None = None,
) -> dict[str, Any]:
    """
    Pipeline A→F: analistas → Bull → Bear → debate → risco → coordenador.
    """
    settings = get_settings()
    if settings.live_trading_locked and settings.trading_environment == "LIVE":
        settings.trading_environment = "PAPER"  # type: ignore[misc]

    rounds_cfg = debate_rounds if debate_rounds is not None else settings.max_debate_rounds
    rounds_cfg = max(0, min(int(rounds_cfg), 4))
    enabled = enabled_roles or {}
    started_perf = time.perf_counter()

    run: AnalysisRun | None = None
    if analysis_run_id:
        run = db.get(AnalysisRun, analysis_run_id)
    if run is None:
        run = AnalysisRun(
            symbol=symbol.upper(),
            timeframe=timeframe.upper(),
            workflow="tradingagents",
            status="queued",
            debate_rounds_requested=rounds_cfg,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

    from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control

    control = get_ai_control()
    try:
        control.begin_analysis_context(authorization_id or "", analysis_run_id=run.id)
        if authorization_id:
            control.bind_authorization(authorization_id, run.id)
    except (AIDisabled, AIAuthorizationError) as exc:
        run.status = "blocked"
        run.error = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "analysis_run_id": run.id,
            "recommendation": "HOLD",
            "risk_approved": False,
            "executed": False,
            "status": "blocked",
            "error": str(exc),
        }

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    run.debate_rounds_requested = rounds_cfg
    db.commit()
    analysis_control.set_active(run.id)
    analysis_control.clear_cancel(run.id)

    stages: dict[str, str] = {
        "market_data": "pending",
        "analysts": "pending",
        "bull_opening": "pending",
        "bear_opening": "pending",
        "debate": "pending",
        "risk": "pending",
        "coordinator": "pending",
    }

    def _checkpoint(partial: dict[str, Any] | None = None) -> None:
        run.result_json = json.dumps(
            {
                "analysis_run_id": run.id,
                "status": run.status,
                "stages": stages,
                "partial": partial or {},
            },
            default=str,
        )
        db.commit()

    def _fail(status: str, message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        run.status = status
        run.error = message
        run.finished_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.perf_counter() - started_perf) * 1000)
        for k, v in list(stages.items()):
            if v == "pending":
                stages[k] = "cancelled" if status == "cancelled" else "error" if status == "failed" else "skipped"
        result = {
            "analysis_run_id": run.id,
            "decision_id": run.decision_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "recommendation": "HOLD",
            "risk_approved": False,
            "risk": {"approved": False, "reasons": [message], "lots": 0.0},
            "executed": False,
            "execution": None,
            "workflow": "tradingagents",
            "package": None,
            "debate": {
                "rounds": [],
                "rounds_completed": 0,
                "debate_executed": False,
                "debate_complete": False,
            },
            "reports": [],
            "stages": stages,
            "diagnostics": {
                "analysis_id": run.id,
                "error": message,
                "data_source": (extra or {}).get("data_source"),
                "timeframe": timeframe,
            },
            "status": status,
            "error": message,
            **(extra or {}),
        }
        run.result_json = json.dumps(result, default=str)
        db.commit()
        analysis_control.set_active(None)
        control.end_analysis_context(authorization_id)
        return result

    try:
        assert_budget_allows(db)
    except BudgetExceeded as exc:
        return _fail("blocked", str(exc), {"budget_blocked": True, "label": str(exc)})

    broker = get_broker(db)
    broker_status = broker.status()
    bid = ask = None
    spread = settings.sim_spread_pips
    try:
        quote = broker.quote(symbol)
        spread = quote.spread_pips
        bid, ask = quote.bid, quote.ask
    except Exception:
        quote = None

    stages["market_data"] = "running"
    _checkpoint()
    snap = forex_market_snapshot(
        db,
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        as_of=as_of,
        source=source,
        replay_mode=replay_mode,
        bid=bid,
        ask=ask,
        spread_pips=spread,
    )

    run.market_snapshot_id = snap.get("snapshot_id") or ""
    run.data_source = str(snap.get("data_source") or "")
    run.data_freshness = str(snap.get("freshness") or "unknown")
    run.is_synthetic = bool(snap.get("is_synthetic"))
    if snap.get("as_of"):
        try:
            run.market_time = datetime.fromisoformat(snap["as_of"])
        except ValueError:
            run.market_time = datetime.now(timezone.utc)
    db.commit()

    if not snap.get("ok"):
        stages["market_data"] = "error"
        return _fail(
            "blocked",
            snap.get("reason") or "Dados de mercado inválidos.",
            {
                "data_source": snap.get("data_source"),
                "is_synthetic": snap.get("is_synthetic"),
                "label": snap.get("label"),
                "market": {
                    "last_price": snap.get("last_price"),
                    "as_of": snap.get("as_of"),
                    "data_source": snap.get("data_source"),
                    "is_synthetic": snap.get("is_synthetic"),
                    "label": snap.get("label"),
                    "spread_pips": snap.get("spread_pips"),
                    "freshness": snap.get("freshness"),
                    "source_resolution": (snap.get("bundle") or {}).get("source_resolution"),
                },
                "block_entries": True,
            },
        )
    stages["market_data"] = "completed"
    _checkpoint()

    last_ts = datetime.fromisoformat(snap["as_of"]) if snap.get("as_of") else datetime.now(timezone.utc)
    fund = _fundamental_payload(db, last_ts)

    decision = DecisionRecord(
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        environment=settings.trading_environment,
        recommendation="HOLD",
        market_time=last_ts,
        data_source=str(snap.get("data_source") or ""),
        close_price=float(snap.get("last_price") or 0.0),
        is_simulated=True,
        backtest_run_id=backtest_run_id,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    run.decision_id = decision.id
    db.commit()

    common = {
        "symbol": symbol.upper(),
        "display": snap.get("display"),
        "timeframe": timeframe.upper(),
        "instrument": snap.get("instrument") or {},
        "market": {
            "last_price": snap.get("last_price"),
            "as_of": snap.get("as_of"),
            "data_source": snap.get("data_source"),
            "is_synthetic": snap.get("is_synthetic"),
            "label": snap.get("label"),
            "spread_pips": snap.get("spread_pips"),
            "bid": snap.get("bid"),
            "ask": snap.get("ask"),
            "freshness": snap.get("freshness"),
            "provenance": snap.get("provenance"),
            "pip_size": snap.get("pip_size"),
            "validation": snap.get("validation"),
        },
        "indicators": {tf: _compact_indicators(v) for tf, v in (snap.get("indicators") or {}).items()},
        "environment": settings.trading_environment,
        "snapshot_id": snap.get("snapshot_id"),
        "workflow": "tradingagents",
    }

    def _role_enabled(role: str) -> bool:
        return enabled.get(role, True)

    def _run_or_skip(role: str, schema: type, payload: dict[str, Any]) -> dict[str, Any]:
        if not _role_enabled(role):
            return {
                "role": role,
                "display_name": DISPLAY_NAMES.get(role, role),
                "provider": "",
                "model": "",
                "status": "disabled",
                "bias": "NEUTRAL",
                "recommendation": "HOLD",
                "confidence": 0.0,
                "uncertainty": 1.0,
                "summary": MSG["agent_disabled"],
                "payload": {},
                "error": MSG["agent_disabled"],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "estimated_cost_usd": 0.0,
                "latency_ms": 0,
            }
        return run_agent(db, role, schema, payload)

    limit = _check_limits(run, started_perf)
    if limit:
        return _fail(limit, "Análise cancelada ou tempo máximo excedido.")

    stages["analysts"] = "running"
    _checkpoint()
    technical = _run_or_skip("technical", TechnicalOutput, common)
    trend = _run_or_skip("trend", TrendOutput, common)
    fundamental = _run_or_skip(
        "fundamental", FundamentalOutput, {**common, "fundamentals": fund, "macro_status": fund.get("macro_status")}
    )
    strategy = _run_or_skip(
        "strategy",
        StrategyOutput,
        {
            **common,
            "peer_notes": {
                "technical": _slim(technical),
                "trend": _slim(trend),
                "fundamental": _slim(fundamental),
            },
            "failed_peers": _failed_agent_entries([technical, trend, fundamental]),
            "instruction": (
                "Ignora por completo qualquer peer com status diferente de ok / valid_for_analysis=false. "
                "Não uses conteúdo inválido como evidência."
            ),
        },
    )
    core_analysts = [technical, trend, fundamental, strategy]
    stages["analysts"] = (
        "completed" if any(is_valid_analysis(r) for r in core_analysts) else "error"
    )

    analyst_pack = {
        "technical": _slim(technical),
        "trend": _slim(trend),
        "fundamental": _slim(fundamental),
        "strategy": _slim(strategy),
    }

    debate_rounds_log: list[dict[str, Any]] = []
    bull_args: list[str] = []
    bear_args: list[str] = []
    all_reports: list[dict[str, Any]] = [technical, trend, fundamental, strategy]
    language_warnings: list[str] = []

    # B — Bull argument inicial
    limit = _check_limits(run, started_perf)
    if limit:
        return _fail(limit, "Análise cancelada ou tempo máximo excedido.")

    stages["bull_opening"] = "running"
    _checkpoint()
    bull = _run_or_skip(
        "bull_researcher",
        DebateResearcherOutput,
        {
            **common,
            "phase": "initial_bull",
            "round_index": 0,
            "analyst_reports": analyst_pack,
            "instruction": (
                "Constrói o argumento COMPRADOR (BUY) mais forte com base apenas nos relatórios "
                "e dados fornecidos. Se a evidência for fraca, reconhece-o. Texto em pt-PT."
            ),
        },
    )
    all_reports.append(bull)
    bull_args.extend(_extract_arguments(bull))
    if bull.get("language_warning"):
        language_warnings.append(f"bull: {bull['language_warning']}")
    if not is_valid_analysis(bull):
        bull_args.append(
            f"[Debate não gerado — Analista Comprador: {bull.get('status')} — {bull.get('error') or 'sem resposta'}]"
        )
    stages["bull_opening"] = "completed" if is_valid_analysis(bull) else "error"

    # C — Bear argument inicial
    stages["bear_opening"] = "running"
    _checkpoint()
    bear = _run_or_skip(
        "bear_researcher",
        DebateResearcherOutput,
        {
            **common,
            "phase": "initial_bear",
            "round_index": 0,
            "analyst_reports": analyst_pack,
            "bull_opening": _slim(bull),
            "instruction": (
                "Constrói o argumento VENDEDOR (SELL) ou de cautela mais forte com base apenas "
                "nos relatórios e dados fornecidos. Contesta o Analista Comprador sem inventar factos. Texto em pt-PT."
            ),
        },
    )
    all_reports.append(bear)
    bear_args.extend(_extract_arguments(bear))
    if bear.get("language_warning"):
        language_warnings.append(f"bear: {bear['language_warning']}")
    if not is_valid_analysis(bear):
        bear_args.append(
            f"[Debate não gerado — Analista Vendedor: {bear.get('status')} — {bear.get('error') or 'sem resposta'}]"
        )
    stages["bear_opening"] = "completed" if is_valid_analysis(bear) else "error"
    debate_rounds_log.append(
        {
            "round": 0,
            "type": "ronda_inicial",
            "label": "Ronda inicial (abertura)",
            "bull": _slim(bull),
            "bear": _slim(bear),
        }
    )

    debate_opening_ok = is_valid_analysis(bull) and is_valid_analysis(bear)

    # D — rondas de debate estruturado
    last_bull, last_bear = bull, bear
    completed_rounds = 0
    debate_rounds_ok = True
    if rounds_cfg == 0:
        stages["debate"] = "skipped" if debate_opening_ok else "error"
    else:
        stages["debate"] = "running"
        _checkpoint()
    for rnd in range(1, rounds_cfg + 1):
        limit = _check_limits(run, started_perf)
        if limit:
            debate_rounds_ok = False
            break
        try:
            assert_budget_allows(db)
        except BudgetExceeded:
            debate_rounds_ok = False
            stages["debate"] = "error"
            break

        bull_r = _run_or_skip(
            "bull_researcher",
            DebateResearcherOutput,
            {
                **common,
                "phase": "debate",
                "round_index": rnd,
                "analyst_reports": analyst_pack,
                "opponent": _slim(last_bear),
                "prior_own": _slim(last_bull),
                "instruction": (
                    f"Ronda de debate {rnd}/{rounds_cfg}. Responde aos pontos do Analista Vendedor "
                    "e reforça a tese de compra só com evidência já fornecida. Texto em pt-PT."
                ),
            },
        )
        bear_r = _run_or_skip(
            "bear_researcher",
            DebateResearcherOutput,
            {
                **common,
                "phase": "debate",
                "round_index": rnd,
                "analyst_reports": analyst_pack,
                "opponent": _slim(bull_r),
                "prior_own": _slim(last_bear),
                "instruction": (
                    f"Ronda de debate {rnd}/{rounds_cfg}. Responde aos pontos do Analista Comprador "
                    "e reforça a tese de venda/cautela só com evidência já fornecida. Texto em pt-PT."
                ),
            },
        )
        all_reports.extend([bull_r, bear_r])
        bull_args.extend(_extract_arguments(bull_r))
        bear_args.extend(_extract_arguments(bear_r))
        if not is_valid_analysis(bull_r) or not is_valid_analysis(bear_r):
            debate_rounds_ok = False
        debate_rounds_log.append(
            {
                "round": rnd,
                "type": "ronda_resposta",
                "label": f"Ronda de resposta {rnd}/{rounds_cfg}",
                "bull": _slim(bull_r),
                "bear": _slim(bear_r),
            }
        )
        last_bull, last_bear = bull_r, bear_r
        completed_rounds = rnd

    if stages["debate"] == "running":
        stages["debate"] = (
            "completed"
            if debate_opening_ok and debate_rounds_ok and completed_rounds == rounds_cfg
            else "error"
        )
    debate_executed = debate_opening_ok
    debate_complete = debate_executed and (rounds_cfg == 0 or (debate_rounds_ok and completed_rounds == rounds_cfg))
    # Semântica: debate_rounds_* = rondas de RESPOSTA (não inclui a ronda inicial).
    run.debate_rounds_completed = completed_rounds
    db.commit()

    failed_before_risk = _failed_agent_entries(all_reports)

    # E — Risk Challenger
    limit = _check_limits(run, started_perf)
    if limit == "cancelled":
        return _fail("cancelled", "Análise cancelada pelo utilizador.")

    stages["risk"] = "running"
    _checkpoint()
    challenger = _run_or_skip(
        "risk_challenger",
        RiskChallengerOutput,
        {
            **common,
            "proposed": strategy.get("payload") if is_valid_analysis(strategy) else {},
            "debate": {
                "rounds": debate_rounds_log,
                "opening_round": "completed" if debate_opening_ok else "error",
                "response_rounds_requested": rounds_cfg,
                "response_rounds_completed": completed_rounds,
                "bull_arguments": bull_args,
                "bear_arguments": bear_args,
            },
            "reports": analyst_pack,
            "failed_agents": failed_before_risk,
            "instruction": (
                "Avalia ambos os lados do debate. Destaca fraquezas, riscos de execução "
                "(spread, volatilidade, dimensão da posição) e podes vetar propostas inseguras. "
                "Ignora agentes listados em failed_agents — as suas saídas NÃO são análise válida."
            ),
        },
    )
    all_reports.append(challenger)
    stages["risk"] = "completed" if is_valid_analysis(challenger) else "error"
    if challenger.get("language_warning"):
        language_warnings.append(f"risk: {challenger['language_warning']}")

    failed_before_coordinator = _failed_agent_entries(all_reports)

    # F — Coordenador Principal
    stages["coordinator"] = "running"
    _checkpoint()
    coordinator = _run_or_skip(
        "coordinator",
        CoordinatorOutput,
        {
            **common,
            "reports": {
                **analyst_pack,
                "bull_researcher": _slim(last_bull) if is_valid_analysis(last_bull) else {
                    "role": "bull_researcher",
                    "status": last_bull.get("status"),
                    "valid_for_analysis": False,
                    "error": last_bull.get("error"),
                },
                "bear_researcher": _slim(last_bear) if is_valid_analysis(last_bear) else {
                    "role": "bear_researcher",
                    "status": last_bear.get("status"),
                    "valid_for_analysis": False,
                    "error": last_bear.get("error"),
                },
                "risk_challenger": _slim(challenger) if is_valid_analysis(challenger) else {
                    "role": "risk_challenger",
                    "status": challenger.get("status"),
                    "valid_for_analysis": False,
                    "error": challenger.get("error"),
                },
            },
            "failed_agents": failed_before_coordinator,
            "debate": {
                "opening_round": "completed" if debate_opening_ok else "error",
                "response_rounds_requested": rounds_cfg,
                "response_rounds_completed": completed_rounds,
                "debate_executed": debate_executed,
                "debate_complete": debate_complete,
                "bull_arguments": bull_args,
                "bear_arguments": bear_args,
            },
            "instruction": (
                "Decide BUY, SELL ou HOLD. Pesa o veto do Analista de Risco. "
                "Em caso de conflito forte entre Comprador e Vendedor, prefere HOLD. "
                "Se o debate NÃO foi executado com sucesso, não afirmes que houve consenso de debate — prefere HOLD. "
                "OBRIGATÓRIO: consulta failed_agents. Não uses conteúdo de agentes com falha "
                "(error, invalid_json, language_rejected, unavailable) como evidência. "
                "Menciona explicitamente na rationale quais agentes falharam, se existirem. "
                "Texto em pt-PT."
            ),
        },
    )
    all_reports.append(coordinator)
    stages["coordinator"] = "completed" if is_valid_analysis(coordinator) else "error"
    if coordinator.get("language_warning"):
        language_warnings.append(f"coordinator: {coordinator['language_warning']}")
    for r in (technical, trend, fundamental, strategy):
        if r.get("language_warning"):
            language_warnings.append(f"{r['role']}: {r['language_warning']}")

    for report in all_reports:
        _persist_report(db, decision.id, report)

    payload = coordinator.get("payload") or {}
    recommendation = payload.get("final_recommendation") or coordinator.get("recommendation") or "HOLD"
    if coordinator.get("status") != "ok":
        recommendation = "HOLD"
    # Sem debate executado, não apresentar BUY/SELL como resultado de debate completo.
    if not debate_executed and recommendation in {"BUY", "SELL"}:
        recommendation = "HOLD"
        disagreement_note = (
            "Debate Comprador/Vendedor não foi executado com sucesso — decisão forçada para HOLD."
        )
    else:
        disagreement_note = ""

    veto = bool((challenger.get("payload") or {}).get("veto"))
    veto_reason = str((challenger.get("payload") or {}).get("veto_reason") or "")
    if veto:
        recommendation = "HOLD"

    # Dados obsoletos em PAPER wall-clock: bloquear entradas
    stale = bool(snap.get("is_stale")) and not replay_mode and settings.trading_environment not in {"BACKTEST"}
    block_entries = bool(snap.get("block_entries")) and not replay_mode

    strat_payload = strategy.get("payload") or {}
    entry = float(strat_payload.get("entry_price") or snap.get("last_price") or 0.0)
    stop = float(strat_payload.get("stop_loss") or 0.0)
    take = strat_payload.get("take_profit")
    take_f = float(take) if take else None

    risk = RiskEngine(db, settings).evaluate(
        ProposedTrade(
            symbol=symbol.upper(),
            side=recommendation if recommendation in {"BUY", "SELL"} else "HOLD",
            entry_price=entry,
            stop_loss=stop,
            take_profit=take_f,
            spread_pips=float(snap.get("spread_pips") or settings.sim_spread_pips),
            expected_slippage_pips=settings.sim_slippage_pips,
            market_ts=last_ts,
            broker_connected=broker_status.connected,
            data_is_stale=stale or block_entries,
        )
    )

    approved = bool(
        risk.approved and recommendation in {"BUY", "SELL"} and not veto and not block_entries
    )
    if block_entries and recommendation in {"BUY", "SELL"}:
        risk.reasons.append(snap.get("reason") or "Entradas bloqueadas por qualidade/frescura dos dados.")

    confidence = float(payload.get("confidence") or coordinator.get("confidence") or 0.0)
    key_evidence: list[str] = []
    for r in (technical, trend, fundamental, strategy):
        for ev in (r.get("payload") or {}).get("evidence") or []:
            if isinstance(ev, dict) and ev.get("claim"):
                key_evidence.append(str(ev["claim"]))
    risk_objections = list((challenger.get("payload") or {}).get("contradictions") or [])
    risk_objections.extend((challenger.get("payload") or {}).get("execution_risks") or [])
    if veto_reason:
        risk_objections.append(veto_reason)

    disagreement = payload.get("dissenting_views") or []
    if disagreement_note:
        disagreement_summary = disagreement_note
    elif disagreement:
        disagreement_summary = "; ".join(str(x) for x in disagreement)
    else:
        disagreement_summary = (
            "Sem divergências explícitas registadas."
            if (coordinator.get("payload") or {}).get("agreement_score", 0) >= 0.7
            else "Existe divergência entre os agentes; ver relatórios e debate."
        )
    if not debate_complete:
        disagreement_summary = (
            (disagreement_summary + " ") if disagreement_summary else ""
        ) + "O debate completo não foi concluído — não tratar esta decisão como consenso de debate."

    pt, ct, cost, models = _token_stats(all_reports)
    final_explanation = coordinator.get("summary") or payload.get("rationale") or ""
    if not debate_executed:
        final_explanation = (
            "Debate não executado (Analista Comprador/Vendedor indisponível ou com erro). "
            "A decisão não resulta de um debate completo. "
            + (final_explanation or "HOLD por precaução.")
        )

    package = TradingDecisionPackage(
        instrument=str(snap.get("display") or symbol),
        market_data_timestamp=str(snap.get("as_of") or ""),
        timeframe=timeframe.upper(),
        direction=recommendation if recommendation in {"BUY", "SELL", "HOLD"} else "HOLD",
        confidence_score=confidence if debate_executed else min(confidence, 0.2),
        bull_arguments=bull_args[:12],
        bear_arguments=bear_args[:12],
        key_evidence=key_evidence[:12],
        risk_objections=risk_objections[:12],
        entry_proposal=entry if recommendation in {"BUY", "SELL"} else None,
        stop_loss_proposal=stop if stop else None,
        take_profit_proposal=take_f,
        final_explanation=final_explanation,
        agent_disagreement_summary=disagreement_summary,
        risk_veto=veto,
        risk_veto_reason=veto_reason,
        snapshot_id=str(snap.get("snapshot_id") or ""),
        data_freshness=str(snap.get("freshness") or "unknown"),
        data_source=str(snap.get("data_source") or ""),
        is_synthetic=bool(snap.get("is_synthetic")),
        models_used=models,
        prompt_tokens=pt,
        completion_tokens=ct,
        estimated_cost_usd=cost,
        debate_rounds=completed_rounds,
        debate_executed=debate_executed,
        debate_complete=debate_complete,
        workflow="tradingagents",
    )
    package_dict = package.model_dump()

    decision.recommendation = recommendation
    decision.coordinator_rationale = package.final_explanation
    decision.risk_approved = approved
    decision.risk_reason = "; ".join(risk.reasons)
    db.add(
        AuditLog(
            category="decision",
            level="INFO" if approved else "WARN",
            message=f"[tradingagents] {recommendation} risk_approved={approved}",
            details=json.dumps(
                {
                    "decision_id": decision.id,
                    "analysis_run_id": run.id,
                    "reasons": risk.reasons,
                    "lots": risk.lots,
                    "data_source": snap.get("data_source"),
                    "is_synthetic": snap.get("is_synthetic"),
                    "replay_mode": replay_mode,
                    "debate_rounds": completed_rounds,
                    "veto": veto,
                }
            ),
        )
    )

    execution: dict[str, Any] | None = None
    if execute and approved and not settings.live_authorized:
        if replay_mode or settings.trading_environment != "LIVE":
            from app.execution.engine import execute_approved_decision

            execution = execute_approved_decision(db, decision, risk, symbol.upper(), recommendation, stop, take_f)

    memory = save_decision_memory(
        db,
        analysis_run_id=run.id,
        decision_id=decision.id,
        market_snapshot_id=str(snap.get("snapshot_id") or ""),
        analysis_timestamp=last_ts,
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        agent_reports=analyst_pack,
        debate={
            "rounds": debate_rounds_log,
            "bull_arguments": bull_args,
            "bear_arguments": bear_args,
            "debate_executed": debate_executed,
            "debate_complete": debate_complete,
        },
        final_decision=package_dict,
        simulated_execution=execution,
    )

    overall = "completed"
    if any(v == "error" for v in stages.values()):
        overall = "completed_with_errors"
    if not debate_executed:
        overall = "completed_without_debate"

    run.status = overall
    run.recommendation = recommendation
    run.confidence = float(package.confidence_score)
    run.risk_veto = veto
    run.risk_veto_reason = veto_reason
    run.prompt_tokens = pt
    run.completion_tokens = ct
    run.estimated_cost_usd = cost
    run.models_used_json = json.dumps(models)
    run.finished_at = datetime.now(timezone.utc)
    run.duration_ms = int((time.perf_counter() - started_perf) * 1000)
    run.error = "; ".join(language_warnings) if language_warnings else ""

    diagnostics = {
        "analysis_id": run.id,
        "decision_id": decision.id,
        "datetime_utc": (snap.get("as_of") or datetime.now(timezone.utc).isoformat()),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "data_source": snap.get("data_source"),
        "is_synthetic": snap.get("is_synthetic"),
        "label": snap.get("label"),
        "timeframe": timeframe.upper(),
        "models": models,
        "stages": stages,
        "debate_rounds_requested": rounds_cfg,
        "debate_rounds_completed": completed_rounds,
        "opening_round": "completed" if debate_opening_ok else "error",
        "response_rounds_requested": rounds_cfg,
        "response_rounds_completed": completed_rounds,
        "rounds_label": (
            f"Ronda inicial ({'ok' if debate_opening_ok else 'erro'}) + "
            f"{completed_rounds}/{rounds_cfg} rondas de resposta"
        ),
        "debate_executed": debate_executed,
        "debate_complete": debate_complete,
        "failed_agents": _failed_agent_entries(all_reports),
        "estimated_cost_usd": cost,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "language_warnings": language_warnings,
        "source_resolution": (snap.get("bundle") or {}).get("source_resolution")
        or common.get("market", {}).get("provenance"),
        "errors": [r.get("error") for r in all_reports if r.get("error")],
    }

    result = {
        "analysis_run_id": run.id,
        "memory_id": memory.id,
        "decision_id": decision.id,
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "recommendation": recommendation,
        "confidence": package.confidence_score,
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
        "data_source": snap.get("data_source"),
        "is_synthetic": snap.get("is_synthetic"),
        "label": snap.get("label"),
        "market": common["market"],
        "reports": all_reports,
        "debate": {
            "opening_round": "completed" if debate_opening_ok else "error",
            "response_rounds_requested": rounds_cfg,
            "response_rounds_completed": completed_rounds,
            "rounds_completed": completed_rounds,
            "rounds_label": (
                f"Ronda inicial ({'ok' if debate_opening_ok else 'erro'}) + "
                f"{completed_rounds}/{rounds_cfg} rondas de resposta"
            ),
            "rounds": debate_rounds_log,
            "debate_executed": debate_executed,
            "debate_complete": debate_complete,
            "bull_arguments": bull_args,
            "bear_arguments": bear_args,
        },
        "package": package_dict,
        "stages": stages,
        "diagnostics": diagnostics,
        "workflow": "tradingagents",
        "status": overall,
        "block_entries": block_entries,
        "budget_blocked": False,
    }
    run.result_json = json.dumps(result, default=str)
    db.commit()
    analysis_control.set_active(None)
    control.end_analysis_context(authorization_id)
    analysis_control.clear_cancel(run.id)
    return result
