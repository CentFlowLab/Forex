"""Execução em segundo plano de experiências do Motor Adaptativo."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.adaptive.experiments import run_experiment
from app.database import SessionLocal
from app.models.adaptive import AdaptiveExperiment

logger = logging.getLogger(__name__)


class AdaptiveExperimentRunner:
    """Uma experiência em curso de cada vez (CPU intensiva, local, sem IA)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_id: int | None = None

    def active_id(self) -> int | None:
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
                self._active_id = None
            return self._active_id

    def enqueue(
        self,
        *,
        start: datetime,
        end: datetime,
        symbol: str = "EURUSD",
        timeframe: str = "M15",
        name: str = "experiência-adaptativa",
        require_real_data: bool = True,
        max_combos_per_strategy: int = 8,
    ) -> AdaptiveExperiment:
        db = SessionLocal()
        try:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    raise ValueError(
                        f"Já existe uma experiência em curso (#{self._active_id}). "
                        "Aguarde a conclusão antes de iniciar outra."
                    )
                # Órfãs running/queued sem thread → failed recuperável
                orphans = list(
                    db.scalars(
                        select(AdaptiveExperiment).where(
                            AdaptiveExperiment.status.in_(("running", "queued"))
                        )
                    )
                )
                for row in orphans:
                    row.status = "failed"
                    row.error = "Interrompida por reinício do servidor — volte a executar."
                    row.progress_message = "Interrompida"
                    row.completed_at = datetime.now(timezone.utc)
                if orphans:
                    db.commit()

                exp = AdaptiveExperiment(
                    name=name,
                    symbol=symbol,
                    timeframe=timeframe,
                    start_ts=start,
                    end_ts=end,
                    status="queued",
                    use_openai=False,
                    openai_authorized=False,
                    progress_pct=0.0,
                    progress_message="Em fila — a iniciar simulação local…",
                    notes="Experiência local assíncrona sem chamadas OpenAI.",
                )
                db.add(exp)
                db.commit()
                db.refresh(exp)
                exp_id = exp.id
                self._active_id = exp_id
                self._thread = threading.Thread(
                    target=self._run,
                    args=(exp_id, require_real_data, max_combos_per_strategy),
                    name=f"adaptive-exp-{exp_id}",
                    daemon=True,
                )
                self._thread.start()
                return exp
        finally:
            db.close()

    def _run(self, experiment_id: int, require_real_data: bool, max_combos: int) -> None:
        db = SessionLocal()
        try:
            exp = db.get(AdaptiveExperiment, experiment_id)
            if exp is None:
                return
            exp.status = "running"
            exp.started_at = datetime.now(timezone.utc)
            exp.progress_message = "A carregar velas e validar cobertura…"
            db.commit()

            def on_progress(payload: dict[str, Any]) -> None:
                row = db.get(AdaptiveExperiment, experiment_id)
                if row is None:
                    return
                row.candidates_tested = int(payload.get("tested") or 0)
                row.candidates_total = int(payload.get("total") or 0)
                row.progress_pct = float(payload.get("progress_pct") or 0)
                row.progress_message = str(payload.get("message") or "")
                if payload.get("bars_total") is not None:
                    row.bars_total = int(payload["bars_total"])
                db.commit()

            run_experiment(
                db,
                symbol=exp.symbol,
                timeframe=exp.timeframe,
                start=exp.start_ts if exp.start_ts.tzinfo else exp.start_ts.replace(tzinfo=timezone.utc),
                end=exp.end_ts if exp.end_ts.tzinfo else exp.end_ts.replace(tzinfo=timezone.utc),
                name=exp.name,
                authorize_openai=False,
                max_combos_per_strategy=max_combos,
                require_real_data=require_real_data,
                experiment_id=experiment_id,
                progress_cb=on_progress,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Experiência #%s falhou", experiment_id)
            try:
                row = db.get(AdaptiveExperiment, experiment_id)
                if row and row.status not in {"completed"}:
                    row.status = "failed"
                    row.error = str(exc)
                    row.progress_message = f"Falhou: {exc}"
                    row.completed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()
            with self._lock:
                if self._active_id == experiment_id:
                    self._active_id = None
                self._thread = None


_runner: AdaptiveExperimentRunner | None = None
_runner_lock = threading.Lock()


def get_experiment_runner() -> AdaptiveExperimentRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = AdaptiveExperimentRunner()
        return _runner
