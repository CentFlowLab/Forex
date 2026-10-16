"""GROK FOREX AI FastAPI application. Bound to localhost by default."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.api.routes_adaptive import router as adaptive_router
from app.api.routes_ai import router as ai_router
from app.api.routes_autonomy import router as autonomy_router
from app.api.routes_core import router
from app.api.routes_historical import router as historical_router
from app.api.routes_robot import router as robot_router
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.replay.engine import replay_controller
from app.services.seed import seed_all


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_all(db)
        from app.brokers.mt5_demo_auth import clear_auth_on_startup

        clear_auth_on_startup(db)
    finally:
        db.close()
    # Feed MT5 (principal) + Dukascopy complementar em background (não bloqueia arranque).
    import logging
    import threading

    from app.market.mt5_feed import get_mt5_feed
    from app.market.quote_feed import get_quote_feed

    _log = logging.getLogger("app.startup")
    mt5_feed = get_mt5_feed()
    mt5_snap = mt5_feed.start()
    _log.info(
        "MT5 feed arranque: init=%s connected=%s claims_rt=%s msg=%s",
        mt5_snap.get("initialized"),
        mt5_snap.get("terminal_connected"),
        mt5_snap.get("claims_any_realtime"),
        mt5_snap.get("message"),
    )
    feed = get_quote_feed()

    def _dukascopy_bg() -> None:
        try:
            recovery = feed.recover()
            snap = recovery.get("snapshot") or {}
            _log.info(
                "Dukascopy complementar (bg): ok=%s mid=%s lag=%ss channel=%s status=%s",
                recovery.get("ok"),
                snap.get("mid"),
                snap.get("lag_seconds"),
                snap.get("channel"),
                snap.get("status"),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Dukascopy complementar falhou (não bloqueia MT5): %s", exc)

    threading.Thread(target=_dukascopy_bg, name="dukascopy-complementary", daemon=True).start()
    try:
        yield
    finally:
        mt5_feed.stop()
        feed.stop()


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_origin,
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")
app.include_router(historical_router, prefix="/api")
app.include_router(adaptive_router, prefix="/api")
app.include_router(autonomy_router, prefix="/api")
app.include_router(robot_router, prefix="/api")
app.include_router(ai_router, prefix="/api")


@app.get("/")
def root() -> dict[str, str]:
    return {"app": settings.app_name, "docs": "/docs", "health": "/api/health"}


@app.websocket("/ws/prices")
async def prices_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            db = SessionLocal()
            try:
                broker = get_broker(db)
                replay_payload = None
                if replay_controller.active_id:
                    try:
                        replay_payload = replay_controller.status_payload(db, replay_controller.active_id)
                    except Exception:
                        replay_payload = None
                if isinstance(broker, SimulatedBroker):
                    broker.apply_stops()
                    broker._mark_to_market()
                    db.commit()
                quote = broker.quote("EURUSD")
                account = broker.account()
                from app.market.labels import label_for
                from app.market.quote_feed import get_quote_feed

                label = label_for(quote.source, quote.is_synthetic)
                feed_snap = get_quote_feed().snapshot().as_dict()
                await websocket.send_json(
                    {
                        "type": "tick",
                        "symbol": "EURUSD",
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "spread_pips": quote.spread_pips,
                        "is_synthetic": quote.is_synthetic,
                        "source": quote.source,
                        "label": label,
                        "ts": quote.ts.isoformat(),
                        "equity": account.equity,
                        "unrealized_pnl": account.unrealized_pnl,
                        "server_time": datetime.now(timezone.utc).isoformat(),
                        "replay": replay_payload["session"] if replay_payload else None,
                        "quote_feed": feed_snap,
                    }
                )
            finally:
                db.close()
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


@app.get("/api/account")
def account_endpoint(db: Session = Depends(get_db)) -> dict:
    broker = get_broker(db)
    info = broker.account()
    status = broker.status()
    return {
        "balance": info.balance,
        "equity": info.equity,
        "currency": info.currency,
        "unrealized_pnl": info.unrealized_pnl,
        "is_simulated": info.is_simulated,
        "broker": status.adapter,
        "connected": status.connected,
        "message": status.message,
    }
