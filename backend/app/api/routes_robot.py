"""API do robô de simulação com motor IA."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control
from app.config import get_settings
from app.database import get_db
from app.robot.ai_engine import estimate_single_analysis_usd
from app.robot.service import get_robot_service

router = APIRouter(prefix="/robot", tags=["robot"])


class ManualAIBody(BaseModel):
    confirmed: bool = False
    auth_token: str | None = None
    # Por defeito false — análise sem abrir ordens (nem simuladas).
    allow_execution: bool = False
    max_cost_usd: float = 0.01


class AutonomousActivateBody(BaseModel):
    confirmed: bool = False


class DemoActivateBody(BaseModel):
    confirmed: bool = False


class DemoPauseBody(BaseModel):
    paused: bool = True


class DemoCloseBody(BaseModel):
    confirmed: bool = False


@router.get("/status")
def robot_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_robot_service().status(db)


@router.get("/directional-audit")
def robot_directional_audit(
    db: Session = Depends(get_db),
    force_history: bool = True,
    run_oos: bool = True,
) -> dict[str, Any]:
    """Auditoria do motor quantitativo — sem OpenAI e sem ordens DEMO."""
    from app.robot.directional_audit import run_directional_audit

    return run_directional_audit(
        db, force_history=force_history, run_oos=run_oos
    )


@router.get("/backtest/actionable")
def robot_backtest_actionable(
    force_history: bool = False,
    run: bool = False,
) -> dict[str, Any]:
    """Walk-forward da lógica acionável — sem ordens MT5 e sem OpenAI.

    run=false (defeito): devolve o último relatório gravado / resumo painel.
    run=true: recalcula (pode demorar alguns minutos).
    """
    from app.robot.actionable_backtest import (
        load_latest_backtest_report,
        panel_backtest_payload,
        run_universe_actionable_backtest,
    )

    if run:
        return run_universe_actionable_backtest(force_history=force_history)
    full = load_latest_backtest_report()
    if full:
        return {"available": True, **panel_backtest_payload(), "full_aggregate": full.get("aggregate")}
    return panel_backtest_payload()


@router.post("/start")
def robot_start(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    if settings.live_authorized or settings.trading_environment == "LIVE":
        raise HTTPException(403, "Negociação real bloqueada — apenas simulação.")
    return get_robot_service().start(db)


@router.post("/stop")
def robot_stop(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_robot_service().stop(db)


@router.get("/ai/estimate")
def robot_ai_estimate(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Estimativa de custo/tokens para UMA análise OpenAI (mono ou radar) — não executa."""
    from app.robot.ai_engine import estimate_radar_analysis_usd
    from app.robot.radar import scan_radar
    from app.robot.readiness import assess_data_readiness

    est = estimate_single_analysis_usd(db=db)
    ready = assess_data_readiness(db, recover_feed=True)
    scan = scan_radar(db)
    radar_est = estimate_radar_analysis_usd(scan)
    est["readiness"] = ready.as_dict()
    est["radar"] = {
        "monitored": scan.monitored,
        "with_valid_data": scan.with_valid_data,
        "candidates": [c.symbol for c in scan.candidates],
        "estimate": radar_est,
    }
    return est


@router.post("/ai/authorize")
def robot_ai_authorize(body: ManualAIBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Autoriza explicitamente uma única chamada paga (não inicia o ciclo automático pago)."""
    control = get_ai_control()
    try:
        result = control.create_authorization(
            db,
            confirmed=body.confirmed,
            workflow="robot_ai_single",
            debate_rounds=0,
        )
    except (AIDisabled, AIAuthorizationError) as exc:
        raise HTTPException(403, str(exc)) from exc

    # Forçar 1 chamada — o robô não usa debates multi-agente
    token = str(result.get("authorization_id") or "")
    est = estimate_single_analysis_usd(db=db)
    est_cost = float(est["estimated_cost_usd"])
    cap = float(body.max_cost_usd or 0.01)
    if est_cost > cap:
        raise HTTPException(
            403,
            f"Estimativa ${est_cost:.4f} ultrapassa o teto autorizado ${cap:.4f} — chamada não autorizada.",
        )
    with control._lock:
        auth = control._auths.get(token)
        if auth is not None:
            auth.max_calls = 1
            auth.reserved_cost_usd = est_cost
            auth.max_cost_usd = min(cap, max(est_cost * 1.5, est_cost))
    result["max_calls"] = 1
    result["reserved_cost_usd"] = est_cost
    result["max_cost_cap_usd"] = cap
    result["single_call_estimate"] = est
    result["note"] = "Uma única chamada OpenAI autorizada. O ciclo INICIAR continua em modo simulado."
    return result


@router.get("/ai/autonomous")
def robot_ai_autonomous_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Estado de preparação do cérebro OpenAI autónomo — sem chamada paga."""
    from app.robot.openai_autonomous import get_openai_autonomous

    return get_openai_autonomous().preparation_status(db)


@router.post("/ai/autonomous/arm")
def robot_ai_autonomous_arm(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Prepara parâmetros (ainda sem chamadas pagas)."""
    from app.robot.openai_autonomous import get_openai_autonomous

    return get_openai_autonomous().arm(db)


@router.post("/ai/autonomous/activate")
def robot_ai_autonomous_activate(
    body: AutonomousActivateBody, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Confirmação explícita no programa — habilita agendamento 15 min (sem ordens)."""
    from app.robot.openai_autonomous import get_openai_autonomous

    settings = get_settings()
    if settings.live_authorized or settings.trading_environment == "LIVE":
        raise HTTPException(403, "Negociação real bloqueada.")
    try:
        return get_openai_autonomous().confirm_activation(db, confirmed=bool(body.confirmed))
    except (AIDisabled, AIAuthorizationError) as exc:
        raise HTTPException(403, str(exc)) from exc


@router.post("/ai/autonomous/deactivate")
def robot_ai_autonomous_deactivate(db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.robot.openai_autonomous import get_openai_autonomous

    return get_openai_autonomous().deactivate(db)


@router.post("/ai/manual")
def robot_ai_manual(body: ManualAIBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Teste real da API — manual, com token de autorização. Não é o ciclo INICIAR."""
    settings = get_settings()
    if settings.live_authorized or settings.trading_environment == "LIVE":
        raise HTTPException(403, "Negociação real bloqueada.")
    if not body.confirmed:
        raise HTTPException(400, "Confirmação explícita obrigatória (confirmed=true).")
    if not body.auth_token:
        raise HTTPException(400, "auth_token obrigatório — obtenha via /robot/ai/authorize.")
    control = get_ai_control()
    if not control.is_enabled():
        raise HTTPException(403, "IA desligada (AI_ENABLED=false). Ative antes do teste pago.")
    est = estimate_single_analysis_usd(db=db)
    if float(est["estimated_cost_usd"]) > float(body.max_cost_usd or 0.01):
        raise HTTPException(
            403,
            f"Estimativa ${est['estimated_cost_usd']:.4f} > teto ${body.max_cost_usd:.4f} — não executada.",
        )
    # Pré-check: se dados inválidos, devolver AGUARDAR sem gastar autorização/API.
    from app.robot.readiness import assess_data_readiness, hold_for_readiness
    from app.robot.ai_schema import DECISION_PT

    ready = assess_data_readiness(db, recover_feed=True)
    if not ready.ok_for_paid:
        hold = hold_for_readiness(ready)
        return {
            "decision": hold.decision,
            "decision_pt": DECISION_PT.get(hold.decision),
            "proposal": hold.as_dict(),
            "executed": False,
            "skipped_paid": True,
            "readiness": ready.as_dict(),
            "analysis_kind": ready.analysis_kind,
            "mode": "blocked_no_data",
            "live_trading_locked": True,
            "auto_paid_ai": False,
            "actual_cost_usd": 0.0,
            "note": "API paga não chamada — dados indisponíveis ou demasiado atrasados.",
        }
    try:
        return get_robot_service().run_manual_openai(
            db,
            auth_token=body.auth_token,
            allow_execution=bool(body.allow_execution) and ready.analysis_kind == "fresh",
        )
    except (AIDisabled, AIAuthorizationError) as exc:
        raise HTTPException(403, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


def _demo_panel(db: Session) -> dict[str, Any]:
    from app.brokers.mt5_demo_auth import get_demo_auth_snapshot
    from app.market.mt5_feed import get_mt5_feed
    from app.robot.panel_view import mask_login

    snap = get_mt5_feed().snapshot().as_dict()
    auth = get_demo_auth_snapshot(db)
    return {
        **auth,
        "login_masked": mask_login(auth.get("login") or snap.get("account_login")),
        "trade_mode": snap.get("trade_mode"),
        "trade_mode_label": snap.get("trade_mode_label"),
        "balance": snap.get("balance"),
        "equity": snap.get("equity"),
        "currency": snap.get("currency") or "USD",
        "server_live": snap.get("account_server"),
        "login_live": snap.get("account_login"),
        "live_trading_locked": True,
    }


@router.get("/demo")
def robot_demo_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _demo_panel(db)


@router.post("/demo/activate")
def robot_demo_activate(
    body: DemoActivateBody, db: Session = Depends(get_db)
) -> dict[str, Any]:
    from app.brokers.mt5_demo_auth import activate_demo
    from app.market.mt5_feed import get_mt5_feed

    settings = get_settings()
    if settings.live_authorized or settings.trading_environment == "LIVE":
        raise HTTPException(403, "Negociação real bloqueada.")
    feed = get_mt5_feed()
    try:
        feed.poll_once()
    except Exception:  # noqa: BLE001
        pass
    snap = feed.snapshot().as_dict()
    if not snap.get("terminal_connected"):
        raise HTTPException(403, "MT5 desligado — não é possível autorizar DEMO.")
    if snap.get("trade_mode_label") != "DEMO" and snap.get("trade_mode") != 0:
        raise HTTPException(
            403,
            f"Conta não é DEMO (trade_mode={snap.get('trade_mode_label') or snap.get('trade_mode')}).",
        )
    login = snap.get("account_login")
    server = snap.get("account_server")
    if not login or not server:
        raise HTTPException(403, "Login/servidor MT5 indisponíveis.")
    try:
        activate_demo(db, confirmed=bool(body.confirmed), login=login, server=server)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return _demo_panel(db)


@router.post("/demo/deactivate")
def robot_demo_deactivate(db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.brokers.mt5_demo_auth import deactivate_demo

    deactivate_demo(db)
    return _demo_panel(db)


@router.post("/demo/pause")
def robot_demo_pause(body: DemoPauseBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.brokers.mt5_demo_auth import pause_new_entries

    pause_new_entries(db, paused=bool(body.paused))
    return _demo_panel(db)


@router.post("/demo/emergency")
def robot_demo_emergency(db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.brokers.mt5_demo_auth import trigger_emergency

    trigger_emergency(db)
    return _demo_panel(db)


@router.post("/demo/close-positions")
def robot_demo_close_positions(
    body: DemoCloseBody, db: Session = Depends(get_db)
) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(400, "confirmed=true é obrigatório.")
    from app.brokers.mt5_demo_executor import get_mt5_demo_executor

    result = get_mt5_demo_executor().close_robot_positions(db)
    return {"ok": bool(result.get("ok")), **result, "panel": _demo_panel(db)}
