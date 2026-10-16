"""Controlo de cancelamento de análises manuais (trigger não automático)."""

from __future__ import annotations

import threading
from typing import Set

_lock = threading.Lock()
_cancel_ids: Set[int] = set()
_active_id: int | None = None


def set_active(run_id: int | None) -> None:
    global _active_id
    with _lock:
        _active_id = run_id


def request_cancel(run_id: int) -> None:
    with _lock:
        _cancel_ids.add(run_id)


def clear_cancel(run_id: int) -> None:
    with _lock:
        _cancel_ids.discard(run_id)


def is_cancelled(run_id: int) -> bool:
    with _lock:
        return run_id in _cancel_ids


def active_run_id() -> int | None:
    with _lock:
        return _active_id
