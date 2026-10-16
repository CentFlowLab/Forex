"""Impressões digitais de experiências — evita duplicados."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def experiment_fingerprint(
    *,
    strategy_key: str,
    params: dict[str, Any],
    start_iso: str,
    end_iso: str,
    timeframe: str,
) -> str:
    payload = {
        "k": strategy_key,
        "p": {k: params[k] for k in sorted(params)},
        "s": start_iso[:16],
        "e": end_iso[:16],
        "tf": timeframe,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
