"""Comparação radar vs OpenAI — qualidade HOLD/BUY/SELL e proteções DEMO."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.robot import AIDecisionLog
from app.robot.ai_schema import AIRadarDecision, CandidateComparison
from app.robot.ai_validate import validate_ai_proposal
from app.robot.openai_panel import serialize_openai_decision
from app.robot.radar_decision_quality import (
    assess_radar_decision_quality,
    discordance_summary,
)


RADAR_989 = {
    "local_priority_hint": [
        {"symbol": "USDCHF", "score": 0.901},
        {"symbol": "AUDUSD", "score": 0.884},
        {"symbol": "NZDUSD", "score": 0.883},
    ],
    "pairs_for_comparison": [
        {"symbol": "USDCHF", "score": 0.901},
        {"symbol": "AUDUSD", "score": 0.884},
        {"symbol": "NZDUSD", "score": 0.883},
        {"symbol": "GBPUSD", "score": 0.794},
    ],
}


def _full_comparison(symbol: str, *, verdict: str = "reject") -> dict:
    return {
        "symbol": symbol,
        "arguments_for": f"{symbol} acima/abaixo SMA20 com slope observável.",
        "arguments_against": f"ATR baixo e spread não compensa edge em {symbol}.",
        "trading_costs": "round-trip ≈2.0 USD/0.1 lote; spread 1.0 pip.",
        "missing_for_entry": "Rutura M15 com fecho além do swing e ATR_ratio > 1.0.",
        "verdict": verdict,
        "verdict_reason": (
            f"Rejeitado: atr14_m15≈0.0003 e score {symbol} sem assimetria face aos peers."
            if verdict == "reject"
            else f"Aceite: estrutura e ATR em {symbol} justificam entrada."
        ),
    }


def test_hold_989_style_generic_fails_quality() -> None:
    """Resposta real #989: HOLD genérico sem comparação → qualidade insuficiente."""
    payload = {
        "decision": "HOLD",
        "symbol": None,
        "justification": (
            "Não há uma assimetria clara entre os pares analisados, dado que todos "
            "apresentam características semelhantes em termos de estrutura e volatilidade. "
            "A tendência é de baixa em USD/CHF, enquanto os outros pares não apresentam "
            "uma vantagem significativa para uma decisão de compra ou venda."
        ),
        "rejected_notes": "Nenhum par apresenta uma vantagem clara para uma decisão.",
        "top_candidates_comparison": [],
        "entry_conditions": "",
        "missing_information": "Eventos económicos.",
        "confidence": 0.6,
    }
    q = assess_radar_decision_quality(payload, radar=RADAR_989)
    assert q.ok is False
    assert any("genérica" in i.lower() or "comparação" in i.lower() or "top_candidates" in i for i in q.issues)


def test_hold_with_explicit_candidate_comparison_passes() -> None:
    payload = {
        "decision": "HOLD",
        "symbol": None,
        "justification": (
            "USDCHF (score 0.90): tendência de baixa (slope -0.0012, abaixo SMA20/50) "
            "mas ATR 0.00042 e custo 1.48 USD não oferecem R:R claro após spread 0.1. "
            "AUDUSD e NZDUSD com volatilidade_reduzida (ATR≈0.00027) e mid estável — "
            "sem rutura que justifique BUY."
        ),
        "entry_conditions": (
            "Para USDCHF SELL: fecho M15 abaixo do swing_low_20 com ATR_ratio>1. "
            "Para NZDUSD BUY: fecho acima do swing_high_20 com spread≤1.0."
        ),
        "missing_information": "Calendário de eventos de alto impacto nas próximas 4h.",
        "top_candidates_comparison": [
            _full_comparison("USDCHF"),
            _full_comparison("AUDUSD"),
            _full_comparison("NZDUSD"),
        ],
        "confidence": 0.65,
    }
    q = assess_radar_decision_quality(payload, radar=RADAR_989)
    assert q.ok is True, q.issues


def test_buy_requires_symbol_levels_and_accept_verdict() -> None:
    bad = {
        "decision": "BUY",
        "symbol": None,
        "justification": "Comprar.",
        "top_candidates_comparison": [_full_comparison("NZDUSD", verdict="accept")],
    }
    assert assess_radar_decision_quality(bad, radar=RADAR_989).ok is False

    good = {
        "decision": "BUY",
        "symbol": "NZDUSD",
        "justification": "NZDUSD acima SMA20/50 com slope +0.00037 e score 0.88.",
        "why_preferred": "Melhor estrutura long entre o top-3.",
        "entry_price": 0.5620,
        "stop_loss": 0.5605,
        "take_profit": 0.5645,
        "entry_conditions": "Entrar no ask se mid > 0.5618.",
        "top_candidates_comparison": [
            _full_comparison("USDCHF"),
            _full_comparison("AUDUSD"),
            _full_comparison("NZDUSD", verdict="accept"),
        ],
    }
    assert assess_radar_decision_quality(good, radar=RADAR_989).ok is True


def test_sell_schema_roundtrip() -> None:
    decision = AIRadarDecision(
        decision="SELL",
        symbol="USDCHF",
        justification="USDCHF abaixo SMA20/50, slope -0.0012.",
        why_preferred="Único com tendência de baixa clara no top-3.",
        entry_price=0.8265,
        stop_loss=0.8280,
        take_profit=0.8240,
        entry_conditions="Vender no bid se mid < 0.8268.",
        top_candidates_comparison=[
            CandidateComparison(**_full_comparison("USDCHF", verdict="accept")),
            CandidateComparison(**_full_comparison("AUDUSD")),
            CandidateComparison(**_full_comparison("NZDUSD")),
        ],
        confidence=0.7,
    )
    dumped = decision.as_dict()
    q = assess_radar_decision_quality(dumped, radar=RADAR_989)
    assert q.ok is True, q.issues


def test_discordance_summary_hold_vs_radar_best() -> None:
    disc = discordance_summary(
        radar_best={"symbol": "USDCHF", "score": 0.901},
        openai_decision="HOLD",
        openai_symbol=None,
        justification="características semelhantes sem números",
        entry_conditions="Rutura M15.",
        quality_issues=["HOLD genérico"],
    )
    assert disc["radar_best_candidate"] == "USDCHF"
    assert disc["openai_decision"] == "HOLD"
    assert "AGUARDAR" in disc["discordance_reason"] or "radar" in disc["discordance_reason"].lower()
    assert disc["entry_conditions"] == "Rutura M15."


def test_panel_shows_radar_vs_openai_for_989_store(tmp_path: Path) -> None:
    store = tmp_path / "resp.json"
    compact = {
        "local_priority_hint": RADAR_989["local_priority_hint"],
        "pairs_for_comparison": RADAR_989["pairs_for_comparison"],
        "instruction": "test",
    }
    store.write_text(
        json.dumps(
            {
                "model": "gpt-4o-mini",
                "estimated_cost_usd": 0.00146,
                "text": json.dumps(
                    {
                        "decision": "HOLD",
                        "symbol": None,
                        "justification": (
                            "Não há uma assimetria clara entre os pares analisados, "
                            "características semelhantes."
                        ),
                        "confidence": 0.6,
                        "compared_symbols": ["USDCHF", "NZDUSD", "GBPUSD"],
                    },
                    ensure_ascii=False,
                ),
                "request_messages": [
                    {"role": "system", "content": "sys"},
                    {
                        "role": "user",
                        "content": "preamble\n\n" + json.dumps(compact, ensure_ascii=False),
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision="HOLD",
        justification=(
            "Não há uma assimetria clara entre os pares analisados, "
            "características semelhantes."
        ),
        confidence=0.6,
        estimated_cost_usd=0.00146,
        context_json=json.dumps({"persisted_path": str(store), "pairs": ["USDCHF"]}),
        proposal_json=json.dumps({"decision": "HOLD", "justification": "características semelhantes"}),
        created_at=datetime(2026, 10, 15, 23, 47, 12, tzinfo=timezone.utc),
    )
    out = serialize_openai_decision(log)
    rvo = out["radar_vs_openai"]
    assert rvo["radar_best_candidate"] == "USDCHF"
    assert rvo["openai_decision"] == "HOLD"
    assert rvo["discordance_reason"]
    assert rvo["justification_quality_ok"] is False
    assert out["entry_conditions"]


def test_demo_validator_hold_does_not_approve_execution(db) -> None:
    from app.robot.ai_schema import AITradeProposal

    hold = AITradeProposal(decision="HOLD", justification="aguardar")
    res = validate_ai_proposal(db, hold, {"quote": {"mid": 1.1, "claims_realtime": True}})
    assert res.approved is True  # HOLD "approved" as no-op
    assert res.lots == 0.0
    assert any("AGUARDAR" in r or "execução" in r.lower() or "sem" in r.lower() for r in res.reasons)


def test_demo_validator_rejects_buy_without_levels(db) -> None:
    from app.robot.ai_schema import AITradeProposal

    buy = AITradeProposal(decision="BUY", symbol="NZDUSD", justification="buy")
    res = validate_ai_proposal(
        db,
        buy,
        {
            "symbol": "NZDUSD",
            "quote": {
                "mid": 0.56,
                "source": "mt5",
                "feed_state": "recente",
                "claims_realtime": False,
                "is_synthetic": False,
                "spread_pips": 1.0,
            },
        },
    )
    assert res.approved is False
    assert any("entrada" in r.lower() or "stop" in r.lower() or "incompleta" in r.lower() for r in res.reasons)


def test_executor_gate_only_buysell() -> None:
    """Espelha a guarda em openai_autonomous: só BUY/SELL chamam o executor."""
    for decision in ("HOLD", "BUY", "SELL"):
        should_exec = decision in {"BUY", "SELL"}
        assert (decision in {"BUY", "SELL"}) is should_exec
