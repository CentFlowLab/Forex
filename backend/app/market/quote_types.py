"""Tipos partilhados do serviço de cotações EUR/USD."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

QuoteStatus = Literal["atualizado", "com_atraso", "obsoleto", "desligado"]
FeedChannel = Literal["dukascopy_tcp", "dukascopy_bi5", "mt5", "none"]

STATUS_LABELS_PT: dict[str, str] = {
    "atualizado": "Atualizado",
    "com_atraso": "Com atraso",
    "obsoleto": "Obsoleto",
    "desligado": "Desligado",
}


@dataclass(frozen=True)
class ValidatedTick:
    ts: datetime
    bid: float
    ask: float
    mid: float
    spread_pips: float | None
    channel: FeedChannel
    mid_only: bool = False
    volume: float | None = None


@dataclass
class QuoteFeedSnapshot:
    connected: bool
    status: QuoteStatus
    status_label: str
    channel: FeedChannel
    source_label: str
    symbol: str = "EURUSD"
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread_pips: float | None = None
    quote_ts: datetime | None = None
    received_at: datetime | None = None
    lag_seconds: float | None = None
    update_interval_hint_seconds: float | None = None
    mid_only: bool = False
    is_synthetic: bool = False
    block_entries: bool = True
    message: str = ""
    tick_count: int = 0
    reconnect_attempts: int = 0
    last_error: str = ""
    candles_updated: dict[str, int] = field(default_factory=dict)
    # historico_horario_atrasado | atrasado | recente_tcp | obsoleto | indisponivel
    data_class: str = "indisponivel"
    claims_realtime: bool = False

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "status": self.status,
            "status_label": self.status_label,
            "channel": self.channel,
            "source_label": self.source_label,
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "spread_pips": self.spread_pips,
            "quote_ts": self.quote_ts.isoformat() if self.quote_ts else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "lag_seconds": self.lag_seconds,
            "update_interval_hint_seconds": self.update_interval_hint_seconds,
            "mid_only": self.mid_only,
            "is_synthetic": self.is_synthetic,
            "block_entries": self.block_entries,
            "message": self.message,
            "tick_count": self.tick_count,
            "reconnect_attempts": self.reconnect_attempts,
            "last_error": self.last_error,
            "candles_updated": self.candles_updated,
            "data_class": self.data_class,
            # Nunca rotular bi5/atrasado como tempo real.
            "claims_realtime": bool(self.claims_realtime),
        }
