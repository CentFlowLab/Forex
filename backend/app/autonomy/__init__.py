"""Pacote do motor autónomo (investigação contínua + paper)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.autonomy.service import AutonomyService


def get_autonomy_service() -> AutonomyService:
    from app.autonomy.service import get_autonomy_service as _get

    return _get()
