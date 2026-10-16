"""Autorização de execução MT5 DEMO — sessão apenas (não sobrevive a reinício da API).

Fase 1: sempre inativa. Fase 2: ativação explícita no painel.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy.orm import Session

from app.models.account import AppSetting

FLAG_ACTIVE = "mt5_demo_auth_active"
FLAG_LOGIN = "mt5_demo_auth_login"
FLAG_SERVER = "mt5_demo_auth_server"
FLAG_EMERGENCY = "mt5_demo_emergency"
FLAG_PAUSE = "mt5_demo_pause_entries"
FLAG_BOOT_CLEARED = "mt5_demo_boot_cleared_v1"

_lock = threading.RLock()
_last_attempt: dict[str, Any] | None = None
_boot_cleared = False


def _get(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return (row.value if row else "") or ""


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def clear_auth_on_startup(db: Session) -> None:
    """Revoga autorização DEMO em cada arranque da API."""
    global _boot_cleared, _last_attempt
    with _lock:
        _set(db, FLAG_ACTIVE, "false")
        _set(db, FLAG_LOGIN, "")
        _set(db, FLAG_SERVER, "")
        # Emergência persiste entre reinícios; pause_entries fica true por segurança.
        # (FLAG_EMERGENCY não é limpa aqui.)
        if _get(db, FLAG_EMERGENCY) != "true":
            _set(db, FLAG_PAUSE, "true")
        else:
            _set(db, FLAG_PAUSE, "true")
        _last_attempt = None
        _boot_cleared = True
        _set(db, FLAG_BOOT_CLEARED, "true")


def reset_demo_auth_module_for_tests() -> None:
    global _boot_cleared, _last_attempt
    with _lock:
        _boot_cleared = False
        _last_attempt = None


def ensure_startup_cleared(db: Session) -> None:
    global _boot_cleared
    if _boot_cleared:
        return
    clear_auth_on_startup(db)


def get_demo_auth_snapshot(db: Session) -> dict[str, Any]:
    """Lê o estado DEMO. Não limpa autorização aqui (só no arranque da API)."""
    with _lock:
        return {
            "active": _get(db, FLAG_ACTIVE) == "true",
            "login": _get(db, FLAG_LOGIN),
            "server": _get(db, FLAG_SERVER),
            "emergency": _get(db, FLAG_EMERGENCY) == "true",
            "pause_entries": _get(db, FLAG_PAUSE) != "false",
            "last_attempt": dict(_last_attempt) if _last_attempt else None,
        }


def set_last_attempt(payload: dict[str, Any] | None) -> None:
    global _last_attempt
    with _lock:
        _last_attempt = dict(payload) if payload else None


def activate_demo(
    db: Session,
    *,
    confirmed: bool,
    login: int | str,
    server: str,
) -> dict[str, Any]:
    global _last_attempt
    if not confirmed:
        raise ValueError("confirmed=true é obrigatório para ativar negociação DEMO.")
    if _get(db, FLAG_EMERGENCY) == "true":
        raise ValueError("Paragem de emergência ativa — remova o bloqueio antes de ativar.")
    login_s = str(login).strip()
    server_s = str(server).strip()
    if not login_s or not server_s:
        raise ValueError("Login e servidor DEMO são obrigatórios.")
    _set(db, FLAG_LOGIN, login_s)
    _set(db, FLAG_SERVER, server_s)
    _set(db, FLAG_ACTIVE, "true")
    _set(db, FLAG_PAUSE, "false")
    # Nova autorização — limpar recusa antiga (ex.: mismatch de login de testes).
    with _lock:
        _last_attempt = None
    return get_demo_auth_snapshot(db)


def deactivate_demo(db: Session) -> dict[str, Any]:
    _set(db, FLAG_ACTIVE, "false")
    _set(db, FLAG_PAUSE, "true")
    return get_demo_auth_snapshot(db)


def pause_new_entries(db: Session, *, paused: bool = True) -> dict[str, Any]:
    _set(db, FLAG_PAUSE, "true" if paused else "false")
    return get_demo_auth_snapshot(db)


def trigger_emergency(db: Session) -> dict[str, Any]:
    _set(db, FLAG_EMERGENCY, "true")
    _set(db, FLAG_ACTIVE, "false")
    _set(db, FLAG_PAUSE, "true")
    return get_demo_auth_snapshot(db)


def clear_emergency(db: Session) -> dict[str, Any]:
    _set(db, FLAG_EMERGENCY, "false")
    return get_demo_auth_snapshot(db)


def invalidate_if_account_changed(
    db: Session, *, login: int | str | None, server: str | None
) -> bool:
    """Se a conta MT5 mudou, revoga auth. Devolve True se invalidou."""
    snap = get_demo_auth_snapshot(db)
    if not snap["active"]:
        return False
    if str(login or "") != str(snap["login"] or "") or str(server or "") != str(
        snap["server"] or ""
    ):
        deactivate_demo(db)
        return True
    return False
