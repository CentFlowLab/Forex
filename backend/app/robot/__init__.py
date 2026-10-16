"""Robô Forex automático em simulação (painel único)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.robot.service import RobotService


def get_robot_service() -> RobotService:
    from app.robot.service import get_robot_service as _get

    return _get()
