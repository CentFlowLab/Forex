"""Análises OpenAI autónomas do robô — observação apenas (sem ordens).

Agendamento 15 min, orçamento diário, uma chamada de cada vez.
Nenhuma chamada paga até ativação explícita no programa.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import func, select

from app.ai.budget import BudgetExceeded
from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control
from app.ai.costs import estimate_cost
from app.ai.response_store import persist_raw_provider_response, store_dir
from app.config import get_settings
from app.database import SessionLocal
from app.market.mt5_feed import get_mt5_feed
from app.models.account import AppSetting
from app.models.robot import AIDecisionLog
from app.robot.ai_engine import estimate_radar_analysis_usd, run_radar_decision
from app.robot.ai_schema import DECISION_PT
from app.robot.radar import scan_radar, validate_scan_for_paid
from app.robot.mt5_symbol_discovery import symbols_for_radar
from app.robot.universe import MIN_PAID_MT5_PAIRS, SYMBOLS

logger = logging.getLogger(__name__)

FLAG_ARMED = "robot_openai_autonomous_armed"
FLAG_CONFIRMED = "robot_openai_autonomous_confirmed"
FLAG_SUSPEND_REASON = "robot_openai_suspend_reason"
FLAG_LAST_RUN_AT = "robot_openai_autonomous_last_run_at"
FLAG_NEXT_AT = "robot_openai_autonomous_next_at"
# Bloqueia chamadas autónomas até nova confirmação explícita (ex.: após auditoria).
FLAG_REQUIRES_RECONFIRM = "robot_openai_requires_reconfirm"

# Intervalo por defeito: 15 minutos.
_DEFAULT_INTERVAL_S = 900.0
_DAILY_BUDGET_USD = 0.10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _risk_reasons_for_decision(decision: str, quality: Any) -> str:
    from app.robot.radar_decision_quality import is_decision_quality_insufficient

    if (decision or "").upper() == "HOLD":
        if is_decision_quality_insufficient(quality if isinstance(quality, dict) else None):
            issues = list((quality or {}).get("issues") or [])
            head = "; ".join(issues[:2]) if issues else "contrato incompleto"
            return f"HOLD — sem ordem. Análise incompleta (decision_quality=insufficient): {head}"
        return "HOLD — sem ordem."
    if is_decision_quality_insufficient(quality if isinstance(quality, dict) else None):
        return (
            "BUY/SELL com decision_quality=insufficient — execução DEMO bloqueada "
            "(resposta original preservada)."
        )
    return "Pendente de validação DEMO / risco."


def _get_flag(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return (row.value if row else "") or ""


def _set_flag(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _persist_schedule(db: Session, *, last_run: datetime | None, next_at: datetime | None) -> None:
    if last_run is not None:
        _set_flag(db, FLAG_LAST_RUN_AT, last_run.isoformat())
    if next_at is not None:
        _set_flag(db, FLAG_NEXT_AT, next_at.isoformat())


def _set_flag_no_commit(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def atomic_claim_autonomous_slot(
    db: Session,
    *,
    interval_s: float,
    now: datetime | None = None,
) -> bool:
    """Reserva atómica e durável do intervalo autónomo (cross-process).

    BEGIN IMMEDIATE no SQLite serializa processos concorrentes.
    Apenas um caller consegue avançar last_run/next_at para o mesmo slot.
    """
    from sqlalchemy import text

    now = now or _utcnow()
    interval_s = float(interval_s or _DEFAULT_INTERVAL_S)
    bind = db.get_bind()
    url = str(getattr(bind, "url", "") or "")
    is_sqlite = "sqlite" in url

    try:
        if is_sqlite:
            db.commit()  # limpar estado implícito antes do IMMEDIATE
            db.execute(text("BEGIN IMMEDIATE"))
        last = _parse_iso(_get_flag(db, FLAG_LAST_RUN_AT))
        next_at = _parse_iso(_get_flag(db, FLAG_NEXT_AT))
        if last is None:
            row = db.scalar(
                select(AIDecisionLog)
                .where(AIDecisionLog.mode == "openai_radar_autonomous")
                .order_by(AIDecisionLog.id.desc())
                .limit(1)
            )
            if row and row.created_at:
                last = row.created_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
        if next_at is not None and now < next_at:
            db.rollback()
            return False
        if last is not None:
            earliest = last + timedelta(seconds=interval_s)
            if now < earliest:
                _set_flag_no_commit(db, FLAG_NEXT_AT, earliest.isoformat())
                db.commit()
                return False
        new_last = now
        new_next = now + timedelta(seconds=interval_s)
        _set_flag_no_commit(db, FLAG_LAST_RUN_AT, new_last.isoformat())
        _set_flag_no_commit(db, FLAG_NEXT_AT, new_next.isoformat())
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def autonomous_spent_today_usd(db: Session) -> float:
    """Gasto autónomo reconciliado por authorization_id (sem double-count).

    Inclui AICallLog ok órfãos (pagamento sem AIDecisionLog) e decisões
    autónomas sem call correspondente.
    """
    from app.models.ai_control import AICallLog

    start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    costs: dict[str, float] = {}

    calls = list(
        db.scalars(
            select(AICallLog).where(
                AICallLog.created_at >= start,
                AICallLog.status == "ok",
                AICallLog.role.in_(("robot_radar", "robot_radar_autonomous")),
                AICallLog.authorization_id.notlike("audit-%"),
                AICallLog.authorization_id.notlike("dry-run%"),
            )
        )
    )
    for c in calls:
        auth = (c.authorization_id or "").strip() or f"call:{c.id}"
        costs[auth] = max(costs.get(auth, 0.0), float(c.estimated_cost_usd or 0.0))

    decisions = list(
        db.scalars(
            select(AIDecisionLog).where(
                AIDecisionLog.created_at >= start,
                AIDecisionLog.mode == "openai_radar_autonomous",
            )
        )
    )
    for d in decisions:
        auth = ""
        try:
            ctx = json.loads(d.context_json or "{}")
            if isinstance(ctx, dict):
                auth = str(ctx.get("authorization_id") or "").strip()
        except (json.JSONDecodeError, TypeError):
            auth = ""
        cost = float(d.estimated_cost_usd or 0.0)
        if auth:
            costs[auth] = max(costs.get(auth, 0.0), cost)
        else:
            costs[f"decision:{d.id}"] = max(costs.get(f"decision:{d.id}", 0.0), cost)

    return round(sum(costs.values()), 6)


def assess_mt5_ready_for_paid() -> dict[str, Any]:
    """Valida MT5 para chamada paga — update_age / feed_state, não claims_realtime cego."""
    feed = get_mt5_feed()
    try:
        feed.start()
        feed.poll_once()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"MT5 indisponível: {exc}",
            "terminal_connected": False,
            "pairs_ok": 0,
            "clock_anomaly": False,
        }
    snap = feed.snapshot()
    if not snap.terminal_connected:
        return {
            "ok": False,
            "reason": "Terminal MT5 sem ligação — análises pagas suspensas.",
            "terminal_connected": False,
            "pairs_ok": 0,
            "clock_anomaly": False,
            "message": snap.message,
        }
    settings = get_settings()
    fresh = float(settings.quote_fresh_seconds)
    delayed = float(settings.quote_delayed_seconds)
    ok_n = 0
    clock_anomaly = False
    details = []
    watch = symbols_for_radar(prefer_mt5=True) or list(SYMBOLS)
    for sym in watch:
        q = snap.symbols.get(sym) or {}
        fs = q.get("feed_state")
        update_age = float(q.get("update_age_s") if q.get("update_age_s") is not None else 99999)
        lag = float(q.get("lag_seconds") if q.get("lag_seconds") is not None else 99999)
        if q.get("clock_anomaly"):
            clock_anomaly = True
        candles_ok = all(len(feed.candles(sym, tf)) >= n for tf, n in (("M15", 20), ("H1", 10), ("H4", 5)))
        pair_ok = (
            fs == "recente"
            and q.get("mid") is not None
            and update_age <= fresh
            and lag <= delayed
            and candles_ok
            and fs != "ligacao_perdida"
        )
        if pair_ok:
            ok_n += 1
        details.append(
            {
                "symbol": sym,
                "ok": pair_ok,
                "feed_state": fs,
                "update_age_s": update_age,
                "lag_seconds": lag,
                "candles_ok": candles_ok,
                "clock_anomaly": bool(q.get("clock_anomaly")),
            }
        )
    need = min(MIN_PAID_MT5_PAIRS, max(1, len(watch)))
    if ok_n < need:
        return {
            "ok": False,
            "reason": (
                f"MT5: apenas {ok_n}/{need} pares válidos para análise paga "
                f"(monitorizados={len(watch)}, universo alvo=28)."
            ),
            "terminal_connected": True,
            "pairs_ok": ok_n,
            "clock_anomaly": clock_anomaly,
            "pairs": details,
            "claims_realtime": False,
        }
    return {
        "ok": True,
        "reason": (
            f"{ok_n} pares MT5 com atualização recente e velas "
            f"(mínimo {need}; monitorizados={len(watch)}). "
            f"claims_realtime=false (anomalia_relogio={clock_anomaly})."
        ),
        "terminal_connected": True,
        "pairs_ok": ok_n,
        "clock_anomaly": clock_anomaly,
        "pairs": details,
        "claims_realtime": False,
    }


class OpenAIAutonomousScheduler:
    """Uma análise radar OpenAI a cada 15 min — sem execução de ordens."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._call_lock = threading.Lock()  # impede concorrência
        self._next_at: datetime | None = None
        self._last_run_at: datetime | None = None
        self._last_result: dict[str, Any] = {}
        self._suspend_reason = ""
        self._dry_run = False  # testes sem custo
        self._interval_s = _DEFAULT_INTERVAL_S

    # --- flags de ativação ---
    def is_armed(self, db: Session) -> bool:
        return _get_flag(db, FLAG_ARMED) == "true"

    def is_confirmed(self, db: Session) -> bool:
        if _get_flag(db, FLAG_REQUIRES_RECONFIRM) == "true":
            return False
        return _get_flag(db, FLAG_CONFIRMED) == "true"

    def is_active(self, db: Session) -> bool:
        return self.is_armed(db) and self.is_confirmed(db)

    def preparation_status(self, db: Session, *, light: bool = False) -> dict[str, Any]:
        """light=True: usado dentro de robot.status — sem scan/poll pesados (evita recursão/carga)."""
        settings = get_settings()
        control = get_ai_control()
        key_ok = settings.provider_configured("openai")
        if light:
            feed = get_mt5_feed().snapshot()
            mt5 = {
                "ok": bool(feed.terminal_connected and len(feed.symbols) >= 7),
                "reason": feed.message,
                "terminal_connected": feed.terminal_connected,
                "pairs_ok": sum(
                    1 for q in feed.symbols.values() if q.get("feed_state") == "recente"
                ),
                "clock_anomaly": any(q.get("clock_anomaly") for q in feed.symbols.values()),
                "claims_realtime": feed.claims_any_realtime,
            }
            try:
                from app.robot.service import get_robot_service

                cached = get_robot_service()._last_radar or {}
                mon = int(cached.get("monitored") or len(SYMBOLS))
                valid = int(cached.get("with_valid_data") or 0)
            except Exception:  # noqa: BLE001
                mon, valid = len(SYMBOLS), 0
            est = {
                "estimated_cost_usd": estimate_cost(
                    settings.openai_default_model, 3200, 800
                )
            }
            scan_monitored, scan_valid = mon, valid
        else:
            mt5 = assess_mt5_ready_for_paid()
            scan = scan_radar(db)
            est = estimate_radar_analysis_usd(scan)
            scan_monitored, scan_valid = scan.monitored, scan.with_valid_data
        spent = autonomous_spent_today_usd(db)
        daily_cap = float(getattr(settings, "robot_openai_daily_budget_usd", _DAILY_BUDGET_USD) or _DAILY_BUDGET_USD)
        remaining = max(0.0, daily_cap - spent)
        reserve = float(est.get("estimated_cost_usd") or 0.0)
        # Reserva conservadora: 1.5x estimativa, mínimo 0.002
        conservative = round(max(reserve * 1.5, 0.002), 6)
        can_afford = remaining >= conservative
        armed = self.is_armed(db)
        confirmed = self.is_confirmed(db)
        # NÃO chamar robot.status() aqui — causaria recursão (status → autonomous → status).
        robot_running = False
        try:
            from app.models.robot import RobotState
            from app.robot.service import get_robot_service

            row = db.scalar(select(RobotState).order_by(RobotState.id.desc()).limit(1))
            svc = get_robot_service()
            thread_alive = bool(svc._thread and svc._thread.is_alive())
            robot_running = bool(row and row.running and thread_alive)
        except Exception:  # noqa: BLE001
            robot_running = False

        # Restaurar agenda persistida após reinício (evita rajadas de análises).
        self._hydrate_schedule_from_db(db)

        blockers: list[str] = []
        if not confirmed:
            blockers.append("Ativação ainda não confirmada no programa.")
        else:
            if not key_ok:
                blockers.append("Chave OpenAI não configurada.")
            if not control.is_enabled():
                blockers.append("IA desligada (AI_ENABLED / Consumo de IA).")
            if settings.live_trading_locked is False:
                blockers.append("LIVE_TRADING_LOCKED deve permanecer true.")
            if not mt5.get("ok"):
                blockers.append(str(mt5.get("reason") or "MT5 não pronto."))
            if not can_afford:
                blockers.append(
                    f"Orçamento diário insuficiente (gasto=${spent:.4f}, "
                    f"reserva=${conservative:.4f}, teto=${daily_cap:.2f})."
                )
            if not robot_running:
                blockers.append("Robô parado — agendamento só corre com INICIAR.")

        next_at = self._next_at
        if confirmed and robot_running and next_at is None:
            # Respeitar última corrida persistida antes de marcar "já".
            last_persisted = _parse_iso(_get_flag(db, FLAG_LAST_RUN_AT))
            if last_persisted is not None:
                next_at = last_persisted + timedelta(seconds=self._interval_s)
                if next_at <= _utcnow():
                    next_at = _utcnow()
            else:
                next_at = _utcnow()

        suspend_reason = (self._suspend_reason or _get_flag(db, FLAG_SUSPEND_REASON) or "").strip()
        # Se o robô voltou a correr, limpar suspensão obsoleta "robô parado".
        if robot_running and suspend_reason.lower().startswith("robô parado"):
            suspend_reason = ""
            self._suspend_reason = ""
            _set_flag(db, FLAG_SUSPEND_REASON, "")
        active = bool(armed and confirmed)
        scheduling_active = bool(active and confirmed and robot_running and not suspend_reason)

        # Hidratar última análise a partir da BD se a memória do processo estiver vazia
        # (ex.: após reinício da API) — sem chamadas pagas.
        last_result = self._last_result
        last_run_at = self._last_run_at or _parse_iso(_get_flag(db, FLAG_LAST_RUN_AT))
        if not last_result:
            try:
                from app.robot.openai_panel import (
                    as_last_result_shape,
                    openai_panel_payload,
                )

                panel = openai_panel_payload(db, history_limit=1)
                if panel.get("available"):
                    last_result = as_last_result_shape(panel["latest"])
                    if not last_run_at and panel["latest"].get("analyzed_at"):
                        last_run_at = _parse_iso(str(panel["latest"]["analyzed_at"]))
            except Exception:  # noqa: BLE001
                pass

        if confirmed and scheduling_active:
            note = (
                "OpenAI autónoma autorizada e agendada a cada 15 min (observação). "
                "Sem execução de ordens. LIVE bloqueado."
            )
        elif confirmed and not robot_running:
            note = (
                "Autorização confirmada — agendamento parado "
                "(prima INICIAR para retomar o ciclo de 15 min). Sem execução de ordens."
            )
        elif confirmed and suspend_reason:
            note = (
                f"Autorização confirmada — agendamento suspenso: {suspend_reason} "
                "Sem execução de ordens."
            )
        else:
            note = (
                "A aguardar autorização — nenhuma chamada paga até confirmar a ativação. "
                "Após confirmação, corre a cada 15 min com o robô ligado. "
                "Sem execução de ordens."
            )

        prep = {
            "model": settings.openai_default_model,
            "interval_seconds": self._interval_s,
            "interval_minutes": self._interval_s / 60.0,
            "openai_key_configured": key_ok,
            "ai_enabled": control.is_enabled(),
            "armed": armed,
            "confirmed": confirmed,
            "active": active,
            "scheduling_active": scheduling_active,
            "robot_running": robot_running,
            "execution_enabled": False,
            "live_trading_locked": bool(settings.live_trading_locked),
            "daily_budget_usd": daily_cap,
            "spent_today_usd": round(spent, 6),
            "remaining_today_usd": round(remaining, 6),
            "estimate_cost_usd": reserve,
            "conservative_reserve_usd": conservative,
            "can_afford_next": can_afford,
            "mt5": {
                "ok": mt5.get("ok"),
                "reason": mt5.get("reason"),
                "terminal_connected": mt5.get("terminal_connected"),
                "pairs_ok": mt5.get("pairs_ok"),
                "clock_anomaly": mt5.get("clock_anomaly"),
                "claims_realtime": mt5.get("claims_realtime"),
            },
            "radar": {
                "monitored": scan_monitored,
                "with_valid_data": scan_valid,
                "primary_source": "mt5",
            },
            "next_analysis_at": next_at.isoformat() if next_at else None,
            "last_run_at": last_run_at.isoformat() if last_run_at else None,
            "last_result": last_result or {},
            "suspend_reason": suspend_reason,
            "blockers": blockers,
            "ready_for_activation": (not confirmed)
            and key_ok
            and control.is_enabled()
            and bool(mt5.get("ok"))
            and can_afford,
            "paid_calls_blocked_until_confirm": not confirmed,
            "note": note,
        }
        from app.robot.openai_panel import authorization_label

        prep["authorization_label"] = authorization_label(prep)
        prep["authorization_state"] = (
            "confirmed" if confirmed else "pending"
        )
        prep["schedule_state"] = (
            "active" if scheduling_active else ("suspended" if suspend_reason else "stopped")
        )
        return prep

    def arm(self, db: Session) -> dict[str, Any]:
        """Prepara parâmetros — ainda sem chamadas pagas. Não limpa bloqueio de reconfirmação."""
        _set_flag(db, FLAG_ARMED, "true")
        # Não limpar suspensão de auditoria / reconfirmação com "Preparar".
        if _get_flag(db, FLAG_REQUIRES_RECONFIRM) != "true":
            current = (self._suspend_reason or _get_flag(db, FLAG_SUSPEND_REASON) or "").lower()
            if "auditoria" not in current:
                _set_flag(db, FLAG_SUSPEND_REASON, "")
                self._suspend_reason = ""
        return self.preparation_status(db)

    def confirm_activation(self, db: Session, *, confirmed: bool) -> dict[str, Any]:
        """Confirmação explícita no programa — habilita o agendamento pago."""
        if not confirmed:
            raise AIAuthorizationError("confirmed=true é obrigatório.")
        # Temporariamente ignorar FLAG_REQUIRES_RECONFIRM para ler pré-requisitos reais.
        prev_lock = _get_flag(db, FLAG_REQUIRES_RECONFIRM)
        if prev_lock == "true":
            _set_flag(db, FLAG_REQUIRES_RECONFIRM, "false")
        try:
            prep = self.preparation_status(db)
        finally:
            if prev_lock == "true":
                _set_flag(db, FLAG_REQUIRES_RECONFIRM, "true")
        if not prep.get("openai_key_configured"):
            raise AIAuthorizationError("Chave OpenAI em falta.")
        if not prep.get("ai_enabled"):
            raise AIAuthorizationError("Ative a IA em Consumo de IA (AI_ENABLED) antes.")
        settings = get_settings()
        control = get_ai_control()
        if not control.is_enabled():
            if settings.ai_enabled:
                control.set_enabled(True, emergency=False)
            else:
                raise AIAuthorizationError("IA desligada.")
        if settings.live_trading_locked is False:
            raise AIAuthorizationError("LIVE_TRADING_LOCKED deve permanecer true.")
        _set_flag(db, FLAG_ARMED, "true")
        _set_flag(db, FLAG_CONFIRMED, "true")
        _set_flag(db, FLAG_REQUIRES_RECONFIRM, "false")
        _set_flag(db, FLAG_SUSPEND_REASON, "")
        self._suspend_reason = ""
        # Elegível só se o intervalo desde a última corrida já passou (anti-duplicação).
        self._next_at = self._earliest_eligible_at(db)
        _persist_schedule(db, last_run=None, next_at=self._next_at)
        self.ensure_loop()
        status = self.preparation_status(db)
        status["activation_confirmed"] = True
        status["message"] = (
            "Ativação confirmada. A primeira análise paga ocorrerá com o robô LIGADO "
            "quando MT5 e orçamento permitirem (intervalo 15 min)."
        )
        return status

    def deactivate(self, db: Session) -> dict[str, Any]:
        _set_flag(db, FLAG_CONFIRMED, "false")
        _set_flag(db, FLAG_ARMED, "false")
        self._next_at = None
        self._suspend_reason = "Desativado pelo utilizador."
        _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
        _set_flag(db, FLAG_NEXT_AT, "")
        self.stop_loop()
        return self.preparation_status(db)

    def on_robot_start(self) -> None:
        db = SessionLocal()
        try:
            if self.is_confirmed(db):
                # Não forçar "agora" — respeita intervalo desde a última análise paga.
                self._suspend_reason = ""
                _set_flag(db, FLAG_SUSPEND_REASON, "")
                self._next_at = self._earliest_eligible_at(db)
                _persist_schedule(db, last_run=None, next_at=self._next_at)
                self.ensure_loop()
        finally:
            db.close()

    def on_robot_stop(self) -> None:
        """PARAR interrompe novas análises OpenAI agendadas."""
        self.stop_loop()
        self._suspend_reason = "Robô parado — agendamento OpenAI interrompido."
        db = SessionLocal()
        try:
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            if self._next_at is not None:
                _set_flag(db, FLAG_NEXT_AT, self._next_at.isoformat())
        finally:
            db.close()

    def ensure_loop(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._interval_s = float(
                getattr(get_settings(), "robot_openai_interval_seconds", _DEFAULT_INTERVAL_S)
                or _DEFAULT_INTERVAL_S
            )
            self._thread = threading.Thread(
                target=self._loop, name="robot-openai-autonomous", daemon=True
            )
            self._thread.start()

    def stop_loop(self) -> None:
        self._stop.set()

    def _hydrate_schedule_from_db(self, db: Session) -> None:
        if self._next_at is None:
            persisted_next = _parse_iso(_get_flag(db, FLAG_NEXT_AT))
            if persisted_next is not None:
                self._next_at = persisted_next
        if self._last_run_at is None:
            self._last_run_at = _parse_iso(_get_flag(db, FLAG_LAST_RUN_AT))

    def _earliest_eligible_at(self, db: Session) -> datetime:
        """Próximo slot ≥ agora, nunca antes de last_run + intervalo (durável)."""
        self._hydrate_schedule_from_db(db)
        now = _utcnow()
        last = self._last_run_at or _parse_iso(_get_flag(db, FLAG_LAST_RUN_AT))
        if last is None:
            # Também consultar última decisão autónomo na BD (reinício / memória vazia).
            row = db.scalar(
                select(AIDecisionLog)
                .where(AIDecisionLog.mode == "openai_radar_autonomous")
                .order_by(AIDecisionLog.id.desc())
                .limit(1)
            )
            if row and row.created_at:
                last = row.created_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                self._last_run_at = last
        if last is None:
            return now
        earliest = last + timedelta(seconds=self._interval_s)
        return earliest if earliest > now else now

    def _claim_schedule_slot(self, db: Session) -> bool:
        """Reserva atómica na BD (cross-process) antes da chamada paga."""
        claimed = atomic_claim_autonomous_slot(db, interval_s=self._interval_s)
        if claimed:
            self._last_run_at = _parse_iso(_get_flag(db, FLAG_LAST_RUN_AT))
            self._next_at = _parse_iso(_get_flag(db, FLAG_NEXT_AT))
        else:
            self._hydrate_schedule_from_db(db)
            self._next_at = _parse_iso(_get_flag(db, FLAG_NEXT_AT)) or self._next_at
        return claimed

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_schedule()
            except Exception:  # noqa: BLE001
                logger.exception("Erro no agendador OpenAI autónomo")
            self._stop.wait(5.0)

    def _tick_schedule(self) -> None:
        db = SessionLocal()
        try:
            if not self.is_confirmed(db):
                return
            from app.robot.service import get_robot_service

            # Robô tem de estar a correr (thread viva).
            st = get_robot_service()._state(db)
            thread_alive = bool(
                get_robot_service()._thread and get_robot_service()._thread.is_alive()
            )
            if not (st.running and thread_alive):
                return
            # Intervalo: só dispara se elegível; a reserva atómica ocorre após pré-checks
            # dentro de run_once (evita queimar 15 min em falha MT5/orçamento).
            self._hydrate_schedule_from_db(db)
            now = _utcnow()
            eligible = self._earliest_eligible_at(db)
            if self._next_at is not None and now < self._next_at:
                return
            if now < eligible:
                self._next_at = eligible
                _persist_schedule(db, last_run=None, next_at=self._next_at)
                return
            self.run_once(db, dry_run=self._dry_run)
        finally:
            db.close()

    def run_once(self, db: Session, *, dry_run: bool = False) -> dict[str, Any]:
        """Uma análise. dry_run=True simula sem chamada paga (testes)."""
        if not self._call_lock.acquire(blocking=False):
            return {
                "ok": False,
                "skipped": True,
                "reason": "Análise já em curso — concorrência bloqueada.",
            }
        try:
            return self._run_once_locked(db, dry_run=dry_run)
        finally:
            self._call_lock.release()

    def _run_once_locked(self, db: Session, *, dry_run: bool) -> dict[str, Any]:
        settings = get_settings()
        daily_cap = float(
            getattr(settings, "robot_openai_daily_budget_usd", _DAILY_BUDGET_USD) or _DAILY_BUDGET_USD
        )
        mt5 = assess_mt5_ready_for_paid()
        if not mt5.get("ok"):
            self._suspend_reason = str(mt5.get("reason") or "MT5 não pronto")
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            result = {"ok": False, "skipped": True, "reason": self._suspend_reason, "mt5": mt5}
            self._last_result = result
            self._last_run_at = _utcnow()
            return result

        if not settings.provider_configured("openai"):
            self._suspend_reason = "Chave OpenAI em falta."
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            result = {"ok": False, "skipped": True, "reason": self._suspend_reason}
            self._last_result = result
            return result

        control = get_ai_control()
        if not control.is_enabled():
            self._suspend_reason = "IA desligada."
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            result = {"ok": False, "skipped": True, "reason": self._suspend_reason}
            self._last_result = result
            return result

        scan = scan_radar(db)
        if not dry_run:
            quality = validate_scan_for_paid(scan)
            if not quality.get("ok"):
                self._suspend_reason = str(
                    quality.get("reason") or "Qualidade de dados inválida."
                )
                _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
                result = {
                    "ok": False,
                    "skipped": True,
                    "reason": self._suspend_reason,
                    "data_quality": quality.get("data_quality"),
                    "executed": False,
                }
                self._last_result = result
                self._last_run_at = _utcnow()
                return result

        est = estimate_radar_analysis_usd(scan)
        reserve = round(max(float(est["estimated_cost_usd"]) * 1.5, 0.002), 6)
        spent = autonomous_spent_today_usd(db)
        if spent + reserve > daily_cap:
            self._suspend_reason = (
                f"Orçamento diário insuficiente "
                f"(${spent:.4f}+${reserve:.4f} > ${daily_cap:.2f})."
            )
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            result = {
                "ok": False,
                "skipped": True,
                "reason": self._suspend_reason,
                "spent_today_usd": spent,
                "reserve_usd": reserve,
            }
            self._last_result = result
            self._last_run_at = _utcnow()
            return result

        # Reserva do intervalo só depois dos pré-checks (MT5, qualidade, orçamento).
        if not dry_run and not self._claim_schedule_slot(db):
            return {
                "ok": False,
                "skipped": True,
                "reason": "Intervalo de 15 min ainda não decorrido (reserva concorrente).",
                "executed": False,
            }

        if dry_run:
            # Persistência simulada + decisão local sem OpenAI.
            sim_text = json.dumps(
                {
                    "decision": "HOLD",
                    "symbol": None,
                    "justification": "Dry-run autónomo — sem chamada paga.",
                    "risks": "Nenhum (simulação).",
                    "compared_symbols": [c.symbol for c in scan.all_valid],
                    "confidence": 0.5,
                },
                ensure_ascii=False,
            )
            path = persist_raw_provider_response(
                provider="openai",
                model=settings.openai_default_model,
                raw_http={"choices": [{"message": {"content": sim_text}}], "dry_run": True},
                text=sim_text,
                authorization_id="dry-run-autonomous",
                role="robot_radar_autonomous",
                reason="dry_run",
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost_usd=0.0,
                status="dry_run",
            )
            result = {
                "ok": True,
                "dry_run": True,
                "decision": "HOLD",
                "decision_pt": "AGUARDAR",
                "symbol": None,
                "actual_cost_usd": 0.0,
                "persisted_path": str(path),
                "executed": False,
                "reserve_usd": reserve,
            }
            self._last_result = result
            self._last_run_at = _utcnow()
            self._suspend_reason = ""
            return result

        # Reserva + autorização de 1 chamada.
        try:
            auth = control.create_authorization(
                db,
                confirmed=True,
                workflow="robot_radar_autonomous",
                debate_rounds=0,
                model=settings.openai_default_model,
                max_calls=1,
                daily_budget_override_usd=daily_cap,
                spent_today_override_usd=spent,
            )
        except (AIDisabled, AIAuthorizationError, BudgetExceeded) as exc:
            self._suspend_reason = str(exc)
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            result = {"ok": False, "skipped": True, "reason": self._suspend_reason}
            self._last_result = result
            self._last_run_at = _utcnow()
            return result

        token = str(auth["authorization_id"])
        with control._lock:
            a = control._auths.get(token)
            if a:
                a.max_calls = 1
                a.reserved_cost_usd = reserve
                a.max_cost_usd = min(daily_cap - spent, max(reserve, float(est["estimated_cost_usd"])))

        # Verificar disco de persistência antes da chamada.
        try:
            store_dir().mkdir(parents=True, exist_ok=True)
            probe = store_dir() / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            self._suspend_reason = f"Erro de persistência: {exc}"
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            control.end_analysis_context(token)
            result = {"ok": False, "skipped": True, "reason": self._suspend_reason}
            self._last_result = result
            return result

        try:
            ctx, prop, scan2, usage = run_radar_decision(
                db,
                mode="openai",
                auth_token=token,
                call_role="robot_radar_autonomous",
            )
        except Exception as exc:  # noqa: BLE001
            # Sem retry automático.
            self._suspend_reason = f"Falha na análise (sem repetição): {exc}"
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)
            result = {
                "ok": False,
                "skipped": False,
                "error": str(exc),
                "reason": self._suspend_reason,
                "persisted_path": "",
                "executed": False,
            }
            self._last_result = result
            self._last_run_at = _utcnow()
            return result

        quote = ctx.get("quote") or {}
        persisted = str(usage.get("persisted_path") or "")
        cost = float(usage.get("estimated_cost_usd") or 0.0)

        demo_auth: dict[str, Any] = {}
        try:
            from app.brokers.mt5_demo_auth import get_demo_auth_snapshot

            demo_auth = get_demo_auth_snapshot(db) or {}
        except Exception:  # noqa: BLE001
            demo_auth = {}
        demo_ready = bool(demo_auth.get("active")) and not bool(
            demo_auth.get("emergency") or demo_auth.get("pause_entries")
        )
        synthetic_run = bool(str(token).startswith("test-") or dry_run)

        log = AIDecisionLog(
            mode="openai_radar_autonomous",
            decision=prop.decision,
            justification=prop.justification,
            favoring_scenario=prop.favoring_scenario,
            invalidating_scenario=prop.invalidating_scenario,
            entry_price=float(prop.entry_price or 0),
            stop_loss=float(prop.stop_loss or 0),
            take_profit=float(prop.take_profit or 0),
            time_horizon=prop.time_horizon or "",
            risks=prop.risks or "",
            missing_information=(prop.missing_information or "")[:2000],
            confidence=float(prop.confidence or 0),
            context_json=json.dumps(
                {
                    "symbol": prop.symbol,
                    "quote": quote,
                    "authorization_id": token,
                    "mt5_clock_anomaly": mt5.get("clock_anomaly"),
                    "observed_at": _utcnow().isoformat(),
                    "pairs": [c.symbol for c in scan2.all_valid],
                    "local_priority_hint": (ctx.get("radar") or {}).get("local_priority_hint")
                    or (ctx.get("local_priority_hint")),
                    "radar_best_candidate": (
                        ((ctx.get("radar") or {}).get("local_priority_hint") or [{}])[0]
                        if (ctx.get("radar") or {}).get("local_priority_hint")
                        else None
                    ),
                    "decision_quality": ctx.get("decision_quality"),
                    "directional_opportunities": (ctx.get("radar") or {}).get(
                        "directional_opportunities"
                    )
                    or (
                        getattr(scan2, "directional", None) or {}
                    ).get("top_opportunities"),
                    "directional_snapshot_at": (
                        (getattr(scan2, "directional", None) or {}).get("generated_at")
                    ),
                    "data_quality": (ctx.get("radar") or {}).get("data_quality")
                    or (scan2.compact_for_ai().get("data_quality") if scan2 else None),
                    "temporal_disclaimer": (ctx.get("radar") or {}).get("temporal_disclaimer"),
                    "persisted_path": persisted,
                    # HOLD nunca executa; BUY/SELL seguem para o validador DEMO se autorizado.
                    "execution_disabled": not demo_ready,
                    "demo_execution_armed": demo_ready,
                    "synthetic_test": synthetic_run,
                },
                ensure_ascii=False,
                default=str,
            )[:14000],
            proposal_json=json.dumps(prop.as_dict(), ensure_ascii=False),
            risk_approved=False,
            risk_reasons=_risk_reasons_for_decision(prop.decision, ctx.get("decision_quality")),
            quote_lag_seconds=float(quote.get("lag_seconds") or 0),
            estimated_cost_usd=cost,
            executed=False,
        )
        exec_result: dict[str, Any] = {"attempted": False}
        try:
            db.add(log)
            db.commit()
            db.refresh(log)
            log_id = log.id
            try:
                from app.robot.prediction_store import persist_scan_predictions

                dir_scan = getattr(scan2, "directional", None) or {}
                if dir_scan:
                    persist_scan_predictions(
                        db,
                        dir_scan,
                        decision_id=int(log_id or 0),
                        openai_decision=str(prop.decision or ""),
                    )
            except Exception as pe:  # noqa: BLE001
                logger.debug("persist previsões pós-OpenAI: %s", pe)
            from app.robot.radar_decision_quality import is_decision_quality_insufficient

            quality_bad = is_decision_quality_insufficient(ctx.get("decision_quality"))
            # HOLD: nunca executa. BUY/SELL com qualidade insuficiente: bloqueado.
            if prop.decision in {"BUY", "SELL"} and quality_bad:
                exec_result = {
                    "attempted": False,
                    "ok": False,
                    "blocked_quality": True,
                    "reason": (
                        "decision_quality=insufficient — execução DEMO bloqueada. "
                        "Resposta original preservada; ver quality_issues no painel."
                    ),
                    "quality_issues": list(
                        (ctx.get("decision_quality") or {}).get("issues") or []
                    ),
                }
                log.risk_approved = False
                log.risk_reasons = exec_result["reason"]
                db.commit()
            elif prop.decision in {"BUY", "SELL"}:
                # Execução DEMO só se autorizado — independente da autorização OpenAI.
                try:
                    from app.brokers.mt5_demo_executor import get_mt5_demo_executor

                    exec_result = get_mt5_demo_executor().try_execute_decision(db, log)
                    exec_result["attempted"] = True
                    db.refresh(log)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Executor DEMO após decisão")
                    exec_result = {
                        "attempted": True,
                        "ok": False,
                        "reason": str(exc),
                    }
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            # Resposta já deve estar no response_store pelo provider.
            log_id = None
            self._suspend_reason = f"ORM falhou após resposta (preservada em disco): {exc}"
            _set_flag(db, FLAG_SUSPEND_REASON, self._suspend_reason)

        result = {
            "ok": True,
            "dry_run": False,
            "decision": prop.decision,
            "decision_pt": DECISION_PT.get(prop.decision, prop.decision),
            "symbol": prop.symbol,
            "justification": prop.justification,
            "risks": prop.risks,
            "invalidating_scenario": prop.invalidating_scenario,
            "favoring_scenario": prop.favoring_scenario,
            "observed_quote": {
                "mid": quote.get("mid"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "spread_pips": quote.get("spread_pips"),
                "source": quote.get("source"),
                "feed_state": quote.get("feed_state"),
                "lag_seconds": quote.get("lag_seconds"),
                "clock_anomaly": quote.get("clock_anomaly"),
            },
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "actual_cost_usd": cost,
            "reserve_usd": reserve,
            "persisted_path": persisted,
            "decision_log_id": log_id,
            "executed": bool(getattr(log, "executed", False)) if log_id else False,
            "demo_execution": exec_result,
            "observed_at": _utcnow().isoformat(),
        }
        self._last_result = result
        self._last_run_at = _utcnow()
        self._suspend_reason = ""
        _set_flag(db, FLAG_SUSPEND_REASON, "")
        return result


_scheduler: OpenAIAutonomousScheduler | None = None
_sched_lock = threading.Lock()


def get_openai_autonomous() -> OpenAIAutonomousScheduler:
    global _scheduler
    with _sched_lock:
        if _scheduler is None:
            _scheduler = OpenAIAutonomousScheduler()
        return _scheduler
