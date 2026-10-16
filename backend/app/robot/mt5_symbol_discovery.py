"""Descoberta de símbolos MT5 para o universo Forex 28.

Resolve prefixos/sufixos do broker sem assumir que os 28 pares existem.
Não envia ordens.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.robot.universe import FOREX_28_SYMBOLS, META_BY_SYMBOL, normalize_symbol_key

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_cache: "UniverseDiscovery | None" = None
_cache_mono: float = 0.0
_CACHE_TTL_S = 60.0

# Sufixos/prefixos comuns em brokers MT5 retail.
_STRIP_TOKENS = (
    "PRO",
    "RAW",
    "ECN",
    "STD",
    "MINI",
    "MICRO",
    "SB",
    "I",
    "A",
    "M",
    "FX",
)


@dataclass
class ResolvedSymbol:
    canonical: str
    broker_symbol: str
    display: str
    tradeable: bool
    reason: str = ""
    spread_points: float | None = None
    digits: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "broker_symbol": self.broker_symbol,
            "display": self.display,
            "tradeable": self.tradeable,
            "reason": self.reason,
            "spread_points": self.spread_points,
            "digits": self.digits,
        }


@dataclass
class UniverseDiscovery:
    found: list[ResolvedSymbol] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    mt5_available: bool = False
    scanned_broker_symbols: int = 0
    observed_at_mono: float = 0.0

    @property
    def eligible(self) -> list[ResolvedSymbol]:
        return [r for r in self.found if r.tradeable]

    @property
    def eligible_canonical(self) -> list[str]:
        return [r.canonical for r in self.eligible]

    def broker_name(self, canonical: str) -> str | None:
        for r in self.eligible:
            if r.canonical == canonical:
                return r.broker_symbol
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mt5_available": self.mt5_available,
            "universe_target": 28,
            "scanned_broker_symbols": self.scanned_broker_symbols,
            "found": len(self.found),
            "eligible": len(self.eligible),
            "missing": len(self.missing),
            "excluded": len(self.excluded),
            "eligible_symbols": self.eligible_canonical,
            "found_detail": [r.as_dict() for r in self.found],
            "missing_detail": list(self.missing),
            "excluded_detail": list(self.excluded)[:40],
        }


def _strip_to_canonical(name: str) -> str | None:
    """Tenta mapear nome do broker → canónico de 6 letras."""
    key = normalize_symbol_key(name)
    if not key:
        return None
    if key in META_BY_SYMBOL:
        return key
    # Remover tokens conhecidos iterativamente.
    cleaned = key
    for _ in range(4):
        progressed = False
        for tok in _STRIP_TOKENS:
            if cleaned.startswith(tok) and cleaned[len(tok) :] in META_BY_SYMBOL:
                return cleaned[len(tok) :]
            if cleaned.endswith(tok) and cleaned[: -len(tok)] in META_BY_SYMBOL:
                return cleaned[: -len(tok)]
            if cleaned.endswith(tok):
                cleaned2 = cleaned[: -len(tok)]
                if cleaned2 != cleaned:
                    cleaned = cleaned2
                    progressed = True
        if not progressed:
            break
        if cleaned in META_BY_SYMBOL:
            return cleaned
    # Procurar substring canónica de 6 letras.
    for canon in FOREX_28_SYMBOLS:
        if canon in key:
            return canon
    # Padrão ISO: 6 letras de moeda.
    m = re.search(
        r"(EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY)(EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY)",
        key,
    )
    if m:
        cand = m.group(1) + m.group(2)
        if cand in META_BY_SYMBOL:
            return cand
    return None


def _trade_mode_ok(info: Any) -> tuple[bool, str]:
    """Aceitar modos que permitem trading; excluir disabled."""
    # MetaTrader5: SYMBOL_TRADE_MODE_DISABLED = 0
    mode = getattr(info, "trade_mode", None)
    if mode is None:
        return True, ""
    try:
        mode_i = int(mode)
    except (TypeError, ValueError):
        return True, ""
    if mode_i == 0:
        return False, "trade_mode=disabled"
    return True, ""


def discover_forex_universe(mt5: Any | None = None, *, force: bool = False) -> UniverseDiscovery:
    """Descobre quais dos 28 cruzamentos existem e são negociáveis no MT5."""
    global _cache, _cache_mono
    now = time.monotonic()
    with _lock:
        if (
            not force
            and _cache is not None
            and (now - _cache_mono) < _CACHE_TTL_S
        ):
            return _cache

    result = UniverseDiscovery(observed_at_mono=now)
    if mt5 is None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:  # noqa: BLE001
            result.mt5_available = False
            for sym in FOREX_28_SYMBOLS:
                result.missing.append({"canonical": sym, "reason": "MT5 módulo indisponível"})
            with _lock:
                _cache = result
                _cache_mono = now
            return result

    try:
        term = mt5.terminal_info()
        if term is None:
            result.mt5_available = False
            for sym in FOREX_28_SYMBOLS:
                result.missing.append({"canonical": sym, "reason": "terminal_info None"})
            with _lock:
                _cache = result
                _cache_mono = now
            return result
    except Exception as exc:  # noqa: BLE001
        result.mt5_available = False
        for sym in FOREX_28_SYMBOLS:
            result.missing.append({"canonical": sym, "reason": f"terminal: {exc}"})
        with _lock:
            _cache = result
            _cache_mono = now
        return result

    result.mt5_available = True
    broker_names: list[str] = []
    try:
        symbols = mt5.symbols_get()
        if symbols:
            broker_names = [str(s.name) for s in symbols if getattr(s, "name", None)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("symbols_get falhou: %s", exc)

    result.scanned_broker_symbols = len(broker_names)
    mapped: dict[str, str] = {}
    for bname in broker_names:
        canon = _strip_to_canonical(bname)
        if not canon:
            continue
        # Preferir nome mais curto (menos sufixo) se houver colisão.
        prev = mapped.get(canon)
        if prev is None or len(bname) < len(prev):
            mapped[canon] = bname

    for canon in FOREX_28_SYMBOLS:
        meta = META_BY_SYMBOL[canon]
        bname = mapped.get(canon)
        if not bname:
            # Tentativa directa (alguns brokers só expõem após select).
            for candidate in (canon, f"{canon}m", f"{canon}.a", f"{canon}pro"):
                try:
                    info = mt5.symbol_info(candidate)
                except Exception:  # noqa: BLE001
                    info = None
                if info is not None:
                    bname = candidate
                    break
        if not bname:
            result.missing.append({"canonical": canon, "reason": "símbolo não encontrado no terminal"})
            continue
        try:
            mt5.symbol_select(bname, True)
        except Exception:  # noqa: BLE001
            pass
        try:
            info = mt5.symbol_info(bname)
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            result.excluded.append(
                {"canonical": canon, "broker_symbol": bname, "reason": "symbol_info None"}
            )
            continue
        ok_mode, mode_reason = _trade_mode_ok(info)
        if not ok_mode:
            result.excluded.append(
                {"canonical": canon, "broker_symbol": bname, "reason": mode_reason}
            )
            continue
        try:
            tick = mt5.symbol_info_tick(bname)
        except Exception:  # noqa: BLE001
            tick = None
        if tick is None or float(getattr(tick, "bid", 0) or 0) <= 0:
            result.excluded.append(
                {
                    "canonical": canon,
                    "broker_symbol": bname,
                    "reason": "sem cotação bid/ask válida",
                }
            )
            continue
        spread = None
        try:
            spread = float(getattr(info, "spread", None) or 0) or None
        except (TypeError, ValueError):
            spread = None
        digits = getattr(info, "digits", None)
        result.found.append(
            ResolvedSymbol(
                canonical=canon,
                broker_symbol=bname,
                display=meta.display,
                tradeable=True,
                reason="ok",
                spread_points=spread,
                digits=int(digits) if digits is not None else None,
            )
        )

    with _lock:
        _cache = result
        _cache_mono = now
    return result


def get_cached_discovery() -> UniverseDiscovery | None:
    with _lock:
        return _cache


def broker_symbol_for(canonical: str, mt5: Any | None = None) -> str:
    """Nome no broker ou o canónico se desconhecido."""
    disc = get_cached_discovery()
    if disc is None:
        disc = discover_forex_universe(mt5)
    name = disc.broker_name(canonical)
    return name or canonical


def symbols_for_radar(*, prefer_mt5: bool = True) -> list[str]:
    """Lista canónica a monitorizar: elegíveis MT5, ou os 28 se MT5 offline."""
    if prefer_mt5:
        disc = get_cached_discovery()
        if disc is None:
            disc = discover_forex_universe()
        if disc.mt5_available and disc.eligible:
            return list(disc.eligible_canonical)
    return list(FOREX_28_SYMBOLS)


def reset_discovery_cache() -> None:
    global _cache, _cache_mono
    with _lock:
        _cache = None
        _cache_mono = 0.0
