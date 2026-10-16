"""Fully functional simulated broker. Used for BACKTEST and PAPER."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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
from app.models.account import Account
from app.models.market import Candle
from app.models.trading import ClosedTrade, Order, Position
from app.risk.instruments import get_instrument


class SimulatedBroker(BrokerAdapter):
    name = "simulated"
    is_simulated = True

    def __init__(self, db: Session):
        self.db = db
        self._connected = True
        self._heartbeat = datetime.now(timezone.utc)
        # Optional mid override used by historical replay (never live prices).
        # Replay partilhado por processo: get_broker() devolve novas instâncias.
        self._quote_mid: float | None = None
        self._quote_ts: datetime | None = None
        self._quote_source: str = "synthetic"
        self._quote_synthetic: bool = True

    # Estado de replay ao nível da classe — sobrevive a novas instâncias SimulatedBroker(db).
    _replay_mid: float | None = None
    _replay_ts: datetime | None = None
    _replay_source: str = "synthetic"
    _replay_synthetic: bool = True

    def set_replay_quote(
        self,
        mid: float,
        ts: datetime,
        source: str = "dukascopy",
        is_synthetic: bool = False,
    ) -> None:
        SimulatedBroker._replay_mid = float(mid)
        SimulatedBroker._replay_ts = ts
        SimulatedBroker._replay_source = source
        SimulatedBroker._replay_synthetic = is_synthetic
        self._quote_mid = float(mid)
        self._quote_ts = ts
        self._quote_source = source
        self._quote_synthetic = is_synthetic

    def clear_replay_quote(self) -> None:
        SimulatedBroker._replay_mid = None
        SimulatedBroker._replay_ts = None
        self._quote_mid = None
        self._quote_ts = None

    def _account_row(self) -> Account:
        row = self.db.scalar(select(Account).limit(1))
        if row is None:
            settings = get_settings()
            row = Account(
                name="SIMULATED",
                currency=settings.account_currency,
                environment=settings.trading_environment,
                starting_equity=settings.starting_equity,
                balance=settings.starting_equity,
                equity=settings.starting_equity,
                peak_equity=settings.starting_equity,
                is_simulated=True,
            )
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    def connect(self) -> ConnectionStatus:
        self._connected = True
        self._heartbeat = datetime.now(timezone.utc)
        return self.status()

    def disconnect(self) -> None:
        self._connected = False

    def status(self) -> ConnectionStatus:
        settings = get_settings()
        return ConnectionStatus(
            connected=self._connected,
            adapter=self.name,
            environment=settings.trading_environment,
            message=(
                "Corretora simulada ligada" if self._connected else "Corretora simulada desligada"
            ),
            is_simulated=True,
            last_heartbeat=self._heartbeat,
        )

    def account(self) -> AccountInfo:
        row = self._account_row()
        return AccountInfo(
            balance=row.balance,
            equity=row.equity,
            currency=row.currency,
            unrealized_pnl=row.unrealized_pnl,
            margin_used=0.0,
            is_simulated=True,
        )

    def instrument(self, symbol: str) -> dict[str, Any]:
        spec = get_instrument(symbol)
        return spec.__dict__ | {"source": "simulated-metadata"}

    def _last_candle(self, symbol: str, timeframe: str = "M15") -> Candle | None:
        return self.db.scalar(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
            .order_by(Candle.ts.desc())
            .limit(1)
        )

    def candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = list(
            self.db.scalars(
                select(Candle)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.ts.desc())
                .limit(limit)
            )
        )
        rows.reverse()
        return [
            {
                "ts": c.ts.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "source": c.source,
                "is_synthetic": c.is_synthetic,
            }
            for c in rows
        ]

    def quote(self, symbol: str) -> Quote:
        settings = get_settings()
        spec = get_instrument(symbol)
        replay_mid = self._quote_mid if self._quote_mid is not None else SimulatedBroker._replay_mid
        if replay_mid is not None:
            mid = float(replay_mid)
            ts = (
                self._quote_ts
                or SimulatedBroker._replay_ts
                or datetime.now(timezone.utc)
            )
            is_synthetic = (
                self._quote_synthetic
                if self._quote_mid is not None
                else SimulatedBroker._replay_synthetic
            )
            source = (
                self._quote_source
                if self._quote_mid is not None
                else SimulatedBroker._replay_source
            )
            half = (settings.sim_spread_pips / 2.0) * spec.pip_size
            return Quote(
                symbol=symbol,
                bid=round(mid - half, spec.digits),
                ask=round(mid + half, spec.digits),
                spread_pips=settings.sim_spread_pips,
                ts=ts,
                is_synthetic=is_synthetic,
                source=source,
            )

        # Preferir cotação MT5 do par (fonte principal). Sem order_send.
        key = symbol.upper().replace("/", "")
        try:
            from app.market.mt5_feed import get_mt5_feed

            mt5_q = get_mt5_feed().get_quote(key)
        except Exception:
            mt5_q = None
        if mt5_q is not None and mt5_q.feed_state != "ligacao_perdida":
            return Quote(
                symbol=symbol,
                bid=round(mt5_q.bid, spec.digits),
                ask=round(mt5_q.ask, spec.digits),
                spread_pips=mt5_q.spread_pips,
                ts=mt5_q.tick_time,
                is_synthetic=False,
                source="mt5",
            )
        try:
            from app.robot.pair_quotes import get_pair_quote_store

            cached = get_pair_quote_store().get(key)
        except Exception:
            cached = None
        if cached is not None and cached.source == "mt5" and cached.feed_state != "ligacao_perdida":
            return Quote(
                symbol=symbol,
                bid=round(cached.tick.bid, spec.digits),
                ask=round(cached.tick.ask, spec.digits),
                spread_pips=float(cached.tick.spread_pips or settings.sim_spread_pips),
                ts=cached.tick.ts,
                is_synthetic=False,
                source="mt5",
            )

        # Fallback Dukascopy verificado (EURUSD) — histórico complementar.
        if key in {"EURUSD", "EUR/USD"}:
            try:
                from app.market.quote_feed import get_quote_feed

                verified = get_quote_feed().verified_quote()
            except Exception:
                verified = None
            if verified is not None:
                spread = verified.spread_pips
                if spread is None:
                    half = (settings.sim_spread_pips / 2.0) * spec.pip_size
                    return Quote(
                        symbol=symbol,
                        bid=round(verified.mid - half, spec.digits),
                        ask=round(verified.mid + half, spec.digits),
                        spread_pips=settings.sim_spread_pips,
                        ts=verified.ts,
                        is_synthetic=False,
                        source="dukascopy",
                    )
                return Quote(
                    symbol=symbol,
                    bid=round(verified.bid, spec.digits),
                    ask=round(verified.ask, spec.digits),
                    spread_pips=spread,
                    ts=verified.ts,
                    is_synthetic=False,
                    source="dukascopy",
                )

        candle = self._last_candle(symbol)
        mid = candle.close if candle else 1.08500
        ts = candle.ts if candle else datetime.now(timezone.utc)
        is_synthetic = bool(candle.is_synthetic) if candle else True
        source = candle.source if candle else "synthetic"
        half = (settings.sim_spread_pips / 2.0) * spec.pip_size
        return Quote(
            symbol=symbol,
            bid=round(mid - half, spec.digits),
            ask=round(mid + half, spec.digits),
            spread_pips=settings.sim_spread_pips,
            ts=ts,
            is_synthetic=is_synthetic,
            source=source,
        )

    def submit_order(self, request: OrderRequest) -> OrderResult:
        existing = self.db.scalar(select(Order).where(Order.client_order_id == request.client_order_id))
        if existing:
            return OrderResult(
                client_order_id=request.client_order_id,
                broker_order_id=existing.broker_order_id,
                status=existing.status,
                filled_price=existing.filled_price,
                message="Duplicate client_order_id ignored",
                accepted=existing.status == "FILLED",
            )
        if not self._connected:
            return self._reject(request, "Broker disconnected")

        settings = get_settings()
        delay = max(0, settings.sim_execution_delay_ms) / 1000.0
        if delay:
            time.sleep(min(delay, 0.5))

        spec = get_instrument(request.symbol)
        quote = self.quote(request.symbol)
        slip = settings.sim_slippage_pips * spec.pip_size
        if request.side == "BUY":
            fill = quote.ask + slip
        else:
            fill = quote.bid - slip

        now = datetime.now(timezone.utc)
        order = Order(
            client_order_id=request.client_order_id,
            broker_order_id=f"SIM-{request.client_order_id}",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            volume_lots=request.volume_lots,
            requested_price=request.limit_price or 0.0,
            filled_price=round(fill, spec.digits),
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            status="FILLED",
            environment=settings.trading_environment,
            is_simulated=True,
            created_at=now,
            updated_at=now,
        )
        self.db.add(order)
        position = Position(
            symbol=request.symbol,
            side=request.side,
            volume_lots=request.volume_lots,
            entry_price=order.filled_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            current_price=order.filled_price,
            environment=settings.trading_environment,
            is_simulated=True,
            client_order_id=request.client_order_id,
            opened_at=now,
        )
        self.db.add(position)
        account = self._account_row()
        # Comissão cobrada uma única vez no fecho (evita round-trip 2×).
        self._mark_to_market()
        self.db.commit()
        return OrderResult(
            client_order_id=request.client_order_id,
            broker_order_id=order.broker_order_id,
            status="FILLED",
            filled_price=order.filled_price,
            message="Execução simulada",
            accepted=True,
        )

    def _reject(self, request: OrderRequest, reason: str) -> OrderResult:
        return OrderResult(
            client_order_id=request.client_order_id,
            broker_order_id="",
            status="REJECTED",
            filled_price=0.0,
            message=reason,
            accepted=False,
        )

    def order_status(self, client_order_id: str) -> OrderResult:
        order = self.db.scalar(select(Order).where(Order.client_order_id == client_order_id))
        if not order:
            return OrderResult(client_order_id, "", "NOT_FOUND", 0.0, "Unknown order", False)
        return OrderResult(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            status=order.status,
            filled_price=order.filled_price,
            message=order.reject_reason or order.status,
            accepted=order.status == "FILLED",
        )

    def positions(self) -> list[PositionView]:
        self._mark_to_market()
        rows = list(self.db.scalars(select(Position)))
        return [
            PositionView(
                symbol=p.symbol,
                side=p.side,
                volume_lots=p.volume_lots,
                entry_price=p.entry_price,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                current_price=p.current_price,
                unrealized_pnl=p.unrealized_pnl,
                client_order_id=p.client_order_id,
            )
            for p in rows
        ]

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = list(self.db.scalars(select(ClosedTrade).order_by(ClosedTrade.closed_at.desc()).limit(limit)))
        return [
            {
                "symbol": t.symbol,
                "side": t.side,
                "volume_lots": t.volume_lots,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "pips": t.pips,
                "commission": t.commission,
                "close_reason": t.close_reason,
                "is_simulated": t.is_simulated,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat(),
            }
            for t in rows
        ]

    def _mark_to_market(self) -> None:
        account = self._account_row()
        total_upl = 0.0
        for pos in self.db.scalars(select(Position)):
            spec = get_instrument(pos.symbol)
            quote = self.quote(pos.symbol)
            px = quote.bid if pos.side == "BUY" else quote.ask
            pos.current_price = px
            pos.unrealized_pnl = spec.pnl(pos.side, pos.entry_price, px, pos.volume_lots, account.currency)
            total_upl += pos.unrealized_pnl
        account.unrealized_pnl = total_upl
        account.equity = account.balance + total_upl
        if account.equity > account.peak_equity:
            account.peak_equity = account.equity
        if account.peak_equity > 0:
            dd = (account.peak_equity - account.equity) / account.peak_equity * 100.0
            account.max_drawdown_pct = max(account.max_drawdown_pct, dd)
        account.updated_at = datetime.now(timezone.utc)

    def close_position(self, position_id: int, reason: str = "MANUAL") -> ClosedTrade | None:
        pos = self.db.get(Position, position_id)
        if not pos:
            return None
        settings = get_settings()
        spec = get_instrument(pos.symbol)
        quote = self.quote(pos.symbol)
        exit_px = quote.bid if pos.side == "BUY" else quote.ask
        account = self._account_row()
        pnl = spec.pnl(pos.side, pos.entry_price, exit_px, pos.volume_lots, account.currency)
        commission = settings.sim_commission_per_lot * pos.volume_lots
        now = datetime.now(timezone.utc)
        trade = ClosedTrade(
            symbol=pos.symbol,
            side=pos.side,
            volume_lots=pos.volume_lots,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            pnl=pnl - commission,
            commission=commission,
            pips=spec.pips_from_price_delta((exit_px - pos.entry_price) * (1 if pos.side == "BUY" else -1)),
            environment=pos.environment,
            is_simulated=True,
            close_reason=reason,
            client_order_id=pos.client_order_id,
            opened_at=pos.opened_at,
            closed_at=now,
        )
        self.db.add(trade)
        account.balance += pnl - commission
        account.realized_pnl += pnl - commission
        today = now.date().isoformat()
        if account.daily_pnl_date != today:
            account.daily_realized_pnl = 0.0
            account.daily_pnl_date = today
        account.daily_realized_pnl += pnl - commission
        self.db.delete(pos)
        self._mark_to_market()
        self.db.commit()
        return trade

    def apply_stops(self) -> list[int]:
        closed: list[int] = []
        for pos in list(self.db.scalars(select(Position))):
            quote = self.quote(pos.symbol)
            hit_sl = False
            hit_tp = False
            if pos.side == "BUY":
                hit_sl = pos.stop_loss > 0 and quote.bid <= pos.stop_loss
                hit_tp = pos.take_profit > 0 and quote.bid >= pos.take_profit
            else:
                hit_sl = pos.stop_loss > 0 and quote.ask >= pos.stop_loss
                hit_tp = pos.take_profit > 0 and quote.ask <= pos.take_profit
            if hit_sl or hit_tp:
                closed.append(pos.id)
                self.close_position(pos.id, "STOP_LOSS" if hit_sl else "TAKE_PROFIT")
        return closed
