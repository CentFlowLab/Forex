"""Dukascopy historical tick datafeed (bi5) → OHLC candles.

Public datafeed — no broker account, registration or API key.
Does not fabricate candles when hours are unavailable.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import lzma

from app.market.labels import DUKASCOPY_SOURCE

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
HTTP_BASE_URL = "http://datafeed.dukascopy.com/datafeed"
INSTRUMENT = "EURUSD"
POINT = 100_000  # EURUSD / maioria dos pares
TF_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}

# Escala bi5 por símbolo (JPY = 1000).
_BI5_POINT: dict[str, int] = {
    "EURUSD": 100_000,
    "GBPUSD": 100_000,
    "USDJPY": 1_000,
    "USDCHF": 100_000,
    "AUDUSD": 100_000,
    "USDCAD": 100_000,
    "NZDUSD": 100_000,
}


def bi5_point_for(symbol: str) -> int:
    key = symbol.replace("/", "").upper()
    return int(_BI5_POINT.get(key, POINT))

ProgressCb = Callable[[dict[str, Any]], None]


class DukascopyError(Exception):
    """Raised when the public datafeed cannot satisfy a download request."""


def _hour_path(ts: datetime, symbol: str = INSTRUMENT) -> str:
    ts = ts.astimezone(timezone.utc)
    key = symbol.replace("/", "").upper()
    # Dukascopy months are 0-indexed.
    return (
        f"/{key}/{ts.year}/{ts.month - 1:02d}/"
        f"{ts.day:02d}/{ts.hour:02d}h_ticks.bi5"
    )


def _hour_url(ts: datetime, *, base: str | None = None, symbol: str = INSTRUMENT) -> str:
    return f"{base or BASE_URL}{_hour_path(ts, symbol)}"


def _decompress_bi5(payload: bytes) -> bytes:
    if not payload:
        return b""
    if payload.startswith(b"<html") or payload.startswith(b"<!DOCTYPE"):
        raise DukascopyError("Datafeed returned HTML instead of bi5 ticks.")
    try:
        return lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise DukascopyError(f"Failed to decompress bi5 payload: {exc}") from exc


def parse_bi5_ticks(
    payload: bytes,
    hour_start: datetime,
    *,
    symbol: str = INSTRUMENT,
) -> list[tuple[datetime, float, float]]:
    """Return list of (ts, bid, ask)."""
    raw = _decompress_bi5(payload)
    if len(raw) % 20 != 0:
        raise DukascopyError(f"Unexpected bi5 size {len(raw)} (not multiple of 20).")
    hour_start = hour_start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    point = bi5_point_for(symbol)
    ticks: list[tuple[datetime, float, float]] = []
    for i in range(0, len(raw), 20):
        ms, ask_i, bid_i, _ask_v, _bid_v = struct.unpack(">IIIff", raw[i : i + 20])
        bid = bid_i / point
        ask = ask_i / point
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        ticks.append((hour_start + timedelta(milliseconds=ms), bid, ask))
    return ticks


def fetch_hour_ticks(
    hour_start: datetime,
    client: httpx.Client,
    retries: int = 3,
    *,
    symbol: str = INSTRUMENT,
) -> tuple[list[tuple[datetime, float, float]], str]:
    """Download one hour. Returns (ticks, status) where status is ok|empty|unavailable|error.

    Tenta HTTP (porto 80) e depois HTTPS — nesta rede o :443 do datafeed é instável.
    """
    last_err = ""
    bases = (HTTP_BASE_URL, BASE_URL)
    for attempt in range(retries):
        for base in bases:
            url = _hour_url(hour_start, base=base, symbol=symbol)
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                last_err = str(exc)
                continue
            if response.status_code == 404 or (response.status_code == 200 and not response.content):
                return [], "empty"
            if response.status_code == 503 or response.status_code >= 500:
                last_err = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                last_err = f"http_{response.status_code}"
                continue
            try:
                ticks = parse_bi5_ticks(response.content, hour_start, symbol=symbol)
            except DukascopyError as exc:
                last_err = str(exc)
                continue
            if not ticks:
                return [], "empty"
            return ticks, "ok"
        time.sleep(0.5 * (attempt + 1))
    return [], f"unavailable:{last_err or 'retries_exhausted'}"


def aggregate_ticks_to_candles(
    ticks: list[tuple[datetime, float, float]],
    timeframe: str,
    *,
    symbol: str = INSTRUMENT,
) -> list[dict[str, Any]]:
    minutes = TF_MINUTES[timeframe]
    key = symbol.replace("/", "").upper()
    buckets: dict[datetime, dict[str, Any]] = {}
    for ts, bid, ask in ticks:
        mid = (bid + ask) / 2.0
        epoch_min = int(ts.timestamp() // 60)
        bucket_min = epoch_min - (epoch_min % minutes)
        bucket_ts = datetime.fromtimestamp(bucket_min * 60, tz=timezone.utc)
        bar = buckets.get(bucket_ts)
        if bar is None:
            buckets[bucket_ts] = {
                "symbol": key,
                "timeframe": timeframe,
                "ts": bucket_ts,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "volume": 1.0,
                "source": DUKASCOPY_SOURCE,
                "is_synthetic": False,
            }
        else:
            bar["high"] = max(bar["high"], mid)
            bar["low"] = min(bar["low"], mid)
            bar["close"] = mid
            bar["volume"] += 1.0
    return [buckets[k] for k in sorted(buckets)]


def download_symbol_range(
    start: datetime,
    end: datetime,
    *,
    symbol: str = INSTRUMENT,
    timeframes: list[str] | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Download Dukascopy ticks for one symbol and aggregate to timeframes.

    Never fills gaps with synthetic prices. Unavailable hours are reported.
    """
    key = symbol.replace("/", "").upper()
    start = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise DukascopyError("end must be after start")
    tfs = timeframes or ["M5", "M15", "H1", "H4"]
    for tf in tfs:
        if tf not in TF_MINUTES:
            raise DukascopyError(f"Unsupported timeframe: {tf}")

    hours: list[datetime] = []
    cursor = start
    while cursor < end:
        hours.append(cursor)
        cursor += timedelta(hours=1)

    all_ticks: list[tuple[datetime, float, float]] = []
    hour_stats = {"ok": 0, "empty": 0, "unavailable": 0, "error": 0}
    errors: list[str] = []

    headers = {"User-Agent": "GROK-FOREX-AI/0.2 (local research; historical bi5 multi)"}
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=True) as client:
        for idx, hour in enumerate(hours):
            ticks, status = fetch_hour_ticks(hour, client, symbol=key)
            if status == "ok":
                hour_stats["ok"] += 1
                all_ticks.extend(ticks)
            elif status == "empty":
                hour_stats["empty"] += 1
            elif status.startswith("unavailable") or status.startswith("http_"):
                hour_stats["unavailable"] += 1
                errors.append(f"{hour.isoformat()}: {status}")
            else:
                hour_stats["error"] += 1
                errors.append(f"{hour.isoformat()}: {status}")
            if progress:
                progress(
                    {
                        "phase": "download",
                        "symbol": key,
                        "current": idx + 1,
                        "total": len(hours),
                        "hour": hour.isoformat(),
                        "status": status,
                        "ticks_so_far": len(all_ticks),
                    }
                )

    if not all_ticks:
        raise DukascopyError(
            f"No Dukascopy ticks downloaded for {key}. "
            f"hours={hour_stats}. "
            "Endpoint may be temporarily unavailable — synthetic data was NOT used."
        )

    candles_by_tf: dict[str, list[dict[str, Any]]] = {}
    for tf in tfs:
        candles_by_tf[tf] = [
            c
            for c in aggregate_ticks_to_candles(all_ticks, tf, symbol=key)
            if start <= c["ts"] <= end
        ]

    return {
        "instrument": key,
        "source": DUKASCOPY_SOURCE,
        "is_synthetic": False,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours_requested": len(hours),
        "hour_stats": hour_stats,
        "tick_count": len(all_ticks),
        "candles": candles_by_tf,
        "errors": errors[:50],
        "label": "DADOS HISTÓRICOS REAIS — Dukascopy (negociação apenas simulada)",
    }


def download_eurusd_range(
    start: datetime,
    end: datetime,
    timeframes: list[str] | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Compatibilidade — descarrega EURUSD."""
    return download_symbol_range(
        start, end, symbol="EURUSD", timeframes=timeframes, progress=progress
    )
