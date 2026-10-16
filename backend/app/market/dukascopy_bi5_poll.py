"""Poller do datafeed bi5 Dukascopy para a hora mais recente disponível.

Os ficheiros `HHh_ticks.bi5` são históricos por hora — NÃO são um feed em tempo real.
A hora em curso costuma devolver 404 até estar completa. Sem conta, sem chave API.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.market.dukascopy import fetch_hour_ticks
from app.market.quote_types import ValidatedTick
from app.market.tick_validation import TickValidationError, validate_tick

logger = logging.getLogger(__name__)


def latest_tick_from_hours(
    client: httpx.Client,
    *,
    lookback_hours: int = 6,
    now: datetime | None = None,
    symbol: str = "EURUSD",
) -> tuple[ValidatedTick | None, list[tuple[datetime, float, float]], dict[str, Any]]:
    """Procura a hora bi5 mais recente com ticks válidos.

    Não fabrica dados se todas as horas falharem.
    """
    now = now or datetime.now(timezone.utc)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    key = symbol.replace("/", "").upper()
    meta: dict[str, Any] = {"attempts": [], "now": now.isoformat(), "symbol": key}

    for delta in range(0, lookback_hours + 1):
        hour = current_hour - timedelta(hours=delta)
        t0 = time.perf_counter()
        ticks, status = fetch_hour_ticks(hour, client, retries=2, symbol=key)
        ms = round((time.perf_counter() - t0) * 1000)
        meta["attempts"].append({"hour": hour.isoformat(), "status": status, "ms": ms, "ticks": len(ticks)})
        if status != "ok" or not ticks:
            continue
        last_ts, bid, ask = ticks[-1]
        try:
            validated = validate_tick(
                ts=last_ts,
                bid=bid,
                ask=ask,
                channel="dukascopy_bi5",
                now=now,
                symbol=key,
            )
        except TickValidationError as exc:
            meta["attempts"][-1]["validation_error"] = str(exc)
            continue
        meta["selected_hour"] = hour.isoformat()
        meta["tick_count_hour"] = len(ticks)
        meta["last_tick_ts"] = last_ts.isoformat()
        meta["lag_seconds"] = (now - last_ts).total_seconds()
        return validated, ticks, meta

    return None, [], meta
