from __future__ import annotations

from sqlalchemy.orm import Session

from app.brokers.base import BrokerAdapter
from app.brokers.ig import IGBrokerAdapter
from app.brokers.mt5 import MT5BrokerAdapter
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings


def get_broker(db: Session, name: str | None = None) -> BrokerAdapter:
    settings = get_settings()
    chosen = (name or "simulated").lower()
    if settings.trading_environment != "LIVE":
        return SimulatedBroker(db)
    if not settings.live_authorized:
        return SimulatedBroker(db)
    if chosen == "ig":
        return IGBrokerAdapter()
    if chosen == "mt5":
        return MT5BrokerAdapter()
    return SimulatedBroker(db)
