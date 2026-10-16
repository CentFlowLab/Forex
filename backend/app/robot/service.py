"""Robô autónomo: contexto → decisão IA (simulada/paga) → risco → ordem simulada."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.autonomy.timeutil import iso_utc, utcnow
from app.brokers.base import OrderRequest
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.database import SessionLocal
from app.market.mt5_feed import get_mt5_feed
from app.market.quote_feed import get_quote_feed
from app.models.robot import AIDecisionLog, RobotState
from app.models.trading import ClosedTrade, Position
from app.robot.ai_engine import run_ai_decision, run_radar_decision
from app.robot.ai_schema import DECISION_PT
from app.robot.ai_validate import validate_ai_proposal
from app.robot.evaluate import evaluation_summary
from app.robot.pair_refresh import get_pair_refresh_tracker
from app.robot.radar import scan_radar
from app.robot.universe import SYMBOLS

logger = logging.getLogger(__name__)


def _is_baseline_trade(t: ClosedTrade) -> bool:
    reason = (t.close_reason or "").strip().upper()
    cid = (t.client_order_id or "").strip().upper()
    return reason == "BASELINE" or cid.startswith("BASELINE")


def _safe_demo_performance(db: Session) -> dict[str, Any]:
    try:
        from app.robot.demo_performance import compute_demo_performance

        return compute_demo_performance(db)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "sample_sufficient_for_edge_claim": False}


def _directional_panel_payload(db: Session, scan_cache: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.robot.prediction_store import panel_predictions_payload

        return panel_predictions_payload(db, scan_cache)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "top_opportunities": [], "pair_directions": []}


def _backtest_panel_payload() -> dict[str, Any]:
    try:
        from app.robot.actionable_backtest import panel_backtest_payload

        return panel_backtest_payload()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc), "layer": "backtest"}


def _ensure_mt5_quotes() -> dict[str, Any]:
    """Garante feed MT5 ativo e devolve snapshot (sem bloquear se falhar)."""
    feed = get_mt5_feed()
    try:
        feed.start()
        feed.poll_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("MT5 feed no status/tick: %s", exc)
    return feed.snapshot().as_dict()


def get_ai_control_enabled() -> bool:
    try:
        from app.ai.control import get_ai_control

        return bool(get_ai_control().is_enabled())
    except Exception:  # noqa: BLE001
        return bool(get_settings().ai_enabled)


def _autonomous_status_safe(db: Session) -> dict[str, Any]:
    try:
        from app.robot.openai_autonomous import get_openai_autonomous

        # light=True evita recursão e scan/poll duplicados a cada poll do painel.
        return get_openai_autonomous().preparation_status(db, light=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "confirmed": False, "active": False}


def _quote_for_panel(mt5_snap: dict[str, Any]) -> dict[str, Any]:
    """Cotação principal do painel: MT5 EURUSD; Dukascopy só complementar."""
    sym = (mt5_snap.get("symbols") or {}).get("EURUSD") or {}
    if sym.get("mid") is not None:
        return {
            "source": "mt5",
            "source_label": "MetaTrader 5",
            "age_seconds": sym.get("lag_seconds"),
            "update_age_s": sym.get("update_age_s"),
            "tick_time_lag_s": sym.get("tick_time_lag_s"),
            "tick_msc_lag_s": sym.get("tick_msc_lag_s"),
            "mid": sym.get("mid"),
            "bid": sym.get("bid"),
            "ask": sym.get("ask"),
            "spread_pips": sym.get("spread_pips"),
            "status": sym.get("status"),
            "status_label": {
                "atualizado": "Atualizado (MT5)",
                "com_atraso": "Com atraso",
                "obsoleto": "Obsoleto / mercado fechado",
                "desligado": "Ligação perdida",
            }.get(str(sym.get("status") or ""), str(sym.get("feed_state") or "mt5")),
            "feed_state": sym.get("feed_state"),
            "block_entries": sym.get("block_entries"),
            "message": sym.get("message") or mt5_snap.get("message") or "",
            "channel": "mt5",
            "data_class": sym.get("data_class"),
            "claims_realtime": bool(sym.get("claims_realtime")),
            "clock_anomaly": bool(sym.get("clock_anomaly")),
            "clock_offset_hint_s": sym.get("clock_offset_hint_s"),
            "terminal_connected": bool(mt5_snap.get("terminal_connected")),
        }
    # Fallback informativo Dukascopy — não é fonte principal.
    feed = get_quote_feed().snapshot()
    return {
        "source": feed.source_label or feed.channel,
        "source_label": feed.source_label,
        "age_seconds": feed.lag_seconds,
        "mid": feed.mid,
        "bid": feed.bid,
        "ask": feed.ask,
        "status": feed.status,
        "status_label": feed.status_label,
        "block_entries": True,
        "message": (
            "MT5 indisponível — Dukascopy complementar apenas. " + (feed.message or "")
        ).strip(),
        "channel": feed.channel,
        "data_class": feed.data_class,
        "claims_realtime": False,
        "clock_anomaly": False,
        "terminal_connected": False,
        "complementary_only": True,
    }


@dataclass
class RobotTestHarness:
    """Apenas para testes E2E — força sinal sem IA."""

    force_signal: str
    allow_synthetic: bool = True
    label: str = "estratégia_teste_isolada"


class RobotService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Ciclo mais lento: 1 análise simulada por intervalo (sem spam).
        self._interval_seconds = 20.0
        self._last_radar: dict[str, Any] = {}
        self._best_opportunity: str = ""

    def _state(self, db: Session) -> RobotState:
        row = db.scalar(select(RobotState).order_by(RobotState.id.desc()).limit(1))
        if row is None:
            row = RobotState(running=False, decision="HOLD", decision_mode="simulated")
            db.add(row)
            db.commit()
            db.refresh(row)
        return row

    def status(self, db: Session) -> dict[str, Any]:
        st = self._state(db)
        settings = get_settings()
        broker = get_broker(db)
        if isinstance(broker, SimulatedBroker):
            broker.connect()
            broker._mark_to_market()
            acct = broker.account()
        else:
            acct = broker.account()

        mt5_snap = _ensure_mt5_quotes()
        quote = _quote_for_panel(mt5_snap)

        # Radar: refrescar no máximo a cada 5s (painel faz poll a 3s).
        radar_pairs = self._last_radar.get("pairs") or []
        now_ts = utcnow().timestamp()
        last_scan_ts = float(getattr(self, "_last_radar_scan_ts", 0.0) or 0.0)
        if now_ts - last_scan_ts >= 5.0 or not radar_pairs:
            try:
                scan = scan_radar(db)
                radar_pairs = list(scan.pairs)
                top5 = [c.summary() for c in scan.candidates[:5]]
                discovery_snap = None
                try:
                    from app.robot.mt5_symbol_discovery import get_cached_discovery

                    disc = get_cached_discovery()
                    discovery_snap = disc.as_dict() if disc else None
                except Exception:  # noqa: BLE001
                    discovery_snap = None
                best = ""
                if scan.candidates:
                    top = scan.candidates[0]
                    best = f"{top.symbol} · score {top.score:.2f} (triagem local)"
                dir_scan = getattr(scan, "directional", None) or {}
                if dir_scan.get("top_opportunities"):
                    top_d = dir_scan["top_opportunities"][0]
                    best = (
                        f"{top_d.get('symbol')} {top_d.get('side')} · "
                        f"EV≈{float(top_d.get('expected_net_usd') or 0):+.2f}$ "
                        f"(p_hit={float(top_d.get('p_hit_tp_before_sl') or 0):.0%})"
                    )
                self._last_radar = {
                    **(self._last_radar or {}),
                    **scan.as_dict(),
                    "monitored": scan.monitored,
                    "with_valid_data": scan.with_valid_data,
                    "universe_target": 28,
                    "pairs": radar_pairs,
                    "candidates": top5,
                    "top5": top5,
                    "primary_source": "mt5",
                    "discovery": discovery_snap,
                    "best_opportunity": best or self._last_radar.get("best_opportunity", "—"),
                    "decision_cycle": "radar_status_refresh",
                    "directional": dir_scan,
                }
                self._last_radar_scan_ts = now_ts
                if best:
                    self._best_opportunity = best
                # Persistir previsões no máximo 1×/15 min (sem OpenAI).
                last_pred = float(getattr(self, "_last_pred_persist_ts", 0.0) or 0.0)
                if dir_scan and now_ts - last_pred >= 900.0:
                    try:
                        from app.robot.prediction_store import persist_scan_predictions

                        persist_scan_predictions(db, dir_scan)
                        self._last_pred_persist_ts = now_ts
                    except Exception as pe:  # noqa: BLE001
                        logger.debug("persist previsões: %s", pe)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scan_radar no status: %s", exc)
                radar_pairs = self._last_radar.get("pairs") or []

        open_pos = list(db.scalars(select(Position).limit(20)))
        closed_all = list(
            db.scalars(select(ClosedTrade).order_by(ClosedTrade.id.desc()).limit(80))
        )
        session_closed = [t for t in closed_all if not _is_baseline_trade(t)]
        baseline_closed = [t for t in closed_all if _is_baseline_trade(t)]
        session_realized = sum(t.pnl for t in session_closed)
        baseline_realized = sum(t.pnl for t in baseline_closed)
        # Dukascopy complementar — nunca bloqueia o painel quando MT5 está ok.
        duk = get_quote_feed().snapshot()

        thread_alive = bool(self._thread and self._thread.is_alive())
        last_ai = None
        if st.last_ai_decision_id:
            last_ai = db.get(AIDecisionLog, st.last_ai_decision_id)

        from app.robot.openai_panel import openai_panel_payload

        openai_real = openai_panel_payload(db, history_limit=5)

        block = (st.block_reason or "").strip()
        # Não promover erros TCP/Dukascopy a bloqueio do robô se MT5 estiver ligado.
        if quote.get("source") == "mt5" and quote.get("feed_state") == "recente":
            if "tcp" in block.lower() or "dukascopy" in block.lower():
                block = ""
        if st.running and not thread_alive:
            block = block or "Robô marcado como ativo mas o ciclo não está a correr — prima INICIAR novamente."

        monitored = int(self._last_radar.get("monitored") or len(SYMBOLS))
        with_valid = int(self._last_radar.get("with_valid_data") or 0)

        # Ciclo local/simulado — nunca misturar com decisão OpenAI real no painel.
        sim_decision = st.decision
        sim_justification = st.last_justification or (
            last_ai.justification
            if last_ai and not str(last_ai.mode or "").startswith("openai")
            else ""
        )
        auto_status = _autonomous_status_safe(db)

        from app.robot.panel_view import build_panel_overlay

        try:
            from app.brokers.mt5_demo_auth import get_demo_auth_snapshot

            demo_auth = get_demo_auth_snapshot(db)
        except Exception:  # noqa: BLE001
            demo_auth = {
                "active": False,
                "emergency": False,
                "pause_entries": True,
                "last_attempt": None,
            }

        last_attempt = demo_auth.get("last_attempt")
        # Fallback: última tentativa persistida (ignora stubs de teste login=111).
        if not last_attempt:
            try:
                from app.models.demo_execution import DemoExecutionAttempt

                row = db.scalar(
                    select(DemoExecutionAttempt)
                    .where(DemoExecutionAttempt.account_login != "111")
                    .order_by(DemoExecutionAttempt.id.desc())
                    .limit(1)
                )
                if row is None:
                    row = db.scalar(
                        select(DemoExecutionAttempt).order_by(DemoExecutionAttempt.id.desc()).limit(1)
                    )
                # Só expor tentativas não-sintéticas no painel de execução.
                if row is not None and str(row.account_login or "") not in {"111", "Demo-Server"}:
                    last_attempt = {
                        "status": row.status,
                        "reason": row.reject_reason or "",
                        "reject_reason": row.reject_reason or "",
                        "at": iso_utc(row.created_at),
                        "created_at": iso_utc(row.created_at),
                        "decision_id": row.decision_id,
                        "attempt_id": row.id,
                        "side": row.side,
                        "symbol": row.symbol,
                        "order_ticket": row.order_ticket,
                        "ticket": row.order_ticket,
                        "order_send_called": bool(row.order_send_json),
                        "order_check_called": bool(row.order_check_json),
                        "synthetic_test": str(row.account_login or "") == "111",
                    }
            except Exception:  # noqa: BLE001
                pass

        capital_sim = {
            "balance": round(acct.balance, 2),
            "equity": round(acct.equity, 2),
            "currency": acct.currency,
            "unrealized_pnl": round(acct.unrealized_pnl, 2),
            "realized_pnl": round(session_realized, 2),
            "session_realized_pnl": round(session_realized, 2),
            "baseline_realized_pnl": round(baseline_realized, 2),
            "starting_equity": settings.starting_equity,
            "is_simulated": True,
        }
        panel = build_panel_overlay(
            running=bool(st.running and thread_alive),
            thread_alive=thread_alive,
            block_reason=block,
            mt5_snap=mt5_snap if isinstance(mt5_snap, dict) else {},
            quote=quote if isinstance(quote, dict) else {},
            auto=auto_status if isinstance(auto_status, dict) else {},
            openai_real=openai_real if isinstance(openai_real, dict) else {},
            account_simulated=capital_sim,
            demo_auth_active=bool(demo_auth.get("active")),
            emergency=bool(demo_auth.get("emergency")),
            pause_entries=bool(demo_auth.get("pause_entries", True)),
            last_execution_attempt=last_attempt,
            auth_login=demo_auth.get("login"),
            auth_server=demo_auth.get("server"),
        )

        return {
            "title": "Robô Forex (observação + DEMO)",
            "disclaimer": (
                "Fonte principal: MetaTrader 5. Negociação real bloqueada. "
                "Operações só numa conta DEMO explicitamente autorizada. "
                "Capital SimulatedBroker (TEST/BASELINE) é separado do saldo DEMO."
            ),
            **panel,
            "running": bool(st.running and thread_alive),
            "thread_alive": thread_alive,
            "decision": sim_decision,
            "decision_pt": DECISION_PT.get(sim_decision, "AGUARDAR"),
            "block_reason": block,
            "last_message": st.last_message,
            "last_justification": sim_justification,
            "last_tick_at": iso_utc(st.last_tick_at),
            "ticks_completed": st.ticks_completed,
            "openai_real": openai_real,
            "ai": {
                "mode": st.decision_mode or "simulated",
                "label": "IA simulada (sem custo)"
                if (st.decision_mode or "simulated") != "openai"
                else "OpenAI (manual)",
                "is_simulated": (st.decision_mode or "simulated") != "openai",
                "paid_calls": st.paid_ai_calls,
                "last_decision_id": st.last_ai_decision_id,
                "favoring": last_ai.favoring_scenario
                if last_ai and not str(last_ai.mode or "").startswith("openai")
                else "",
                "invalidating": last_ai.invalidating_scenario
                if last_ai and not str(last_ai.mode or "").startswith("openai")
                else "",
                "risks": last_ai.risks
                if last_ai and not str(last_ai.mode or "").startswith("openai")
                else "",
                "time_horizon": last_ai.time_horizon
                if last_ai and not str(last_ai.mode or "").startswith("openai")
                else "",
                "executed": bool(last_ai.executed) if last_ai else False,
                "risk_approved": bool(last_ai.risk_approved) if last_ai else False,
                "evaluation": evaluation_summary(db),
                "symbol": self._last_radar.get("best_symbol") if self._last_radar else None,
                "why_preferred": self._last_radar.get("why_preferred", ""),
                "context_source": "mt5",
                "note": "Triagem/ciclo local — não é decisão OpenAI paga.",
            },
            "radar": {
                "label": "RADAR LOCAL (triagem automática)",
                "monitored": monitored,
                "with_valid_data": with_valid,
                "universe_target": 28,
                "best_opportunity": self._best_opportunity
                or self._last_radar.get("best_opportunity", "—"),
                "candidates": self._last_radar.get("candidates", []),
                "top5": (self._last_radar.get("candidates") or [])[:5],
                "pairs": radar_pairs,
                "refreshing": get_pair_refresh_tracker().running,
                "pair_statuses": radar_pairs,
                "primary_source": "mt5",
                "discovery": self._last_radar.get("discovery"),
                "note": (
                    "Até 28 cruzamentos; top-5 oportunidades direcionais para OpenAI. "
                    "Ranking por EV líquido ≠ ordem. Ciclo simulado ≠ decisão paga."
                ),
            },
            "directional_predictions": _directional_panel_payload(
                db, self._last_radar.get("directional") or {}
            ),
            "backtest_validation": _backtest_panel_payload(),
            "quote": quote,
            "mt5": {
                "terminal_connected": bool(mt5_snap.get("terminal_connected")),
                "initialized": bool(mt5_snap.get("initialized")),
                "account_login": mt5_snap.get("account_login"),
                "account_server": mt5_snap.get("account_server"),
                "claims_any_realtime": bool(mt5_snap.get("claims_any_realtime")),
                "message": mt5_snap.get("message"),
                "poll_count": mt5_snap.get("poll_count"),
                "trade_mode": mt5_snap.get("trade_mode"),
                "trade_mode_label": mt5_snap.get("trade_mode_label"),
                "balance": mt5_snap.get("balance"),
                "equity": mt5_snap.get("equity"),
                "margin": mt5_snap.get("margin"),
                "margin_free": mt5_snap.get("margin_free"),
                "profit": mt5_snap.get("profit"),
                "currency": mt5_snap.get("currency"),
            },
            "dukascopy_complementary": {
                "channel": duk.channel,
                "status": duk.status,
                "lag_seconds": duk.lag_seconds,
                "message": duk.message,
                "blocks_robot": False,
            },
            # Compat: capital = SimulatedBroker (não misturar com DEMO).
            "capital": capital_sim,

            "strategy": {
                "key": "ai_radar",
                "approved_count": 0,
                "approved": [],
                "engine": "ai_radar_multi",
            },
            "open_positions": [
                {
                    "id": p.id,
                    "side": p.side,
                    "entry": p.entry_price,
                    "volume": p.volume_lots,
                    "stop": p.stop_loss,
                    "take": p.take_profit,
                    "unrealized_pnl": p.unrealized_pnl,
                }
                for p in open_pos
            ],
            "trades": [
                {
                    "id": t.id,
                    "side": t.side,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl": t.pnl,
                    "reason": t.close_reason,
                    "closed_at": iso_utc(t.closed_at),
                    "is_test": str(t.client_order_id or "").startswith("TEST-"),
                    "is_baseline": False,
                    "session": "current",
                }
                for t in session_closed[:20]
            ],
            "baseline_trades": [
                {
                    "id": t.id,
                    "side": t.side,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl": t.pnl,
                    "reason": t.close_reason,
                    "closed_at": iso_utc(t.closed_at),
                    "is_test": False,
                    "is_baseline": True,
                    "session": "baseline",
                }
                for t in baseline_closed[:12]
            ],
            "openai_autonomous": auto_status,
            "demo_auth": {
                **demo_auth,
                "login_masked": panel.get("account_mt5", {}).get("login_masked"),
                "trade_mode_label": mt5_snap.get("trade_mode_label"),
                "balance": mt5_snap.get("balance"),
                "equity": mt5_snap.get("equity"),
                "currency": mt5_snap.get("currency") or "USD",
            },
            "security": {
                "live_trading_locked": True,
                "ai_enabled": bool(get_ai_control_enabled()),
                "ai_calls": st.ai_calls,
                "paid_ai_calls": st.paid_ai_calls,
                "environment": settings.trading_environment,
                # True só com autorização confirmada + agendamento autónomo ativo.
                "auto_paid_ai": bool(auto_status.get("scheduling_active")),
            },
            "demo_performance": _safe_demo_performance(db),
        }

    def start(self, db: Session) -> dict[str, Any]:
        settings = get_settings()
        if settings.trading_environment == "LIVE" or settings.live_authorized:
            st = self._state(db)
            st.running = False
            st.block_reason = "Negociação real bloqueada — o robô só opera em simulação."
            st.last_message = st.block_reason
            db.commit()
            return self.status(db)

        st = self._state(db)
        st.running = True
        st.decision_mode = "simulated"
        st.block_reason = ""
        st.last_message = "Robô iniciado — motor IA simulado (sem custos OpenAI)."
        st.ai_calls = 0
        st.updated_at = utcnow()
        db.commit()
        try:
            get_mt5_feed().start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Arranque feed MT5: %s", exc)
        # Dukascopy complementar em background — nunca atrasa INICIAR nem bloqueia o robô.
        def _duk_bg() -> None:
            try:
                get_quote_feed().recover()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Recuperação Dukascopy complementar: %s", exc)
            try:
                from app.robot.pair_refresh import refresh_majors_background

                refresh_majors_background()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Refresh multimeda bi5 complementar: %s", exc)

        threading.Thread(target=_duk_bg, name="robot-duk-complementary", daemon=True).start()
        self.ensure_loop()
        try:
            from app.robot.openai_autonomous import get_openai_autonomous

            get_openai_autonomous().on_robot_start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agendador OpenAI autónomo: %s", exc)
        try:
            self.tick(db)
        except Exception as exc:  # noqa: BLE001
            st = self._state(db)
            st.block_reason = str(exc)
            st.last_message = f"Falha no primeiro ciclo: {exc}"
            db.commit()
        return self.status(db)

    def stop(self, db: Session) -> dict[str, Any]:
        self._stop.set()
        try:
            from app.robot.openai_autonomous import get_openai_autonomous

            get_openai_autonomous().on_robot_stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parar agendador OpenAI: %s", exc)
        st = self._state(db)
        st.running = False
        st.last_message = "Robô parado — agendamento OpenAI interrompido."
        st.block_reason = ""
        st.decision = "HOLD"
        st.updated_at = utcnow()
        db.commit()
        return self.status(db)

    def ensure_loop(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="robot-ai", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                st = self._state(db)
                if not st.running:
                    break
                self.tick(db)
            except Exception:  # noqa: BLE001
                logger.exception("Erro no ciclo do robô")
            finally:
                db.close()
            self._stop.wait(self._interval_seconds)

    def tick(self, db: Session, *, test: RobotTestHarness | None = None) -> dict[str, Any]:
        st = self._state(db)
        settings = get_settings()
        st.ai_calls = 0

        if settings.trading_environment == "LIVE" or settings.live_authorized:
            return self._block(db, st, "HOLD", "Negociação real bloqueada.")

        broker = get_broker(db)
        if not isinstance(broker, SimulatedBroker):
            return self._block(db, st, "HOLD", "Apenas a corretora simulada pode executar.")
        broker.connect()

        # Fonte principal do ciclo: MT5. Dukascopy não bloqueia o tick.
        mt5_snap = _ensure_mt5_quotes()
        panel_q = _quote_for_panel(mt5_snap)
        quote_age = float(
            panel_q.get("age_seconds")
            if panel_q.get("age_seconds") is not None
            else 9999.0
        )
        st.quote_source = str(panel_q.get("source") or "mt5")
        st.quote_age_seconds = quote_age

        if test is not None:
            return self._tick_test_harness(db, st, broker, test)

        # Radar MT5 + IA simulada; TCP/Dukascopy não é pré-requisito.

        closed_ids = broker.apply_stops()
        if closed_ids:
            from app.robot.evaluate import sync_outcomes

            sync_outcomes(db)
            st.last_message = f"Fechadas {len(closed_ids)} posição(ões) por stop/take (simulado)."
            st.decision = "HOLD"
            st.block_reason = ""
            st.last_tick_at = utcnow()
            st.ticks_completed += 1
            db.commit()
            return {"ok": True, "action": "closed", "closed": closed_ids}

        open_count = len(list(db.scalars(select(Position).limit(5))))
        if open_count > 0:
            st.decision = "HOLD"
            st.block_reason = ""
            st.last_message = "Posição aberta — a gerir stops (sem nova análise de entrada)."
            st.last_tick_at = utcnow()
            st.ticks_completed += 1
            db.commit()
            return {"ok": True, "action": "manage", "open": open_count}

        # --- Radar multimeda + IA simulada (OpenAI paga NUNCA no loop) ---
        context, proposal, scan, _usage = run_radar_decision(db, mode="simulated")
        discovery_snap = None
        try:
            from app.robot.mt5_symbol_discovery import get_cached_discovery

            disc = get_cached_discovery()
            discovery_snap = disc.as_dict() if disc else None
        except Exception:  # noqa: BLE001
            discovery_snap = None
        self._last_radar = {
            **scan.as_dict(),
            "best_symbol": proposal.symbol,
            "why_preferred": getattr(proposal, "why_preferred", "") or "",
            "best_opportunity": (
                f"{proposal.symbol or '—'} · {DECISION_PT.get(proposal.decision, 'AGUARDAR')}"
            ),
            "discovery": discovery_snap,
            "decision_cycle": "simulated_local",
        }
        self._best_opportunity = self._last_radar["best_opportunity"]

        # MT5 recente pode não ter claims_realtime (anomalia de relógio) — análise ok;
        # allow_delayed só para validação de execução simulada com fonte mt5.
        mt5_ok = (context.get("quote") or {}).get("source") == "mt5" and (
            (context.get("quote") or {}).get("feed_state") == "recente"
        )
        validation = validate_ai_proposal(db, proposal, context, allow_delayed=mt5_ok)

        log = AIDecisionLog(
            mode="simulated_radar",
            decision=proposal.decision,
            justification=proposal.justification,
            favoring_scenario=proposal.favoring_scenario,
            invalidating_scenario=proposal.invalidating_scenario,
            entry_price=float(proposal.entry_price or 0),
            stop_loss=float(proposal.stop_loss or 0),
            take_profit=float(proposal.take_profit or 0),
            time_horizon=proposal.time_horizon,
            risks=proposal.risks,
            missing_information=(
                (proposal.missing_information or "")
                + " | "
                + (getattr(proposal, "why_preferred", "") or "")
            )[:2000],
            confidence=float(proposal.confidence or 0),
            context_json=json.dumps(
                {
                    "radar": scan.as_dict(),
                    "symbol": proposal.symbol,
                    "quote": context.get("quote"),
                    "rejected": scan.rejected[:12],
                },
                ensure_ascii=False,
                default=str,
            )[:8000],
            proposal_json=json.dumps(proposal.as_dict(), ensure_ascii=False),
            risk_approved=validation.approved and proposal.decision in {"BUY", "SELL"},
            risk_reasons="; ".join(validation.reasons),
            quote_lag_seconds=float((context.get("quote") or {}).get("lag_seconds") or quote_age),
            estimated_cost_usd=0.0,
        )
        db.add(log)
        db.flush()

        st.decision = proposal.decision
        st.last_justification = proposal.justification
        st.last_ai_decision_id = log.id
        st.strategy_key = "ai_radar"
        st.decision_mode = "simulated"
        st.last_tick_at = utcnow()
        st.ticks_completed += 1
        st.updated_at = utcnow()

        # Fase observação: nunca enviar ordens (nem SimulatedBroker).
        sym = proposal.symbol or "—"
        log.executed = False
        log.risk_reasons = (
            (log.risk_reasons + " | ").lstrip(" |")
            + "Observação — execução desativada (nem SimulatedBroker)."
        ).strip(" |")
        st.block_reason = ""
        st.last_message = (
            f"Radar ({scan.with_valid_data}/{scan.monitored} pares) · "
            f"IA: {DECISION_PT.get(proposal.decision, 'AGUARDAR')} [{sym}] · "
            f"sem execução. {proposal.justification[:200]}"
        )
        db.commit()
        return {
            "ok": True,
            "action": proposal.decision,
            "symbol": proposal.symbol,
            "executed": False,
            "mode": "simulated_radar_observe",
            "decision_id": log.id,
            "radar": self._last_radar,
            "validation": validation.as_dict(),
        }

    def _tick_test_harness(
        self,
        db: Session,
        st: RobotState,
        broker: SimulatedBroker,
        test: RobotTestHarness,
    ) -> dict[str, Any]:
        """Percurso E2E antigo com sinal forçado (sem OpenAI)."""
        from app.risk.engine import ProposedTrade, RiskEngine
        from app.risk.instruments import get_instrument

        settings = get_settings()
        closed_ids = broker.apply_stops()
        if closed_ids:
            st.ticks_completed += 1
            db.commit()
            return {"ok": True, "action": "closed", "closed": closed_ids}
        if list(db.scalars(select(Position).limit(1))):
            st.ticks_completed += 1
            db.commit()
            return {"ok": True, "action": "manage"}

        signal = test.force_signal.upper()
        if signal not in {"BUY", "SELL"}:
            st.decision = "HOLD"
            st.ticks_completed += 1
            db.commit()
            return {"ok": True, "action": "HOLD"}

        q = broker.quote("EURUSD")
        entry = q.ask if signal == "BUY" else q.bid
        spec = get_instrument("EURUSD")
        if signal == "BUY":
            stop = entry - spec.price_from_pips(15)
            take = entry + spec.price_from_pips(25)
        else:
            stop = entry + spec.price_from_pips(15)
            take = entry - spec.price_from_pips(25)
        risk = RiskEngine(db).evaluate(
            ProposedTrade(
                symbol="EURUSD",
                side=signal,
                entry_price=entry,
                stop_loss=stop,
                take_profit=take,
                spread_pips=q.spread_pips,
                expected_slippage_pips=settings.sim_slippage_pips,
                market_ts=utcnow(),
                broker_connected=True,
                data_is_stale=False,
            )
        )
        if not risk.approved:
            return self._block(db, st, "HOLD", "; ".join(risk.reasons))
        client_id = f"TEST-{uuid.uuid4().hex[:14]}"
        result = broker.submit_order(
            OrderRequest(
                client_order_id=client_id,
                symbol="EURUSD",
                side=signal,  # type: ignore[arg-type]
                volume_lots=risk.lots,
                stop_loss=stop,
                take_profit=take,
            )
        )
        st.decision = signal
        st.ticks_completed += 1
        st.last_message = "TESTE E2E forçado — não é IA paga nem lucro real."
        db.commit()
        return {
            "ok": result.accepted,
            "action": signal,
            "is_test": True,
            "client_order_id": client_id,
            "filled_price": result.filled_price,
            "lots": risk.lots,
        }

    def run_manual_openai(
        self,
        db: Session,
        *,
        auth_token: str,
        allow_execution: bool = False,
    ) -> dict[str, Any]:
        """Uma análise OpenAI paga — apenas com autorização explícita (fora do loop)."""
        st = self._state(db)
        # Recuperar feeds antes da avaliação / chamada.
        try:
            from app.market.mt5_feed import get_mt5_feed

            get_mt5_feed().recover()
        except Exception as exc:  # noqa: BLE001
            logger.warning("recover MT5 antes de OpenAI: %s", exc)
        # Dukascopy complementar — nunca bloquear a análise OpenAI/MT5.
        try:
            threading.Thread(
                target=lambda: get_quote_feed().recover(),
                name="duk-recover-before-openai",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("recover Dukascopy (bg) antes de OpenAI: %s", exc)

        context, proposal, usage = run_ai_decision(
            db, mode="openai", auth_token=auth_token, recover_feed=True
        )
        validation = validate_ai_proposal(db, proposal, context, allow_delayed=True)
        from app.robot.ai_engine import estimate_single_analysis_usd

        est = estimate_single_analysis_usd(db=db)
        skipped = bool(usage.get("skipped_paid"))
        ready = usage.get("readiness") or context.get("readiness") or {}
        analysis_kind = str(ready.get("analysis_kind") or context.get("analysis_kind") or "")
        # Histórico / bloqueado: nunca executar.
        can_exec = bool(allow_execution) and analysis_kind == "fresh" and not skipped
        actual_cost = 0.0 if skipped else float(usage.get("estimated_cost_usd") or 0)
        mode_label = "blocked_no_data" if skipped else ("openai_historical" if analysis_kind == "historical" else "openai")

        log = AIDecisionLog(
            mode=mode_label,
            decision=proposal.decision,
            justification=proposal.justification,
            favoring_scenario=proposal.favoring_scenario,
            invalidating_scenario=proposal.invalidating_scenario,
            entry_price=float(proposal.entry_price or 0),
            stop_loss=float(proposal.stop_loss or 0),
            take_profit=float(proposal.take_profit or 0),
            time_horizon=proposal.time_horizon,
            risks=proposal.risks,
            missing_information=proposal.missing_information,
            confidence=float(proposal.confidence or 0),
            context_json=json.dumps(
                {
                    "quote": context.get("quote"),
                    "structure": context.get("structure"),
                    "volatility": context.get("volatility"),
                    "trading_costs": context.get("trading_costs"),
                    "readiness": ready,
                    "analysis_kind": analysis_kind,
                },
                ensure_ascii=False,
                default=str,
            )[:8000],
            proposal_json=json.dumps(proposal.as_dict(), ensure_ascii=False),
            risk_approved=False,
            risk_reasons="; ".join(validation.reasons),
            quote_lag_seconds=float((context.get("quote") or {}).get("lag_seconds") or 0),
            estimated_cost_usd=actual_cost,
            executed=False,
        )
        db.add(log)
        db.flush()
        if not skipped:
            st.paid_ai_calls += 1
        st.ai_calls = 1
        st.last_ai_decision_id = log.id
        st.last_justification = proposal.justification
        st.decision = proposal.decision
        st.decision_mode = mode_label
        if skipped:
            st.last_message = (
                f"API paga NÃO chamada — {DECISION_PT.get(proposal.decision)}. {proposal.justification[:240]}"
            )
            st.block_reason = proposal.justification[:500]
        else:
            st.block_reason = ""
            st.last_message = (
                f"Análise OpenAI ({analysis_kind}): {DECISION_PT.get(proposal.decision)}. "
                f"Custo≈${actual_cost:.4f}. Sem execução."
            )

        executed = False
        if (
            can_exec
            and proposal.decision in {"BUY", "SELL"}
            and validation.approved
        ):
            trade_symbol = (getattr(proposal, "symbol", None) or "").strip().upper()
            broker = get_broker(db)
            if not trade_symbol:
                st.last_message = (
                    f"OpenAI {DECISION_PT.get(proposal.decision)} sem par — "
                    "execução simulada recusada."
                )
            elif isinstance(broker, SimulatedBroker):
                broker.connect()
                if not list(db.scalars(select(Position).limit(1))):
                    client_id = f"AI-PAID-{uuid.uuid4().hex[:12]}"
                    res = broker.submit_order(
                        OrderRequest(
                            client_order_id=client_id,
                            symbol=trade_symbol,
                            side=proposal.decision,  # type: ignore[arg-type]
                            volume_lots=validation.lots,
                            stop_loss=float(proposal.stop_loss or 0),
                            take_profit=float(proposal.take_profit or 0),
                        )
                    )
                    executed = bool(res.accepted)
                    log.executed = executed
                    log.client_order_id = client_id if executed else ""
                    log.risk_approved = executed
        db.commit()
        return {
            "decision": proposal.decision,
            "decision_pt": DECISION_PT.get(proposal.decision),
            "proposal": proposal.as_dict(),
            "validation": validation.as_dict(),
            "executed": executed,
            "decision_id": log.id,
            "estimated_cost_usd": est["estimated_max_cost_usd"],
            "actual_cost_usd": actual_cost,
            "skipped_paid": skipped,
            "readiness": ready,
            "analysis_kind": analysis_kind,
            "token_estimate": usage.get("token_estimate") or est.get("context_token_estimate"),
            "usage": {
                "provider": usage.get("provider"),
                "model": usage.get("model"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "estimated_cost_usd": actual_cost,
                "latency_ms": usage.get("latency_ms"),
            },
            "raw_response": usage.get("raw_text"),
            "context_summary": {
                "quote": context.get("quote"),
                "structure": context.get("structure"),
                "volatility": context.get("volatility"),
                "trading_costs": context.get("trading_costs"),
                "analysis_kind": analysis_kind,
                "data_disclaimer": context.get("data_disclaimer"),
                "candles_counts": {
                    tf: len((context.get("candles_closed") or {}).get(tf) or [])
                    for tf in ("M15", "H1", "H4")
                },
            },
            "mode": mode_label,
            "live_trading_locked": True,
            "auto_paid_ai": False,
        }

    def _block(self, db: Session, st: RobotState, decision: str, reason: str) -> dict[str, Any]:
        st.decision = decision
        st.block_reason = reason
        st.last_message = reason
        st.last_tick_at = utcnow()
        st.ticks_completed += 1
        st.updated_at = utcnow()
        db.commit()
        return {"ok": False, "blocked": True, "reason": reason, "decision": decision}


_svc: RobotService | None = None
_svc_lock = threading.Lock()


def get_robot_service() -> RobotService:
    global _svc
    with _svc_lock:
        if _svc is None:
            _svc = RobotService()
        return _svc
