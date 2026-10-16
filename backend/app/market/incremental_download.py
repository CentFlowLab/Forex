"""Descarregamento incremental Dukascopy com progresso, cancelamento e retoma.

Uma tarefa em segundo plano de cada vez. Sem chamadas a fornecedores de IA.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DATA_DIR, get_settings
from app.database import SessionLocal
from app.i18n_pt import MSG
from app.market.cache import persist_candles, write_parquet, parquet_path
from app.market.dukascopy import aggregate_ticks_to_candles, fetch_hour_ticks
from app.market.history_inventory import (
    analyze_coverage,
    estimate_download,
    hours_already_satisfied,
    is_forex_weekend_gap,
)
from app.market.labels import DUKASCOPY_SOURCE, REAL_HISTORICAL_LABEL
from app.models.audit import AuditLog
from app.models.replay import DownloadJob, ValidatedHistoryHour

logger = logging.getLogger(__name__)

BI5_CACHE_DIR = DATA_DIR / "historical" / "bi5_cache"

FetchHourFn = Callable[[datetime, httpx.Client, int], tuple[list[tuple[datetime, float, float]], str]]


class IncrementalDownloadController:
    """Controlador singleton — no máximo um descarregamento ativo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_job_id: int | None = None
        self._fetch_hour: FetchHourFn = fetch_hour_ticks

    def set_fetch_hour_for_tests(self, fn: FetchHourFn | None) -> None:
        self._fetch_hour = fn or fetch_hour_ticks

    def active_job_id(self) -> int | None:
        self._reap_stale_thread()
        return self._active_job_id

    def _reap_stale_thread(self) -> None:
        """Limpa referência a threads mortas (ex.: após cancelamento ou reinício parcial)."""
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
                self._active_job_id = None

    def latest_job(self, db: Session) -> DownloadJob | None:
        return db.scalar(select(DownloadJob).order_by(DownloadJob.id.desc()).limit(1))

    def reconcile_active(self, db: Session) -> DownloadJob | None:
        """Alinha memória ↔ BD: liberta cadeados fantasmas e marca tarefas órfãs."""
        self._reap_stale_thread()
        with self._lock:
            aid = self._active_job_id
            alive = self._thread is not None and self._thread.is_alive()

        # Tarefas "running" sem thread (reinício do processo) → interrupted (retomáveis).
        orphans = list(
            db.scalars(
                select(DownloadJob).where(DownloadJob.status.in_(("running", "queued")))
            )
        )
        for row in orphans:
            if alive and aid == row.id:
                continue
            row.status = "interrupted"
            row.message = (
                (row.message or "")
                + " [Interrompido — reinicie com «Retomar descarregamento».]"
            )
            row.updated_at = datetime.now(timezone.utc)
            logger.info("Tarefa de descarregamento #%s marcada como interrupted (órfã).", row.id)
        if orphans:
            db.commit()

        if aid is None:
            return self.latest_job(db)
        row = db.get(DownloadJob, aid)
        if row is None:
            with self._lock:
                self._thread = None
                self._active_job_id = None
            return self.latest_job(db)
        if row.status in {"completed", "cancelled", "failed", "interrupted"} and not alive:
            with self._lock:
                self._thread = None
                self._active_job_id = None
        elif row.status in {"completed", "cancelled", "failed", "interrupted"} and alive:
            logger.warning(
                "Thread de descarregamento #%s ainda viva com estado BD=%s — a libertar cadeado.",
                aid,
                row.status,
            )
            with self._lock:
                self._thread = None
                self._active_job_id = None
        return row

    def start_missing(
        self,
        db: Session,
        *,
        start: datetime,
        end: datetime,
        symbol: str = "EURUSD",
        timeframe: str = "M15",
        confirmed: bool = False,
        resume_job_id: int | None = None,
    ) -> DownloadJob:
        if not confirmed:
            raise ValueError(
                "Confirmação obrigatória: o descarregamento de períodos longos "
                "pode demorar e gerar muito tráfego ao datafeed Dukascopy."
            )
        settings = get_settings()
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start:
            raise ValueError("A data de fim tem de ser posterior ao início.")
        max_days = settings.dukascopy_adaptive_max_days
        if (end - start) > timedelta(days=max_days):
            raise ValueError(
                f"O intervalo excede dukascopy_adaptive_max_days={max_days}. Reduza o período."
            )
        if symbol.replace("/", "").upper() != "EURUSD":
            raise ValueError("Apenas EURUSD é suportado nesta fase.")

        self.reconcile_active(db)

        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError(
                    f"Já existe um descarregamento em curso (tarefa #{self._active_job_id}). "
                    "Use «Cancelar descarregamento» ou «Retomar» após cancelar."
                )

            job: DownloadJob | None = None
            if resume_job_id:
                job = db.get(DownloadJob, resume_job_id)
                if job is None:
                    raise ValueError("Tarefa de retoma não encontrada.")
                if job.status not in {"cancelled", "failed", "interrupted"}:
                    raise ValueError("Só é possível retomar tarefas canceladas, falhadas ou interrompidas.")
                start = job.start_ts if job.start_ts.tzinfo else job.start_ts.replace(tzinfo=timezone.utc)
                end = job.end_ts if job.end_ts.tzinfo else job.end_ts.replace(tzinfo=timezone.utc)

            coverage = analyze_coverage(
                db, start=start, end=end, symbol=symbol, timeframe=timeframe, source=DUKASCOPY_SOURCE
            )
            present_hours: set[datetime] = set()
            # Horas já satisfeitas no cache de velas.
            hour_cursor = start.replace(minute=0, second=0, microsecond=0)
            while hour_cursor < end:
                if hours_already_satisfied(db, hour_cursor, symbol=symbol, timeframe=timeframe):
                    present_hours.add(hour_cursor)
                hour_cursor += timedelta(hours=1)

            # Horas validadas (ok/empty) no registo local.
            validated = list(
                db.scalars(
                    select(ValidatedHistoryHour).where(
                        ValidatedHistoryHour.symbol == symbol,
                        ValidatedHistoryHour.source == DUKASCOPY_SOURCE,
                        ValidatedHistoryHour.hour_ts >= start,
                        ValidatedHistoryHour.hour_ts < end,
                        ValidatedHistoryHour.status.in_(("ok", "empty")),
                    )
                )
            )
            for v in validated:
                ht = v.hour_ts if v.hour_ts.tzinfo else v.hour_ts.replace(tzinfo=timezone.utc)
                present_hours.add(ht.replace(minute=0, second=0, microsecond=0))

            hours_to_fetch: list[datetime] = []
            hour_cursor = start.replace(minute=0, second=0, microsecond=0)
            while hour_cursor < end:
                # Fins de semana: marcar como empty sem pedir ao datafeed (poupa pedidos).
                if is_forex_weekend_gap(hour_cursor):
                    self._ensure_validated(db, hour_cursor, status="empty", tick_count=0, symbol=symbol)
                    present_hours.add(hour_cursor)
                elif hour_cursor not in present_hours:
                    hours_to_fetch.append(hour_cursor)
                hour_cursor += timedelta(hours=1)

            est = estimate_download(missing_hours=len(hours_to_fetch))

            if job is None:
                job = DownloadJob(
                    symbol="EURUSD",
                    source=DUKASCOPY_SOURCE,
                    start_ts=start,
                    end_ts=end,
                    status="queued",
                    message="Descarregamento incremental em fila.",
                    timeframe=timeframe,
                    cancel_requested=False,
                    hours_total=len(hours_to_fetch),
                    hours_done=0,
                    resume_hour=hours_to_fetch[0] if hours_to_fetch else None,
                )
                db.add(job)
            else:
                job.cancel_requested = False
                job.error = ""
                job.hours_total = len(hours_to_fetch)
                job.hours_done = 0
                job.resume_hour = hours_to_fetch[0] if hours_to_fetch else None
                job.timeframe = timeframe

            job.details_json = json.dumps(
                {
                    "mode": "incremental_missing",
                    "timeframe": timeframe,
                    "coverage_before": {
                        "coverage_pct": coverage["coverage_pct"],
                        "bars_valid_real": coverage["bars_valid_real"],
                        "missing_hours_count": coverage["missing_hours_count"],
                    },
                    "estimate": est,
                    "hours_planned": len(hours_to_fetch),
                    "ai_calls": False,
                },
                default=str,
            )
            if not hours_to_fetch:
                job.status = "completed"
                job.progress_pct = 100.0
                job.message = (
                    "Nada a descarregar — o período já está coberto por velas Dukascopy validadas "
                    "ou horas marcadas como vazias (fins de semana / sem ticks)."
                )
                job.updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(job)
                return job

            job.status = "running"
            job.message = (
                f"A descarregar {len(hours_to_fetch)} horas em falta · "
                f"estimativa {est['estimated_duration_label']}"
            )
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)

            job_id = job.id
            planned = [h.isoformat() for h in hours_to_fetch]
            self._active_job_id = job_id
            self._thread = threading.Thread(
                target=self._run_job,
                args=(job_id, planned, timeframe),
                name=f"dukascopy-incremental-{job_id}",
                daemon=True,
            )
            self._thread.start()
            return job

    def cancel(self, db: Session, job_id: int) -> DownloadJob:
        job = db.get(DownloadJob, job_id)
        if job is None:
            raise ValueError("Tarefa de descarregamento não encontrada.")
        job.cancel_requested = True
        job.message = "Cancelamento pedido — a parar após a hora atual…"
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
        return job

    def job_payload(self, job: DownloadJob) -> dict[str, Any]:
        details = {}
        try:
            details = json.loads(job.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        return {
            "id": job.id,
            "status": job.status,
            "progress_pct": job.progress_pct,
            "message": job.message,
            "error": job.error,
            "symbol": job.symbol,
            "source": job.source,
            "timeframe": getattr(job, "timeframe", None) or "M15",
            "start": job.start_ts.isoformat() if job.start_ts else None,
            "end": job.end_ts.isoformat() if job.end_ts else None,
            "hours_done": getattr(job, "hours_done", 0) or 0,
            "hours_total": getattr(job, "hours_total", 0) or 0,
            "resume_hour": job.resume_hour.isoformat() if getattr(job, "resume_hour", None) else None,
            "cancel_requested": bool(getattr(job, "cancel_requested", False)),
            "bars": {"M5": job.bars_m5, "M15": job.bars_m15, "H1": job.bars_h1, "H4": job.bars_h4},
            "details": details,
            "label": REAL_HISTORICAL_LABEL,
            "is_synthetic": False,
            "ai_calls": False,
        }

    def _ensure_validated(
        self,
        db: Session,
        hour: datetime,
        *,
        status: str,
        tick_count: int,
        symbol: str,
    ) -> None:
        hour = hour.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        row = db.scalar(
            select(ValidatedHistoryHour).where(
                ValidatedHistoryHour.symbol == symbol,
                ValidatedHistoryHour.source == DUKASCOPY_SOURCE,
                ValidatedHistoryHour.hour_ts == hour,
            )
        )
        if row is None:
            row = ValidatedHistoryHour(
                symbol=symbol,
                source=DUKASCOPY_SOURCE,
                hour_ts=hour,
                status=status,
                tick_count=tick_count,
            )
            db.add(row)
        else:
            row.status = status
            row.tick_count = tick_count
            row.validated_at = datetime.now(timezone.utc)

    def _bi5_cache_path(self, hour: datetime) -> Path:
        hour = hour.astimezone(timezone.utc)
        return (
            BI5_CACHE_DIR
            / f"{hour.year:04d}"
            / f"{hour.month:02d}"
            / f"{hour.day:02d}"
            / f"{hour.hour:02d}.ok"
        )

    def _run_job(self, job_id: int, planned_iso: list[str], timeframe: str) -> None:
        settings = get_settings()
        delay = max(0.05, float(settings.dukascopy_hour_delay_seconds))
        retries = max(1, int(settings.dukascopy_hour_retries))
        tfs = ["M5", "M15", "H1", "H4"]
        if timeframe not in tfs:
            tfs = [timeframe, "M15"]

        db = SessionLocal()
        try:
            job = db.get(DownloadJob, job_id)
            if job is None:
                return
            hours = [datetime.fromisoformat(h.replace("Z", "+00:00")) for h in planned_iso]
            all_ticks: list[tuple[datetime, float, float]] = []
            chunk_ticks: list[tuple[datetime, float, float]] = []
            hour_stats = {"ok": 0, "empty": 0, "unavailable": 0, "error": 0, "skipped_cache": 0}
            errors: list[str] = []
            headers = {"User-Agent": "GROK-FOREX-AI/0.1 (local research; historical bi5 incremental)"}

            with httpx.Client(timeout=45.0, headers=headers, follow_redirects=True) as client:
                for idx, hour in enumerate(hours):
                    # Libertação explícita antes de I/O de rede — evita «database is locked»
                    # no dashboard / WebSocket durante pedidos HTTP longos.
                    try:
                        db.commit()
                    except Exception:  # noqa: BLE001
                        db.rollback()
                    db.expire_all()
                    job = db.get(DownloadJob, job_id)
                    if job is None:
                        return
                    if job.cancel_requested:
                        job.status = "cancelled"
                        job.message = (
                            f"Cancelado após {idx}/{len(hours)} horas. "
                            "Pode retomar sem repetir horas já validadas."
                        )
                        job.resume_hour = hour
                        job.hours_done = idx
                        job.progress_pct = round(100.0 * idx / max(1, len(hours)), 2)
                        job.updated_at = datetime.now(timezone.utc)
                        self._flush_ticks(db, job, chunk_ticks, tfs)
                        db.commit()
                        return

                    already = hours_already_satisfied(db, hour, timeframe=timeframe)
                    db.commit()  # fechar leitura antes do HTTP

                    ticks: list[tuple[datetime, float, float]] = []
                    status = "empty"
                    if already:
                        hour_stats["skipped_cache"] += 1
                        self._ensure_validated(db, hour, status="ok", tick_count=0, symbol=job.symbol)
                    else:
                        cache_mark = self._bi5_cache_path(hour)
                        ticks, status = self._fetch_hour(hour, client, retries)
                        job = db.get(DownloadJob, job_id)
                        if job is None:
                            return
                        if status == "ok":
                            hour_stats["ok"] += 1
                            chunk_ticks.extend(ticks)
                            all_ticks.extend(ticks)
                            self._ensure_validated(
                                db, hour, status="ok", tick_count=len(ticks), symbol=job.symbol
                            )
                            cache_mark.parent.mkdir(parents=True, exist_ok=True)
                            cache_mark.write_text(f"ticks={len(ticks)}\n", encoding="utf-8")
                        elif status == "empty":
                            hour_stats["empty"] += 1
                            self._ensure_validated(
                                db, hour, status="empty", tick_count=0, symbol=job.symbol
                            )
                        else:
                            hour_stats["unavailable" if "unavailable" in status else "error"] += 1
                            errors.append(f"{hour.isoformat()}: {status}")

                    job.hours_done = idx + 1
                    job.resume_hour = hour + timedelta(hours=1)
                    job.progress_pct = round(100.0 * (idx + 1) / max(1, len(hours)), 2)
                    job.message = (
                        f"Hora {idx + 1}/{len(hours)} · ticks_acumulados={len(all_ticks)} · "
                        f"ok={hour_stats['ok']} vazias={hour_stats['empty']}"
                    )
                    job.details_json = json.dumps(
                        {
                            "mode": "incremental_missing",
                            "hour_stats": hour_stats,
                            "errors": errors[-30:],
                            "ticks_so_far": len(all_ticks),
                            "ai_calls": False,
                        },
                        default=str,
                    )
                    job.updated_at = datetime.now(timezone.utc)

                    # Chunks mais pequenos = escritas mais curtas (menos bloqueio do dashboard).
                    if len(chunk_ticks) >= 2500 or (idx + 1) % 6 == 0:
                        self._flush_ticks(db, job, chunk_ticks, tfs)
                        chunk_ticks = []
                    else:
                        db.commit()

                    time.sleep(delay)

            self._flush_ticks(db, job, chunk_ticks, tfs)
            job = db.get(DownloadJob, job_id)
            assert job is not None
            if hour_stats["ok"] == 0 and hour_stats["skipped_cache"] == 0 and not all_ticks:
                job.status = "failed"
                job.error = (
                    "Nenhuma hora Dukascopy com ticks foi obtida. "
                    "Dados sintéticos NÃO foram usados. Pode retomar mais tarde."
                )
                job.message = MSG.get("download_failed", "Descarregamento falhou.")
            else:
                job.status = "completed"
                job.progress_pct = 100.0
                job.message = (
                    f"Concluído · horas ok={hour_stats['ok']} · vazias={hour_stats['empty']} · "
                    f"cache={hour_stats['skipped_cache']} · ticks={len(all_ticks)} · "
                    f"M15={job.bars_m15}"
                )
            job.updated_at = datetime.now(timezone.utc)
            db.add(
                AuditLog(
                    category="market",
                    level="INFO" if job.status == "completed" else "ERROR",
                    message=job.message,
                    details=(job.details_json or "")[:2000],
                )
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 — registar e marcar tarefa
            logger.exception("Falha no descarregamento incremental #%s", job_id)
            try:
                job = db.get(DownloadJob, job_id)
                if job:
                    job.status = "failed"
                    job.error = str(exc)
                    job.message = "Descarregamento interrompido por erro — pode retomar."
                    job.updated_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None
                self._thread = None

    def _flush_ticks(
        self,
        db: Session,
        job: DownloadJob,
        ticks: list[tuple[datetime, float, float]],
        tfs: list[str],
    ) -> None:
        if not ticks:
            db.commit()
            return
        start = min(t[0] for t in ticks)
        end = max(t[0] for t in ticks) + timedelta(seconds=1)
        for tf in tfs:
            rows = aggregate_ticks_to_candles(ticks, tf)
            if not rows:
                continue
            try:
                path = parquet_path("EURUSD", tf, start, end)
                write_parquet(rows, path)
            except Exception:  # noqa: BLE001
                pass
            summary = persist_candles(db, rows, tf)
            bars = summary.get("validation", {}).get("bars", 0) or len(rows)
            if tf == "M5":
                job.bars_m5 += int(bars)
            elif tf == "M15":
                job.bars_m15 += int(bars)
            elif tf == "H1":
                job.bars_h1 += int(bars)
            elif tf == "H4":
                job.bars_h4 += int(bars)
        db.commit()


_controller: IncrementalDownloadController | None = None
_controller_lock = threading.Lock()


def get_incremental_download_controller() -> IncrementalDownloadController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = IncrementalDownloadController()
        return _controller
