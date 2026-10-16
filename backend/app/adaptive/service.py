"""Fachada do Motor Adaptativo para API e dashboard."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adaptive.experiments import run_experiment
from app.adaptive.regimes import REGIME_LABELS_PT, classify_regime
from app.adaptive.strategies import list_catalog
from app.config import get_settings
from app.market.history_inventory import (
    DEFAULT_INVESTIGATION_END,
    DEFAULT_INVESTIGATION_START,
    analyze_coverage,
    cache_bounds,
    prepare_experiment,
)
from app.market.incremental_download import get_incremental_download_controller
from app.market.labels import DUKASCOPY_SOURCE
from app.models.adaptive import (
    AdaptiveChangeLog,
    AdaptiveDecisionLog,
    AdaptiveExperiment,
    AdaptiveSchedule,
    AdaptiveStrategyState,
)
from app.models.market import Candle


class AdaptiveEngineService:
    def dashboard(self, db: Session) -> dict[str, Any]:
        settings = get_settings()
        regime = self.current_regime(db)
        strategies = list(db.scalars(select(AdaptiveStrategyState).order_by(AdaptiveStrategyState.updated_at.desc()).limit(100)))
        experiments = list(db.scalars(select(AdaptiveExperiment).order_by(AdaptiveExperiment.created_at.desc()).limit(20)))
        changes = list(db.scalars(select(AdaptiveChangeLog).order_by(AdaptiveChangeLog.created_at.desc()).limit(40)))
        schedule = db.scalar(select(AdaptiveSchedule).order_by(AdaptiveSchedule.id.desc()).limit(1))

        by_status: dict[str, list[dict[str, Any]]] = {
            "experimental": [],
            "validating": [],
            "approved": [],
            "suspended": [],
            "retired": [],
        }
        for s in strategies:
            bucket = by_status.get(s.status, by_status["experimental"])
            bucket.append(self._strategy_dict(s))

        latest = experiments[0] if experiments else None
        latest_results = json.loads(latest.results_json or "{}") if latest else {}

        return {
            "title": "Motor Adaptativo",
            "disclaimer": (
                "Experiências e resultados são exclusivamente simulados. "
                "Desempenho histórico não é previsão de lucro futuro. "
                "A negociação real permanece permanentemente bloqueada."
            ),
            "regime": regime,
            "catalog": [
                {
                    "key": s.key,
                    "display_name": s.display_name,
                    "description": s.description,
                    "default_params": s.default_params,
                    "param_bounds": {
                        k: {"min": v[0], "max": v[1], "step": v[2]} for k, v in s.param_bounds.items()
                    },
                    "preferred_regimes": list(s.preferred_regimes),
                    "complexity": s.complexity,
                }
                for s in list_catalog()
            ],
            "strategies": by_status,
            "performance": [
                {
                    "id": s.id,
                    "name": s.display_name,
                    "key": s.strategy_key,
                    "status": s.status,
                    "validation_score": s.validation_score,
                    "out_of_sample_score": s.out_of_sample_score,
                    "max_drawdown_pct": s.max_drawdown_pct,
                    "net_pnl": s.net_pnl,
                    "trading_costs": s.trading_costs,
                    "experiment_count": s.experiment_count,
                    "reason": s.reason,
                }
                for s in strategies[:30]
            ],
            "out_of_sample": latest_results.get("candidates", [])[:12],
            "changes": [
                {
                    "id": c.id,
                    "strategy_state_id": c.strategy_state_id,
                    "experiment_id": c.experiment_id,
                    "action": c.action,
                    "from_status": c.from_status,
                    "to_status": c.to_status,
                    "reason": c.reason,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in changes
            ],
            "experiments": [self._experiment_dict(e) for e in experiments],
            "schedule": self._schedule_dict(schedule) if schedule else None,
            "stats": {
                "strategies_total": len(strategies),
                "experiments_total": db.scalar(select(func.count()).select_from(AdaptiveExperiment)) or 0,
                "decisions_total": db.scalar(select(func.count()).select_from(AdaptiveDecisionLog)) or 0,
                "approved": len(by_status["approved"]),
                "suspended": len(by_status["suspended"]),
            },
            "risk": {
                "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
                "max_daily_loss_pct": settings.max_daily_loss_pct,
                "auto_risk_increase": False,
                "live_trading_locked": True,
            },
            "latest_selection": latest_results.get("selection"),
            "costs_model": latest_results.get("costs_model"),
            "history": self.history_summary(db),
            "ai_enabled": bool(settings.ai_enabled),
            "ai_calls_in_adaptive": False,
        }

    def history_summary(
        self,
        db: Session,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        symbol: str = "EURUSD",
        timeframe: str = "M15",
    ) -> dict[str, Any]:
        start = start or DEFAULT_INVESTIGATION_START
        end = end or DEFAULT_INVESTIGATION_END
        bounds = cache_bounds(db, symbol=symbol, source=DUKASCOPY_SOURCE)
        coverage = analyze_coverage(
            db, start=start, end=end, symbol=symbol, timeframe=timeframe, source=DUKASCOPY_SOURCE
        )
        prep = prepare_experiment(
            db,
            start=start,
            end=end,
            symbol=symbol,
            timeframe=timeframe,
            require_real_data=True,
        )
        syn_bounds = cache_bounds(db, symbol=symbol, source="synthetic")
        ctrl = get_incremental_download_controller()
        latest = ctrl.reconcile_active(db)
        latest_payload = ctrl.job_payload(latest) if latest else None
        return {
            "default_period": {
                "start": DEFAULT_INVESTIGATION_START.isoformat(),
                "end": DEFAULT_INVESTIGATION_END.isoformat(),
                "symbol": "EURUSD",
                "timeframe": "M15",
                "label_pt": "01/10/2025 00:00 UTC → 01/10/2026 00:00 UTC",
            },
            "selected_period": {
                "start": start.isoformat() if hasattr(start, "isoformat") else str(start),
                "end": end.isoformat() if hasattr(end, "isoformat") else str(end),
                "symbol": symbol,
                "timeframe": timeframe,
            },
            "cache_dukascopy": bounds,
            "cache_synthetic": syn_bounds,
            "coverage": coverage,
            "preparation": prep,
            "download_estimate": prep.get("download_estimate"),
            "latest_download": latest_payload,
            "active_download_id": ctrl.active_job_id(),
        }

    def current_regime(self, db: Session, symbol: str = "EURUSD", timeframe: str = "M15") -> dict[str, Any]:
        rows = list(
            db.scalars(
                select(Candle)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.ts.desc())
                .limit(120)
            )
        )
        rows.reverse()
        if not rows:
            return {
                "label": "desconhecido",
                "label_pt": REGIME_LABELS_PT["desconhecido"],
                "bars_used": 0,
                "message": "Sem velas disponíveis para classificar o regime.",
            }
        snap = classify_regime(
            [c.close for c in rows],
            [c.high for c in rows],
            [c.low for c in rows],
        )
        return snap.as_dict() | {
            "as_of": rows[-1].ts.isoformat(),
            "data_source": rows[-1].source,
            "is_synthetic": rows[-1].is_synthetic,
        }

    def start_experiment(
        self,
        db: Session,
        *,
        start: datetime,
        end: datetime,
        symbol: str = "EURUSD",
        timeframe: str = "M15",
        name: str = "experiência-adaptativa",
        authorize_openai: bool = False,
        require_real_data: bool = True,
        async_run: bool = True,
        max_combos_per_strategy: int = 8,
    ) -> dict[str, Any]:
        # Motor Adaptativo: 100% local — ignorar pedidos OpenAI.
        _ = authorize_openai
        if require_real_data:
            prep = prepare_experiment(
                db,
                start=start,
                end=end,
                symbol=symbol,
                timeframe=timeframe,
                require_real_data=True,
            )
            if not prep["ready"]:
                raise ValueError(prep["message"] + " " + " ".join(prep["reasons"]))

        if async_run:
            from app.adaptive.runner import get_experiment_runner

            exp = get_experiment_runner().enqueue(
                start=start,
                end=end,
                symbol=symbol,
                timeframe=timeframe,
                name=name,
                require_real_data=require_real_data,
                max_combos_per_strategy=max_combos_per_strategy,
            )
            # Re-ler na sessão do pedido HTTP
            row = db.get(AdaptiveExperiment, exp.id)
            return self._experiment_dict(row or exp) | {"async": True, "poll_url": f"/api/adaptive/experiments/{exp.id}"}

        exp = run_experiment(
            db,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            name=name,
            authorize_openai=False,
            require_real_data=require_real_data,
            max_combos_per_strategy=max_combos_per_strategy,
        )
        return self._experiment_dict(exp) | {"async": False}

    def update_schedule(
        self,
        db: Session,
        *,
        enabled: bool,
        interval_minutes: int = 1440,
        lookback_days: int = 30,
        authorize_openai: bool = False,
        symbol: str = "EURUSD",
        timeframe: str = "M15",
    ) -> dict[str, Any]:
        if authorize_openai:
            raise ValueError(
                "O agendamento automático não pode autorizar OpenAI. "
                "Hipóteses com OpenAI exigem autorização explícita por experiência manual."
            )
        row = db.scalar(select(AdaptiveSchedule).order_by(AdaptiveSchedule.id.desc()).limit(1))
        now = datetime.now(timezone.utc)
        if row is None:
            row = AdaptiveSchedule()
            db.add(row)
        row.enabled = enabled
        row.interval_minutes = max(60, interval_minutes)
        row.lookback_days = max(7, lookback_days)
        row.authorize_openai = False
        row.symbol = symbol
        row.timeframe = timeframe
        row.next_run_at = now + timedelta(minutes=row.interval_minutes) if enabled else None
        row.updated_at = now
        row.notes = "Agendamento local sem chamadas OpenAI."
        db.commit()
        db.refresh(row)
        return self._schedule_dict(row)

    def maybe_run_schedule(self, db: Session) -> dict[str, Any] | None:
        row = db.scalar(select(AdaptiveSchedule).where(AdaptiveSchedule.enabled.is_(True)).limit(1))
        if row is None or row.next_run_at is None:
            return None
        now = datetime.now(timezone.utc)
        if row.next_run_at > now:
            return None
        end = now
        start = now - timedelta(days=row.lookback_days)
        try:
            # Agenda: enfileira em segundo plano (não bloqueia o dashboard).
            result = self.start_experiment(
                db,
                start=start,
                end=end,
                symbol=row.symbol,
                timeframe=row.timeframe,
                name="experiência-agendada",
                authorize_openai=False,
                require_real_data=True,
                async_run=True,
            )
        except ValueError as exc:
            row.last_run_at = now
            row.next_run_at = now + timedelta(minutes=row.interval_minutes)
            row.notes = f"Agenda adiada: {exc}"
            db.commit()
            return None
        row.last_run_at = now
        row.next_run_at = now + timedelta(minutes=row.interval_minutes)
        db.commit()
        return result

    def _strategy_dict(self, s: AdaptiveStrategyState) -> dict[str, Any]:
        return {
            "id": s.id,
            "strategy_key": s.strategy_key,
            "display_name": s.display_name,
            "params": json.loads(s.params_json or "{}"),
            "status": s.status,
            "preferred_regimes": json.loads(s.regime_affinity_json or "[]"),
            "complexity_score": s.complexity_score,
            "experiment_count": s.experiment_count,
            "in_sample_score": s.in_sample_score,
            "validation_score": s.validation_score,
            "out_of_sample_score": s.out_of_sample_score,
            "max_drawdown_pct": s.max_drawdown_pct,
            "net_pnl": s.net_pnl,
            "trading_costs": s.trading_costs,
            "reason": s.reason,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }

    def _experiment_dict(self, e: AdaptiveExperiment) -> dict[str, Any]:
        results = {}
        try:
            results = json.loads(e.results_json or "{}")
        except json.JSONDecodeError:
            results = {}
        return {
            "id": e.id,
            "name": e.name,
            "symbol": e.symbol,
            "timeframe": e.timeframe,
            "start_ts": e.start_ts.isoformat() if e.start_ts else None,
            "end_ts": e.end_ts.isoformat() if e.end_ts else None,
            "data_source": e.data_source,
            "is_synthetic": e.is_synthetic,
            "status": e.status,
            "progress_pct": getattr(e, "progress_pct", 0.0) or 0.0,
            "progress_message": getattr(e, "progress_message", "") or "",
            "candidates_total": getattr(e, "candidates_total", 0) or 0,
            "started_at": e.started_at.isoformat() if getattr(e, "started_at", None) else None,
            "openai_authorized": e.openai_authorized,
            "use_openai": e.use_openai,
            "bars_total": e.bars_total,
            "train_bars": e.train_bars,
            "validation_bars": e.validation_bars,
            "test_bars": e.test_bars,
            "candidates_tested": e.candidates_tested,
            "selected_strategy_id": e.selected_strategy_id,
            "selected_action": e.selected_action,
            "current_regime": e.current_regime,
            "current_regime_pt": REGIME_LABELS_PT.get(e.current_regime, e.current_regime),
            "results": results,
            "notes": e.notes,
            "error": e.error,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
        }

    def _schedule_dict(self, s: AdaptiveSchedule) -> dict[str, Any]:
        return {
            "id": s.id,
            "enabled": s.enabled,
            "interval_minutes": s.interval_minutes,
            "lookback_days": s.lookback_days,
            "authorize_openai": False,
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "notes": s.notes,
        }


_service: AdaptiveEngineService | None = None
_lock = threading.Lock()


def get_adaptive_service() -> AdaptiveEngineService:
    global _service
    with _lock:
        if _service is None:
            _service = AdaptiveEngineService()
        return _service
