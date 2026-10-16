"""Controlo centralizado de custos de IA — único ponto de autorização de chamadas pagas.

Predefinição: IA desligada. Nenhuma chamada HTTP a fornecedores sem autorização válida.
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded, assert_budget_allows, spent_since, spent_today_usd
from app.ai.costs import PRICE_TABLE, estimate_cost
from app.config import get_settings
from app.database import SessionLocal
from app.models.audit import ApiUsage

# Contexto da análise em curso (preenchido pelo orquestrador).
_auth_token: ContextVar[str | None] = ContextVar("ai_auth_token", default=None)
_call_role: ContextVar[str] = ContextVar("ai_call_role", default="")
_call_reason: ContextVar[str] = ContextVar("ai_call_reason", default="")
_analysis_id: ContextVar[int] = ContextVar("ai_analysis_id", default=0)


class AIDisabled(Exception):
    """IA globalmente desligada ou bloqueio de emergência."""


class AIAuthorizationError(Exception):
    """Autorização em falta, expirada ou esgotada."""


@dataclass
class Authorization:
    token: str
    analysis_run_id: int | None
    max_calls: int
    calls_used: int = 0
    reserved_cost_usd: float = 0.0
    spent_cost_usd: float = 0.0
    max_cost_usd: float = 0.0
    workflow: str = "tradingagents"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=30))
    consumed: bool = False  # marcada após conclusão da análise
    cancelled: bool = False


class AIControl:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._auths: dict[str, Authorization] = {}
        self._active_analyses: set[int] = set()
        self._session_started = datetime.now(timezone.utc)
        self._emergency_block = False
        self._runtime_enabled: bool | None = None  # None = usar settings

    def _db_flag(self, key: str) -> str:
        from app.models.account import AppSetting

        db = SessionLocal()
        try:
            row = db.get(AppSetting, key)
            return row.value if row else ""
        except Exception:  # noqa: BLE001
            return ""
        finally:
            db.close()

    def _set_db_flag(self, key: str, value: str) -> None:
        from app.models.account import AppSetting

        db = SessionLocal()
        try:
            row = db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    # --- estado global ---
    def is_enabled(self) -> bool:
        if self._emergency_block or self._db_flag("ai_emergency_block") == "true":
            return False
        if self._runtime_enabled is not None:
            return self._runtime_enabled
        override = self._db_flag("ai_enabled_override")
        if override == "false":
            return False
        if override == "true":
            return True
        return bool(get_settings().ai_enabled)

    def set_enabled(self, enabled: bool, *, emergency: bool = False) -> None:
        with self._lock:
            if emergency or not enabled:
                self._emergency_block = True
                self._set_db_flag("ai_emergency_block", "true")
                self._runtime_enabled = False
                self._set_db_flag("ai_enabled_override", "false")
                # Cancelar autorizações pendentes.
                for auth in self._auths.values():
                    auth.cancelled = True
                self._active_analyses.clear()
            else:
                self._emergency_block = False
                self._set_db_flag("ai_emergency_block", "false")
                self._runtime_enabled = True
                self._set_db_flag("ai_enabled_override", "true")

    def block_emergency(self) -> None:
        self.set_enabled(False, emergency=True)

    # --- estimativas ---
    def estimate_call_reserve_usd(self, model: str) -> float:
        """Reserva conservadora por chamada (não é faturação confirmada)."""
        settings = get_settings()
        # Assume prompt típico + max_tokens de saída.
        return estimate_cost(model, 2500, int(settings.ai_max_tokens))

    def estimate_analysis(
        self,
        *,
        model: str | None = None,
        max_calls: int | None = None,
        debate_rounds: int = 2,
    ) -> dict[str, Any]:
        settings = get_settings()
        model = model or settings.openai_default_model
        # Limite duro de chamadas.
        capped = min(
            max_calls or settings.ai_max_calls_per_analysis,
            settings.ai_max_calls_per_analysis,
        )
        # Heurística: analistas + debate*2 + risk + coordinator ≈ até capped
        suggested = min(capped, 6 + max(0, debate_rounds) * 2 + 2)
        per_call = self.estimate_call_reserve_usd(model)
        if per_call <= 0:
            raise AIAuthorizationError(
                "Não foi possível estimar o custo com segurança — chamada bloqueada."
            )
        reserved = round(per_call * capped, 6)
        max_cost = min(reserved, settings.ai_max_cost_per_analysis_usd)
        return {
            "model": model,
            "max_calls": capped,
            "suggested_calls": suggested,
            "reserve_per_call_usd": round(per_call, 6),
            "estimated_max_cost_usd": max_cost,
            "disclaimer": (
                "Estimativa local conservadora — não é o valor de faturação confirmado da OpenAI."
            ),
            "ai_enabled": self.is_enabled(),
            "require_manual_approval": settings.ai_require_manual_approval,
            "daily_budget_usd": settings.ai_daily_budget_usd,
            "session_budget_usd": settings.ai_session_budget_usd,
            "max_cost_per_analysis_usd": settings.ai_max_cost_per_analysis_usd,
            "price_known": model in PRICE_TABLE,
        }

    def create_authorization(
        self,
        db: Session,
        *,
        confirmed: bool,
        workflow: str = "tradingagents",
        debate_rounds: int = 2,
        analysis_run_id: int | None = None,
        model: str | None = None,
        max_calls: int | None = None,
        daily_budget_override_usd: float | None = None,
        spent_today_override_usd: float | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise AIAuthorizationError("Confirmação explícita do utilizador é obrigatória.")
        if not self.is_enabled():
            raise AIDisabled(
                "IA desligada (AI_ENABLED=false ou bloqueio de emergência). "
                "Ative a IA na página Consumo de IA antes de autorizar."
            )
        settings = get_settings()
        with self._lock:
            # Concorrência
            if len(self._active_analyses) >= settings.ai_max_concurrent_analyses:
                raise AIAuthorizationError(
                    f"Já existe análise ativa (máx. {settings.ai_max_concurrent_analyses})."
                )
            est = self.estimate_analysis(
                model=model, max_calls=max_calls, debate_rounds=debate_rounds
            )
            if not est["price_known"] and settings.ai_block_unknown_price_models:
                raise AIAuthorizationError(
                    f"Modelo {est['model']} sem preço conhecido — chamada bloqueada por segurança."
                )
            # Orçamento (overrides permitem orçamento autónomo do robô isolado)
            if spent_today_override_usd is None:
                assert_budget_allows(db, self._session_started)
            daily = (
                float(spent_today_override_usd)
                if spent_today_override_usd is not None
                else spent_today_usd(db)
            )
            daily_cap = float(
                daily_budget_override_usd
                if daily_budget_override_usd is not None
                else settings.ai_daily_budget_usd
            )
            if daily + est["estimated_max_cost_usd"] > daily_cap:
                raise BudgetExceeded(
                    "Reserva da análise ultrapassaria o orçamento diário de IA."
                )
            session_spent = spent_since(db, self._session_started)
            if session_spent + est["estimated_max_cost_usd"] > settings.ai_session_budget_usd:
                raise BudgetExceeded(
                    "Reserva da análise ultrapassaria o orçamento de sessão de IA."
                )

            token = uuid.uuid4().hex
            auth = Authorization(
                token=token,
                analysis_run_id=analysis_run_id,
                max_calls=int(est["max_calls"]),
                reserved_cost_usd=float(est["estimated_max_cost_usd"]),
                max_cost_usd=float(est["estimated_max_cost_usd"]),
                workflow=workflow,
            )
            self._auths[token] = auth
            return {
                "authorization_id": token,
                "max_calls": auth.max_calls,
                "reserved_cost_usd": auth.reserved_cost_usd,
                "expires_at": auth.expires_at.isoformat(),
                "estimate": est,
                "single_use": True,
                "note": "Autorização de utilização única — não reutilizável em análises posteriores.",
            }

    def bind_authorization(self, token: str, analysis_run_id: int) -> None:
        with self._lock:
            auth = self._require_auth_locked(token)
            if auth.analysis_run_id and auth.analysis_run_id != analysis_run_id:
                raise AIAuthorizationError("Autorização já vinculada a outra análise.")
            auth.analysis_run_id = analysis_run_id
            self._active_analyses.add(analysis_run_id)

    def begin_analysis_context(self, token: str, analysis_run_id: int = 0) -> None:
        settings = get_settings()
        if not self.is_enabled():
            raise AIDisabled("IA desligada — nenhuma chamada será efetuada.")
        if settings.ai_require_manual_approval:
            if not token:
                raise AIAuthorizationError("Autorização de análise em falta.")
            with self._lock:
                auth = self._require_auth_locked(token)
                if auth.analysis_run_id is None and analysis_run_id:
                    auth.analysis_run_id = analysis_run_id
                if analysis_run_id:
                    if len(self._active_analyses) >= settings.ai_max_concurrent_analyses and analysis_run_id not in self._active_analyses:
                        raise AIAuthorizationError("Análise simultânea não autorizada.")
                    self._active_analyses.add(analysis_run_id)
        _auth_token.set(token or None)
        _analysis_id.set(analysis_run_id)

    def end_analysis_context(self, token: str | None = None) -> None:
        tok = token or _auth_token.get()
        with self._lock:
            if tok and tok in self._auths:
                self._auths[tok].consumed = True
                aid = self._auths[tok].analysis_run_id
                if aid:
                    self._active_analyses.discard(aid)
        _auth_token.set(None)
        _analysis_id.set(0)
        _call_role.set("")
        _call_reason.set("")

    def set_call_meta(self, *, role: str, reason: str) -> None:
        _call_role.set(role)
        _call_reason.set(reason)

    def _require_auth_locked(self, token: str) -> Authorization:
        auth = self._auths.get(token)
        if auth is None:
            raise AIAuthorizationError("Autorização inválida ou desconhecida.")
        if auth.cancelled:
            raise AIAuthorizationError("Autorização cancelada (bloqueio de IA).")
        if auth.consumed:
            raise AIAuthorizationError("Autorização já utilizada — não é reutilizável.")
        if datetime.now(timezone.utc) > auth.expires_at:
            raise AIAuthorizationError("Autorização expirada.")
        return auth

    def before_provider_call(self, *, provider: str, model: str) -> None:
        """Único controlo obrigatório antes de qualquer HTTP a fornecedores."""
        if not self.is_enabled():
            self._audit(
                provider=provider,
                model=model,
                authorized=False,
                status="blocked_ai_disabled",
                error="AI_ENABLED=false ou bloqueio de emergência",
            )
            raise AIDisabled("IA desligada — chamada ao fornecedor bloqueada.")

        settings = get_settings()
        token = _auth_token.get()
        role = _call_role.get() or ""
        reason = _call_reason.get() or "provider_complete"
        analysis_id = _analysis_id.get() or 0

        if settings.ai_require_manual_approval:
            if not token:
                self._audit(
                    provider=provider,
                    model=model,
                    authorized=False,
                    status="blocked_no_auth",
                    error="Sem autorização de análise",
                )
                raise AIAuthorizationError("Chamada rejeitada: sem autorização válida.")
            with self._lock:
                auth = self._require_auth_locked(token)
                if auth.calls_used >= auth.max_calls:
                    self._audit(
                        provider=provider,
                        model=model,
                        authorized=False,
                        status="blocked_call_limit",
                        error=f"Limite de {auth.max_calls} chamadas atingido",
                    )
                    raise AIAuthorizationError(
                        f"Limite de chamadas da análise atingido ({auth.max_calls})."
                    )
                reserve = self.estimate_call_reserve_usd(model)
                if reserve <= 0:
                    raise AIAuthorizationError("Custo não estimável — chamada bloqueada.")
                if auth.spent_cost_usd + reserve > auth.max_cost_usd + 1e-9:
                    self._audit(
                        provider=provider,
                        model=model,
                        authorized=False,
                        status="blocked_cost_limit",
                        error="Limite de custo por análise",
                    )
                    raise AIAuthorizationError("Limite de custo por análise atingido.")
                # Reserva otimista da chamada (retries também passam aqui).
                auth.calls_used += 1
                auth.spent_cost_usd += reserve

        # Orçamento global
        db = SessionLocal()
        try:
            assert_budget_allows(db, self._session_started)
        except BudgetExceeded as exc:
            self._audit(
                provider=provider,
                model=model,
                authorized=False,
                status="blocked_budget",
                error=str(exc),
            )
            raise
        finally:
            db.close()

        self._audit(
            provider=provider,
            model=model,
            authorized=True,
            status="authorized",
            role=role,
            reason=reason,
            analysis_id=analysis_id,
        )

    def record_call_result(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        estimated_cost_usd: float,
        status: str,
        error: str = "",
        attempt: int = 1,
        response_text: str = "",
        raw_http: dict[str, Any] | None = None,
        response_store_path: str = "",
    ) -> int | None:
        """Regista auditoria ORM. Se response_text/raw_http forem dados, persiste em disco
        antes do ORM (ou liga ao ficheiro já gravado). Devolve call_log_id ou None."""
        token = _auth_token.get() or ""
        # Persistência durável ANTES do ORM — não depender do SQL para não perder a resposta.
        store_path = response_store_path
        if (response_text or raw_http) and not store_path:
            try:
                from app.ai.response_store import persist_raw_provider_response

                path = persist_raw_provider_response(
                    provider=provider,
                    model=model,
                    raw_http=raw_http,
                    text=response_text or "",
                    authorization_id=token,
                    role=_call_role.get() or "",
                    reason=_call_reason.get() or "",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    status=status,
                )
                store_path = str(path)
            except Exception:  # noqa: BLE001 — falha de disco não deve impedir auditoria
                store_path = ""

        with self._lock:
            if token and token in self._auths:
                auth = self._auths[token]
                # Ajustar gasto real vs reserva (não reembolsar calls_used).
                # Mantém o máximo entre reserva acumulada e custo real reportado.
                auth.spent_cost_usd = max(auth.spent_cost_usd, estimated_cost_usd + (auth.calls_used - 1) * 0.0)
        call_log_id = self._audit(
            provider=provider,
            model=model,
            authorized=True,
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=estimated_cost_usd,
            error=error,
            attempt=attempt,
            role=_call_role.get() or "",
            reason=_call_reason.get() or "",
            analysis_id=_analysis_id.get() or 0,
        )
        if store_path and call_log_id is not None:
            try:
                from app.ai.response_store import update_persisted_meta
                from pathlib import Path

                update_persisted_meta(
                    Path(store_path),
                    call_log_id=call_log_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                    status=status,
                )
            except Exception:  # noqa: BLE001
                pass
        return call_log_id

    def _audit(self, **kwargs: Any) -> int | None:
        from app.models.ai_control import AICallLog

        db = SessionLocal()
        try:
            row = AICallLog(
                provider=str(kwargs.get("provider") or ""),
                model=str(kwargs.get("model") or ""),
                role=str(kwargs.get("role") or _call_role.get() or ""),
                analysis_id=int(kwargs.get("analysis_id") or _analysis_id.get() or 0),
                authorization_id=str(_auth_token.get() or ""),
                reason=str(kwargs.get("reason") or _call_reason.get() or ""),
                authorized=bool(kwargs.get("authorized")),
                prompt_tokens=int(kwargs.get("prompt_tokens") or 0),
                completion_tokens=int(kwargs.get("completion_tokens") or 0),
                estimated_cost_usd=float(kwargs.get("estimated_cost_usd") or 0.0),
                status=str(kwargs.get("status") or ""),
                error=str(kwargs.get("error") or "")[:1000],
                attempt=int(kwargs.get("attempt") or 1),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return int(row.id)
        except Exception:  # noqa: BLE001 — auditoria nunca derruba a app
            db.rollback()
            return None
        finally:
            db.close()

    def status(self, db: Session) -> dict[str, Any]:
        settings = get_settings()
        today = spent_today_usd(db)
        session = spent_since(db, self._session_started)
        from app.models.ai_control import AICallLog

        last = db.scalar(select(AICallLog).order_by(AICallLog.created_at.desc()).limit(1))
        calls_today = db.scalar(
            select(func.count())
            .select_from(AICallLog)
            .where(AICallLog.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
        ) or 0
        authorized_open = sum(
            1 for a in self._auths.values() if not a.consumed and not a.cancelled
        )
        return {
            "ai_enabled": self.is_enabled(),
            "ai_enabled_setting": settings.ai_enabled,
            "emergency_block": self._emergency_block or self._db_flag("ai_emergency_block") == "true",
            "require_manual_approval": settings.ai_require_manual_approval,
            "max_calls_per_analysis": settings.ai_max_calls_per_analysis,
            "max_concurrent_analyses": settings.ai_max_concurrent_analyses,
            "max_cost_per_analysis_usd": settings.ai_max_cost_per_analysis_usd,
            "daily_budget_usd": settings.ai_daily_budget_usd,
            "session_budget_usd": settings.ai_session_budget_usd,
            "estimated_daily_cost_usd": round(today, 6),
            "estimated_session_cost_usd": round(session, 6),
            "cost_disclaimer": "Valores estimados localmente — não são faturação confirmada.",
            "calls_logged_today": int(calls_today),
            "authorizations_open": authorized_open,
            "active_analyses": len(self._active_analyses),
            "last_call": None
            if last is None
            else {
                "created_at": last.created_at.isoformat() if last.created_at else None,
                "provider": last.provider,
                "model": last.model,
                "role": last.role,
                "status": last.status,
                "authorized": last.authorized,
                "estimated_cost_usd": last.estimated_cost_usd,
                "analysis_id": last.analysis_id,
            },
        }


_control: AIControl | None = None
_ctrl_lock = threading.Lock()


def get_ai_control() -> AIControl:
    global _control
    with _ctrl_lock:
        if _control is None:
            _control = AIControl()
        return _control


def reset_ai_control_for_tests() -> AIControl:
    global _control
    with _ctrl_lock:
        _control = AIControl()
        return _control
