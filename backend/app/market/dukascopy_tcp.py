"""Cliente TCP anónimo do datafeed Dukascopy (porto 9999).

Protocolo público documentado: mensagens terminadas em NUL, subscrição
`$connect|EUR/USD:1`, ticks `#id,preço,volume` (~10 s). Sem conta nem chave API.

Não assume disponibilidade: a ligação pode falhar por firewall/rede.
Não inventa ticks quando a ligação falha.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.market.tick_validation import TickValidationError, validate_tick
from app.market.quote_types import ValidatedTick

logger = logging.getLogger(__name__)

DEFAULT_HOST = "datafeed.dukascopy.com"
DEFAULT_PORT = 9999
INSTRUMENT = "EUR/USD"


class DukascopyTcpClient:
    """Uma ligação TCP por processo; callbacks em thread de leitura."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        # Timeouts curtos: Dukascopy é complementar — nunca deve atrasar MT5/painel.
        connect_timeout: float = 3.0,
        read_timeout: float = 8.0,
        on_tick: Callable[[ValidatedTick], None] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.on_tick = on_tick
        self.on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self.reconnect_attempts = 0
        self.last_error = ""
        self.connected = False
        self.subscribed = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="dukascopy-tcp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_sock()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _emit_status(self, **kwargs: Any) -> None:
        if self.on_status:
            self.on_status(
                {
                    "connected": self.connected,
                    "subscribed": self.subscribed,
                    "reconnect_attempts": self.reconnect_attempts,
                    "last_error": self.last_error,
                    **kwargs,
                }
            )

    def _close_sock(self) -> None:
        with self._lock:
            sock = self._sock
            self._sock = None
        self.connected = False
        self.subscribed = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _write(self, sock: socket.socket, line: str) -> None:
        sock.sendall(line.encode("utf-8") + b"\x00")

    def _read(self, sock: socket.socket) -> str | None:
        buf = bytearray()
        while True:
            chunk = sock.recv(1)
            if not chunk:
                return None
            if chunk == b"\x00":
                return buf.decode("utf-8", errors="replace")
            buf.extend(chunk)
            if len(buf) > 8192:
                raise OSError("Mensagem TCP demasiado longa")

    def _run_loop(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 5.0
            except Exception as exc:  # noqa: BLE001 — isolamento da thread de rede
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.connected = False
                self.subscribed = False
                self.reconnect_attempts += 1
                logger.warning("Dukascopy TCP: %s", self.last_error)
                self._emit_status(event="error")
                self._close_sock()
            if self._stop.is_set():
                break
            # Reconexão controlada com backoff exponencial (máx. 5 min).
            sleep_for = min(backoff, 300.0)
            self._emit_status(event="reconnect_wait", sleep_for=sleep_for)
            self._stop.wait(sleep_for)
            backoff = min(backoff * 2.0, 300.0)

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(self.read_timeout)
        with self._lock:
            self._sock = sock
        self.connected = True
        self.last_error = ""
        welcome = self._read(sock)
        self._emit_status(event="welcome", welcome=welcome or "")
        self._write(sock, f"$connect|{INSTRUMENT}:1")
        while not self._stop.is_set():
            msg = self._read(sock)
            if msg is None:
                raise ConnectionError("Ligação TCP encerrada pelo servidor")
            if msg.startswith("@"):
                if "Subscribed" in msg or "Logged in" in msg:
                    self.subscribed = True
                self._emit_status(event="server", message=msg)
                continue
            if msg.startswith("#"):
                tick = self._parse_tick(msg)
                if tick and self.on_tick:
                    self.on_tick(tick)
                continue
            self._emit_status(event="other", message=msg)

    def _parse_tick(self, msg: str) -> ValidatedTick | None:
        # Formato: #1,1.3304,12
        try:
            body = msg[1:]
            parts = body.split(",")
            if len(parts) < 2:
                return None
            price = float(parts[1])
            volume = float(parts[2]) if len(parts) > 2 and parts[2] != "" else None
            # O protocolo TCP entrega um preço único (não bid/ask separados).
            # Timestamp = receção local — o servidor não envia relógio de mercado.
            return validate_tick(
                ts=datetime.now(timezone.utc),
                bid=None,
                ask=None,
                mid=price,
                channel="dukascopy_tcp",
                volume=volume,
            )
        except (TickValidationError, ValueError, IndexError) as exc:
            self.last_error = f"tick_parse:{exc}"
            return None


def probe_tcp(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 5.0) -> dict[str, Any]:
    """Sonda rápida de conectividade (sem manter a sessão)."""
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        buf = bytearray()
        while True:
            b = sock.recv(1)
            if not b or b == b"\x00":
                break
            buf.extend(b)
        sock.close()
        return {
            "ok": True,
            "host": host,
            "port": port,
            "welcome": buf.decode("utf-8", errors="replace"),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
