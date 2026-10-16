"""Histórico profundo MT5 com paginação — sem velas sintéticas.

Distingue: ok | absent (broker sem série) | error (falha de API/terminal).
Não envia ordens.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

HistoryStatus = Literal["ok", "absent", "error", "unavailable"]

# Alvos: ~6–8 meses M15 (~12–16k barras FX 24/5); H1/H4 proporcionais.
# Pedir o máximo disponível até ao alvo; o broker pode devolver menos.
_TARGET_BARS = {"M15": 16000, "H1": 4000, "H4": 1200}
_PAGE = 1000  # chunk seguro para copy_rates_from_pos
_BAR_MIN = {"M15": 15, "H1": 60, "H4": 240}
_CACHE_TTL_S = 900.0  # 15 min


@dataclass
class TfHistory:
    timeframe: str
    bars: list[dict[str, Any]] = field(default_factory=list)
    status: HistoryStatus = "unavailable"
    message: str = ""
    first_ts: str | None = None
    last_ts: str | None = None
    months_approx: float | None = None
    broker_symbol: str = ""
    pages_fetched: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "bars": len(self.bars),
            "status": self.status,
            "message": self.message,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "months_approx": self.months_approx,
            "broker_symbol": self.broker_symbol,
            "pages_fetched": self.pages_fetched,
        }


@dataclass
class SymbolHistory:
    canonical: str
    broker_symbol: str
    by_tf: dict[str, TfHistory] = field(default_factory=dict)
    symbol_info: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""

    def as_dict(self, *, include_bars: bool = False) -> dict[str, Any]:
        out = {
            "canonical": self.canonical,
            "broker_symbol": self.broker_symbol,
            "fetched_at": self.fetched_at,
            "symbol_info": self.symbol_info,
            "timeframes": {k: v.as_dict() for k, v in self.by_tf.items()},
        }
        if include_bars:
            out["bars"] = {k: v.bars for k, v in self.by_tf.items()}
        return out


_lock = threading.RLock()
_cache: dict[str, tuple[float, SymbolHistory]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_unix(ts: int | float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _months_span(first: datetime, last: datetime) -> float:
    days = max(0.0, (last - first).total_seconds() / 86400.0)
    return round(days / 30.437, 2)


def _get_mt5() -> Any | None:
    try:
        import MetaTrader5 as mt5

        # Preferir sessão já aberta pelo feed (mesmo processo). Não chamar
        # initialize() se o terminal já responde — evita bloqueios.
        try:
            if mt5.terminal_info() is not None:
                return mt5
        except Exception:  # noqa: BLE001
            pass
        ok = mt5.initialize()
        if not ok:
            logger.warning("mt5.initialize falhou: %s", mt5.last_error())
            return None
        return mt5
    except Exception as exc:  # noqa: BLE001
        logger.warning("MT5 indisponível para histórico: %s", exc)
        return None


def _broker_symbol(canonical: str, mt5: Any) -> str:
    try:
        from app.robot.mt5_symbol_discovery import broker_symbol_for

        return broker_symbol_for(canonical, mt5)
    except Exception:  # noqa: BLE001
        return canonical


def _symbol_specs(mt5: Any, broker_sym: str) -> dict[str, Any]:
    si = mt5.symbol_info(broker_sym)
    if si is None:
        return {"ok": False, "reason": "symbol_info None"}
    tick = mt5.symbol_info_tick(broker_sym)
    mid = None
    if tick and tick.bid and tick.ask:
        mid = (float(tick.bid) + float(tick.ask)) / 2.0
    return {
        "ok": True,
        "name": si.name,
        "digits": int(si.digits),
        "point": float(si.point),
        "trade_tick_size": float(si.trade_tick_size),
        "trade_tick_value": float(si.trade_tick_value),
        "trade_contract_size": float(si.trade_contract_size),
        "currency_profit": str(si.currency_profit),
        "currency_margin": str(getattr(si, "currency_margin", "") or ""),
        "spread_points": int(si.spread) if si.spread is not None else None,
        "mid": mid,
        "bid": float(tick.bid) if tick else None,
        "ask": float(tick.ask) if tick else None,
    }


def fetch_tf_history(
    mt5: Any,
    broker_sym: str,
    timeframe: str,
    *,
    target_bars: int | None = None,
) -> TfHistory:
    """Página copy_rates_from_pos até target ou até o broker não devolver mais."""
    tf_map = {
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
    }
    if timeframe not in tf_map:
        return TfHistory(timeframe=timeframe, status="error", message="timeframe inválido")
    target = int(target_bars or _TARGET_BARS[timeframe])
    const = tf_map[timeframe]
    bar_min = _BAR_MIN[timeframe]
    now = _utcnow()
    collected: list[dict[str, Any]] = []
    pos = 0
    pages = 0
    last_err: Any = None

    mt5.symbol_select(broker_sym, True)

    while len(collected) < target:
        need = min(_PAGE, target - len(collected) + 5)
        rates = mt5.copy_rates_from_pos(broker_sym, const, pos, need)
        pages += 1
        if rates is None:
            last_err = mt5.last_error()
            break
        if len(rates) == 0:
            break
        batch: list[dict[str, Any]] = []
        for r in rates:
            ts = _from_unix(int(r["time"]))
            end = ts.timestamp() + bar_min * 60
            if end > now.timestamp():
                continue  # vela em formação — não fabricar
            batch.append(
                {
                    "ts": ts.isoformat(),
                    "o": float(r["open"]),
                    "h": float(r["high"]),
                    "l": float(r["low"]),
                    "c": float(r["close"]),
                    "source": "mt5",
                }
            )
        if not batch and len(rates) > 0:
            # Só velas em formação no início — avançar
            pos += len(rates)
            if pages > 2:
                break
            continue
        # rates vêm do mais antigo (pos) para o mais recente dentro do chunk?
        # copy_rates_from_pos(start_pos, count): start_pos=0 é a barra atual;
        # o array fica ordenado do mais antigo ao mais recente do intervalo.
        # Para pos>0, pedimos barras mais antigas.
        collected = batch + collected
        # Dedup por ts
        seen: set[str] = set()
        dedup: list[dict[str, Any]] = []
        for b in collected:
            if b["ts"] in seen:
                continue
            seen.add(b["ts"])
            dedup.append(b)
        collected = sorted(dedup, key=lambda x: x["ts"])
        if len(rates) < need:
            break  # fim do histórico do broker
        pos += len(rates)
        if pages > 40:
            break  # safety

    if not collected:
        if last_err and last_err != (1, "Success"):
            return TfHistory(
                timeframe=timeframe,
                status="error",
                message=f"copy_rates falhou: {last_err}",
                broker_symbol=broker_sym,
                pages_fetched=pages,
            )
        return TfHistory(
            timeframe=timeframe,
            status="absent",
            message="Broker não devolveu velas (sem fabricar).",
            broker_symbol=broker_sym,
            pages_fetched=pages,
        )

    first = datetime.fromisoformat(collected[0]["ts"])
    last = datetime.fromisoformat(collected[-1]["ts"])
    return TfHistory(
        timeframe=timeframe,
        bars=collected[-target:],
        status="ok",
        message="ok",
        first_ts=collected[0]["ts"],
        last_ts=collected[-1]["ts"],
        months_approx=_months_span(first, last),
        broker_symbol=broker_sym,
        pages_fetched=pages,
    )


def fetch_symbol_history(
    canonical: str,
    *,
    force: bool = False,
    timeframes: tuple[str, ...] = ("M15", "H1", "H4"),
) -> SymbolHistory:
    key = canonical.replace("/", "").upper()
    with _lock:
        hit = _cache.get(key)
        if hit and not force and (time.monotonic() - hit[0]) < _CACHE_TTL_S:
            return hit[1]

    mt5 = _get_mt5()
    if mt5 is None:
        hist = SymbolHistory(
            canonical=key,
            broker_symbol=key,
            fetched_at=_utcnow().isoformat(),
        )
        for tf in timeframes:
            hist.by_tf[tf] = TfHistory(
                timeframe=tf,
                status="unavailable",
                message="MetaTrader5 não inicializado / terminal indisponível",
            )
        return hist

    broker = _broker_symbol(key, mt5)
    specs = _symbol_specs(mt5, broker)
    hist = SymbolHistory(
        canonical=key,
        broker_symbol=broker,
        symbol_info=specs,
        fetched_at=_utcnow().isoformat(),
    )
    for tf in timeframes:
        try:
            hist.by_tf[tf] = fetch_tf_history(mt5, broker, tf)
        except Exception as exc:  # noqa: BLE001
            hist.by_tf[tf] = TfHistory(
                timeframe=tf,
                status="error",
                message=str(exc),
                broker_symbol=broker,
            )

    with _lock:
        _cache[key] = (time.monotonic(), hist)
    return hist


def fetch_universe_history(
    symbols: list[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Carrega histórico para o universo; relatório sem barras brutas."""
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        h = fetch_symbol_history(sym, force=force)
        rows.append(h.as_dict(include_bars=False))
    ok_m15 = sum(1 for r in rows if (r["timeframes"].get("M15") or {}).get("status") == "ok")
    return {
        "generated_at": _utcnow().isoformat(),
        "symbols": rows,
        "summary": {
            "requested": len(symbols),
            "m15_ok": ok_m15,
            "m15_absent": sum(
                1 for r in rows if (r["timeframes"].get("M15") or {}).get("status") == "absent"
            ),
            "m15_error": sum(
                1 for r in rows if (r["timeframes"].get("M15") or {}).get("status") == "error"
            ),
            "m15_unavailable": sum(
                1
                for r in rows
                if (r["timeframes"].get("M15") or {}).get("status") == "unavailable"
            ),
            "target_bars": dict(_TARGET_BARS),
        },
    }


def get_cached_bars(canonical: str, timeframe: str) -> list[dict[str, Any]] | None:
    key = canonical.replace("/", "").upper()
    with _lock:
        hit = _cache.get(key)
        if not hit:
            return None
        tf = hit[1].by_tf.get(timeframe)
        if not tf or tf.status != "ok":
            return None
        return list(tf.bars)
