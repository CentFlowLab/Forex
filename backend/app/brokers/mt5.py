"""MetaTrader 5 compatibility scaffold. Requires a local MT5 terminal."""

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


class MT5BrokerAdapter(BrokerAdapter):
    """
    Scaffold for MetaTrader 5 via the official MetaTrader5 Python package.

    This build does not import MetaTrader5 at module load time so the
    application remains runnable on machines without the terminal.
    """

    name = "mt5"
    is_simulated = False

    def __init__(self) -> None:
        self._connected = False

    def _configured(self) -> bool:
        s = get_settings()
        return bool(s.mt5_login and s.mt5_password and s.mt5_server)

    def connect(self) -> ConnectionStatus:
        if not self._configured():
            return ConnectionStatus(
                False,
                self.name,
                get_settings().trading_environment,
                "MT5 credentials missing. Adapter remains disabled.",
                False,
                None,
            )
        if not get_settings().live_authorized:
            return ConnectionStatus(
                False,
                self.name,
                get_settings().trading_environment,
                "MT5 adapter requires LIVE_TRADING_ENABLED and LIVE_MANUAL_AUTHORIZATION.",
                False,
                None,
            )
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            return ConnectionStatus(
                False,
                self.name,
                get_settings().trading_environment,
                "MetaTrader5 package is not installed. Install MetaTrader5 and a local terminal.",
                False,
                None,
            )
        _ = mt5
        return ConnectionStatus(
            False,
            self.name,
            get_settings().trading_environment,
            "MT5 initialize/login handshake is scaffolded but not implemented in this build.",
            False,
            datetime.now(timezone.utc),
        )

    def disconnect(self) -> None:
        self._connected = False

    def status(self) -> ConnectionStatus:
        return self.connect()

    def account(self) -> AccountInfo:
        raise RuntimeError("MT5 account_info is not implemented in this build.")

    def instrument(self, symbol: str) -> dict[str, Any]:
        raise RuntimeError("MT5 symbol_info is not implemented in this build.")

    def candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[dict[str, Any]]:
        raise RuntimeError("MT5 copy_rates is not implemented in this build.")

    def quote(self, symbol: str) -> Quote:
        raise RuntimeError("MT5 symbol_info_tick is not implemented in this build.")

    def submit_order(self, request: OrderRequest) -> OrderResult:
        # Único caminho: MT5DemoExecutor (autorização DEMO + trade_mode).
        # Este adapter LIVE nunca envia ordens diretamente.
        try:
            from app.brokers.mt5_demo_executor import protected_order_send

            protected_order_send({"client_order_id": request.client_order_id})
        except RuntimeError as exc:
            return OrderResult(
                client_order_id=request.client_order_id,
                broker_order_id="",
                status="REJECTED",
                filled_price=0.0,
                message=str(exc),
                accepted=False,
            )
        return OrderResult(
            client_order_id=request.client_order_id,
            broker_order_id="",
            status="REJECTED",
            filled_price=0.0,
            message="MT5 order_send bloqueado — use executor DEMO autorizado.",
            accepted=False,
        )

    def order_status(self, client_order_id: str) -> OrderResult:
        return OrderResult(client_order_id, "", "UNSUPPORTED", 0.0, "MT5 order status not implemented.", False)

    def positions(self) -> list[PositionView]:
        return []

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return []
