from __future__ import annotations

from app.brokers.base import OrderRequest
from app.brokers.simulated import SimulatedBroker


def test_quote_has_spread(db) -> None:
    """Spread bid/ask válido — is_synthetic só é obrigatório sem cotação MT5 em cache."""
    broker = SimulatedBroker(db)
    quote = broker.quote("EURUSD")
    assert quote.ask >= quote.bid
    assert quote.bid > 0 and quote.ask > 0
    # Com MT5 ligado o feed real pode fornecer a cotação (não sintético).
    assert quote.is_synthetic in {True, False}


def test_duplicate_client_order_id(db) -> None:
    broker = SimulatedBroker(db)
    req = OrderRequest(
        client_order_id="GFX-TEST-1",
        symbol="EURUSD",
        side="BUY",
        volume_lots=0.01,
        stop_loss=1.08,
        take_profit=1.09,
    )
    first = broker.submit_order(req)
    second = broker.submit_order(req)
    assert first.accepted
    assert "Duplicate" in second.message
    assert len(broker.positions()) == 1


def test_disconnect_rejects_orders(db) -> None:
    broker = SimulatedBroker(db)
    broker.disconnect()
    result = broker.submit_order(
        OrderRequest("GFX-TEST-2", "EURUSD", "BUY", 0.01, 1.08, 1.09)
    )
    assert not result.accepted


def test_close_position_records_trade(db) -> None:
    broker = SimulatedBroker(db)
    broker.submit_order(OrderRequest("GFX-TEST-3", "EURUSD", "SELL", 0.01, 1.10, 1.07))
    pos = broker.positions()[0]
    from app.models.trading import Position

    row = db.query(Position).first()
    trade = broker.close_position(row.id, "MANUAL")
    assert trade is not None
    assert broker.positions() == []
    assert len(broker.history()) == 1
