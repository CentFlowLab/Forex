"""Timestamps UTC coerentes para o motor autónomo (SQLite + memória)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Instante atual em UTC com tzinfo explícita."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | str | None) -> datetime | None:
    """
    Normaliza para UTC aware.

    Política para datas antigas SQLite sem timezone: tratar como UTC
    (as escritas do motor foram sempre em UTC; o driver SQLite devolve naive).
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return as_utc(dt)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime | str | None) -> str | None:
    """Serialização ISO-8601 sempre com offset UTC."""
    dt = as_utc(value)
    if dt is None:
        return None
    return dt.isoformat()


def ensure_aware_utc(value: Any) -> datetime:
    """Igual a as_utc, mas exige valor e falha com TypeError se inválido."""
    dt = as_utc(value)
    if dt is None:
        raise TypeError("timestamp em falta")
    return dt
