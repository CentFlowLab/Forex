from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agents.memory_store import record_simulated_outcome, save_decision_memory
from app.agents.orchestrator import run_collaboration
from app.agents.tradingagents_pipeline import run_tradingagents_collaboration
from app.market.forex_adapter import forex_market_snapshot
from app.risk.instruments import get_instrument
from app.schemas.agents import DebateResearcherOutput, TradingDecisionPackage
from tests.ai_test_helpers import authorize_via_api, enable_ai_offline_no_approval


def test_debate_schema_requires_structured_fields() -> None:
    raw = {
        "bias": "BULLISH",
        "recommendation": "BUY",
        "confidence": 0.6,
        "uncertainty": 0.4,
        "rationale": "Momentum alinhado nos indicadores fornecidos.",
        "evidence": [{"claim": "RSI acima de 50", "source": "bull_debate", "strength": 0.5}],
        "risks": ["Spread alargado"],
        "insufficient_information": False,
        "timestamp_utc": "2025-10-13T12:00:00+00:00",
        "argument": "Compra com suporte nos dados fornecidos.",
        "counterpoints_addressed": ["Volatilidade elevada"],
        "key_evidence": ["MACD positivo"],
        "round_index": 1,
    }
    parsed = DebateResearcherOutput.model_validate(raw)
    assert parsed.recommendation == "BUY"
    assert parsed.round_index == 1


def test_final_package_schema() -> None:
    pkg = TradingDecisionPackage(
        instrument="EUR/USD",
        market_data_timestamp="2025-10-13T12:00:00+00:00",
        timeframe="M15",
        direction="HOLD",
        confidence_score=0.2,
        bull_arguments=["suporte"],
        bear_arguments=["resistência"],
        key_evidence=["ATR"],
        risk_objections=["spread"],
        final_explanation="Evidência conflituante — aguardar.",
        agent_disagreement_summary="Comprador e Vendedor divergem.",
    )
    assert pkg.direction == "HOLD"


def test_invalid_direction_rejected() -> None:
    with pytest.raises(ValidationError):
        TradingDecisionPackage(
            instrument="EUR/USD",
            market_data_timestamp="2025-10-13T12:00:00+00:00",
            timeframe="M15",
            direction="LONG",  # type: ignore[arg-type]
        )


def test_pip_size_eurusd() -> None:
    spec = get_instrument("EURUSD")
    assert spec.pip_size == pytest.approx(0.0001)


def test_forex_adapter_snapshot(db) -> None:
    """Snapshot Forex em modo replay — seed local; gaps reais não inventam velas."""
    snap = forex_market_snapshot(db, "EURUSD", "M15", replay_mode=True)
    assert snap.get("pip_size") == pytest.approx(0.0001) or "pip_size" in snap or snap.get("ok") is False
    if snap.get("ok") is True:
        assert snap["provenance"]["live"] is False
        assert "snapshot_id" in snap
    else:
        # Host com série incompleta / gaps: ok=False é honesto (não fabricar).
        assert snap.get("block_entries") is True or snap.get("reason")


def test_tradingagents_offline_hold(db) -> None:
    from app.config import get_settings

    enable_ai_offline_no_approval()
    result = run_tradingagents_collaboration(db, execute=False, debate_rounds=1)
    assert result["recommendation"] in {"BUY", "SELL", "HOLD"}
    assert result["workflow"] == "tradingagents"
    assert result["executed"] is False
    assert "package" in result
    assert "debate" in result
    any_key = any(get_settings().provider_configured(p) for p in ("openai", "xai", "anthropic"))
    if not any_key:
        assert result["recommendation"] == "HOLD"
    if result.get("package"):
        assert result["package"]["direction"] in {"BUY", "SELL", "HOLD"}
    # Relatórios inválidos nunca contam como análise válida
    for report in result.get("reports") or []:
        if report.get("status") != "ok":
            assert report.get("valid_for_analysis", False) is False


def test_classic_workflow_still_available(db) -> None:
    from app.config import get_settings

    enable_ai_offline_no_approval()
    result = run_collaboration(db, execute=False, workflow="classic")
    assert result["workflow"] == "classic"
    assert result["recommendation"] in {"BUY", "SELL", "HOLD"}
    if not any(get_settings().provider_configured(p) for p in ("openai", "xai", "anthropic")):
        assert result["recommendation"] == "HOLD"


def test_memory_rejects_lookahead(db) -> None:
    now = datetime.now(timezone.utc)
    mem = save_decision_memory(
        db,
        analysis_run_id=0,
        decision_id=0,
        market_snapshot_id="abc",
        analysis_timestamp=now,
        symbol="EURUSD",
        timeframe="M15",
        agent_reports={},
        debate={},
        final_decision={"direction": "HOLD"},
    )
    with pytest.raises(ValueError, match="Look-ahead"):
        record_simulated_outcome(
            db,
            mem.id,
            realized_virtual_pnl=10.0,
            outcome={"note": "cedo demais"},
            as_of=now - timedelta(hours=1),
        )
    updated = record_simulated_outcome(
        db,
        mem.id,
        realized_virtual_pnl=-5.0,
        outcome={"note": "depois da análise"},
        lessons=["Respeitar veto de risco"],
        as_of=now + timedelta(minutes=5),
    )
    assert updated.realized_virtual_pnl == -5.0
    assert updated.outcome_recorded_at is not None


def test_analysis_api_start_offline(client) -> None:
    from app.config import get_settings

    token = authorize_via_api(client)
    res = client.post(
        "/api/analysis/start",
        json={
            "symbol": "EURUSD",
            "timeframe": "M15",
            "execute": False,
            "debate_rounds": 0,
            "authorization_id": token,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["recommendation"] in {"BUY", "SELL", "HOLD"}
    assert data.get("workflow") == "tradingagents" or data.get("status") in {
        "completed",
        "blocked",
        "completed_without_debate",
        "completed_with_errors",
    }
    if not any(get_settings().provider_configured(p) for p in ("openai", "xai", "anthropic")):
        assert data["recommendation"] == "HOLD"
    hist = client.get("/api/analysis")
    assert hist.status_code == 200
    assert isinstance(hist.json(), list)
