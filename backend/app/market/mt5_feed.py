"""Feed de mercado MetaTrader 5 — cotações apenas. Nunca envia ordens."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.config import get_settings
from app.market.quote_types import ValidatedTick
from app.risk.instruments import get_instrument
from app.robot.pair_quotes import get_pair_quote_store
from app.robot.mt5_symbol_discovery import broker_symbol_for, discover_forex_universe, symbols_for_radar
from app.robot.universe import SYMBOLS

logger = logging.getLogger(__name__)

FeedState = Literal["recente", "atrasado", "mercado_fechado", "ligacao_perdida"]

_TF_MAP = {"M15": None, "H1": None, "H4": None}  # filled after mt5 import


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_unix(ts: float | int) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


@dataclass
class Mt5SymbolQuote:
    symbol: str
    bid: float
    ask: float
    mid: float
    spread_pips: float
    tick_time: datetime
    tick_time_msc: datetime
    received_at: datetime
    tick_time_lag_s: float
    tick_msc_lag_s: float
    update_age_s: float
    measured_lag_s: float
    feed_state: FeedState
    claims_realtime: bool
    block_entries: bool
    price_changed: bool
    clock_anomaly: bool
    clock_offset_hint_s: float | None
    message: str
    source: str = "mt5"
    terminal_connected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "spread_pips": self.spread_pips,
            "ts": self.tick_time.isoformat(),
            "tick_time": self.tick_time.isoformat(),
            "tick_time_msc": self.tick_time_msc.isoformat(),
            "received_at": self.received_at.isoformat(),
            "tick_time_lag_s": round(self.tick_time_lag_s, 3),
            "tick_msc_lag_s": round(self.tick_msc_lag_s, 3),
            "update_age_s": round(self.update_age_s, 3),
            "lag_seconds": round(self.measured_lag_s, 3),
            "status": self._status_code(),
            "feed_state": self.feed_state,
            "channel": "mt5",
            "data_class": self._data_class(),
            "claims_realtime": self.claims_realtime,
            "block_entries": self.block_entries,
            "is_synthetic": False,
            "source": self.source,
            "price_changed": self.price_changed,
            "clock_anomaly": self.clock_anomaly,
            "clock_offset_hint_s": self.clock_offset_hint_s,
            "terminal_connected": self.terminal_connected,
            "message": self.message,
        }

    def _status_code(self) -> str:
        if self.feed_state == "ligacao_perdida":
            return "desligado"
        if self.feed_state == "mercado_fechado":
            return "obsoleto"
        if self.feed_state == "atrasado":
            return "com_atraso"
        return "atualizado"

    def _data_class(self) -> str:
        if self.feed_state == "ligacao_perdida":
            return "ligacao_perdida"
        if self.feed_state == "mercado_fechado":
            return "mercado_fechado"
        if self.feed_state == "atrasado":
            return "atrasado"
        if self.clock_anomaly:
            return "mt5_atualizacao_recente_relogio_inconsistente"
        if self.claims_realtime:
            return "mt5_recente"
        return "mt5_atualizado"


@dataclass
class Mt5FeedSnapshot:
    enabled: bool
    initialized: bool
    terminal_connected: bool
    account_login: int | None
    account_server: str
    trade_allowed: bool
    poll_count: int
    last_error: str
    message: str
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Nunca afirmar tempo-real só porque initialize() devolveu True.
    claims_any_realtime: bool = False
    # Conta MT5 (account_info) — fonte de verdade para saldo/equity no painel.
    trade_mode: int | None = None
    trade_mode_label: str = ""
    balance: float | None = None
    equity: float | None = None
    margin: float | None = None
    margin_free: float | None = None
    profit: float | None = None
    currency: str = ""
    company: str = ""
    # Botão AutoTrading do terminal (terminal_info.trade_allowed).
    terminal_trade_allowed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "initialized": self.initialized,
            "terminal_connected": self.terminal_connected,
            "account_login": self.account_login,
            "account_server": self.account_server,
            "trade_allowed": self.trade_allowed,
            "terminal_trade_allowed": self.terminal_trade_allowed,
            "autotrading_enabled": self.terminal_trade_allowed,
            "poll_count": self.poll_count,
            "last_error": self.last_error,
            "message": self.message,
            "claims_any_realtime": self.claims_any_realtime,
            "symbols": self.symbols,
            "source": "mt5",
            "orders_disabled": True,
            "trade_mode": self.trade_mode,
            "trade_mode_label": self.trade_mode_label,
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "margin_free": self.margin_free,
            "profit": self.profit,
            "currency": self.currency,
            "company": self.company,
            "symbols_count": len(self.symbols),
        }


class Mt5MarketFeed:
    """Fonte principal de cotações Forex (até 28 cruzamentos). Sem order_send."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initialized = False
        self._terminal_connected = False
        self._account_login: int | None = None
        self._account_server = ""
        self._trade_allowed = False
        self._terminal_trade_allowed = False
        self._trade_mode: int | None = None
        self._trade_mode_label = ""
        self._balance: float | None = None
        self._equity: float | None = None
        self._margin: float | None = None
        self._margin_free: float | None = None
        self._profit: float | None = None
        self._currency = ""
        self._company = ""
        self._poll_count = 0
        self._last_error = ""
        self._message = "MT5 feed parado."
        self._quotes: dict[str, Mt5SymbolQuote] = {}
        self._candles: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._last_mids: dict[str, float] = {}
        self._last_tick_keys: dict[str, tuple[int, float, float]] = {}
        self._offset_samples: list[float] = []
        self._forced_disconnect = False

    def _apply_account_info(self, ai: Any, mt5_mod: Any | None = None) -> None:
        """Atualiza campos de conta a partir de mt5.account_info() — sem inventar DEMO."""
        if ai is None:
            self._account_login = None
            self._account_server = ""
            self._trade_allowed = False
            self._trade_mode = None
            self._trade_mode_label = ""
            self._balance = None
            self._equity = None
            self._margin = None
            self._margin_free = None
            self._profit = None
            self._currency = ""
            self._company = ""
            return
        self._account_login = int(ai.login) if getattr(ai, "login", None) is not None else None
        self._account_server = str(getattr(ai, "server", "") or "")
        self._trade_allowed = bool(getattr(ai, "trade_allowed", False))
        mode = getattr(ai, "trade_mode", None)
        self._trade_mode = int(mode) if mode is not None else None
        label = "desconhecido"
        if mt5_mod is not None and self._trade_mode is not None:
            demo = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_DEMO", 0)
            contest = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_CONTEST", 1)
            real = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_REAL", 2)
            if self._trade_mode == demo:
                label = "DEMO"
            elif self._trade_mode == contest:
                label = "CONTEST"
            elif self._trade_mode == real:
                label = "REAL"
            else:
                label = f"modo_{self._trade_mode}"
        elif self._trade_mode == 0:
            label = "DEMO"
        elif self._trade_mode == 1:
            label = "CONTEST"
        elif self._trade_mode == 2:
            label = "REAL"
        self._trade_mode_label = label
        self._balance = float(getattr(ai, "balance", 0.0) or 0.0)
        self._equity = float(getattr(ai, "equity", 0.0) or 0.0)
        self._margin = float(getattr(ai, "margin", 0.0) or 0.0)
        self._margin_free = float(getattr(ai, "margin_free", 0.0) or 0.0)
        self._profit = float(getattr(ai, "profit", 0.0) or 0.0)
        self._currency = str(getattr(ai, "currency", "") or "")
        self._company = str(getattr(ai, "company", "") or "")

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> dict[str, Any]:
        settings = get_settings()
        if not getattr(settings, "mt5_quote_feed_enabled", True):
            self._message = "Feed MT5 desligado na configuração."
            return self.snapshot().as_dict()
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot().as_dict()
            self._stop.clear()
            self._forced_disconnect = False
            self._thread = threading.Thread(target=self._loop, name="mt5-market-feed", daemon=True)
            self._thread.start()
        # Primeiro poll síncrono para o arranque ter dados.
        self.poll_once()
        return self.snapshot().as_dict()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)
        with self._lock:
            if self._initialized:
                try:
                    import MetaTrader5 as mt5  # type: ignore

                    mt5.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self._initialized = False
            self._terminal_connected = False
            self._message = "MT5 feed parado."

    def force_disconnect(self) -> None:
        """Simula interrupção: marca ligação perdida sem apresentar ticks antigos como atuais."""
        with self._lock:
            self._forced_disconnect = True
            self._terminal_connected = False
            self._initialized = False
            self._message = "Interrupção MT5 simulada — ligação perdida."
            now = _utcnow()
            for sym, q in list(self._quotes.items()):
                stale = Mt5SymbolQuote(
                    symbol=sym,
                    bid=q.bid,
                    ask=q.ask,
                    mid=q.mid,
                    spread_pips=q.spread_pips,
                    tick_time=q.tick_time,
                    tick_time_msc=q.tick_time_msc,
                    received_at=q.received_at,
                    tick_time_lag_s=(now - q.tick_time).total_seconds(),
                    tick_msc_lag_s=(now - q.tick_time_msc).total_seconds(),
                    update_age_s=(now - q.received_at).total_seconds(),
                    measured_lag_s=(now - q.received_at).total_seconds(),
                    feed_state="ligacao_perdida",
                    claims_realtime=False,
                    block_entries=True,
                    price_changed=False,
                    clock_anomaly=q.clock_anomaly,
                    clock_offset_hint_s=q.clock_offset_hint_s,
                    message="Ligação MT5 perdida — cotação anterior não é atual.",
                    terminal_connected=False,
                )
                self._quotes[sym] = stale
                get_pair_quote_store().set_mt5(sym, stale)
            try:
                import MetaTrader5 as mt5  # type: ignore

                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def recover(self) -> dict[str, Any]:
        with self._lock:
            self._forced_disconnect = False
            self._last_error = ""
        ok = self._ensure_init()
        if ok:
            self.poll_once()
        return {"ok": ok, "snapshot": self.snapshot().as_dict()}

    # --- public reads ------------------------------------------------------

    def get_quote(self, symbol: str) -> Mt5SymbolQuote | None:
        key = symbol.replace("/", "").upper()
        with self._lock:
            return self._quotes.get(key)

    def candles(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        key = symbol.replace("/", "").upper()
        with self._lock:
            return list(self._candles.get(key, {}).get(timeframe, []))

    def snapshot(self) -> Mt5FeedSnapshot:
        with self._lock:
            syms = {k: v.as_dict() for k, v in self._quotes.items()}
            any_rt = any(v.claims_realtime for v in self._quotes.values())
            return Mt5FeedSnapshot(
                enabled=bool(getattr(get_settings(), "mt5_quote_feed_enabled", True)),
                initialized=self._initialized,
                terminal_connected=self._terminal_connected,
                account_login=self._account_login,
                account_server=self._account_server,
                trade_allowed=self._trade_allowed,
                poll_count=self._poll_count,
                last_error=self._last_error,
                message=self._message,
                symbols=syms,
                claims_any_realtime=any_rt,
                trade_mode=self._trade_mode,
                trade_mode_label=self._trade_mode_label,
                balance=self._balance,
                equity=self._equity,
                margin=self._margin,
                margin_free=self._margin_free,
                profit=self._profit,
                currency=self._currency,
                company=self._company,
                terminal_trade_allowed=self._terminal_trade_allowed,
            )

    # --- polling -----------------------------------------------------------

    def _loop(self) -> None:
        settings = get_settings()
        interval = float(getattr(settings, "mt5_poll_seconds", 2.0) or 2.0)
        while not self._stop.is_set():
            if not self._forced_disconnect:
                try:
                    self.poll_once()
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                    logger.warning("MT5 poll: %s", exc)
                    self._mark_all_disconnected(str(exc))
            self._stop.wait(interval)

    def _ensure_init(self) -> bool:
        if self._forced_disconnect:
            return False
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            self._last_error = "Pacote MetaTrader5 não instalado."
            self._message = self._last_error
            self._initialized = False
            self._terminal_connected = False
            return False
        settings = get_settings()
        path = (settings.mt5_path or "").strip() or None
        if self._initialized:
            ti = mt5.terminal_info()
            if ti is not None and bool(getattr(ti, "connected", False)):
                self._terminal_connected = True
                return True
            # Re-init se terminal caiu.
            mt5.shutdown()
            self._initialized = False
        ok = mt5.initialize(path) if path else mt5.initialize()
        if not ok:
            self._last_error = f"mt5.initialize falhou: {mt5.last_error()}"
            self._message = self._last_error
            self._initialized = False
            self._terminal_connected = False
            return False
        # Login opcional — terminal já pode ter sessão demo aberta.
        login = (settings.mt5_login or "").strip()
        password = (settings.mt5_password or "").strip()
        server = (settings.mt5_server or "").strip()
        if login and password and server:
            try:
                logged = mt5.login(int(login), password=password, server=server)
                if not logged:
                    self._last_error = f"mt5.login falhou: {mt5.last_error()}"
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"mt5.login erro: {exc}"
        ti = mt5.terminal_info()
        ai = mt5.account_info()
        self._initialized = True
        self._terminal_connected = bool(ti and getattr(ti, "connected", False))
        self._apply_account_info(ai, mt5)
        if self._terminal_connected:
            self._message = (
                f"MT5 ligado (conta {self._account_login} @ {self._account_server}, "
                f"{self._trade_mode_label or 'modo?'}). "
                "Feed de cotações — ordens só via executor DEMO autorizado."
            )
        else:
            self._message = "mt5.initialize() ok mas terminal.connected=False — sem tempo-real."
        return self._terminal_connected

    def poll_once(self) -> Mt5FeedSnapshot:
        if not self._ensure_init():
            self._mark_all_disconnected(self._last_error or "MT5 não ligado")
            return self.snapshot()

        import MetaTrader5 as mt5  # type: ignore

        # Conta / AutoTrading do terminal — refrescar em cada poll.
        try:
            self._apply_account_info(mt5.account_info(), mt5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("account_info no poll: %s", exc)
        try:
            ti = mt5.terminal_info()
            self._terminal_trade_allowed = bool(ti and getattr(ti, "trade_allowed", False))
            self._terminal_connected = bool(ti and getattr(ti, "connected", False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("terminal_info no poll: %s", exc)
            self._terminal_trade_allowed = False

        now = _utcnow()
        settings = get_settings()
        fresh = float(settings.quote_fresh_seconds)
        delayed = float(settings.quote_delayed_seconds)

        try:
            discover_forex_universe(mt5)
        except Exception:  # noqa: BLE001
            pass
        watch = symbols_for_radar(prefer_mt5=True) or list(SYMBOLS)
        for symbol in watch:
            try:
                broker_sym = broker_symbol_for(symbol, mt5)
                mt5.symbol_select(broker_sym, True)
                tick = mt5.symbol_info_tick(broker_sym)
                if tick is None:
                    self._set_missing(symbol, now, "symbol_info_tick devolveu None")
                    continue
                spec = get_instrument(symbol)
                bid = float(tick.bid)
                ask = float(tick.ask)
                if bid <= 0 or ask <= 0 or ask < bid:
                    self._set_missing(symbol, now, "tick bid/ask inválido")
                    continue
                mid = (bid + ask) / 2.0
                spread_pips = (ask - bid) / spec.pip_size

                tick_time = _from_unix(int(tick.time))
                tick_msc = _from_unix(float(tick.time_msc) / 1000.0)
                lag_s = (now - tick_time).total_seconds()
                lag_msc = (now - tick_msc).total_seconds()

                tick_key = (int(tick.time_msc), bid, ask)
                prev_key = self._last_tick_keys.get(symbol)
                price_changed = prev_key is not None and tick_key != prev_key
                self._last_tick_keys[symbol] = tick_key
                self._last_mids[symbol] = mid

                # Desvio de relógio: só reportar se medido empiricamente (amostras).
                clock_anomaly = lag_s < -30.0
                if clock_anomaly:
                    self._offset_samples.append(lag_s)
                    if len(self._offset_samples) > 40:
                        self._offset_samples = self._offset_samples[-40:]
                offset_hint = None
                if len(self._offset_samples) >= 5:
                    # Mediana das amostras negativas (segundos à frente do UTC).
                    neg = sorted(self._offset_samples)
                    offset_hint = neg[len(neg) // 2]

                received_at = now
                update_age = 0.0

                # Atraso operacional: se o relógio do tick é plausível em UTC, usar lag do tick;
                # se o tick está no futuro (anomalia), o atraso medido é a idade da atualização.
                if lag_s >= -5.0:
                    measured = max(0.0, lag_s)
                else:
                    measured = update_age

                feed_state, claims_rt, block, msg = self._classify(
                    terminal_connected=self._terminal_connected,
                    measured_lag=measured,
                    tick_lag=lag_s,
                    update_age=update_age,
                    price_changed=price_changed or prev_key is None,
                    clock_anomaly=clock_anomaly,
                    fresh=fresh,
                    delayed=delayed,
                    now=now,
                )

                q = Mt5SymbolQuote(
                    symbol=symbol,
                    bid=round(bid, spec.digits),
                    ask=round(ask, spec.digits),
                    mid=round(mid, spec.digits),
                    spread_pips=round(spread_pips, 2),
                    tick_time=tick_time,
                    tick_time_msc=tick_msc,
                    received_at=received_at,
                    tick_time_lag_s=lag_s,
                    tick_msc_lag_s=lag_msc,
                    update_age_s=update_age,
                    measured_lag_s=measured,
                    feed_state=feed_state,
                    claims_realtime=claims_rt,
                    block_entries=block,
                    price_changed=price_changed,
                    clock_anomaly=clock_anomaly,
                    clock_offset_hint_s=offset_hint,
                    message=msg,
                    terminal_connected=self._terminal_connected,
                )
                with self._lock:
                    # Se já tínhamos cotação, update_age real = tempo desde última receção distinta.
                    prev = self._quotes.get(symbol)
                    if prev is not None and not price_changed and prev.feed_state != "ligacao_perdida":
                        update_age = (now - prev.received_at).total_seconds()
                        if lag_s >= -5.0:
                            measured = max(0.0, lag_s)
                        else:
                            measured = update_age
                        feed_state, claims_rt, block, msg = self._classify(
                            terminal_connected=self._terminal_connected,
                            measured_lag=measured,
                            tick_lag=lag_s,
                            update_age=update_age,
                            price_changed=False,
                            clock_anomaly=clock_anomaly,
                            fresh=fresh,
                            delayed=delayed,
                            now=now,
                        )
                        q = Mt5SymbolQuote(
                            symbol=symbol,
                            bid=q.bid,
                            ask=q.ask,
                            mid=q.mid,
                            spread_pips=q.spread_pips,
                            tick_time=tick_time,
                            tick_time_msc=tick_msc,
                            received_at=prev.received_at if not price_changed else received_at,
                            tick_time_lag_s=lag_s,
                            tick_msc_lag_s=lag_msc,
                            update_age_s=update_age,
                            measured_lag_s=measured,
                            feed_state=feed_state,
                            claims_realtime=claims_rt,
                            block_entries=block,
                            price_changed=False,
                            clock_anomaly=clock_anomaly,
                            clock_offset_hint_s=offset_hint,
                            message=msg,
                            terminal_connected=self._terminal_connected,
                        )
                    elif price_changed or prev is None:
                        q = Mt5SymbolQuote(
                            symbol=q.symbol,
                            bid=q.bid,
                            ask=q.ask,
                            mid=q.mid,
                            spread_pips=q.spread_pips,
                            tick_time=tick_time,
                            tick_time_msc=tick_msc,
                            received_at=received_at,
                            tick_time_lag_s=lag_s,
                            tick_msc_lag_s=lag_msc,
                            update_age_s=0.0,
                            measured_lag_s=measured if lag_s >= -5.0 else 0.0,
                            feed_state=feed_state,
                            claims_realtime=claims_rt,
                            block_entries=block,
                            price_changed=True,
                            clock_anomaly=clock_anomaly,
                            clock_offset_hint_s=offset_hint,
                            message=msg,
                            terminal_connected=self._terminal_connected,
                        )
                    self._quotes[symbol] = q
                get_pair_quote_store().set_mt5(symbol, q)
                self._refresh_candles(mt5, symbol)
            except Exception as exc:  # noqa: BLE001
                self._set_missing(symbol, now, str(exc))

        with self._lock:
            self._poll_count += 1
            any_rt = any(q.claims_realtime for q in self._quotes.values())
            n_ok = sum(1 for q in self._quotes.values() if q.feed_state == "recente")
            self._message = (
                f"MT5 poll#{self._poll_count}: {n_ok}/7 recentes; "
                f"claims_realtime={any_rt}; terminal.connected={self._terminal_connected}"
            )
        return self.snapshot()

    def _classify(
        self,
        *,
        terminal_connected: bool,
        measured_lag: float,
        tick_lag: float,
        update_age: float,
        price_changed: bool,
        clock_anomaly: bool,
        fresh: float,
        delayed: float,
        now: datetime,
    ) -> tuple[FeedState, bool, bool, str]:
        if not terminal_connected:
            return (
                "ligacao_perdida",
                False,
                True,
                "Terminal MT5 sem ligação — não é tempo-real.",
            )

        # Mercado fechado: fim de semana UTC e tick sem mudança prolongada.
        weekday = now.weekday()  # 5=Sat 6=Sun
        if weekday >= 5 and update_age > fresh and not price_changed:
            return (
                "mercado_fechado",
                False,
                True,
                "Sem atualização de ticks — mercado provavelmente fechado.",
            )

        # claims_realtime só com lag de relógio UTC plausível (não só initialize).
        clock_ok = -5.0 <= tick_lag <= fresh
        if clock_ok and measured_lag <= fresh:
            return (
                "recente",
                True,
                False,
                "Tick MT5 com timestamp UTC coerente e atraso dentro do limiar.",
            )

        if clock_anomaly and (price_changed or update_age <= fresh):
            return (
                "recente",
                False,
                False,
                (
                    "Atualização MT5 recente, mas tick.time vs UTC atual é inconsistente "
                    f"(lag_relogio={tick_lag:.1f}s). Sem correção fixa aplicada."
                ),
            )

        if measured_lag <= delayed or update_age <= delayed:
            return (
                "atrasado",
                False,
                True,
                f"Cotação MT5 atrasada (atraso_medido={measured_lag:.1f}s, update_age={update_age:.1f}s).",
            )

        if weekday >= 5:
            return (
                "mercado_fechado",
                False,
                True,
                "Dados antigos em fim de semana — mercado fechado.",
            )
        return (
            "atrasado",
            False,
            True,
            f"Cotação MT5 obsoleta (atraso_medido={measured_lag:.1f}s).",
        )

    def _refresh_candles(self, mt5: Any, symbol: str) -> None:
        tf_const = {
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
        }
        # Alinhado com pair_snapshot._LIMITS (calibração direcional + SMA50).
        limits = {"M15": 120, "H1": 60, "H4": 40}
        bar_min = {"M15": 15, "H1": 60, "H4": 240}
        now = _utcnow()
        out: dict[str, list[dict[str, Any]]] = {}
        for tf, const in tf_const.items():
            rates = mt5.copy_rates_from_pos(symbol, const, 0, limits[tf] + 3)
            if rates is None:
                continue
            closed: list[dict[str, Any]] = []
            for r in rates:
                ts = _from_unix(int(r["time"]))
                end = ts.timestamp() + bar_min[tf] * 60
                if end > now.timestamp():
                    continue  # vela em formação
                closed.append(
                    {
                        "ts": ts.isoformat(),
                        "o": float(r["open"]),
                        "h": float(r["high"]),
                        "l": float(r["low"]),
                        "c": float(r["close"]),
                        "source": "mt5",
                    }
                )
            out[tf] = closed[-limits[tf] :]
        with self._lock:
            self._candles[symbol] = out

    def _set_missing(self, symbol: str, now: datetime, reason: str) -> None:
        with self._lock:
            prev = self._quotes.get(symbol)
            if prev is None:
                return
            q = Mt5SymbolQuote(
                symbol=symbol,
                bid=prev.bid,
                ask=prev.ask,
                mid=prev.mid,
                spread_pips=prev.spread_pips,
                tick_time=prev.tick_time,
                tick_time_msc=prev.tick_time_msc,
                received_at=prev.received_at,
                tick_time_lag_s=(now - prev.tick_time).total_seconds(),
                tick_msc_lag_s=(now - prev.tick_time_msc).total_seconds(),
                update_age_s=(now - prev.received_at).total_seconds(),
                measured_lag_s=(now - prev.received_at).total_seconds(),
                feed_state="ligacao_perdida" if not self._terminal_connected else "atrasado",
                claims_realtime=False,
                block_entries=True,
                price_changed=False,
                clock_anomaly=prev.clock_anomaly,
                clock_offset_hint_s=prev.clock_offset_hint_s,
                message=f"Tick indisponível: {reason}",
                terminal_connected=self._terminal_connected,
            )
            self._quotes[symbol] = q
            get_pair_quote_store().set_mt5(symbol, q)

    def _mark_all_disconnected(self, reason: str) -> None:
        now = _utcnow()
        with self._lock:
            self._terminal_connected = False
            self._message = f"Ligação MT5 perdida: {reason}"
            for sym, prev in list(self._quotes.items()):
                q = Mt5SymbolQuote(
                    symbol=sym,
                    bid=prev.bid,
                    ask=prev.ask,
                    mid=prev.mid,
                    spread_pips=prev.spread_pips,
                    tick_time=prev.tick_time,
                    tick_time_msc=prev.tick_time_msc,
                    received_at=prev.received_at,
                    tick_time_lag_s=(now - prev.tick_time).total_seconds(),
                    tick_msc_lag_s=(now - prev.tick_time_msc).total_seconds(),
                    update_age_s=(now - prev.received_at).total_seconds(),
                    measured_lag_s=(now - prev.received_at).total_seconds(),
                    feed_state="ligacao_perdida",
                    claims_realtime=False,
                    block_entries=True,
                    price_changed=False,
                    clock_anomaly=prev.clock_anomaly,
                    clock_offset_hint_s=prev.clock_offset_hint_s,
                    message="Ligação perdida — cotação anterior não é atual.",
                    terminal_connected=False,
                )
                self._quotes[sym] = q
                get_pair_quote_store().set_mt5(sym, q)


_feed: Mt5MarketFeed | None = None
_feed_lock = threading.Lock()


def get_mt5_feed() -> Mt5MarketFeed:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = Mt5MarketFeed()
        return _feed
