"""Execution layer. Only receives already-approved decisions."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.brokers.base import OrderRequest
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.models.agent import DecisionRecord
from app.models.audit import AuditLog
from app.risk.engine import RiskDecision


def new_client_order_id(decision_id: int) -> str:
    return f"GFX-{decision_id}-{uuid.uuid4().hex[:12]}"


def execute_approved_decision(
    db: Session,
    decision: DecisionRecord,
    risk: RiskDecision,
    symbol: str,
    side: str,
    stop_loss: float,
    take_profit: float | None,
) -> dict[str, Any]:
    settings = get_settings()
    if settings.kill_switch:
        return {"accepted": False, "message": "Paragem de emergência ativa."}
    if not risk.approved:
        return {"accepted": False, "message": "O motor de risco rejeitou a operação."}
    if settings.trading_environment == "LIVE" and not settings.live_authorized:
        return {"accepted": False, "message": "A negociação real está desativada."}
    if settings.trading_environment == "LIVE":
        return {
            "accepted": False,
            "message": "A execução LIVE não está ativada nesta versão. Utilize PAPER/BACKTEST.",
        }

    broker = get_broker(db)
    if not isinstance(broker, SimulatedBroker):
        return {
            "accepted": False,
            "message": "Apenas a corretora simulada pode executar nesta versão.",
        }

    client_id = new_client_order_id(decision.id)
    result = broker.submit_order(
        OrderRequest(
            client_order_id=client_id,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            volume_lots=risk.lots,
            stop_loss=stop_loss,
            take_profit=take_profit or 0.0,
        )
    )
    decision.executed = result.accepted
    decision.client_order_id = client_id
    db.add(
        AuditLog(
            category="execution",
            level="INFO" if result.accepted else "ERROR",
            message=f"Order {client_id} {result.status}",
            details=result.message,
        )
    )
    db.commit()
    return {
        "accepted": result.accepted,
        "client_order_id": result.client_order_id,
        "broker_order_id": result.broker_order_id,
        "status": result.status,
        "filled_price": result.filled_price,
        "message": result.message,
        "is_simulated": True,
    }
