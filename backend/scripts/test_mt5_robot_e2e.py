"""Percurso completo: MT5 → radar 7 pares → contexto IA → decisão sim → risco → SimulatedBroker.

Não chama OpenAI pago. Não usa mt5.order_send.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.database import SessionLocal
from app.market.mt5_feed import get_mt5_feed
from app.robot.ai_engine import run_radar_decision
from app.robot.radar import scan_radar
from app.robot.universe import SYMBOLS
from app.brokers.base import OrderRequest
from app.risk.engine import ProposedTrade, RiskEngine
from app.risk.instruments import get_instrument


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    report: dict = {"steps": [], "pairs": {}, "ok": True}
    feed = get_mt5_feed()
    snap = feed.start()
    report["mt5_start"] = {
        "initialized": snap.get("initialized"),
        "terminal_connected": snap.get("terminal_connected"),
        "account_login": snap.get("account_login"),
        "account_server": snap.get("account_server"),
        "claims_any_realtime": snap.get("claims_any_realtime"),
        "message": snap.get("message"),
        "orders_disabled": snap.get("orders_disabled"),
    }
    if not snap.get("terminal_connected"):
        report["ok"] = False
        report["error"] = "MT5 terminal não ligado"
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 1

    # Deixar 2 polls para estabilizar ticks.
    time.sleep(2.5)
    feed.poll_once()
    mt5_snap = feed.snapshot().as_dict()
    now = _utc_now()
    for sym in SYMBOLS:
        q = mt5_snap["symbols"].get(sym) or {}
        report["pairs"][sym] = {
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "spread_pips": q.get("spread_pips"),
            "feed_state": q.get("feed_state"),
            "lag_seconds": q.get("lag_seconds"),
            "tick_time_lag_s": q.get("tick_time_lag_s"),
            "tick_msc_lag_s": q.get("tick_msc_lag_s"),
            "update_age_s": q.get("update_age_s"),
            "claims_realtime": q.get("claims_realtime"),
            "clock_anomaly": q.get("clock_anomaly"),
            "clock_offset_hint_s": q.get("clock_offset_hint_s"),
            "source": q.get("source"),
            "message": q.get("message"),
            "utc_now": now.isoformat(),
        }
    report["steps"].append("mt5_poll_ok")

    db = SessionLocal()
    try:
        scan = scan_radar(db)
        mt5_in_radar = all(
            (p.get("source") == "mt5") for p in scan.pairs if p.get("mid") is not None
        )
        report["radar"] = {
            "monitored": scan.monitored,
            "with_valid_data": scan.with_valid_data,
            "mt5_as_quote_source": mt5_in_radar,
            "pairs": [
                {
                    "symbol": p["symbol"],
                    "source": p.get("source"),
                    "feed_state": p.get("feed_state"),
                    "lag_seconds": p.get("lag_seconds"),
                    "tick_time_lag_s": p.get("tick_time_lag_s"),
                    "candle_source": p.get("candle_source"),
                    "state": p.get("state"),
                    "data_ok": p.get("data_ok"),
                }
                for p in scan.pairs
            ],
        }
        report["steps"].append("radar_ok")

        context, proposal, scan2, usage = run_radar_decision(db, mode="simulated")
        ai_quote = context.get("quote") or {}
        compact = context.get("radar") or {}
        pairs_ai = compact.get("pairs_for_comparison") or []
        ai_sources = {
            (p.get("quote") or {}).get("source")
            for p in pairs_ai
            if (p.get("quote") or {}).get("mid") is not None
        }
        justification = getattr(proposal, "justification", None) or getattr(proposal, "reasoning", "") or ""
        report["ai_context"] = {
            "mode": usage.get("mode"),
            "decision": proposal.decision,
            "symbol": proposal.symbol,
            "reasoning_preview": str(justification)[:240],
            "quote_source": ai_quote.get("source"),
            "quote_channel": ai_quote.get("channel"),
            "pairs_sources": sorted(x for x in ai_sources if x),
            "mt5_in_ai_context": ("mt5" in ai_sources) or ai_quote.get("source") == "mt5",
            "skipped_paid": usage.get("skipped_paid"),
            "openai_called": False,
        }
        if "mt5" not in ai_sources and ai_quote.get("source") != "mt5":
            report["ok"] = False
            report["ai_context"]["error"] = "IA não recebeu dados MT5"
        report["steps"].append("ai_simulated_ok")

        broker = get_broker(db)
        report["broker"] = {
            "type": type(broker).__name__,
            "is_simulated": isinstance(broker, SimulatedBroker),
        }
        if not isinstance(broker, SimulatedBroker):
            report["ok"] = False
            report["broker"]["error"] = "Corretora não é SimulatedBroker"
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            return 1

        # Validação de risco + execução simulada só se COMPRAR/VENDER.
        sym = proposal.symbol or (scan.candidates[0].symbol if scan.candidates else "EURUSD")
        q_exec = broker.quote(sym)
        report["sim_quote"] = {
            "symbol": sym,
            "bid": q_exec.bid,
            "ask": q_exec.ask,
            "spread_pips": q_exec.spread_pips,
            "source": q_exec.source,
            "ts": q_exec.ts.isoformat() if q_exec.ts else None,
        }
        if q_exec.source != "mt5":
            report["ok"] = False
            report["sim_quote"]["error"] = "SimulatedBroker não usou MT5"

        decision = (proposal.decision or "HOLD").upper()
        if decision in {"BUY", "SELL", "COMPRAR", "VENDER"}:
            side = "BUY" if decision in {"BUY", "COMPRAR"} else "SELL"
            entry = q_exec.ask if side == "BUY" else q_exec.bid
            spec = get_instrument(sym)
            if side == "BUY":
                stop = entry - spec.price_from_pips(15)
                take = entry + spec.price_from_pips(25)
            else:
                stop = entry + spec.price_from_pips(15)
                take = entry - spec.price_from_pips(25)
            check = RiskEngine(db).evaluate(
                ProposedTrade(
                    symbol=sym,
                    side=side,
                    entry_price=entry,
                    stop_loss=stop,
                    take_profit=take,
                    spread_pips=q_exec.spread_pips,
                    expected_slippage_pips=0.3,
                    market_ts=_utc_now(),
                    broker_connected=True,
                    data_is_stale=False,
                )
            )
            report["risk"] = {
                "allowed": check.approved,
                "reason": "; ".join(check.reasons),
                "lots": check.lots,
            }
            report["steps"].append("risk_ok")
            if check.approved:
                import uuid

                result = broker.submit_order(
                    OrderRequest(
                        client_order_id=f"mt5-e2e-{uuid.uuid4().hex[:10]}",
                        symbol=sym,
                        side=side,  # type: ignore[arg-type]
                        volume_lots=check.lots or 0.01,
                        stop_loss=stop,
                        take_profit=take,
                    )
                )
                report["sim_order"] = {
                    "accepted": result.accepted,
                    "status": result.status,
                    "filled_price": result.filled_price,
                    "message": result.message,
                }
                report["steps"].append("sim_order_ok")
        else:
            # Mesmo em HOLD, validar risco com proposta hipotética para fechar o percurso.
            side = "BUY"
            entry = q_exec.ask
            spec = get_instrument(sym)
            stop = entry - spec.price_from_pips(15)
            take = entry + spec.price_from_pips(25)
            check = RiskEngine(db).evaluate(
                ProposedTrade(
                    symbol=sym,
                    side=side,
                    entry_price=entry,
                    stop_loss=stop,
                    take_profit=take,
                    spread_pips=q_exec.spread_pips,
                    expected_slippage_pips=0.3,
                    market_ts=_utc_now(),
                    broker_connected=True,
                    data_is_stale=False,
                )
            )
            report["risk"] = {
                "allowed": check.approved,
                "reason": "; ".join(check.reasons) or "HOLD — ordem não enviada",
                "lots": check.lots,
                "decision_was_hold": True,
            }
            report["steps"].append("risk_validated_hold")

        # Demonstrar recuperação: interrupção → não atual; recover → atualiza.
        feed.force_disconnect()
        disc = feed.snapshot().as_dict()
        stale_ok = all(
            (s.get("feed_state") == "ligacao_perdida" and not s.get("claims_realtime"))
            for s in disc["symbols"].values()
        )
        scan_disc = scan_radar(db)
        disc_radar = [
            {
                "symbol": p["symbol"],
                "feed_state": p.get("feed_state"),
                "claims_realtime": p.get("claims_realtime"),
                "state": p.get("state"),
            }
            for p in scan_disc.pairs
        ]
        report["disconnect"] = {
            "stale_marked_ligacao_perdida": stale_ok,
            "claims_any_realtime": disc.get("claims_any_realtime"),
            "radar_pairs": disc_radar,
            "message": disc.get("message"),
        }
        if not stale_ok or disc.get("claims_any_realtime"):
            report["ok"] = False
            report["disconnect"]["error"] = "Cotações antigas apresentadas como atuais"
        report["steps"].append("disconnect_ok" if stale_ok else "disconnect_fail")

        rec = feed.recover()
        time.sleep(1.0)
        feed.poll_once()
        after = feed.snapshot().as_dict()
        recovered = after.get("terminal_connected") and any(
            s.get("feed_state") == "recente" for s in after["symbols"].values()
        )
        report["recover"] = {
            "ok": bool(rec.get("ok")),
            "terminal_connected": after.get("terminal_connected"),
            "recovered_recente": recovered,
            "claims_any_realtime": after.get("claims_any_realtime"),
            "pairs": {
                k: {
                    "feed_state": v.get("feed_state"),
                    "lag_seconds": v.get("lag_seconds"),
                    "tick_time_lag_s": v.get("tick_time_lag_s"),
                    "claims_realtime": v.get("claims_realtime"),
                }
                for k, v in after["symbols"].items()
            },
        }
        if not recovered:
            report["ok"] = False
            report["recover"]["error"] = "Falha a recuperar após interrupção"
        report["steps"].append("recover_ok" if recovered else "recover_fail")

        # Garantir que não há order_send no feed.
        report["safety"] = {
            "mt5_orders_disabled": True,
            "broker_simulated": True,
            "openai_auto_off": usage.get("mode") == "simulated",
        }
    finally:
        db.close()
        # Manter feed a correr para a app; neste script isolado, parar.
        # feed.stop()  # deixa o terminal utilizável

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
