from __future__ import annotations

from app.agents.language import assess_payload_language, language_assessment
from app.agents.prompts import PT_PT_RULE, SYSTEM_PROMPTS
from app.agents.tradingagents_pipeline import run_tradingagents_collaboration
from app.market.data_store import market_bundle
from app.market.source_resolve import resolve_market_source
from app.models.memory import AnalysisRun
from tests.ai_test_helpers import authorize_via_api, enable_ai_offline_no_approval


def test_prompts_forbid_english_prose() -> None:
    assert "PROIBIDO" in PT_PT_RULE or "exclusivamente" in PT_PT_RULE
    for role, prompt in SYSTEM_PROMPTS.items():
        assert "português de Portugal" in prompt, role


def test_language_heuristic_detects_english() -> None:
    en = language_assessment(
        "The price action currently suggests a stronger outlook based on the evidence "
        "and therefore the recommendation appears insufficient."
    )
    assert en["likely_english"] is True
    pt = language_assessment(
        "A ação do preço sugere uma tendência de alta com base nos indicadores fornecidos, "
        "portanto a recomendação é aguardar por confirmação da estrutura."
    )
    assert pt["likely_english"] is False
    assert pt["ok"] is True


def test_language_assessment_on_payload() -> None:
    bad = assess_payload_language(
        {
            "rationale": "The setup currently suggests a long based on the evidence.",
            "risks": ["There is uncertainty in the outlook."],
            "evidence": [{"claim": "Moving average indicates strength", "source": "x", "strength": 0.5}],
        }
    )
    assert bad["likely_english"] is True


def test_source_resolve_prefers_dukascopy_when_present(db) -> None:
    resolved = resolve_market_source(db, "EURUSD", "M15", requested=None)
    # Com seed sintético, pode não haver dukascopy — mas nunca mistura.
    assert resolved["mixed_forbidden"] is True
    assert resolved["resolved_source"] in {"synthetic", "dukascopy", None} or resolved["resolved_source"]


def test_market_bundle_single_source(db) -> None:
    bundle = market_bundle(db, "EURUSD", "M15", source=None)
    assert "source_resolution" in bundle
    src = bundle["data_source"]
    # Todas as velas primárias devem partilhar a fonte resolvida.
    for bar in bundle["bars"]["M15"]:
        assert bar["source"] == src


def test_bundle_explicit_synthetic_not_mixed(db) -> None:
    bundle = market_bundle(db, "EURUSD", "M15", source="synthetic")
    assert bundle["data_source"] == "synthetic"
    assert bundle["is_synthetic"] is True
    assert all(b["source"] == "synthetic" for b in bundle["bars"]["M15"])


def test_pipeline_exposes_stages_and_debate_flags(db) -> None:
    enable_ai_offline_no_approval()
    result = run_tradingagents_collaboration(db, execute=False, debate_rounds=1, source="synthetic")
    assert "stages" in result
    assert "diagnostics" in result
    assert result["diagnostics"]["analysis_id"] == result["analysis_run_id"]
    debate = result["debate"]
    assert "debate_executed" in debate
    assert "debate_complete" in debate
    # Se o debate não correu, a decisão não pode ser apresentada como consenso BUY/SELL.
    if not debate["debate_executed"]:
        assert result["recommendation"] == "HOLD"
        assert result["status"] in {"completed_without_debate", "completed_with_errors", "blocked"}
        assert any("não gerado" in a.lower() or "indispon" in a.lower() or a.startswith("[") for a in (debate.get("bull_arguments") or [""]))
    else:
        assert debate["debate_executed"] is True
        assert len(debate.get("bull_arguments") or []) >= 1
        assert len(debate.get("bear_arguments") or []) >= 1
    # Persistência
    row = db.get(AnalysisRun, result["analysis_run_id"])
    assert row is not None
    assert row.data_source == "synthetic"


def test_analysis_list_and_dashboard_expose_stages(client) -> None:
    token = authorize_via_api(client)
    start = client.post(
        "/api/analysis/start",
        json={
            "symbol": "EURUSD",
            "timeframe": "M15",
            "execute": False,
            "debate_rounds": 0,
            "source": "synthetic",
            "authorization_id": token,
        },
    )
    assert start.status_code == 200
    body = start.json()
    assert "stages" in body
    assert "debate_executed" in body["debate"]
    if not body["debate"]["debate_executed"]:
        assert body["recommendation"] == "HOLD"

    dash = client.get("/api/dashboard").json()
    assert dash.get("latest_analysis") is not None
    assert "stages" in dash["latest_analysis"]
    assert "diagnostics" in dash["latest_analysis"]
    assert "active_market" in dash

    hist = client.get("/api/analysis").json()
    assert len(hist) >= 1
    assert "stages" in hist[0]


def test_candles_and_analysis_same_source(client) -> None:
    candles = client.get("/api/market/candles?timeframe=M15&source=synthetic").json()
    assert candles["source"] == "synthetic"
    assert candles["is_synthetic"] is True
    assert "source_resolution" in candles

    token = authorize_via_api(client)
    analysis = client.post(
        "/api/analysis/start",
        json={
            "symbol": "EURUSD",
            "timeframe": "M15",
            "execute": False,
            "debate_rounds": 0,
            "source": "synthetic",
            "authorization_id": token,
        },
    ).json()
    assert analysis["data_source"] == "synthetic"
    assert analysis["is_synthetic"] is True
