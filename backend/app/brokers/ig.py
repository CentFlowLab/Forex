"""IG REST API scaffolding. Endpoints are not pretended to be live."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    ConnectionStatus,
    OrderRequest,
    OrderResult,
    PositionView,
    Quote,
)
from app.config import get_settings


class IGBrokerAdapter(BrokerAdapter):
    """
    Integration scaffold for IG REST API.

    This adapter never silently succeeds. It only reports ready when
    credentials exist AND LIVE flags are set. Network calls are not
    fabricated; methods raise or return explicit not-implemented status.
    """

    name = "ig"
    is_simulated = False

    def __init__(self) -> None:
        self._connected = False

    def _configured(self) -> bool:
        s = get_settings()
        return bool(s.ig_api_key and s.ig_username and s.ig_password)

    def connect(self) -> ConnectionStatus:
        if not self._configured():
            return ConnectionStatus(
                False,
                self.name,
                get_settings().trading_environment,
                "IG credentials missing. Adapter remains disabled.",
                False,
                None,
            )
        if not get_settings().live_authorized:
            return ConnectionStatus(
                False,
                self.name,
                get_settings().trading_environment,
                "IG adapter requires LIVE_TRADING_ENABLED and LIVE_MANUAL_AUTHORIZATION.",
                False,
                None,
            )
        return ConnectionStatus(
            False,
            self.name,
            get_settings().trading_environment,
            "IG REST session handshake is scaffolded but not implemented in this build.",
            False,
            datetime.now(timezone.utc),
        )

    def disconnect(self) -> None:
        self._connected = False

    def status(self) -> ConnectionStatus:
        return self.connect()

    def account(self) -> AccountInfo:
        raise RuntimeError("IG account endpoint is not implemented in this build.")

    def instrument(self, symbol: str) -> dict[str, Any]:
        raise RuntimeError("IG instrument metadata endpoint is not implemented in this build.")

    def candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[dict[str, Any]]:
        raise RuntimeError("IG historical prices endpoint is not implemented in this build.")

    def quote(self, symbol: str) -> Quote:
        raise RuntimeError("IG quote endpoint is not implemented in this build.")

    def submit_order(self, request: OrderRequest) -> OrderResult:
        return OrderResult(
            client_order_id=request.client_order_id,
            broker_order_id="",
            status="REJECTED",
            filled_price=0.0,
            message="IG order submission is scaffolded and disabled.",
            accepted=False,
        )

    def order_status(self, client_order_id: str) -> OrderResult:
        return OrderResult(client_order_id, "", "UNSUPPORTED", 0.0, "IG order status not implemented.", False)

    def positions(self) -> list[PositionView]:
        return []

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return []
