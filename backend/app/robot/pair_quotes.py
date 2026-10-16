"""Cache de cotações por par — MT5 principal; Dukascopy complementar."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.market.quote_types import ValidatedTick


@dataclass
class PairQuote:
    symbol: str
    tick: ValidatedTick
    received_at: datetime
    is_synthetic: bool = False
    source: str = "dukascopy_bi5"
    feed_state: str = "atrasado"
    claims_realtime: bool = False
    block_entries: bool = True
    tick_time_lag_s: float | None = None
    tick_msc_lag_s: float | None = None
    update_age_s: float | None = None
    clock_anomaly: bool = False
    clock_offset_hint_s: float | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def lag_seconds(self) -> float:
        if self.update_age_s is not None and (self.clock_anomaly or self.source == "mt5"):
            # Com anomalia de relógio, o atraso operacional é a idade da atualização.
            if self.tick_time_lag_s is not None and self.tick_time_lag_s >= -5.0:
                return max(0.0, self.tick_time_lag_s)
            return max(0.0, self.update_age_s)
        now = datetime.now(timezone.utc)
        return (now - self.tick.ts).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.tick.bid,
            "ask": self.tick.ask,
            "mid": self.tick.mid,
            "spread_pips": self.tick.spread_pips,
            "ts": self.tick.ts.isoformat(),
            "lag_seconds": self.lag_seconds,
            "tick_time_lag_s": self.tick_time_lag_s,
            "tick_msc_lag_s": self.tick_msc_lag_s,
            "update_age_s": self.update_age_s,
            "channel": self.tick.channel,
            "is_synthetic": self.is_synthetic,
            "source": self.source,
            "feed_state": self.feed_state,
            "claims_realtime": self.claims_realtime,
            "block_entries": self.block_entries,
            "clock_anomaly": self.clock_anomaly,
            "clock_offset_hint_s": self.clock_offset_hint_s,
            "message": self.message,
        }


class PairQuoteStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, PairQuote] = {}

    def set(
        self,
        symbol: str,
        tick: ValidatedTick,
        *,
        is_synthetic: bool = False,
        source: str = "dukascopy_bi5",
    ) -> None:
        key = symbol.replace("/", "").upper()
        with self._lock:
            existing = self._quotes.get(key)
            # Não sobrescrever cotação MT5 recente com bi5 atrasado.
            if (
                existing is not None
                and existing.source == "mt5"
                and existing.feed_state in {"recente", "atrasado"}
                and source.startswith("dukascopy")
            ):
                return
            self._quotes[key] = PairQuote(
                symbol=key,
                tick=tick,
                received_at=datetime.now(timezone.utc),
                is_synthetic=is_synthetic,
                source=source,
                feed_state="atrasado" if source.startswith("dukascopy") else "recente",
                claims_realtime=False,
                block_entries=True,
            )

    def set_mt5(self, symbol: str, q: Any) -> None:
        key = symbol.replace("/", "").upper()
        tick = ValidatedTick(
            ts=q.tick_time,
            bid=q.bid,
            ask=q.ask,
            mid=q.mid,
            spread_pips=q.spread_pips,
            channel="mt5",
            mid_only=False,
        )
        with self._lock:
            self._quotes[key] = PairQuote(
                symbol=key,
                tick=tick,
                received_at=q.received_at,
                is_synthetic=False,
                source="mt5",
                feed_state=q.feed_state,
                claims_realtime=q.claims_realtime,
                block_entries=q.block_entries,
                tick_time_lag_s=q.tick_time_lag_s,
                tick_msc_lag_s=q.tick_msc_lag_s,
                update_age_s=q.update_age_s,
                clock_anomaly=q.clock_anomaly,
                clock_offset_hint_s=q.clock_offset_hint_s,
                message=q.message,
            )

    def get(self, symbol: str) -> PairQuote | None:
        key = symbol.replace("/", "").upper()
        with self._lock:
            return self._quotes.get(key)

    def clear(self) -> None:
        with self._lock:
            self._quotes.clear()

    def all(self) -> dict[str, PairQuote]:
        with self._lock:
            return dict(self._quotes)


_store: PairQuoteStore | None = None
_store_lock = threading.Lock()


def get_pair_quote_store() -> PairQuoteStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = PairQuoteStore()
        return _store
