"""Auditoria completa do motor quantitativo (histórico + EV + OOS).

Sem ordens DEMO e sem chamadas OpenAI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.market.mt5_history import fetch_symbol_history, fetch_universe_history
from app.robot.directional_engine import (
    analyze_pair_directional,
    directional_from_snapshots,
    opportunities_for_openai,
)
from app.robot.directional_validation import run_universe_validation
from app.robot.ev_math import compute_ev, pip_value_from_mt5_specs, resolve_pip_value
from app.robot.pair_snapshot import build_pair_snapshot
from app.robot.universe import FOREX_28_SYMBOLS
from app.risk.instruments import get_instrument

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enrich_snap_with_history(snap: dict[str, Any], hist: Any) -> dict[str, Any]:
    """Substitui velas por histórico profundo MT5 quando status=ok (sem sintéticos)."""
    out = dict(snap)
    cc = dict(out.get("candles_closed") or {})
    counts = dict(out.get("candles_counts") or {})
    for tf in ("M15", "H1", "H4"):
        tfh = hist.by_tf.get(tf)
        if tfh and tfh.status == "ok" and tfh.bars:
            cc[tf] = list(tfh.bars)
            counts[tf] = len(tfh.bars)
    out["candles_closed"] = cc
    out["candles_counts"] = counts
    out["candle_source"] = "mt5_deep" if any(
        (hist.by_tf.get(tf) and hist.by_tf[tf].status == "ok") for tf in ("M15", "H1", "H4")
    ) else out.get("candle_source")
    out["mt5_symbol_info"] = hist.symbol_info
    out["history_meta"] = {
        tf: hist.by_tf[tf].as_dict() if tf in hist.by_tf else None for tf in ("M15", "H1", "H4")
    }
    # Permitir calibração se M15 profundo existir, mesmo sem H4 completo.
    m15_n = counts.get("M15") or 0
    dq = dict(out.get("data_quality") or {})
    if m15_n >= 80 and out.get("quote", {}).get("mid"):
        dq["data_ok"] = True
        dq["candles_ok"] = m15_n >= 80
        dq["deep_history"] = True
    out["data_quality"] = dq
    return out


def _numeric_ev_self_checks() -> list[dict[str, Any]]:
    """Testes numéricos determinísticos da fórmula EV (sem MT5)."""
    checks = []
    # EURUSD: pip=$1 / 0.1 lote (spec), RR=1.6, P_hit=0.5
    # risk=10 pips, reward=16 → gross = 1*(0.5*16 - 0.5*10)=3; costs≈1.2+0.6+0.7=2.5 → net≈0.5
    ev = compute_ev(
        symbol="EURUSD",
        side="BUY",
        mid=1.1000,
        atr_price=0.0010,  # 10 pips
        p_hit_tp_before_sl=0.50,
        p_hit_source="unit_test",
        p_direction=0.55,
        spread_pips=1.2,
        slippage_pips=0.3,
        commission_per_lot=7.0,
        mt5_specs=None,
    )
    be = ev.breakeven_p_hit
    checks.append(
        {
            "name": "EURUSD_BE_RR1.6",
            "ok": abs(be - (1.0 / 2.6)) < 1e-3,
            "breakeven_p_hit": be,
            "expected_approx": 0.3846,
        }
    )
    # P_hit = BE ⇒ gross≈0 (antes custos)
    ev_be = compute_ev(
        symbol="EURUSD",
        side="BUY",
        mid=1.1000,
        atr_price=0.0010,
        p_hit_tp_before_sl=be,
        p_hit_source="unit_test",
        p_direction=0.5,
        spread_pips=0.0,
        slippage_pips=0.0,
        commission_per_lot=0.0,
    )
    checks.append(
        {
            "name": "EURUSD_gross_zero_at_BE",
            "ok": abs(ev_be.gross_usd) < 0.05,
            "gross_usd": ev_be.gross_usd,
        }
    )
    # P_dir não entra no EV — alterar p_direction não muda net se p_hit fixo
    ev2 = compute_ev(
        symbol="EURUSD",
        side="BUY",
        mid=1.1000,
        atr_price=0.0010,
        p_hit_tp_before_sl=0.50,
        p_hit_source="unit_test",
        p_direction=0.99,
        spread_pips=1.2,
        slippage_pips=0.3,
        commission_per_lot=7.0,
    )
    checks.append(
        {
            "name": "P_dir_not_used_in_EV",
            "ok": abs(ev.expected_net_usd - ev2.expected_net_usd) < 1e-9,
            "ev_a": ev.expected_net_usd,
            "ev_b": ev2.expected_net_usd,
        }
    )
    # JPY pip via MT5-like specs: tick_size=0.001, tick_value≈0.666... for 1 lot at ~150
    # pip_size=0.01 ⇒ pip_value/lot = (0.666/0.001)*0.01 = 6.66; /0.1 lot = 0.666
    pv = pip_value_from_mt5_specs(
        trade_tick_size=0.001,
        trade_tick_value=0.666,
        pip_size=0.01,
        lots=0.10,
    )
    checks.append(
        {
            "name": "USDJPY_pip_value_0.1lot",
            "ok": pv is not None and 0.55 <= pv <= 0.80,
            "pip_value": pv,
            "note": "faixa típica ~0.67 USD / 0.1 lote",
        }
    )
    # Heurística antiga errada: p_hit = p_dir * 0.675 com p_dir=0.45 → 0.30 < BE
    old_p_hit = 0.45 * (0.55 + 0.25 * 1.6 / 2.0)
    checks.append(
        {
            "name": "old_heuristic_below_breakeven",
            "ok": old_p_hit < be,
            "old_p_hit": round(old_p_hit, 4),
            "breakeven": round(be, 4),
            "note": "explica EV sempre negativo na versão anterior",
        }
    )
    return checks


def run_directional_audit(
    db: Session,
    *,
    symbols: list[str] | None = None,
    force_history: bool = True,
    run_oos: bool = True,
) -> dict[str, Any]:
    symbols = list(symbols or FOREX_28_SYMBOLS)
    snaps: list[dict[str, Any]] = []
    hist_payload: dict[str, dict[str, Any]] = {}
    history_rows = []

    for idx, sym in enumerate(symbols):
        print(f"audit_history {idx+1}/{len(symbols)} {sym}", flush=True)
        hist = fetch_symbol_history(sym, force=force_history)
        base = build_pair_snapshot(db, sym)
        enriched = _enrich_snap_with_history(base, hist)
        snaps.append(enriched)
        m15 = (enriched.get("candles_closed") or {}).get("M15") or []
        q = enriched.get("quote") or {}
        history_rows.append(
            {
                "symbol": sym,
                "broker_symbol": hist.broker_symbol,
                "m15_bars": len(m15),
                "m15_status": (hist.by_tf.get("M15").status if hist.by_tf.get("M15") else "unavailable"),
                "m15_months": (hist.by_tf.get("M15").months_approx if hist.by_tf.get("M15") else None),
                "m15_first": (hist.by_tf.get("M15").first_ts if hist.by_tf.get("M15") else None),
                "m15_last": (hist.by_tf.get("M15").last_ts if hist.by_tf.get("M15") else None),
                "h1_bars": len((enriched.get("candles_closed") or {}).get("H1") or []),
                "h1_status": (hist.by_tf.get("H1").status if hist.by_tf.get("H1") else "unavailable"),
                "h4_bars": len((enriched.get("candles_closed") or {}).get("H4") or []),
                "h4_status": (hist.by_tf.get("H4").status if hist.by_tf.get("H4") else "unavailable"),
                "spread_pips": q.get("spread_pips"),
                "pip_audit": resolve_pip_value(sym, 0.10, mt5_specs=hist.symbol_info).as_dict(),
            }
        )
        if m15:
            hist_payload[sym] = {
                "bars_m15": m15,
                "spread_pips": float(q.get("spread_pips") or 1.2),
                "symbol_info": hist.symbol_info,
            }

    print("audit_ranking...", flush=True)
    ranking = directional_from_snapshots(snaps, top_n=5)
    calibrated_n = sum(
        1
        for v in ranking.get("pair_views") or []
        if v.get("forecasts") and (v.get("buy") or v.get("sell"))
    )
    print(f"audit_ranking_done calibrated={calibrated_n}", flush=True)

    oos = None
    if run_oos and hist_payload:
        print(f"audit_oos pairs={len(hist_payload)}...", flush=True)
        oos = run_universe_validation(hist_payload)
        print("audit_oos_done", flush=True)

    history_report = {
        "summary": {
            "requested": len(symbols),
            "m15_ok": sum(1 for r in history_rows if r.get("m15_status") == "ok"),
            "m15_absent": sum(1 for r in history_rows if r.get("m15_status") == "absent"),
            "m15_error": sum(1 for r in history_rows if r.get("m15_status") == "error"),
            "m15_unavailable": sum(
                1 for r in history_rows if r.get("m15_status") == "unavailable"
            ),
            "target_bars": {"M15": 16000, "H1": 4000, "H4": 1200},
        }
    }

    actionable = [
        o
        for o in (ranking.get("top_opportunities") or [])
        if float(o.get("expected_net_usd") or 0) > 0
        and float(o.get("evidence_quality") or 0) >= 0.35
        and not (o.get("reject_reasons") or [])
    ]
    # Contar em todo o universo, não só top
    all_opps = []
    for v in ranking.get("pair_views") or []:
        if v.get("buy"):
            all_opps.append(v["buy"])
        if v.get("sell"):
            all_opps.append(v["sell"])
    pass_filters = [
        o
        for o in all_opps
        if float(o.get("expected_net_usd") or 0) > 0
        and float(o.get("evidence_quality") or 0) >= 0.35
        and not (o.get("reject_reasons") or [])
    ]

    limitations = []
    if history_report["summary"]["m15_ok"] < 28:
        limitations.append(
            f"Histórico M15 ok só em {history_report['summary']['m15_ok']}/28 "
            f"(absent={history_report['summary']['m15_absent']}, "
            f"error={history_report['summary']['m15_error']}, "
            f"unavailable={history_report['summary']['m15_unavailable']})"
        )
    short = [r for r in history_rows if (r.get("m15_months") or 0) < 6 and r.get("m15_status") == "ok"]
    if short:
        limitations.append(
            f"{len(short)} pares com <6 meses M15 apesar de status ok"
        )
    if not pass_filters:
        limitations.append(
            "Nenhuma oportunidade passa EV>0 + evidência + sem rejeições "
            "(não forçar trades)"
        )
    if oos and oos.get("aggregate"):
        mea = oos["aggregate"].get("mean_ev_buy")
        if mea is not None and mea <= 0:
            limitations.append(
                f"EV médio OOS BUY={mea} ≤ 0 — sem evidência de edge agregado"
            )

    return {
        "generated_at": _utcnow().isoformat(),
        "safety": {
            "demo_orders": False,
            "openai_paid_calls": False,
            "note": "Auditoria apenas — sem execução nem OpenAI",
        },
        "history": {
            "summary": history_report["summary"],
            "per_symbol": history_rows,
        },
        "calibration": {
            "pairs_with_full_forecasts": calibrated_n,
            "pairs_analyzed": ranking.get("pairs_analyzed"),
            "method": "empirical direction buckets + barrier P(TP before SL)",
        },
        "ev_formula": (
            "EV_net(0.1 lot) = pip_value × (P_hit × reward_pips − (1−P_hit) × risk_pips) "
            "− (spread_pips + 2×slippage_pips) × pip_value − commission_per_lot × 0.1; "
            "P_hit = taxa empírica OHLC de TP-antes-SL no passado (≠ P_up/P_down); "
            "SL=1.0×ATR, TP=1.6×ATR; pip_value preferencialmente MT5 tick_value/tick_size"
        ),
        "numeric_checks": _numeric_ev_self_checks(),
        "ranking": {
            "metric": ranking.get("ranking_metric"),
            "top_opportunities": ranking.get("top_opportunities"),
            "top_buys": ranking.get("top_buys"),
            "top_sells": ranking.get("top_sells"),
            "actionable_in_top": len(actionable),
            "pass_filters_universe": len(pass_filters),
            "pass_filters_examples": pass_filters[:5],
            "openai_compact": opportunities_for_openai(ranking),
        },
        "pair_directions": [
            {
                "symbol": v.get("symbol"),
                "most_likely_direction": v.get("most_likely_direction"),
                "best_side": v.get("best_side"),
                "best_expected_net_usd": v.get("best_expected_net_usd"),
                "buy_p_hit": (v.get("buy") or {}).get("p_hit_tp_before_sl"),
                "sell_p_hit": (v.get("sell") or {}).get("p_hit_tp_before_sl"),
                "buy_ev": (v.get("buy") or {}).get("expected_net_usd"),
                "sell_ev": (v.get("sell") or {}).get("expected_net_usd"),
                "evidence": (v.get("buy") or v.get("sell") or {}).get("evidence_quality"),
            }
            for v in (ranking.get("pair_views") or [])
        ],
        "out_of_sample": oos,
        "limitations": limitations,
        "disclaimer": (
            "Não se declara rentabilidade. Resultados OOS são descritivos. "
            "Ausência de EV positivo após custos e validação ⇒ não negociar."
        ),
    }
