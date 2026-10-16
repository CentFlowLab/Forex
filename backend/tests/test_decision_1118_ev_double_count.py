"""Regressão #1118: OpenAI re-subtraiu custos a EV já líquido."""

from __future__ import annotations

from app.robot.radar_decision_quality import (
    assess_radar_decision_quality,
    detect_ev_double_count,
)


def _gbpaud_snapshot() -> dict:
    """Snapshot exacto enviado na decisão #1118 (do response_store)."""
    return {
        "symbol": "GBPAUD",
        "side": "BUY",
        "entry": 1.91728,
        "stop_loss": 1.91631,
        "take_profit": 1.91883,
        "rr": 1.6,
        "p_hit_tp_before_sl": 0.6444,
        "expected_net_usd_after_costs": 2.6285,
        "expected_net_usd_0.1lot": 2.6285,
        "cost_usd": 1.69,
        "costs_already_included_in_net": True,
        "evidence_quality": 0.525,
        "p_up_30m": 0.4272,
        "p_down_30m": 0.3988,
        "expected_move_pips_30m": 0.39,
    }


def test_detect_double_count_1118_pattern():
    radar = {"directional_opportunities": [_gbpaud_snapshot()]}
    payload = {
        "decision": "HOLD",
        "justification": (
            "A melhor oportunidade, GBPAUD, apresenta um EV líquido de 0.9388, "
            "que é inferior ao custo de transação."
        ),
        "cited_expected_net_usd": 2.6285,
        "cited_p_up": 0.4272,
        "cited_p_down": 0.3988,
        "top_candidates_comparison": [
            {
                "symbol": "GBPAUD",
                "verdict": "reject",
                "verdict_reason": "EV líquido de 0.9388 é inferior ao custo de transação.",
                "arguments_for": "RR 1.6",
                "arguments_against": "custo",
                "trading_costs": "1.69 USD",
                "missing_for_entry": "n/a",
            }
        ],
    }
    # 2.6285 - 1.69 = 0.9385 ≈ 0.9388
    assert abs(2.6285 - 1.69 - 0.9388) < 0.01
    check = detect_ev_double_count(payload, radar=radar)
    assert check["ai_decision_inconsistent"] is True
    assert check["inconsistency_type"] == "ev_costs_double_counted"
    assert check["matches"]
    assert check["matches"][0]["symbol"] == "GBPAUD"
    assert check["real_block_reason"]


def test_quality_marks_insufficient_but_does_not_flip_hold():
    radar = {
        "local_priority_hint": [{"symbol": "GBPAUD", "score": 0.975}],
        "directional_opportunities": [_gbpaud_snapshot()],
    }
    payload = {
        "decision": "HOLD",
        "justification": (
            "GBPAUD e CHFJPY rejeitados. EV líquido de 0.9388 inferior ao custo."
        ),
        "entry_conditions": "Aguardar volatilidade",
        "cited_expected_net_usd": 2.6285,
        "top_candidates_comparison": [
            {
                "symbol": "GBPAUD",
                "arguments_for": "a",
                "arguments_against": "b",
                "trading_costs": "1.69",
                "missing_for_entry": "n/a",
                "verdict": "reject",
                "verdict_reason": "EV líquido de 0.9388",
            },
            {
                "symbol": "CHFJPY",
                "arguments_for": "a",
                "arguments_against": "b",
                "trading_costs": "1.57",
                "missing_for_entry": "n/a",
                "verdict": "reject",
                "verdict_reason": "EV líquido de 0.8151",
            },
        ],
    }
    q = assess_radar_decision_quality(payload, radar=radar)
    assert q.ai_decision_inconsistent is True
    assert q.ok is False
    assert payload["decision"] == "HOLD"  # nunca alterar automaticamente


def test_numeric_gbpaud_reconciliation():
    """Relação P_hit / pips / pip_value / custos → net ≈ 2.6285."""
    pip = 0.66
    risk = (1.91728 - 1.91631) / 0.0001
    reward = (1.91883 - 1.91728) / 0.0001
    p_hit = 0.6444
    cost = 1.69
    gross = pip * (p_hit * reward - (1 - p_hit) * risk)
    net = gross - cost
    assert abs(risk - 9.7) < 0.05
    assert abs(reward - 15.5) < 0.05
    assert abs(net - 2.6285) < 0.05
    # Movimento esperado +0.39 pips NÃO é o EV$
    assert 0.39 != net
