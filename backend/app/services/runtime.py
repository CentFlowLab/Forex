"""In-process runtime flags that persist via AppSetting."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import reload_settings
from app.models.account import AppSetting


def get_flag(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_flag(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def apply_kill_switch(db: Session, active: bool) -> None:
    set_flag(db, "kill_switch", "true" if active else "false")
    settings = reload_settings()
    object.__setattr__(settings, "kill_switch", active)
