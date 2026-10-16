"""Ciclo autónomo de investigação + orquestração paper (local, sem IA paga)."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptive.runner import get_experiment_runner
from app.adaptive.service import get_adaptive_service
from app.adaptive.strategies import list_catalog, param_grid
from app.autonomy.fingerprint import experiment_fingerprint
from app.autonomy.paper import get_paper_service
from app.autonomy.timeutil import as_utc, iso_utc, utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.market.history_inventory import (
    DEFAULT_INVESTIGATION_END,
    DEFAULT_INVESTIGATION_START,
    prepare_experiment,
)
from app.models.adaptive import AdaptiveExperiment, AdaptiveStrategyState
from app.models.autonomy import (
    AutonomyAlert,
    AutonomyCycleLog,
    AutonomyHypothesis,
    AutonomyState,
    ExperimentFingerprint,
    PaperTradeLog,
)
from app.models.trading import ClosedTrade, Position

logger = logging.getLogger(__name__)

# Estados distintos expostos à UI (pt-PT via i18n).
STATUS_OFF = "off"
STATUS_PAUSED = "paused"
STATUS_IDLE = "idle"  # Ativo / à espera
STATUS_EXECUTING = "executing"  # Em execução
STATUS_ERROR = "error"


class AutonomyService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cycle_busy = False
        self._now_fn: Callable[[], datetime] | None = None

    def set_clock(self, fn: Callable[[], datetime] | None) -> None:
        """Relógio injetável para testes (deve devolver UTC)."""
        self._now_fn = fn

    def _now(self) -> datetime:
        if self._now_fn is not None:
            return as_utc(self._now_fn()) or utcnow()
        return utcnow()

    def _state(self, db: Session) -> AutonomyState:
        row = db.scalar(select(AutonomyState).order_by(AutonomyState.id.desc()).limit(1))
        if row is None:
            row = AutonomyState(paused=True, enabled=False, paper_enabled=False, run_status=STATUS_OFF)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row

    def _record_tick_error(self, db: Session, exc: BaseException) -> None:
        try:
            st = self._state(db)
            st.last_error = str(exc)
            st.last_message = f"Ciclo bloqueado por erro: {exc}"
            st.run_status = STATUS_ERROR
            st.updated_at = self._now()
            db.add(AutonomyAlert(level="ERROR", message=st.last_message))
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao persistir erro do ciclo autónomo")

    def compute_display_status(
        self,
        st: AutonomyState,
        *,
        active_experiment_id: int | None = None,
    ) -> str:
        """Estado real para a UI — nunca 'executing' se o ciclo falhou."""
        if not st.enabled:
            return STATUS_OFF
        if st.paused:
            return STATUS_PAUSED
        if self._cycle_busy or active_experiment_id is not None:
            return STATUS_EXECUTING
        if (st.last_error or "").strip():
            return STATUS_ERROR
        return STATUS_IDLE

    def dashboard(self, db: Session) -> dict[str, Any]:
        try:
            self.maybe_tick(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Autonomy tick: %s", exc)
            self._record_tick_error(db, exc)

        st = self._state(db)
        settings = get_settings()
        strategies = list(db.scalars(select(AdaptiveStrategyState).limit(200)))
        by_status = {"experimental": [], "validating": [], "approved": [], "suspended": [], "retired": []}
        for s in strategies:
            bucket = by_status.get(s.status, by_status["experimental"])
            bucket.append(
                {
                    "id": s.id,
                    "key": s.strategy_key,
                    "name": s.display_name,
                    "status": s.status,
                    "score": s.validation_score,
                    "oos": s.out_of_sample_score,
                    "reason": s.reason,
                }
            )

        open_paper = list(db.scalars(select(PaperTradeLog).where(PaperTradeLog.status == "open").limit(20)))
        closed_paper = list(
            db.scalars(select(PaperTradeLog).where(PaperTradeLog.status == "closed").order_by(PaperTradeLog.id.desc()).limit(40))
        )
        wins = [t for t in closed_paper if t.pnl > 0]
        losses = [t for t in closed_paper if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
        net = sum(t.pnl for t in closed_paper)
        costs = sum(t.costs for t in closed_paper)
        hit = (len(wins) / len(closed_paper) * 100.0) if closed_paper else 0.0

        alerts = list(db.scalars(select(AutonomyAlert).order_by(AutonomyAlert.id.desc()).limit(30)))
        hyps = list(db.scalars(select(AutonomyHypothesis).order_by(AutonomyHypothesis.id.desc()).limit(20)))
        running = list(
            db.scalars(
                select(AdaptiveExperiment).where(AdaptiveExperiment.status.in_(("queued", "running"))).limit(5)
            )
        )
        latest_exp = db.scalar(select(AdaptiveExperiment).order_by(AdaptiveExperiment.id.desc()).limit(1))
        rejection = self._rejection_summary(db, latest_exp)
        latest_cycle = db.scalar(select(AutonomyCycleLog).order_by(AutonomyCycleLog.id.desc()).limit(1))
        recent_cycles = list(
            db.scalars(select(AutonomyCycleLog).order_by(AutonomyCycleLog.id.desc()).limit(10))
        )

        account_equity = settings.starting_equity + net
        eq = settings.starting_equity
        peak_eq = eq
        max_dd = 0.0
        for t in reversed(closed_paper):
            eq += t.pnl
            peak_eq = max(peak_eq, eq)
            if peak_eq > 0:
                max_dd = max(max_dd, (peak_eq - eq) / peak_eq * 100.0)

        positions = list(db.scalars(select(Position).limit(10)))
        trades_sim = list(db.scalars(select(ClosedTrade).order_by(ClosedTrade.id.desc()).limit(20)))
        active_id = get_experiment_runner().active_id()
        display = self.compute_display_status(st, active_experiment_id=active_id)
        # Persist espelho para retoma / diagnóstico.
        if st.run_status != display:
            st.run_status = display
            db.commit()

        catalog = list_catalog()
        return {
            "title": "Centro de Autonomia",
            "disclaimer": (
                "Sistema de investigação e paper trading local. "
                "Resultados históricos/simulados não são previsão de lucro. "
                "Negociação real permanentemente bloqueada. IA paga desligada por defeito."
            ),
            "engine": {
                "enabled": st.enabled,
                "paused": st.paused,
                "paper_enabled": st.paper_enabled,
                "interval_minutes": st.interval_minutes,
                "lookback_days": st.lookback_days,
                "max_combos": st.max_combos,
                "last_cycle_at": iso_utc(st.last_cycle_at),
                "next_cycle_at": iso_utc(st.next_cycle_at),
                "last_message": st.last_message,
                "last_error": st.last_error,
                "cycles_completed": st.cycles_completed,
                "ai_calls": st.ai_calls,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "active_experiment_id": active_id,
                "display_status": display,
                "run_status": display,
                "catalog_strategies": [s.key for s in catalog],
            },
            "last_cycle": self._cycle_dict(latest_cycle),
            "recent_cycles": [self._cycle_dict(c) for c in recent_cycles],
            "security": {
                "live_trading_locked": True,
                "ai_enabled": bool(settings.ai_enabled),
                "risk_limits_auto_change": False,
            },
            "strategies": by_status,
            "paper": {
                "open": [
                    {
                        "id": t.id,
                        "side": t.side,
                        "entry": t.entry_price,
                        "stop": t.stop,
                        "take": t.take,
                        "strategy": t.strategy_key,
                        "reason": t.reason,
                        "opened_at": iso_utc(t.opened_at),
                        "is_replay": t.is_replay,
                    }
                    for t in open_paper
                ],
                "closed": [
                    {
                        "id": t.id,
                        "side": t.side,
                        "pnl": t.pnl,
                        "costs": t.costs,
                        "strategy": t.strategy_key,
                        "closed_at": iso_utc(t.closed_at),
                    }
                    for t in closed_paper[:20]
                ],
                "net_pnl": round(net, 4),
                "equity_approx": round(account_equity, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "trades": len(closed_paper),
                "win_rate_pct": round(hit, 2),
                "profit_factor": round(pf, 4),
                "costs": round(costs, 4),
            },
            "broker_positions": len(positions),
            "broker_closed_sample": len(trades_sim),
            "hypotheses": [
                {
                    "id": h.id,
                    "strategy_key": h.strategy_key,
                    "status": h.status,
                    "rationale": h.rationale,
                    "priority": h.priority,
                }
                for h in hyps
            ],
            "running_tasks": [
                {
                    "id": e.id,
                    "status": e.status,
                    "progress_pct": getattr(e, "progress_pct", 0),
                    "progress_message": getattr(e, "progress_message", ""),
                }
                for e in running
            ],
            "alerts": [
                {
                    "id": a.id,
                    "level": a.level,
                    "message": a.message,
                    "created_at": iso_utc(a.created_at) or "",
                }
                for a in alerts
            ],
            "rejection_audit": rejection,
            "catalog_size": len(catalog),
        }

    def _cycle_dict(self, row: AutonomyCycleLog | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row.id,
            "started_at": iso_utc(row.started_at),
            "finished_at": iso_utc(row.finished_at),
            "duration_seconds": row.duration_seconds,
            "status": row.status,
            "message": row.message,
            "experiment_id": row.experiment_id,
            "strategies_count": row.strategies_count,
        }

    def _rejection_summary(self, db: Session, exp: AdaptiveExperiment | None) -> dict[str, Any]:
        if exp is None:
            return {"experiment_id": None, "message": "Ainda sem experiências."}
        try:
            results = json.loads(exp.results_json or "{}")
        except json.JSONDecodeError:
            results = {}
        cands = results.get("candidates") or []
        reasons: dict[str, int] = {}
        near = []
        for c in cands:
            val_pf = float((c.get("validation_metrics") or {}).get("profit_factor") or 0)
            oos_pf = float((c.get("test_metrics") or {}).get("profit_factor") or 0)
            trades = int((c.get("validation_metrics") or {}).get("trades") or 0)
            score = float(c.get("score") or 0)
            if c.get("overfit_rejected"):
                reasons["sobreajuste_ou_poucas_ops"] = reasons.get("sobreajuste_ou_poucas_ops", 0) + 1
            elif val_pf < 1.05:
                reasons["pf_validacao_abaixo_1.05"] = reasons.get("pf_validacao_abaixo_1.05", 0) + 1
            elif oos_pf < 1.0:
                reasons["pf_teste_abaixo_1.0"] = reasons.get("pf_teste_abaixo_1.0", 0) + 1
            elif trades < 5:
                reasons["poucas_operacoes_validacao"] = reasons.get("poucas_operacoes_validacao", 0) + 1
            elif score < 0.5:
                reasons["score_abaixo_0.5"] = reasons.get("score_abaixo_0.5", 0) + 1
            else:
                reasons["outros"] = reasons.get("outros", 0) + 1
            if score >= 0 and val_pf >= 0.95:
                near.append(
                    {
                        "key": c.get("strategy_key"),
                        "score": score,
                        "pf_val": val_pf,
                        "pf_oos": oos_pf,
                        "trades_val": trades,
                    }
                )
        approved = sum(1 for c in cands if c.get("status") == "approved")
        return {
            "experiment_id": exp.id,
            "status": exp.status,
            "candidates": len(cands),
            "approved": approved,
            "selection": results.get("selection"),
            "rejection_counts": reasons,
            "near_misses": sorted(near, key=lambda x: -x["score"])[:5],
            "promotion_criteria": {
                "min_score": 0.5,
                "min_pf_validation": 1.05,
                "min_pf_test": 1.0,
                "min_validation_trades": 5,
                "note": "Critérios não foram relaxados para forçar aprovações.",
            },
            "message": (
                "Nenhuma estratégia aprovada: após custos, a validação não mostrou "
                "vantagem estável (PF≥1.05 e OOS≥1.0 com operações suficientes)."
                if approved == 0
                else f"{approved} estratégias aprovadas."
            ),
        }

    def configure(
        self,
        db: Session,
        *,
        enabled: bool | None = None,
        paused: bool | None = None,
        paper_enabled: bool | None = None,
        interval_minutes: int | None = None,
        lookback_days: int | None = None,
        max_combos: int | None = None,
    ) -> dict[str, Any]:
        st = self._state(db)
        if enabled is not None:
            st.enabled = enabled
            if enabled and st.paused:
                st.paused = False
        if paused is not None:
            st.paused = paused
        if paper_enabled is not None:
            st.paper_enabled = paper_enabled
        if interval_minutes is not None:
            st.interval_minutes = max(30, min(interval_minutes, 24 * 60))
        if lookback_days is not None:
            st.lookback_days = max(30, min(lookback_days, 400))
        if max_combos is not None:
            st.max_combos = max(2, min(max_combos, 8))
        now = self._now()
        if st.enabled and not st.paused:
            st.next_cycle_at = now
            st.last_error = ""
            st.last_message = "Ciclo autónomo ativo — a agendar investigação local."
            st.run_status = STATUS_IDLE
            self.ensure_background_loop()
        else:
            st.last_message = "Ciclo pausado ou desligado."
            st.run_status = STATUS_PAUSED if st.paused and st.enabled else STATUS_OFF
            if paused is True or enabled is False:
                self.stop_background_loop()
        st.updated_at = now
        db.commit()
        if st.enabled and not st.paused:
            try:
                self.maybe_tick(db)
            except Exception as exc:  # noqa: BLE001
                self._record_tick_error(db, exc)
        return self.dashboard(db)["engine"]

    def ensure_background_loop(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="autonomy-cycle", daemon=True)
            self._thread.start()

    def stop_background_loop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                self.maybe_tick(db)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Erro no ciclo autónomo")
                self._record_tick_error(db, exc)
            finally:
                db.close()
            self._stop.wait(20.0)

    def maybe_tick(self, db: Session, *, now: datetime | None = None) -> dict[str, Any] | None:
        st = self._state(db)
        st.ai_calls = 0

        if st.paper_enabled:
            try:
                get_paper_service().tick(db)
            except Exception as exc:  # noqa: BLE001
                st.last_error = f"Paper: {exc}"
                st.run_status = STATUS_ERROR
                db.commit()

        if not st.enabled or st.paused:
            st.run_status = STATUS_PAUSED if st.paused and st.enabled else STATUS_OFF
            db.commit()
            return None

        clock = as_utc(now) if now is not None else self._now()
        assert clock is not None

        next_at = as_utc(st.next_cycle_at)
        if next_at is not None and next_at > clock:
            st.run_status = STATUS_ERROR if (st.last_error or "").strip() else STATUS_IDLE
            db.commit()
            return None

        if get_experiment_runner().active_id() is not None:
            st.last_message = "A aguardar conclusão da experiência em curso."
            st.run_status = STATUS_EXECUTING
            db.commit()
            return None

        self._cycle_busy = True
        st.run_status = STATUS_EXECUTING
        db.commit()
        cycle_row = AutonomyCycleLog(
            started_at=clock,
            status="running",
            message="Ciclo a iniciar",
            strategies_count=len(list_catalog()),
        )
        db.add(cycle_row)
        db.commit()
        db.refresh(cycle_row)

        try:
            result = self._run_research_cycle(db, st, clock=clock)
            finished = self._now()
            cycle_row.finished_at = finished
            started_at = as_utc(cycle_row.started_at) or clock
            cycle_row.duration_seconds = max(0.0, (finished - started_at).total_seconds())
            if result.get("error"):
                cycle_row.status = "error"
                cycle_row.message = str(result["error"])
                st.last_error = str(result["error"])
                st.run_status = STATUS_ERROR
            elif result.get("skipped"):
                cycle_row.status = "skipped"
                cycle_row.message = str(result.get("reason") or st.last_message)
                # skipped não é erro fatal — limpar erro antigo de comparação de datas
                if "offset-naive" not in (st.last_error or ""):
                    pass
                st.run_status = STATUS_IDLE
            else:
                cycle_row.status = "ok"
                cycle_row.message = st.last_message or "Ciclo concluído"
                cycle_row.experiment_id = int((result.get("started") or {}).get("id") or 0)
                st.last_error = ""
                st.run_status = STATUS_IDLE
            cycle_row.result_json = json.dumps(result, ensure_ascii=False, default=str)[:4000]
            st.last_cycle_at = clock
            st.next_cycle_at = clock + timedelta(minutes=st.interval_minutes)
            st.cycles_completed += 1
            st.updated_at = finished
            db.commit()
            return result
        except Exception as exc:  # noqa: BLE001
            finished = self._now()
            cycle_row.finished_at = finished
            started = as_utc(cycle_row.started_at) or clock
            cycle_row.duration_seconds = max(0.0, (finished - started).total_seconds())
            cycle_row.status = "error"
            cycle_row.message = str(exc)
            cycle_row.result_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
            st.last_error = str(exc)
            st.last_message = f"Ciclo bloqueado por erro: {exc}"
            st.last_cycle_at = clock
            st.next_cycle_at = clock + timedelta(minutes=st.interval_minutes)
            st.run_status = STATUS_ERROR
            st.updated_at = finished
            db.add(AutonomyAlert(level="ERROR", message=st.last_message))
            db.commit()
            raise
        finally:
            self._cycle_busy = False

    def _run_research_cycle(
        self, db: Session, st: AutonomyState, *, clock: datetime
    ) -> dict[str, Any]:
        end = DEFAULT_INVESTIGATION_END
        start = DEFAULT_INVESTIGATION_START
        if st.lookback_days < 360:
            end = clock.replace(minute=0, second=0, microsecond=0)
            start = end - timedelta(days=st.lookback_days)

        start = as_utc(start) or start
        end = as_utc(end) or end

        prep = prepare_experiment(
            db, start=start, end=end, symbol=st.symbol, timeframe=st.timeframe, require_real_data=True
        )
        if not prep["ready"]:
            st.last_message = "Ciclo adiado: " + prep["message"]
            db.add(AutonomyAlert(level="WARN", message=st.last_message))
            self._enqueue_hypotheses_from_gaps(db, prep)
            return {"skipped": True, "reason": prep["message"]}

        catalog = list_catalog()
        catalog_keys = sorted(s.key for s in catalog)
        created = 0
        for spec in catalog:
            for params in param_grid(spec, max_combos=min(3, st.max_combos)):
                fp = experiment_fingerprint(
                    strategy_key=spec.key,
                    params=params,
                    start_iso=start.isoformat(),
                    end_iso=end.isoformat(),
                    timeframe=st.timeframe,
                )
                exists = db.scalar(
                    select(ExperimentFingerprint).where(ExperimentFingerprint.fingerprint == fp)
                )
                if exists:
                    continue
                pending = db.scalar(
                    select(AutonomyHypothesis).where(
                        AutonomyHypothesis.strategy_key == spec.key,
                        AutonomyHypothesis.params_json == json.dumps(params, sort_keys=True),
                        AutonomyHypothesis.status == "pending",
                    )
                )
                if pending:
                    continue
                db.add(
                    AutonomyHypothesis(
                        strategy_key=spec.key,
                        params_json=json.dumps(params, sort_keys=True, ensure_ascii=False),
                        rationale=f"Hipótese controlada do catálogo {spec.display_name}.",
                        status="pending",
                        priority=0,
                    )
                )
                created += 1
                if created >= 4:
                    break
            if created >= 4:
                break
        db.commit()

        batch_fp = experiment_fingerprint(
            strategy_key="__batch__",
            params={
                "max_combos": st.max_combos,
                "strategies": catalog_keys,
                "n_strategies": len(catalog_keys),
            },
            start_iso=start.isoformat(),
            end_iso=end.isoformat(),
            timeframe=st.timeframe,
        )
        if db.scalar(select(ExperimentFingerprint).where(ExperimentFingerprint.fingerprint == batch_fp)):
            st.last_message = "Experiência equivalente já registada — a propor novas hipóteses apenas."
            self._propose_from_failures(db)
            return {"skipped": True, "reason": "duplicate_batch", "strategies": catalog_keys}

        try:
            exp_payload = get_adaptive_service().start_experiment(
                db,
                start=start,
                end=end,
                symbol=st.symbol,
                timeframe=st.timeframe,
                name=f"autonomia-ciclo-{st.cycles_completed + 1}",
                require_real_data=True,
                async_run=True,
                max_combos_per_strategy=st.max_combos,
            )
            db.add(
                ExperimentFingerprint(
                    fingerprint=batch_fp,
                    strategy_key="__batch__",
                    params_json=json.dumps(
                        {"max_combos": st.max_combos, "strategies": catalog_keys},
                        ensure_ascii=False,
                    ),
                    period_start=start.isoformat(),
                    period_end=end.isoformat(),
                    experiment_id=int(exp_payload.get("id") or 0),
                )
            )
            st.last_message = (
                f"Experiência #{exp_payload.get('id')} enfileirada "
                f"({len(catalog_keys)} estratégias, {st.max_combos} combos/estratégia, "
                f"período {start.date()}→{end.date()})."
            )
            st.last_error = ""
            db.add(AutonomyAlert(level="INFO", message=st.last_message))
            db.commit()
            return {"started": exp_payload, "strategies": catalog_keys}
        except ValueError as exc:
            st.last_error = str(exc)
            st.last_message = f"Ciclo sem nova experiência: {exc}"
            db.add(AutonomyAlert(level="WARN", message=st.last_message))
            db.commit()
            return {"error": str(exc), "strategies": catalog_keys}

    def _enqueue_hypotheses_from_gaps(self, db: Session, prep: dict[str, Any]) -> None:
        db.add(
            AutonomyHypothesis(
                strategy_key="__data__",
                params_json="{}",
                rationale="Cobertura insuficiente — descarregar histórico antes de investigar.",
                status="skipped",
                priority=-1,
            )
        )
        db.commit()

    def _propose_from_failures(self, db: Session) -> None:
        """Novas hipóteses a partir de falhas — parâmetros vizinhos limitados."""
        latest = db.scalar(select(AdaptiveExperiment).order_by(AdaptiveExperiment.id.desc()).limit(1))
        if not latest:
            return
        try:
            results = json.loads(latest.results_json or "{}")
        except json.JSONDecodeError:
            return
        cands = sorted(results.get("candidates") or [], key=lambda c: -float(c.get("score") or 0))
        for c in cands[:3]:
            params = dict(c.get("params") or {})
            for key in list(params.keys()):
                if isinstance(params[key], int):
                    params[key] = max(1, int(params[key]) + 1)
                    break
            db.add(
                AutonomyHypothesis(
                    strategy_key=str(c.get("strategy_key") or ""),
                    params_json=json.dumps(params, sort_keys=True, ensure_ascii=False),
                    rationale=(
                        f"Proposta a partir de near-miss score={c.get('score')} "
                        f"PF_val={(c.get('validation_metrics') or {}).get('profit_factor')}."
                    ),
                    status="pending",
                    priority=1,
                )
            )
        db.commit()


_svc: AutonomyService | None = None
_svc_lock = threading.Lock()


def get_autonomy_service() -> AutonomyService:
    global _svc
    with _svc_lock:
        if _svc is None:
            _svc = AutonomyService()
        return _svc
