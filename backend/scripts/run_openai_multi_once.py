"""Uma análise OpenAI multimeda autorizada — sem execução, sem retries pagas."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

from app.ai.control import get_ai_control
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.market.dukascopy_bi5_poll import latest_tick_from_hours
from app.models.robot import AIDecisionLog, RobotState
from app.robot.ai_engine import estimate_radar_analysis_usd, run_radar_decision
from app.robot.ai_schema import DECISION_PT
from app.robot.pair_quotes import get_pair_quote_store
from app.robot.radar import scan_radar
from app.robot.universe import SYMBOLS

OUT = Path(__file__).resolve().parents[1] / "tmp"
OUT.mkdir(exist_ok=True)
CAP = 0.01


def main() -> int:
    init_db()
    settings = get_settings()
    control = get_ai_control()
    print(
        "PRECHECK",
        "live_locked=",
        settings.live_trading_locked,
        "env=",
        settings.trading_environment,
        "ai_control=",
        control.is_enabled(),
        flush=True,
    )
    if settings.live_authorized or settings.trading_environment == "LIVE":
        print("ABORT LIVE", flush=True)
        return 3
    if not control.is_enabled():
        print("ABORT AI disabled", flush=True)
        return 3

    store = get_pair_quote_store()
    lags: dict = {}
    headers = {"User-Agent": "GROK-FOREX-AI/0.2 (multi openai prep)"}
    with httpx.Client(timeout=35.0, headers=headers, follow_redirects=True) as client:
        for sym in SYMBOLS:
            try:
                tick, _, meta = latest_tick_from_hours(client, lookback_hours=8, symbol=sym)
                if tick is None:
                    lags[sym] = {"ok": False, "lag": None, "mid": None}
                    print("UPDATE", sym, "FAIL", flush=True)
                else:
                    store.set(sym, tick, is_synthetic=False, source="dukascopy_bi5")
                    lag = float(meta.get("lag_seconds") or 0)
                    lags[sym] = {
                        "ok": True,
                        "lag": lag,
                        "mid": tick.mid,
                        "ts": tick.ts.isoformat(),
                    }
                    print("UPDATE", sym, "lag=", round(lag, 1), "mid=", tick.mid, flush=True)
            except Exception as exc:  # noqa: BLE001
                lags[sym] = {"ok": False, "error": str(exc)}
                print("UPDATE", sym, "ERR", exc, flush=True)

    db = SessionLocal()
    try:
        scan = scan_radar(db)
        est = estimate_radar_analysis_usd(scan)
        print("ESTIMATE", json.dumps(est, ensure_ascii=False), flush=True)
        print("VALID", [c.symbol for c in scan.all_valid], "n=", len(scan.all_valid), flush=True)
        if float(est["estimated_cost_usd"]) > CAP:
            (OUT / "openai_multi_abort.json").write_text(
                json.dumps({"est": est, "lags": lags}, indent=2), encoding="utf-8"
            )
            print("ABORT estimate exceeds", CAP, flush=True)
            return 2

        auth = control.create_authorization(
            db, confirmed=True, workflow="robot_radar_multi", debate_rounds=0
        )
        token = auth["authorization_id"]
        with control._lock:
            a = control._auths.get(token)
            if a:
                a.max_calls = 1
                a.reserved_cost_usd = float(est["estimated_cost_usd"])
                a.max_cost_usd = CAP
        print("AUTH ok max_cost=", CAP, flush=True)

        try:
            ctx, prop, scan2, usage = run_radar_decision(db, mode="openai", auth_token=token)
        except Exception as exc:  # noqa: BLE001
            (OUT / "openai_multi_error.json").write_text(
                json.dumps(
                    {"error": str(exc), "type": type(exc).__name__, "lags": lags, "estimate": est},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print("FAIL", exc, flush=True)
            return 1

        sym_label = prop.symbol or "—"
        log = AIDecisionLog(
            mode="openai_radar_multi",
            decision=prop.decision,
            justification=prop.justification,
            favoring_scenario=prop.favoring_scenario,
            invalidating_scenario=prop.invalidating_scenario,
            entry_price=float(prop.entry_price or 0),
            stop_loss=float(prop.stop_loss or 0),
            take_profit=float(prop.take_profit or 0),
            time_horizon=prop.time_horizon or "",
            risks=prop.risks or "",
            missing_information=(
                (prop.missing_information or "") + " | " + (prop.why_preferred or "")
            )[:2000],
            confidence=float(prop.confidence or 0),
            context_json=json.dumps(
                {
                    "pairs": [c.symbol for c in scan2.all_valid],
                    "lags": lags,
                    "temporal": (ctx.get("radar") or {}).get("temporal_disclaimer"),
                },
                ensure_ascii=False,
                default=str,
            )[:8000],
            proposal_json=json.dumps(prop.as_dict(), ensure_ascii=False),
            risk_approved=False,
            risk_reasons="Sem execução — teste de análise apenas.",
            quote_lag_seconds=0.0,
            estimated_cost_usd=float(usage.get("estimated_cost_usd") or 0),
            executed=False,
        )
        db.add(log)
        st = db.scalar(select(RobotState).order_by(RobotState.id.desc()).limit(1))
        if st:
            st.paid_ai_calls = int(st.paid_ai_calls or 0) + 1
            st.decision = prop.decision
            st.last_justification = prop.justification
            st.decision_mode = "openai_radar_multi"
            st.last_message = (
                f"OpenAI multimeda: {DECISION_PT.get(prop.decision)} [{sym_label}] — sem execução."
            )
        db.commit()
        db.refresh(log)
        if st:
            st.last_ai_decision_id = log.id
            db.commit()

        report = {
            "model": usage.get("model"),
            "provider": usage.get("provider"),
            "pairs_analyzed": [c.symbol for c in scan2.all_valid],
            "pair_lags": lags,
            "decision": prop.decision,
            "decision_pt": DECISION_PT.get(prop.decision),
            "preferred_symbol": prop.symbol,
            "justification": prop.justification,
            "why_preferred": prop.why_preferred,
            "favoring_scenario": prop.favoring_scenario,
            "invalidating_scenario": prop.invalidating_scenario,
            "risks": prop.risks,
            "missing_information": prop.missing_information,
            "compared_symbols": prop.compared_symbols,
            "entry_price": prop.entry_price,
            "stop_loss": prop.stop_loss,
            "take_profit": prop.take_profit,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "actual_cost_usd": usage.get("estimated_cost_usd"),
            "estimate_before": est,
            "executed": False,
            "live_trading_locked": True,
            "auto_paid_ai": False,
            "decision_id": log.id,
            "raw_response": usage.get("raw_text"),
            "limitations": [
                "Dados bi5 históricos — não são tempo real.",
                "Atrasos entre pares não são iguais — ver pair_lags.",
                "Nenhuma operação executada.",
            ],
        }
        (OUT / "openai_multi_result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(
            "OK",
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "model",
                        "decision_pt",
                        "preferred_symbol",
                        "prompt_tokens",
                        "completion_tokens",
                        "actual_cost_usd",
                        "pairs_analyzed",
                        "executed",
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        print("JUST", (prop.justification or "")[:600], flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
