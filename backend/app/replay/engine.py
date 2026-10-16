"""Historical market replay with anti look-ahead and simulated execution."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.baseline import sma_crossover_backtest
from app.backtest.metrics import compute_metrics
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.database import SessionLocal
from app.i18n_pt import MSG
from app.market.labels import DUKASCOPY_SOURCE, REAL_HISTORICAL_LABEL
from app.models.agent import DecisionRecord
from app.models.audit import ApiUsage
from app.models.market import Candle
from app.models.replay import ReplaySession
from app.models.trading import ClosedTrade, Position


class ReplayController:
    """In-process replay runner. One active session at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # not paused initially when playing
        self.active_id: int | None = None

    def create_session(
        self,
        db: Session,
        start: datetime,
        end: datetime,
        timeframe: str = "M15",
        speed: float = 1.0,
        ai_every_n_bars: int = 12,
        execute_trades: bool = True,
        source: str = DUKASCOPY_SOURCE,
    ) -> ReplaySession:
        if source == "synthetic":
            raise ValueError(MSG["replay_refuse_synthetic"])
        candles = list(
            db.scalars(
                select(Candle)
                .where(
                    Candle.symbol == "EURUSD",
                    Candle.timeframe == timeframe,
                    Candle.source == source,
                    Candle.is_synthetic.is_(False),
                    Candle.ts >= start,
                    Candle.ts <= end,
                )
                .order_by(Candle.ts.asc())
            )
        )
        if len(candles) < 30:
            raise ValueError(
                MSG["replay_insufficient"].format(
                    bars=len(candles), timeframe=timeframe, source=source
                )
            )
        settings = get_settings()
        session = ReplaySession(
            symbol="EURUSD",
            timeframe=timeframe,
            source=source,
            start_ts=start,
            end_ts=end,
            cursor_ts=candles[0].ts,
            cursor_index=0,
            status="paused",
            speed=max(0.1, min(speed, 50.0)),
            ai_every_n_bars=max(1, ai_every_n_bars),
            execute_trades=execute_trades,
            bars_total=len(candles),
            starting_equity=settings.starting_equity,
            ending_equity=settings.starting_equity,
            is_synthetic=False,
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        # Baseline comparison on identical period (no AI).
        baseline = sma_crossover_backtest(
            candles,
            symbol="EURUSD",
            spread_pips=settings.sim_spread_pips,
            commission_per_lot=settings.sim_commission_per_lot,
        )
        metrics = compute_metrics([t.pnl for t in baseline], settings.starting_equity)
        session.baseline_net_pnl = metrics.net_pnl
        session.report_json = json.dumps(
            {
                "baseline": metrics.as_dict(),
                "label": REAL_HISTORICAL_LABEL,
                "source": source,
            }
        )
        db.commit()
        db.refresh(session)
        self.active_id = session.id
        return session

    def _load_candles(self, db: Session, session: ReplaySession) -> list[Candle]:
        return list(
            db.scalars(
                select(Candle)
                .where(
                    Candle.symbol == session.symbol,
                    Candle.timeframe == session.timeframe,
                    Candle.source == session.source,
                    Candle.is_synthetic.is_(False),
                    Candle.ts >= session.start_ts,
                    Candle.ts <= session.end_ts,
                )
                .order_by(Candle.ts.asc())
            )
        )

    def play(self, db: Session, session_id: int) -> ReplaySession:
        session = db.get(ReplaySession, session_id)
        if not session:
            raise ValueError(MSG["replay_not_found"])
        if session.is_synthetic or session.source == "synthetic":
            raise ValueError(MSG["replay_no_synthetic_play"])
        with self._lock:
            self.active_id = session_id
            session.status = "playing"
            session.updated_at = datetime.now(timezone.utc)
            db.commit()
            self._stop.clear()
            self._pause.set()
            if self._thread and self._thread.is_alive():
                return session
            self._thread = threading.Thread(target=self._run_loop, args=(session_id,), daemon=True)
            self._thread.start()
        db.refresh(session)
        return session

    def pause(self, db: Session, session_id: int) -> ReplaySession:
        session = db.get(ReplaySession, session_id)
        if not session:
            raise ValueError(MSG["replay_not_found"])
        self._pause.clear()
        session.status = "paused"
        session.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(session)
        return session

    def restart(self, db: Session, session_id: int) -> ReplaySession:
        session = db.get(ReplaySession, session_id)
        if not session:
            raise ValueError(MSG["replay_not_found"])
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        session.cursor_index = 0
        session.cursor_ts = session.start_ts
        session.status = "paused"
        session.decisions = 0
        session.trades = 0
        session.ending_equity = session.starting_equity
        session.max_drawdown_pct = 0.0
        session.api_cost_usd = 0.0
        session.ai_net_pnl = 0.0
        session.updated_at = datetime.now(timezone.utc)
        db.commit()
        # Clear open sim positions for a clean restart
        for pos in list(db.scalars(select(Position))):
            db.delete(pos)
        db.commit()
        db.refresh(session)
        self._stop.clear()
        return session

    def set_speed(self, db: Session, session_id: int, speed: float) -> ReplaySession:
        session = db.get(ReplaySession, session_id)
        if not session:
            raise ValueError(MSG["replay_not_found"])
        session.speed = max(0.1, min(float(speed), 50.0))
        session.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(session)
        return session

    def status_payload(self, db: Session, session_id: int) -> dict[str, Any]:
        session = db.get(ReplaySession, session_id)
        if not session:
            raise ValueError(MSG["replay_not_found"])
        candles = self._load_candles(db, session)
        idx = min(session.cursor_index, max(0, len(candles) - 1))
        visible = candles[: idx + 1] if candles else []
        decisions = list(
            db.scalars(
                select(DecisionRecord)
                .where(
                    DecisionRecord.market_time >= session.start_ts,
                    DecisionRecord.market_time <= (session.cursor_ts or session.end_ts),
                    DecisionRecord.data_source == session.source,
                )
                .order_by(DecisionRecord.market_time.asc())
                .limit(200)
            )
        )
        trades = list(
            db.scalars(
                select(ClosedTrade)
                .where(
                    ClosedTrade.opened_at >= session.start_ts,
                    ClosedTrade.is_simulated.is_(True),
                )
                .order_by(ClosedTrade.closed_at.asc())
                .limit(200)
            )
        )
        markers = []
        for d in decisions:
            if d.recommendation in {"BUY", "SELL"}:
                markers.append(
                    {
                        "time": int(d.market_time.replace(tzinfo=timezone.utc).timestamp()),
                        "position": "belowBar" if d.recommendation == "BUY" else "aboveBar",
                        "color": "#3dd68c" if d.recommendation == "BUY" else "#ff5d6c",
                        "shape": "arrowUp" if d.recommendation == "BUY" else "arrowDown",
                        "text": d.recommendation,
                    }
                )
        equity_curve = []
        running = session.starting_equity
        peak = running
        max_dd = 0.0
        for t in trades:
            running += t.pnl
            peak = max(peak, running)
            if peak > 0:
                max_dd = max(max_dd, (peak - running) / peak * 100.0)
            equity_curve.append({"time": t.closed_at.isoformat(), "equity": round(running, 2)})
        cost = float(
            db.scalar(
                select(ApiUsage.estimated_cost_usd)
            )
            or 0.0
        )
        # Sum costs created during session window
        from sqlalchemy import func

        cost = float(
            db.scalar(
                select(func.coalesce(func.sum(ApiUsage.estimated_cost_usd), 0.0)).where(
                    ApiUsage.created_at >= session.created_at
                )
            )
            or 0.0
        )
        by_role = db.execute(
            select(ApiUsage.role, func.sum(ApiUsage.estimated_cost_usd), func.count(ApiUsage.id))
            .where(ApiUsage.created_at >= session.created_at)
            .group_by(ApiUsage.role)
        ).all()
        return {
            "session": {
                "id": session.id,
                "status": session.status,
                "timeframe": session.timeframe,
                "source": session.source,
                "label": REAL_HISTORICAL_LABEL,
                "is_synthetic": False,
                "speed": session.speed,
                "ai_every_n_bars": session.ai_every_n_bars,
                "cursor_index": session.cursor_index,
                "bars_total": session.bars_total,
                "cursor_ts": session.cursor_ts.isoformat() if session.cursor_ts else None,
                "start": session.start_ts.isoformat(),
                "end": session.end_ts.isoformat(),
                "decisions": session.decisions,
                "trades": session.trades,
                "starting_equity": session.starting_equity,
                "ending_equity": session.ending_equity,
                "max_drawdown_pct": session.max_drawdown_pct,
                "baseline_net_pnl": session.baseline_net_pnl,
                "ai_net_pnl": session.ai_net_pnl,
                "api_cost_usd": cost,
            },
            "candles": [
                {
                    "time": int(c.ts.replace(tzinfo=timezone.utc).timestamp()),
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "source": c.source,
                    "is_synthetic": False,
                }
                for c in visible[-400:]
            ],
            "markers": markers,
            "timeline": [
                {
                    "id": d.id,
                    "time": d.market_time.isoformat(),
                    "recommendation": d.recommendation,
                    "risk_approved": d.risk_approved,
                    "executed": d.executed,
                    "rationale": d.coordinator_rationale,
                    "data_source": d.data_source,
                }
                for d in decisions[-50:]
            ],
            "equity_curve": equity_curve,
            "drawdown_pct": max_dd,
            "api_cost_by_agent": [
                {"role": r[0] or "unknown", "estimated_cost_usd": float(r[1] or 0), "calls": int(r[2] or 0)}
                for r in by_role
            ],
            "last_price": visible[-1].close if visible else None,
        }

    def export_report(self, db: Session, session_id: int) -> dict[str, Any]:
        payload = self.status_payload(db, session_id)
        session = db.get(ReplaySession, session_id)
        assert session
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "label": REAL_HISTORICAL_LABEL,
            "disclaimer": MSG["report_disclaimer"],
            "session": payload["session"],
            "baseline": json.loads(session.report_json or "{}").get("baseline"),
            "timeline": payload["timeline"],
            "equity_curve": payload["equity_curve"],
            "api_cost_by_agent": payload["api_cost_by_agent"],
            "comparison": {
                "ai_net_pnl": session.ai_net_pnl,
                "baseline_net_pnl": session.baseline_net_pnl,
                "ai_minus_baseline": session.ai_net_pnl - session.baseline_net_pnl,
            },
        }
        session.report_json = json.dumps({**json.loads(session.report_json or "{}"), "export": report})
        db.commit()
        return report

    def _run_loop(self, session_id: int) -> None:
        while not self._stop.is_set():
            if not self._pause.wait(timeout=0.2):
                continue
            db = SessionLocal()
            try:
                session = db.get(ReplaySession, session_id)
                if not session or session.status not in {"playing"}:
                    break
                candles = self._load_candles(db, session)
                if session.cursor_index >= len(candles):
                    session.status = "finished"
                    session.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    break
                bar = candles[session.cursor_index]
                broker = get_broker(db)
                if isinstance(broker, SimulatedBroker):
                    broker.set_replay_quote(bar.close, bar.ts, source=session.source, is_synthetic=False)
                    broker.apply_stops()
                    broker._mark_to_market()
                    db.commit()

                ran_ai = False
                # Segurança de custos: a reprodução histórica NÃO inicia análises OpenAI.
                # Análises pagas exigem autorização manual explícita no painel (AI_ENABLED + token).
                _ = session.ai_every_n_bars  # preservado na sessão; não dispara IA automaticamente.

                # Update equity / drawdown from account
                if isinstance(broker, SimulatedBroker):
                    acc = broker.account()
                    session.ending_equity = acc.equity
                    session.ai_net_pnl = acc.equity - session.starting_equity
                    peak = max(session.starting_equity, acc.equity)
                    # track via account peak
                    account_row = broker._account_row()
                    session.max_drawdown_pct = account_row.max_drawdown_pct
                    from sqlalchemy import func

                    session.api_cost_usd = float(
                        db.scalar(
                            select(func.coalesce(func.sum(ApiUsage.estimated_cost_usd), 0.0)).where(
                                ApiUsage.created_at >= session.created_at
                            )
                        )
                        or 0.0
                    )
                    _ = peak
                    _ = ran_ai

                session.cursor_index += 1
                session.cursor_ts = bar.ts
                session.updated_at = datetime.now(timezone.utc)
                db.commit()
                # Speed: 1.0 ≈ 4 bars/sec base; higher is faster.
                delay = max(0.02, 0.25 / max(session.speed, 0.1))
            finally:
                db.close()
            time.sleep(delay)


replay_controller = ReplayController()
