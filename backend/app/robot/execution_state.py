"""Estado atual da execução DEMO vs histórico de tentativas.

Uma recusa antiga não substitui o estado operacional quando a autorização
DEMO está válida e a causa da recusa já não se aplica.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_stale_rejection(
    attempt: dict[str, Any] | None,
    *,
    demo_auth_active: bool,
    auth_login: str | int | None,
    auth_server: str | None,
    live_login: str | int | None,
    live_server: str | None,
    max_age_s: float = 6 * 3600.0,
) -> bool:
    """True se a recusa não deve definir o estado operacional atual."""
    if not attempt or str(attempt.get("status") or "") != "rejected":
        return False
    reason = str(attempt.get("reason") or attempt.get("reject_reason") or "")
    # Mismatch de login: obsoleto quando auth e conta live coincidem e auth está ativa.
    if "Login/servidor diferem" in reason or "login/servidor" in reason.lower():
        if (
            demo_auth_active
            and str(auth_login or "") == str(live_login or "")
            and str(auth_server or "") == str(live_server or "")
            and str(auth_login or "") != ""
        ):
            return True
    # Recusas de stubs de teste (login 111) nunca são estado operacional.
    if str(attempt.get("account_login") or "") == "111":
        return True
    if attempt.get("synthetic_test"):
        return True
    at = _parse_at(attempt.get("at") or attempt.get("created_at"))
    if at is not None:
        age = (datetime.now(timezone.utc) - at).total_seconds()
        if age > max_age_s and demo_auth_active:
            return True
    return False


def build_execution_panel_state(
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
    """Estado atual + última recusa (separados)."""
    attempt = dict(last_attempt or {})
    reason = str(attempt.get("reason") or attempt.get("reject_reason") or "").strip()
    at = attempt.get("at") or attempt.get("created_at")
    status = str(attempt.get("status") or "")
    stale = is_stale_rejection(
        attempt,
        demo_auth_active=demo_auth_active,
        auth_login=auth_login,
        auth_server=auth_server,
        live_login=live_login,
        live_server=live_server,
    )

    last_rejection: dict[str, Any] | None = None
    if status == "rejected" and reason:
        last_rejection = {
            "reason": reason,
            "at": at,
            "decision_id": attempt.get("decision_id"),
            "attempt_id": attempt.get("attempt_id") or attempt.get("id"),
            "side": attempt.get("side"),
            "symbol": attempt.get("symbol"),
            "order_send_called": bool(attempt.get("order_send_called")),
            "stale": stale,
        }

    last_success: dict[str, Any] | None = None
    if status == "executed":
        last_success = {
            "at": at,
            "decision_id": attempt.get("decision_id"),
            "ticket": attempt.get("ticket") or attempt.get("order_ticket"),
            "side": attempt.get("side"),
            "symbol": attempt.get("symbol"),
        }

    base_detail = {
        "last_rejection": last_rejection,
        "last_success": last_success,
        "attempt_stale": stale,
        "reason": None if stale else (reason or None),
        "at": None if stale else at,
        "decision_id": None if stale else attempt.get("decision_id"),
        "attempt_id": attempt.get("attempt_id") or attempt.get("id"),
        "side": attempt.get("side"),
        "symbol": attempt.get("symbol"),
        "order_send_called": bool(attempt.get("order_send_called")),
        "order_check_called": bool(attempt.get("order_check_called")),
        "ticket": attempt.get("ticket") or attempt.get("order_ticket"),
    }

    if emergency:
        return {"code": "paused_safety", "label": "Pausada por segurança", **base_detail}
    if pause_entries and demo_auth_active:
        return {"code": "paused_safety", "label": "Pausada por segurança", **base_detail}
    if status == "sending" and not stale:
        return {"code": "order_sending", "label": "Ordem em envio", **base_detail}

    # Recusa FRESCA (não obsoleta) de uma tentativa real — informar sem esconder.
    if status == "rejected" and not stale and not demo_auth_active:
        label = "Ordem recusada"
        if reason:
            short = reason if len(reason) <= 72 else reason[:69] + "…"
            label = f"Ordem recusada — {short}"
        return {
            "code": "order_rejected",
            "label": label,
            **base_detail,
            "reason": reason or None,
            "at": at,
        }

    if demo_auth_active:
        label = "Ativa / pronta"
        if last_rejection and not stale:
            # Auth ativa mas houve recusa recente ainda relevante.
            short = (last_rejection["reason"] or "")[:48]
            label = f"Ativa — última ordem recusada ({short})"
            return {
                "code": "active_with_rejection",
                "label": label,
                **base_detail,
                "reason": last_rejection["reason"],
                "at": last_rejection["at"],
                "decision_id": last_rejection.get("decision_id"),
            }
        if last_rejection and stale:
            label = "Ativa / pronta"
        return {"code": "active", "label": label, **base_detail}

    if status == "executed" and last_success:
        return {
            "code": "order_executed",
            "label": f"Ordem executada (ticket {last_success.get('ticket') or '—'})",
            **base_detail,
        }

    return {"code": "blocked", "label": "Bloqueada", **base_detail}
