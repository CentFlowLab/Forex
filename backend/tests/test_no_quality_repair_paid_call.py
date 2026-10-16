"""Sem segunda chamada paga de reparação quando a qualidade falha."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import get_settings
from app.robot.ai_schema import AIRadarDecision
from app.robot.radar import RadarCandidate, RadarScan
from app.robot.radar_decision_quality import (
    assess_radar_decision_quality,
    is_decision_quality_insufficient,
)


INCOMPLETE_HOLD = {
    "decision": "HOLD",
    "symbol": None,
    "justification": (
        "Não há uma assimetria clara entre os pares analisados, "
        "características semelhantes em termos de estrutura e volatilidade."
    ),
    "favoring_scenario": "Aumento de volatilidade.",
    "invalidating_scenario": "Deterioração.",
    "risks": "Movimentos inesperados.",
    "missing_information": "Eventos.",
    "compared_symbols": ["USDCHF", "AUDUSD", "NZDUSD"],
    "confidence": 0.6,
    "top_candidates_comparison": [],
    "entry_conditions": "",
}


def _fake_scan() -> RadarScan:
    def cand(sym: str, score: float) -> RadarCandidate:
        return RadarCandidate(
            symbol=sym,
            display=sym[:3] + "/" + sym[3:],
            score=score,
            reasons=["dados base OK"],
            snapshot={
                "quote": {
                    "mid": 1.0,
                    "source": "mt5",
                    "feed_state": "recente",
                    "update_age_s": 0.5,
                    "spread_pips": 1.0,
                    "claims_realtime": False,
                    "clock_anomaly": True,
                    "is_synthetic": False,
                },
                "structure": {"sma20": 1.0, "sma50": 1.0, "above_sma20": True, "above_sma50": True},
                "volatility": {
                    "atr14_m15": 0.0003,
                    "trend_slope": 0.0,
                    "local_regime_hint": "mercado_lateral",
                },
                "trading_costs": {"total": 2.0, "spread_pips": 1.0},
                "candles_counts": {"M15": 50, "H1": 20, "H4": 10},
                "candles_closed": {"M15": [{"c": 1.0, "h": 1.01, "l": 0.99, "o": 1.0}] * 12},
                "data_quality": {"executable": True, "candles_ok": True, "quote_ok": True},
                "candle_source": "mt5",
            },
        )

    top = [
        cand("USDCHF", 0.90),
        cand("AUDUSD", 0.88),
        cand("NZDUSD", 0.87),
    ]
    rest = [cand(s, 0.7) for s in ("EURUSD", "GBPUSD", "USDCAD", "USDJPY")]
    all_v = top + rest
    return RadarScan(
        monitored=7,
        with_valid_data=7,
        candidates=top,
        rejected=[],
        pairs=[],
        all_valid=all_v,
    )


def test_quality_repair_flag_off_by_default() -> None:
    assert get_settings().openai_allow_quality_repair_call is False
    assert float(get_settings().robot_openai_daily_budget_usd) == pytest.approx(0.10)


def test_insufficient_hold_does_not_trigger_second_paid_call(db) -> None:
    """Uma análise incompleta → 1 complete() apenas; decision_quality=insufficient."""
    scan = _fake_scan()
    call_count = {"n": 0}

    def fake_complete(*_a, **_k):
        call_count["n"] += 1
        return SimpleNamespace(
            text=json.dumps(INCOMPLETE_HOLD, ensure_ascii=False),
            persisted_path="",
            provider="openai",
            model="gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=50,
            estimated_cost_usd=0.001,
            latency_ms=10,
        )

    provider = MagicMock()
    provider.complete.side_effect = fake_complete

    control = MagicMock()
    control.is_enabled.return_value = True
    control.begin_analysis_context = MagicMock()
    control.end_analysis_context = MagicMock()

    settings = get_settings()
    # Garantir defeito autónomo sem mutar ficheiro .env de forma persistente.
    object.__setattr__(settings, "openai_allow_quality_repair_call", False)

    with patch("app.robot.ai_engine.scan_radar", return_value=scan):
        with patch("app.robot.ai_engine.get_ai_control", return_value=control):
            with patch("app.robot.ai_engine.get_provider", return_value=provider):
                with patch("app.robot.ai_engine.get_settings", return_value=settings):
                    with patch(
                        "app.ai.response_store.update_persisted_meta",
                        MagicMock(),
                    ):
                        from app.robot.ai_engine import run_radar_decision

                        ctx, prop, _scan, usage = run_radar_decision(
                            db, mode="openai", auth_token="test-no-repair"
                        )

    assert call_count["n"] == 1
    assert provider.complete.call_count == 1
    assert usage.get("quality_repair_used") is False
    assert usage.get("paid_calls_this_analysis") == 1
    dq = ctx.get("decision_quality") or {}
    assert dq.get("status") == "insufficient"
    assert dq.get("decision_quality") == "insufficient"
    assert is_decision_quality_insufficient(dq)
    assert prop.decision == "HOLD"  # não transformada
    assert "características semelhantes" in (prop.justification or "")


def test_insufficient_buy_blocks_demo_executor(db) -> None:
    from app.brokers.mt5_demo_executor import MT5DemoExecutor
    from app.models.robot import AIDecisionLog

    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="BUY",
        justification="Comprar NZDUSD sem comparação.",
        entry_price=0.56,
        stop_loss=0.55,
        take_profit=0.57,
        estimated_cost_usd=0.001,
        context_json=json.dumps(
            {
                "symbol": "NZDUSD",
                "decision_quality": {
                    "ok": False,
                    "status": "insufficient",
                    "decision_quality": "insufficient",
                    "issues": ["top_candidates_comparison em falta"],
                },
                "demo_execution_armed": True,
            }
        ),
        proposal_json=json.dumps(
            {
                "decision": "BUY",
                "symbol": "NZDUSD",
                "entry_price": 0.56,
                "stop_loss": 0.55,
                "take_profit": 0.57,
            }
        ),
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    ex = MT5DemoExecutor()
    result = ex.try_execute_decision(db, log)
    assert result.get("ok") is False
    assert "insufficient" in (result.get("reason") or "").lower()
    assert result.get("order_send_called") is not True


def test_autonomous_gate_skips_executor_on_insufficient_quality() -> None:
    from app.robot.openai_autonomous import _risk_reasons_for_decision

    q = {"ok": False, "status": "insufficient", "issues": ["genérica"]}
    assert "insufficient" in _risk_reasons_for_decision("BUY", q).lower()
    assert "HOLD" in _risk_reasons_for_decision("HOLD", q)
    assert "incompleta" in _risk_reasons_for_decision("HOLD", q).lower()


def test_incomplete_hold_still_assessed_without_mutating_decision() -> None:
    q = assess_radar_decision_quality(
        INCOMPLETE_HOLD,
        radar={
            "local_priority_hint": [
                {"symbol": "USDCHF", "score": 0.9},
                {"symbol": "AUDUSD", "score": 0.88},
                {"symbol": "NZDUSD", "score": 0.87},
            ]
        },
    )
    assert q.ok is False
    dumped = AIRadarDecision.model_validate(INCOMPLETE_HOLD)
    assert dumped.decision == "HOLD"
    assert dumped.symbol is None
