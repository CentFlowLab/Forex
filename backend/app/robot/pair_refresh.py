"""Atualização bi5 multimeda — histórico suficiente, estados claros, sem OpenAI."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from app.database import SessionLocal
from app.market.cache import persist_candles
from app.market.dukascopy import DukascopyError, download_symbol_range
from app.market.dukascopy_bi5_poll import latest_tick_from_hours
from app.market.labels import DUKASCOPY_SOURCE
from app.robot.pair_quotes import get_pair_quote_store
from app.robot.universe import SYMBOLS

logger = logging.getLogger(__name__)

PairState = Literal["disponivel", "a_atualizar", "atrasado", "indisponivel"]

# Histórico inicial: ~5 dias de calendário → cobre H4×16 após fins de semana.
_BOOTSTRAP_DAYS = 5
_MIN_M15 = 20
_MIN_H1 = 10
_MIN_H4 = 5
# bi5 horário quase sempre >10 min — marcar como atrasado acima deste limiar.
_DELAYED_LAG = 600.0


@dataclass
class PairRefreshStatus:
    symbol: str
    state: PairState = "indisponivel"
    message: str = ""
    lag_seconds: float | None = None
    mid: float | None = None
    candles: dict[str, int] = field(default_factory=lambda: {"M15": 0, "H1": 0, "H4": 0})
    last_error: str = ""
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state,
            "state_label": {
                "disponivel": "Disponível",
                "a_atualizar": "A atualizar",
                "atrasado": "Atrasado",
                "indisponivel": "Indisponível",
            }.get(self.state, self.state),
            "message": self.message,
            "lag_seconds": self.lag_seconds,
            "mid": self.mid,
            "candles": dict(self.candles),
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


class PairRefreshTracker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._status: dict[str, PairRefreshStatus] = {
            s: PairRefreshStatus(symbol=s) for s in SYMBOLS
        }
        self._running = False

    def get(self, symbol: str) -> PairRefreshStatus:
        with self._lock:
            key = symbol.replace("/", "").upper()
            if key not in self._status:
                self._status[key] = PairRefreshStatus(symbol=key)
            return self._status[key]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._status[s].as_dict() for s in SYMBOLS]

    def set_state(self, symbol: str, state: PairState, **kwargs: Any) -> None:
        key = symbol.replace("/", "").upper()
        with self._lock:
            st = self._status[key]
            st.state = state
            for k, v in kwargs.items():
                if hasattr(st, k):
                    setattr(st, k, v)
            st.updated_at = datetime.now(timezone.utc).isoformat()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def mark_running(self, value: bool) -> None:
        with self._lock:
            self._running = value


_tracker = PairRefreshTracker()
_bg_lock = threading.Lock()


def get_pair_refresh_tracker() -> PairRefreshTracker:
    return _tracker


def _count_candles(db, symbol: str) -> dict[str, int]:
    from sqlalchemy import func, select

    from app.models.market import Candle

    out: dict[str, int] = {}
    for tf in ("M15", "H1", "H4"):
        out[tf] = int(
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol,
                    Candle.timeframe == tf,
                    Candle.source == DUKASCOPY_SOURCE,
                    Candle.is_synthetic == False,  # noqa: E712
                )
            )
            or 0
        )
    return out


def _has_enough(counts: dict[str, int]) -> bool:
    return (
        counts.get("M15", 0) >= _MIN_M15
        and counts.get("H1", 0) >= _MIN_H1
        and counts.get("H4", 0) >= _MIN_H4
    )


def bootstrap_symbol_history(symbol: str, *, days: int = _BOOTSTRAP_DAYS) -> dict[str, Any]:
    """Descarrega histórico bi5 real e persiste M15/H1/H4. Sem sintéticos."""
    key = symbol.replace("/", "").upper()
    _tracker.set_state(key, "a_atualizar", message="A descarregar histórico Dukascopy bi5…", last_error="")
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    try:
        result = download_symbol_range(
            start,
            end,
            symbol=key,
            timeframes=["M15", "H1", "H4"],
        )
    except DukascopyError as exc:
        _tracker.set_state(
            key,
            "indisponivel",
            message="Falha no descarregamento bi5.",
            last_error=str(exc),
        )
        return {"symbol": key, "ok": False, "error": str(exc)}

    db = SessionLocal()
    persisted: dict[str, Any] = {}
    try:
        for tf, rows in (result.get("candles") or {}).items():
            if not rows:
                persisted[tf] = {"inserted": 0, "updated": 0}
                continue
            persisted[tf] = persist_candles(db, rows, tf)
        counts = _count_candles(db, key)
    finally:
        db.close()

    # Cotação mais recente da hora bi5 (não inventada).
    lag = None
    mid = None
    try:
        headers = {"User-Agent": "GROK-FOREX-AI/0.2 (local; multi-pair bi5)"}
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
            tick, _ticks, meta = latest_tick_from_hours(client, lookback_hours=8, symbol=key)
        if tick is not None:
            get_pair_quote_store().set(key, tick, is_synthetic=False, source="dukascopy_bi5")
            mid = tick.mid
            lag = float(meta.get("lag_seconds") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Quote bi5 %s: %s", key, exc)

    if not _has_enough(counts):
        _tracker.set_state(
            key,
            "indisponivel",
            message=f"Velas insuficientes após download: {counts}",
            candles=counts,
            mid=mid,
            lag_seconds=lag,
            last_error=f"hour_stats={result.get('hour_stats')}",
        )
        return {"symbol": key, "ok": False, "counts": counts, "persisted": persisted}

    state: PairState = "atrasado" if (lag is not None and lag > _DELAYED_LAG) else "disponivel"
    if lag is None:
        state = "atrasado"
    _tracker.set_state(
        key,
        state,
        message=(
            "Dados históricos reais carregados (bi5 — não é tempo real)."
            if state != "disponivel"
            else "Dados bi5 disponíveis (atraso baixo relativo)."
        ),
        candles=counts,
        mid=mid,
        lag_seconds=lag,
        last_error="",
    )
    return {
        "symbol": key,
        "ok": True,
        "state": state,
        "counts": counts,
        "tick_count": result.get("tick_count"),
        "hour_stats": result.get("hour_stats"),
        "persisted": {k: {"inserted": v.get("inserted"), "updated": v.get("updated")} for k, v in persisted.items()},
        "lag_seconds": lag,
        "mid": mid,
    }


def refresh_majors_background(*, force_bootstrap: bool = True) -> None:
    """Atualiza os 7 pares em segundo plano — não bloqueia o painel."""
    if not _bg_lock.acquire(blocking=False):
        return
    if _tracker.running:
        _bg_lock.release()
        return
    _tracker.mark_running(True)

    def _run() -> None:
        try:
            for sym in SYMBOLS:
                try:
                    db = SessionLocal()
                    try:
                        counts = _count_candles(db, sym)
                    finally:
                        db.close()
                    if force_bootstrap or not _has_enough(counts):
                        info = bootstrap_symbol_history(sym, days=_BOOTSTRAP_DAYS)
                        logger.info("Bootstrap %s: %s", sym, {k: info.get(k) for k in ("ok", "state", "counts", "error")})
                    else:
                        # Só atualizar cotação recente
                        _tracker.set_state(sym, "a_atualizar", message="A atualizar cotação bi5…", candles=counts)
                        headers = {"User-Agent": "GROK-FOREX-AI/0.2 (local; multi-pair bi5)"}
                        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                            tick, hour_ticks, meta = latest_tick_from_hours(
                                client, lookback_hours=6, symbol=sym
                            )
                        if tick is None:
                            _tracker.set_state(
                                sym,
                                "atrasado" if _has_enough(counts) else "indisponivel",
                                message="Sem tick bi5 recente; a usar velas existentes.",
                                candles=counts,
                                last_error="sem_ticks",
                            )
                            continue
                        get_pair_quote_store().set(sym, tick, is_synthetic=False, source="dukascopy_bi5")
                        lag = float(meta.get("lag_seconds") or 0)
                        state: PairState = "atrasado" if lag > _DELAYED_LAG else "disponivel"
                        _tracker.set_state(
                            sym,
                            state,
                            message="Cotação bi5 atualizada.",
                            candles=counts,
                            mid=tick.mid,
                            lag_seconds=lag,
                        )
                        if hour_ticks:
                            from app.market.dukascopy import aggregate_ticks_to_candles

                            db = SessionLocal()
                            try:
                                for tf in ("M15", "H1", "H4"):
                                    candles = aggregate_ticks_to_candles(hour_ticks, tf, symbol=sym)
                                    for c in candles:
                                        c["source"] = DUKASCOPY_SOURCE
                                        c["is_synthetic"] = False
                                        c["symbol"] = sym
                                    if candles:
                                        persist_candles(db, candles, tf)
                                counts = _count_candles(db, sym)
                                _tracker.set_state(sym, state, candles=counts)
                            finally:
                                db.close()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Refresh %s falhou", sym)
                    _tracker.set_state(sym, "indisponivel", last_error=str(exc), message="Erro na atualização.")
        finally:
            _tracker.mark_running(False)
            _bg_lock.release()

    for sym in SYMBOLS:
        _tracker.set_state(sym, "a_atualizar", message="Na fila de atualização…")
    threading.Thread(target=_run, name="radar-bi5-refresh", daemon=True).start()


# Compatibilidade com chamadas antigas
def refresh_symbol_bi5(symbol: str, *, lookback_hours: int = 4) -> dict[str, Any]:
    return bootstrap_symbol_history(symbol, days=max(2, lookback_hours // 24 + 2))
