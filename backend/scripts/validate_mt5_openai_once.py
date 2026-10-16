"""Validação radar MT5 + no máximo UMA chamada OpenAI (cap 0.01 USD). Sem ordens."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai.control import get_ai_control
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.market.mt5_feed import get_mt5_feed
from app.models.robot import AIDecisionLog
from app.robot.ai_engine import estimate_radar_analysis_usd, run_radar_decision
from app.robot.ai_schema import DECISION_PT
from app.robot.radar import scan_radar
from app.robot.universe import SYMBOLS

OUT = Path(__file__).resolve().parents[1] / "tmp"
OUT.mkdir(exist_ok=True)
CAP = 0.01


def main() -> int:
    init_db()
    settings = get_settings()
    control = get_ai_control()
    report: dict = {"phase": {}, "ok": True}

    if settings.live_authorized or settings.trading_environment == "LIVE":
        report["ok"] = False
        report["abort"] = "LIVE bloqueado"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 3

    feed = get_mt5_feed()
    snap = feed.start()
    time.sleep(2.0)
    feed.poll_once()
    mt5 = feed.snapshot().as_dict()

    pairs_lag = {}
    for sym in SYMBOLS:
        q = mt5["symbols"].get(sym) or {}
        pairs_lag[sym] = {
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "spread_pips": q.get("spread_pips"),
            "bid_eq_ask": q.get("bid") == q.get("ask"),
            "feed_state": q.get("feed_state"),
            "update_age_s": q.get("update_age_s"),
            "tick_time_lag_s": q.get("tick_time_lag_s"),
            "lag_seconds": q.get("lag_seconds"),
            "claims_realtime": q.get("claims_realtime"),
            "clock_anomaly": q.get("clock_anomaly"),
            "source": q.get("source"),
            "price_changed": q.get("price_changed"),
        }
    report["phase"]["mt5"] = {
        "terminal_connected": mt5.get("terminal_connected"),
        "claims_any_realtime": mt5.get("claims_any_realtime"),
        "pairs": pairs_lag,
    }

    # Critério de validade para análise (não exige claims_realtime UTC):
    # - terminal ligado
    # - 7 pares com mid
    # - feed_state recente (atualização MT5)
    # - sem ligação perdida
    valid_for_analysis = bool(mt5.get("terminal_connected")) and all(
        (pairs_lag[s].get("feed_state") == "recente" and pairs_lag[s].get("bid") is not None)
        for s in SYMBOLS
    )
    report["phase"]["validity"] = {
        "valid_for_analysis": valid_for_analysis,
        "claims_realtime": False,
        "note": (
            "Análise permitida com atualizações recentes (update_age/msc); "
            "claims_realtime permanece false porque tick.time não é UTC."
        ),
    }

    db = SessionLocal()
    try:
        scan = scan_radar(db)
        radar_pairs = [
            {
                "symbol": p["symbol"],
                "source": p.get("source"),
                "feed_state": p.get("feed_state"),
                "lag_seconds": p.get("lag_seconds"),
                "tick_time_lag_s": p.get("tick_time_lag_s"),
                "mid": p.get("mid"),
                "data_ok": p.get("data_ok"),
                "candle_source": p.get("candle_source"),
                "state": p.get("state"),
            }
            for p in scan.pairs
        ]
        report["phase"]["radar"] = {
            "monitored": scan.monitored,
            "with_valid_data": scan.with_valid_data,
            "all_valid": [c.symbol for c in scan.all_valid],
            "candidates": [c.symbol for c in scan.candidates],
            "mt5_source_all": all(p.get("source") == "mt5" for p in scan.pairs),
            "pairs": radar_pairs,
            "orders_executed": False,
        }

        if not valid_for_analysis or scan.with_valid_data < 7:
            report["ok"] = False
            report["abort"] = "Dados insuficientes para OpenAI"
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            return 2

        est = estimate_radar_analysis_usd(scan)
        report["phase"]["estimate"] = est
        print("ESTIMATE", json.dumps(est, ensure_ascii=False), flush=True)
        if float(est["estimated_cost_usd"]) > CAP:
            report["ok"] = False
            report["abort"] = f"Estimativa {est['estimated_cost_usd']} > {CAP}"
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            return 2

        if not control.is_enabled():
            report["ok"] = False
            report["abort"] = "AI_ENABLED=false — não é possível chamar OpenAI"
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            (OUT / "mt5_openai_abort.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
            return 3

        if not settings.provider_configured("openai"):
            report["ok"] = False
            report["abort"] = "OPENAI_API_KEY em falta"
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            return 3

        auth = control.create_authorization(
            db, confirmed=True, workflow="robot_radar_multi_mt5", debate_rounds=0
        )
        token = auth["authorization_id"]
        with control._lock:
            a = control._auths.get(token)
            if a:
                a.max_calls = 1
                a.reserved_cost_usd = float(est["estimated_cost_usd"])
                a.max_cost_usd = CAP

        print("CALLING OpenAI once, cap=", CAP, flush=True)
        try:
            ctx, prop, scan2, usage = run_radar_decision(db, mode="openai", auth_token=token)
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            report["phase"]["openai"] = {"error": str(exc), "type": type(exc).__name__}
            (OUT / "mt5_openai_error.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            return 1

        decision_pt = DECISION_PT.get(prop.decision, prop.decision)
        report["phase"]["openai"] = {
            "decision": prop.decision,
            "decision_pt": decision_pt,
            "symbol": prop.symbol,
            "justification": prop.justification,
            "favoring_scenario": prop.favoring_scenario,
            "invalidating_scenario": prop.invalidating_scenario,
            "risks": prop.risks,
            "confidence": prop.confidence,
            "compared_symbols": getattr(prop, "compared_symbols", []),
            "why_preferred": getattr(prop, "why_preferred", ""),
            "missing_information": getattr(prop, "missing_information", ""),
            "entry_price": prop.entry_price,
            "stop_loss": prop.stop_loss,
            "take_profit": prop.take_profit,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "actual_cost_usd": usage.get("estimated_cost_usd"),
            "raw_text": usage.get("raw_text"),
            "mode": usage.get("mode"),
            "orders_executed": False,
            "quote_sources_in_context": list(
                {
                    (p.get("quote") or {}).get("source")
                    for p in (ctx.get("radar") or {}).get("pairs_for_comparison") or []
                }
            ),
        }
        # Persistir resultado ANTES do log ORM (evitar perder a resposta paga).
        (OUT / "mt5_openai_once.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

        try:
            log = AIDecisionLog(
                mode="openai_radar_multi_mt5",
                decision=prop.decision,
                justification=prop.justification,
                favoring_scenario=prop.favoring_scenario,
                invalidating_scenario=prop.invalidating_scenario,
                entry_price=float(prop.entry_price or 0),
                stop_loss=float(prop.stop_loss or 0),
                take_profit=float(prop.take_profit or 0),
                time_horizon=prop.time_horizon or "",
                risks=prop.risks or "",
                missing_information=(prop.missing_information or "")[:2000],
                confidence=float(prop.confidence or 0),
                context_json=json.dumps(
                    {
                        "symbol": prop.symbol,
                        "risks": prop.risks,
                        "mt5": True,
                        "pairs": [c.symbol for c in scan2.all_valid],
                    },
                    ensure_ascii=False,
                )[:8000],
                proposal_json=json.dumps(prop.as_dict(), ensure_ascii=False),
                risk_approved=False,
                risk_reasons="Sem execução — teste de análise apenas.",
                quote_lag_seconds=0.0,
                estimated_cost_usd=float(usage.get("estimated_cost_usd") or 0),
                executed=False,
            )
            db.add(log)
            db.commit()
            report["phase"]["openai"]["decision_log_id"] = log.id
        except Exception as exc:  # noqa: BLE001
            report["phase"]["openai"]["log_error"] = str(exc)
            (OUT / "mt5_openai_once.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )
    finally:
        db.close()

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
