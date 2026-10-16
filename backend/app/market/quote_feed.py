"""Serviço singleton de atualização automática de cotações EUR/USD (Dukascopy).

- Uma ligação/poller por processo (não por utilizador).
- TCP anónimo (porto 9999) quando acessível; fallback bi5 histórico horário.
- Valida ticks; agrega M5/M15/H1/H4; cache local; deteta obsoletos/desligações.
- NÃO inicia análises OpenAI.
- NÃO substitui preços em falta por dados sintéticos.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.database import SessionLocal
from app.market.cache import persist_candles
from app.market.dukascopy import aggregate_ticks_to_candles
from app.market.dukascopy_bi5_poll import latest_tick_from_hours
from app.market.dukascopy_tcp import DukascopyTcpClient
from app.market.labels import DUKASCOPY_SOURCE, REAL_HISTORICAL_LABEL
from app.market.quote_types import (
    STATUS_LABELS_PT,
    FeedChannel,
    QuoteFeedSnapshot,
    QuoteStatus,
    ValidatedTick,
)
from app.market.tick_validation import classify_status, is_duplicate_tick

logger = logging.getLogger(__name__)

_SOURCE_LABELS = {
    "dukascopy_tcp": "Dukascopy TCP (datafeed:9999) — preço único, atraso do protocolo ~10 s",
    "dukascopy_bi5": "Dukascopy bi5 (histórico horário público) — não é feed em tempo real",
    "none": "Nenhuma fonte Dukascopy ativa",
}


class QuoteFeedService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._tcp: DukascopyTcpClient | None = None
        self._started = False

        self._last_tick: ValidatedTick | None = None
        self._prev_tick: ValidatedTick | None = None
        self._received_at: datetime | None = None
        self._tick_count = 0
        self._duplicate_count = 0
        self._channel: FeedChannel = "none"
        self._tcp_connected = False
        self._tcp_reconnects = 0
        self._last_error = ""
        self._candles_updated: dict[str, int] = {}
        self._last_bi5_persist_hour: str | None = None
        self._bi5_ticks_buffer: list[tuple[datetime, float, float]] = []
        self._observed_intervals: list[float] = []

    def start(self) -> None:
        settings = get_settings()
        if not settings.dukascopy_quote_feed_enabled:
            logger.info("Quote feed Dukascopy desativado por configuração.")
            return
        with self._lock:
            if self._started and self._poll_thread is not None and self._poll_thread.is_alive():
                return
            # Reinício limpo se a thread morreu após restart parcial.
            self._started = True
            self._stop.clear()
        if settings.dukascopy_tcp_enabled and self._tcp is None:
            self._tcp = DukascopyTcpClient(
                host=settings.dukascopy_tcp_host,
                port=settings.dukascopy_tcp_port,
                on_tick=self._on_tcp_tick,
                on_status=self._on_tcp_status,
            )
            self._tcp.start()
        # Sondagem bi5 síncrona antes de aceitar pedidos — evita feed «desligado» após restart.
        try:
            self._poll_bi5_once()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"bi5_start:{type(exc).__name__}: {exc}"
            logger.warning("Primeira sondagem bi5 no arranque falhou: %s", self._last_error)
        with self._lock:
            thread_alive = self._poll_thread is not None and self._poll_thread.is_alive()
        if not thread_alive:
            self._poll_thread = threading.Thread(
                target=self._bi5_poll_loop, name="dukascopy-bi5-poll", daemon=True
            )
            self._poll_thread.start()
        logger.info("Quote feed Dukascopy iniciado (TCP=%s).", settings.dukascopy_tcp_enabled)

    def stop(self) -> None:
        self._stop.set()
        if self._tcp:
            self._tcp.stop()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3.0)
        with self._lock:
            self._started = False

    def recover(self) -> dict[str, Any]:
        """Tenta reativar o feed existente (TCP + bi5) sem contas nem APIs pagas."""
        settings = get_settings()
        with self._lock:
            thread_alive = self._poll_thread is not None and self._poll_thread.is_alive()
            if not thread_alive:
                self._started = False
        self.start()
        tcp_probe: dict[str, Any] = {"ok": False, "skipped": True}
        if settings.dukascopy_tcp_enabled:
            from app.market.dukascopy_tcp import probe_tcp

            tcp_probe = probe_tcp(
                host=settings.dukascopy_tcp_host,
                port=settings.dukascopy_tcp_port,
                timeout=1.5,
            )
            # Reiniciar cliente TCP se a sessão atual estiver morta.
            with self._lock:
                tcp_dead = self._tcp is None or not self._tcp_connected
            if tcp_dead or not tcp_probe.get("ok"):
                if self._tcp is not None:
                    try:
                        self._tcp.stop()
                    except Exception:  # noqa: BLE001
                        pass
                self._tcp = DukascopyTcpClient(
                    host=settings.dukascopy_tcp_host,
                    port=settings.dukascopy_tcp_port,
                    connect_timeout=3.0,
                    read_timeout=8.0,
                    on_tick=self._on_tcp_tick,
                    on_status=self._on_tcp_status,
                )
                self._tcp.start()
                with self._lock:
                    self._last_error = (
                        ""
                        if tcp_probe.get("ok")
                        else f"TCP indisponível ({tcp_probe.get('error', 'falha')}); a usar bi5."
                    )

        # Sondagem bi5 síncrona na recuperação (o robô precisa de cotação após restart).
        bi5_error = ""
        try:
            self._poll_bi5_once()
        except Exception as exc:  # noqa: BLE001
            bi5_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._last_error = f"bi5_recover:{bi5_error}"
            logger.warning("Recuperação bi5 síncrona falhou: %s", bi5_error)
            # Retry em background se a sondagem síncrona falhar.
            threading.Thread(target=self._safe_poll_bi5, name="dukascopy-bi5-recover", daemon=True).start()

        snap = self.snapshot()
        return {
            "ok": bool(snap.mid is not None),
            "tcp_probe": tcp_probe,
            "bi5_error": bi5_error or None,
            "snapshot": snap.as_dict(),
            "note": (
                "TCP anónimo pode falhar por firewall; bi5 horário público é o fallback. "
                "bi5 nunca é tempo real."
            ),
        }

    def _safe_poll_bi5(self) -> None:
        try:
            self._poll_bi5_once()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"bi5_recover:{type(exc).__name__}: {exc}"
            logger.warning("Recuperação bi5 falhou: %s", self._last_error)

    def _on_tcp_status(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._tcp_connected = bool(payload.get("connected"))
            self._tcp_reconnects = int(payload.get("reconnect_attempts") or 0)
            err = payload.get("last_error") or ""
            if err:
                self._last_error = str(err)

    def _on_tcp_tick(self, tick: ValidatedTick) -> None:
        self._accept_tick(tick)

    def _accept_tick(self, tick: ValidatedTick) -> bool:
        with self._lock:
            if is_duplicate_tick(self._prev_tick, tick):
                self._duplicate_count += 1
                return False
            now = datetime.now(timezone.utc)
            if self._received_at is not None:
                gap = (now - self._received_at).total_seconds()
                if 0 < gap < 3600:
                    self._observed_intervals.append(gap)
                    if len(self._observed_intervals) > 50:
                        self._observed_intervals = self._observed_intervals[-50:]
            self._prev_tick = self._last_tick
            self._last_tick = tick
            self._received_at = now
            self._tick_count += 1
            self._channel = tick.channel
            # TCP tem prioridade enquanto entregar ticks; bi5 não sobrescreve TCP fresco.
            return True

    def _bi5_poll_loop(self) -> None:
        settings = get_settings()
        # A primeira sondagem já corre em start()/recover(); aqui só o ciclo periódico.
        interval = max(30.0, float(settings.dukascopy_bi5_poll_seconds))
        while not self._stop.is_set():
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            try:
                self._poll_bi5_once()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error = f"bi5_poll:{type(exc).__name__}: {exc}"
                logger.warning("Poll bi5 falhou: %s", self._last_error)

    def _poll_bi5_once(self) -> None:
        settings = get_settings()
        headers = {"User-Agent": "GROK-FOREX-AI/0.2 (local; dukascopy bi5 quote poll)"}
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
            tick, hour_ticks, meta = latest_tick_from_hours(
                client, lookback_hours=settings.dukascopy_bi5_lookback_hours
            )
        if tick is None:
            with self._lock:
                self._last_error = "bi5:sem_ticks_recentes"
            return

        with self._lock:
            # Se TCP entregou tick recente, manter TCP como canal ativo.
            if (
                self._last_tick
                and self._last_tick.channel == "dukascopy_tcp"
                and self._received_at
                and (datetime.now(timezone.utc) - self._received_at).total_seconds()
                <= settings.quote_fresh_seconds
            ):
                # Ainda assim persistir candles bi5 se for hora nova.
                pass
            else:
                self._accept_tick(tick)
            self._bi5_ticks_buffer = hour_ticks

        selected = meta.get("selected_hour")
        if selected and selected != self._last_bi5_persist_hour and hour_ticks:
            self._persist_hour_candles(hour_ticks)
            with self._lock:
                self._last_bi5_persist_hour = selected

    def _persist_hour_candles(self, ticks: list[tuple[datetime, float, float]]) -> None:
        """Agrega e guarda candles — sem disparar análises de IA."""
        updated: dict[str, int] = {}
        db = SessionLocal()
        try:
            for tf in ("M5", "M15", "H1", "H4"):
                candles = aggregate_ticks_to_candles(ticks, tf)
                if not candles:
                    continue
                # Garantir etiquetas reais.
                for c in candles:
                    c["source"] = DUKASCOPY_SOURCE
                    c["is_synthetic"] = False
                # Garantir símbolo nas velas agregadas do feed principal (EURUSD).
                for c in candles:
                    c["symbol"] = "EURUSD"
                result = persist_candles(db, candles, tf)
                updated[tf] = int(result.get("inserted", 0)) + int(result.get("updated", 0))
            with self._lock:
                self._candles_updated = updated
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"persist:{type(exc).__name__}: {exc}"
            logger.warning("Falha ao persistir candles do feed: %s", self._last_error)
        finally:
            db.close()

    def snapshot(self) -> QuoteFeedSnapshot:
        settings = get_settings()
        with self._lock:
            tick = self._last_tick
            received = self._received_at
            channel: FeedChannel = self._channel if tick else "none"
            tcp_connected = self._tcp_connected
            reconnects = self._tcp_reconnects
            last_error = self._last_error
            tick_count = self._tick_count
            candles_updated = dict(self._candles_updated)
            intervals = list(self._observed_intervals)

        now = datetime.now(timezone.utc)
        lag: float | None = None
        if tick is not None:
            lag = (now - tick.ts).total_seconds()

        # Ligado se houver tick verificado em cache ou sessão TCP ativa.
        connected = tick is not None or bool(tcp_connected)

        status = classify_status(
            lag,
            connected=connected and tick is not None,
            fresh_seconds=settings.quote_fresh_seconds,
            delayed_seconds=settings.quote_delayed_seconds,
        )
        # bi5 nunca é «atualizado» no sentido de latência baixa — forçar com_atraso
        # se o atraso ainda estiver dentro do limiar delayed.
        if status == "atualizado" and channel == "dukascopy_bi5":
            status = "com_atraso"

        block = status in {"obsoleto", "desligado"} or tick is None

        freq_hint: float | None
        if channel == "dukascopy_tcp":
            freq_hint = (
                sum(intervals) / len(intervals) if intervals else 10.0
            )
        elif channel == "dukascopy_bi5":
            freq_hint = float(settings.dukascopy_bi5_poll_seconds)
        else:
            freq_hint = None

        message = _message_for(status, channel, lag, last_error)
        # Classificação explícita — nunca apresentar bi5/atrasado como tempo real.
        if tick is None or status == "desligado":
            data_class = "indisponivel"
        elif status == "obsoleto" or block:
            data_class = "obsoleto"
        elif channel == "dukascopy_bi5":
            data_class = "historico_horario_atrasado"
        elif status == "com_atraso":
            data_class = "atrasado"
        elif status == "atualizado" and channel == "dukascopy_tcp":
            data_class = "recente_tcp"
        else:
            data_class = "atrasado"
        claims_realtime = bool(
            channel == "dukascopy_tcp" and status == "atualizado" and (lag is not None and lag <= 45)
        )

        return QuoteFeedSnapshot(
            connected=connected and tick is not None,
            status=status,  # type: ignore[arg-type]
            status_label=STATUS_LABELS_PT.get(status, status),
            channel=channel,
            source_label=_SOURCE_LABELS.get(channel, channel),
            bid=tick.bid if tick else None,
            ask=tick.ask if tick else None,
            mid=tick.mid if tick else None,
            spread_pips=tick.spread_pips if tick else None,
            quote_ts=tick.ts if tick else None,
            received_at=received,
            lag_seconds=lag,
            update_interval_hint_seconds=freq_hint,
            mid_only=bool(tick.mid_only) if tick else False,
            is_synthetic=False,
            block_entries=block,
            message=message,
            tick_count=tick_count,
            reconnect_attempts=reconnects,
            last_error=last_error,
            candles_updated=candles_updated,
            data_class=data_class,
            claims_realtime=claims_realtime,
        )

    def verified_quote(self) -> ValidatedTick | None:
        snap = self.snapshot()
        if snap.status in {"desligado", "obsoleto"} or self._last_tick is None:
            return None
        if snap.is_synthetic:
            return None
        return self._last_tick

    # --- helpers de teste / injeção ---
    def inject_tick_for_tests(self, tick: ValidatedTick) -> None:
        self._accept_tick(tick)

    def clear_for_tests(self) -> None:
        with self._lock:
            self._last_tick = None
            self._prev_tick = None
            self._received_at = None
            self._channel = "none"
            self._tick_count = 0
            self._duplicate_count = 0
            self._tcp_connected = False
            self._last_error = ""
            self._candles_updated = {}
            self._observed_intervals = []


def _message_for(
    status: str, channel: FeedChannel, lag: float | None, last_error: str
) -> str:
    base = {
        "atualizado": "Cotação Dukascopy recebida com atraso baixo (não rotulada como tempo real sem evidência).",
        "com_atraso": "Cotação real Dukascopy com atraso medido — adequada a análise; não é feed contínuo de corretora.",
        "obsoleto": "Dados obsoletos — novas entradas simuladas bloqueadas.",
        "desligado": "Fonte Dukascopy desligada ou sem cotações válidas.",
    }.get(status, "")
    parts = [base, f"Canal: {channel}."]
    if lag is not None:
        parts.append(f"Atraso medido: {lag:.0f}s.")
    if last_error and status in {"desligado", "obsoleto"}:
        parts.append(f"Último erro: {last_error}")
    parts.append(REAL_HISTORICAL_LABEL if channel == "dukascopy_bi5" else "")
    return " ".join(p for p in parts if p)


_feed: QuoteFeedService | None = None
_feed_lock = threading.Lock()


def get_quote_feed() -> QuoteFeedService:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = QuoteFeedService()
        return _feed


def reset_quote_feed_for_tests() -> QuoteFeedService:
    global _feed
    with _feed_lock:
        if _feed is not None:
            _feed.stop()
        _feed = QuoteFeedService()
        return _feed
