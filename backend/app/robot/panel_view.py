"""Vista estruturada do painel do robô — estados claros e motivo de inatividade.

Não envia ordens. Não inventa timestamps. Não corrige relógio MT5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def mask_login(login: int | str | None) -> str:
    raw = str(login or "").strip()
    if not raw:
        return "—"
    if len(raw) <= 4:
        return "****"
    return f"{raw[:2]}…{raw[-3:]}"


def resolve_robot_state(*, running: bool, thread_alive: bool, block_reason: str) -> dict[str, str]:
    if running and thread_alive:
        return {"code": "monitoring", "label": "A monitorizar"}
    if block_reason and ("erro" in block_reason.lower() or "falha" in block_reason.lower()):
        return {"code": "error", "label": "Erro"}
    if running and not thread_alive:
        return {"code": "error", "label": "Erro"}
    return {"code": "stopped", "label": "Parado"}


def resolve_mt5_state(mt5_snap: dict[str, Any], quote: dict[str, Any]) -> dict[str, str]:
    if not mt5_snap.get("terminal_connected"):
        return {"code": "disconnected", "label": "Desligado"}
    mode_label = str(mt5_snap.get("trade_mode_label") or "").upper()
    trade_mode = mt5_snap.get("trade_mode")
    is_demo = mode_label == "DEMO" or trade_mode == 0
    is_real = mode_label == "REAL" or trade_mode == 2
    is_contest = mode_label == "CONTEST" or trade_mode == 1
    feed = str(quote.get("feed_state") or "")
    update_age = quote.get("update_age_s")
    stale = feed in {"atrasado", "ligacao_perdida", "mercado_fechado"} or (
        update_age is not None and float(update_age) > 30.0
    )
    if is_real or is_contest:
        return {"code": "account_not_authorized", "label": "Conta não autorizada"}
    if stale and not is_demo:
        return {"code": "stale_data", "label": "Dados desatualizados"}
    if stale and is_demo:
        return {"code": "stale_data", "label": "Dados desatualizados"}
    if is_demo:
        return {"code": "demo_connected", "label": "Ligado a DEMO"}
    if trade_mode is None and mt5_snap.get("account_login"):
        # Conta ligada mas trade_mode desconhecido — não afirmar DEMO.
        return {"code": "account_not_authorized", "label": "Conta não autorizada"}
    return {"code": "disconnected", "label": "Desligado"}


def resolve_openai_state(
    *,
    auto: dict[str, Any],
    openai_real: dict[str, Any],
) -> dict[str, str]:
    if not auto.get("ai_enabled") or not auto.get("openai_key_configured"):
        return {"code": "disabled", "label": "Desativada"}
    remaining = auto.get("remaining_today_usd")
    if remaining is not None and float(remaining) <= 0:
        return {"code": "budget_exhausted", "label": "Orçamento esgotado"}
    suspend = str(auto.get("suspend_reason") or "")
    if suspend and ("falha" in suspend.lower() or "erro" in suspend.lower()):
        return {"code": "error", "label": "Erro"}
    last = auto.get("last_result") or {}
    if last.get("analyzing") or last.get("in_flight"):
        return {"code": "analyzing", "label": "A analisar"}
    latest = (openai_real or {}).get("latest") or {}
    if latest.get("available") and latest.get("analyzed_at"):
        if auto.get("scheduling_active"):
            return {"code": "waiting_next", "label": "A aguardar próxima análise"}
        return {"code": "decision_received", "label": "Decisão recebida"}
    if auto.get("confirmed") and auto.get("scheduling_active"):
        return {"code": "waiting_next", "label": "A aguardar próxima análise"}
    if auto.get("confirmed"):
        return {"code": "waiting_next", "label": "A aguardar próxima análise"}
    return {"code": "disabled", "label": "Desativada"}


def resolve_execution_state(
    *,
    demo_auth_active: bool = False,
    emergency: bool = False,
    pause_entries: bool = False,
    last_attempt: dict[str, Any] | None = None,
    auth_login: str | int | None = None,
    auth_server: str | None = None,
    live_login: str | int | None = None,
    live_server: str | None = None,
) -> dict[str, Any]:
    """Estado operacional atual — recusas antigas vão para last_rejection."""
    from app.robot.execution_state import build_execution_panel_state

    return build_execution_panel_state(
        demo_auth_active=demo_auth_active,
        emergency=emergency,
        pause_entries=pause_entries,
        last_attempt=last_attempt,
        auth_login=auth_login,
        auth_server=auth_server,
        live_login=live_login,
        live_server=live_server,
    )


def build_why_not_trading(
    *,
    robot: dict[str, str],
    mt5: dict[str, str],
    openai: dict[str, str],
    execution: dict[str, Any],
    auto: dict[str, Any],
    openai_real: dict[str, Any],
    quote: dict[str, Any],
    last_attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Motivo concreto e prioritário — código estruturado + mensagem legível."""
    latest = (openai_real or {}).get("latest") or {}
    decision = str(latest.get("decision") or "").upper()
    attempt = last_attempt or {}
    prior_reject = ""
    if str(attempt.get("status") or "") == "rejected":
        r = str(attempt.get("reason") or attempt.get("reject_reason") or "").strip()
        at = attempt.get("at") or ""
        did = attempt.get("decision_id")
        bits = [x for x in [r, f"decisão #{did}" if did else "", str(at) if at else ""] if x]
        prior_reject = " | ".join(bits)

    def _out(code: str, message: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": code, "message": message, **extra}
        if prior_reject and code != "prior_order_rejected":
            payload["prior_order_rejection"] = prior_reject
        if latest.get("id") is not None:
            payload["decision_id"] = latest.get("id")
        if latest.get("analyzed_at"):
            payload["decision_at"] = latest.get("analyzed_at")
        return payload

    if robot["code"] == "stopped":
        return _out(
            "robot_stopped",
            "O robô está parado — prima INICIAR para monitorizar.",
        )
    if robot["code"] == "error":
        return _out(
            "robot_error",
            "O ciclo do robô não está a correr corretamente — prima INICIAR novamente.",
        )
    if mt5["code"] == "disconnected":
        return _out(
            "mt5_disconnected",
            "MetaTrader 5 desligado — sem cotações fiáveis.",
        )
    if mt5["code"] == "account_not_authorized":
        return _out(
            "mt5_account_not_demo",
            "Conta MT5 não confirmada como DEMO — execução bloqueada.",
        )
    if mt5["code"] == "stale_data":
        return _out(
            "mt5_stale_data",
            "Dados de mercado desatualizados — sem novas operações.",
        )
    if quote.get("feed_state") == "mercado_fechado":
        return _out(
            "market_closed",
            "Mercado fechado para o símbolo observado.",
        )
    if openai["code"] == "disabled":
        return _out(
            "openai_disabled",
            "OpenAI desativada — ative e confirme a análise autónoma no painel.",
        )
    if openai["code"] == "budget_exhausted":
        return _out(
            "openai_budget_exhausted",
            "Orçamento OpenAI do dia esgotado (limite 0,10 USD).",
        )
    if openai["code"] == "error":
        return _out(
            "openai_error",
            str(auto.get("suspend_reason") or "Erro na análise OpenAI."),
        )
    if execution["code"] in {"blocked", "paused_safety"}:
        if execution["code"] == "paused_safety":
            return _out(
                "execution_paused_safety",
                "Execução DEMO pausada por segurança.",
            )
        return _out(
            "demo_execution_disabled",
            "Execução DEMO desativada — o robô observa mas não envia ordens.",
        )

    # WAIT atual ≠ recusa de uma tentativa anterior — distinguir explicitamente.
    if decision in {"HOLD", "WAIT", ""} and latest.get("available"):
        msg = (
            f"OpenAI decidiu AGUARDAR (decisão #{latest.get('id')}"
            + (f", {latest.get('analyzed_at')}" if latest.get("analyzed_at") else "")
            + ") — nenhuma operação nova."
        )
        stale_note = ""
        lr = execution.get("last_rejection") if isinstance(execution, dict) else None
        if isinstance(lr, dict) and lr.get("stale"):
            stale_note = (
                f" Recusa antiga (já não define o estado DEMO): {lr.get('reason')} "
                f"em {lr.get('at')}."
            )
        elif prior_reject:
            msg += (
                " Nota: houve uma tentativa de ordem anterior recusada "
                f"({prior_reject}) — isso não é a decisão WAIT atual."
            )
        return _out("openai_wait", msg + stale_note)

    if execution["code"] in {"order_rejected", "active_with_rejection"} and prior_reject:
        return _out(
            "prior_order_rejected",
            f"Última tentativa de ordem foi recusada: {prior_reject}",
            attempt_decision_id=attempt.get("decision_id"),
            attempt_at=attempt.get("at"),
        )

    if openai["code"] == "waiting_next" or openai["code"] == "analyzing":
        nxt = auto.get("next_analysis_at")
        if nxt:
            return _out(
                "waiting_next_analysis",
                f"Próxima análise ainda não chegou (prevista: {nxt}).",
            )
        return _out(
            "waiting_next_analysis",
            "Próxima análise ainda não chegou.",
        )
    if decision in {"BUY", "SELL"} and not latest.get("executed"):
        if not latest.get("risk_approved", True):
            return _out(
                "risk_rejected",
                str(latest.get("risk_reasons") or "Validação de risco recusou a operação."),
            )
        if prior_reject:
            return _out(
                "prior_order_rejected",
                f"Decisão {decision} #{latest.get('id')} — ordem recusada: {prior_reject}",
                attempt_decision_id=attempt.get("decision_id"),
                attempt_at=attempt.get("at"),
            )
        return _out(
            "decision_not_executed",
            "Houve decisão BUY/SELL mas nenhuma ordem foi enviada (execução DEMO bloqueada ou requisitos falharam).",
        )
    return _out(
        "observing",
        "A monitorizar — sem condição de entrada ativa.",
    )


def build_account_mt5(mt5_snap: dict[str, Any]) -> dict[str, Any]:
    connected = bool(mt5_snap.get("terminal_connected"))
    # AutoTrading = terminal_info.trade_allowed (botão no MT5).
    autotrading = bool(
        mt5_snap.get("terminal_trade_allowed")
        if "terminal_trade_allowed" in mt5_snap
        else mt5_snap.get("autotrading_enabled", mt5_snap.get("trade_allowed"))
    )
    note = (
        "Valores lidos diretamente da conta MT5."
        if connected
        else "Terminal MT5 desligado — sem saldo DEMO."
    )
    if connected and not autotrading:
        note += " AutoTrading MT5 desligado — análises OK; ordens DEMO bloqueadas até ativar."
    return {
        "available": connected and mt5_snap.get("balance") is not None,
        "login": mt5_snap.get("account_login"),
        "login_masked": mask_login(mt5_snap.get("account_login")),
        "server": mt5_snap.get("account_server") or "",
        "trade_mode": mt5_snap.get("trade_mode"),
        "trade_mode_label": mt5_snap.get("trade_mode_label") or "",
        "balance": mt5_snap.get("balance"),
        "equity": mt5_snap.get("equity"),
        "margin": mt5_snap.get("margin"),
        "margin_free": mt5_snap.get("margin_free"),
        "profit": mt5_snap.get("profit"),
        "currency": mt5_snap.get("currency") or "USD",
        "company": mt5_snap.get("company") or "",
        "trade_allowed": bool(mt5_snap.get("trade_allowed")),
        "terminal_trade_allowed": autotrading,
        "autotrading_enabled": autotrading,
        "autotrading_warning": (
            "AutoTrading MT5 desligado — ative o botão AutoTrading no terminal para permitir ordens DEMO."
            if connected and not autotrading
            else ""
        ),
        "source": "mt5_account_info",
        "note": note,
    }


def build_panel_overlay(
    *,
    running: bool,
    thread_alive: bool,
    block_reason: str,
    mt5_snap: dict[str, Any],
    quote: dict[str, Any],
    auto: dict[str, Any],
    openai_real: dict[str, Any],
    account_simulated: dict[str, Any],
    demo_auth_active: bool = False,
    emergency: bool = False,
    pause_entries: bool = False,
    last_execution_attempt: dict[str, Any] | None = None,
    auth_login: str | int | None = None,
    auth_server: str | None = None,
) -> dict[str, Any]:
    robot = resolve_robot_state(
        running=running, thread_alive=thread_alive, block_reason=block_reason or ""
    )
    mt5 = resolve_mt5_state(mt5_snap, quote or {})
    openai = resolve_openai_state(auto=auto or {}, openai_real=openai_real or {})
    live_login = mt5_snap.get("account_login")
    live_server = mt5_snap.get("account_server")
    execution = resolve_execution_state(
        demo_auth_active=demo_auth_active,
        emergency=emergency,
        pause_entries=pause_entries,
        last_attempt=last_execution_attempt,
        auth_login=auth_login,
        auth_server=auth_server,
        live_login=live_login,
        live_server=live_server,
    )
    # Para why_not_trading: só usar recusa como fator se NÃO estiver obsoleta.
    attempt_for_why = last_execution_attempt
    if execution.get("attempt_stale"):
        attempt_for_why = None
    why = build_why_not_trading(
        robot=robot,
        mt5=mt5,
        openai=openai,
        execution=execution,
        auto=auto or {},
        openai_real=openai_real or {},
        quote=quote or {},
        last_attempt=attempt_for_why,
    )
    latest = (openai_real or {}).get("latest") or {}
    last_quote_at = quote.get("received_at") or quote.get("ts")
    last_rej = execution.get("last_rejection")
    return {
        "panel_generated_at": _iso(_utcnow()),
        "backend_link": {"code": "ok", "label": "Ligação OK"},
        "robot_state": robot,
        "mt5_state": mt5,
        "openai_state": openai,
        "execution_state": execution,
        "why_not_trading": why,
        "last_execution_attempt": last_execution_attempt,
        "last_rejection": last_rej,
        "timestamps": {
            "panel_generated_at": _iso(_utcnow()),
            "last_quote_at": _iso(last_quote_at) if last_quote_at else None,
            "last_openai_analysis_at": latest.get("analyzed_at"),
            "next_openai_analysis_at": auto.get("next_analysis_at"),
            "last_execution_attempt_at": (last_execution_attempt or {}).get("at")
            or (last_execution_attempt or {}).get("created_at"),
            "last_rejection_at": (last_rej or {}).get("at"),
        },
        "account_mt5": build_account_mt5(mt5_snap),
        "account_simulated": {
            **account_simulated,
            "label": "Conta virtual SimulatedBroker (TEST/BASELINE)",
            "note": "Não faz parte do saldo da conta MT5 DEMO.",
        },
        "dukascopy_note": (
            "Dukascopy é complementar e opcional — timeouts TCP não bloqueiam "
            "MT5, radar, OpenAI nem execução DEMO."
        ),
    }
