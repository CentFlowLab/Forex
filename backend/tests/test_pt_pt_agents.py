from __future__ import annotations

from app.agents.prompts import CORE_AGENT_ROLES, DISPLAY_NAMES, PT_PT_RULE, SYSTEM_PROMPTS


def test_core_agent_display_names_pt() -> None:
    assert DISPLAY_NAMES["technical"] == "Analista Técnico"
    assert DISPLAY_NAMES["trend"] == "Analista de Tendências"
    assert DISPLAY_NAMES["fundamental"] == "Analista Fundamental e Macroeconómico"
    assert DISPLAY_NAMES["strategy"] == "Analista de Estratégias"
    assert DISPLAY_NAMES["risk_challenger"] == "Analista de Risco"
    assert DISPLAY_NAMES["coordinator"] == "Coordenador Principal"
    assert len(CORE_AGENT_ROLES) == 6


def test_debate_roles_pt() -> None:
    assert DISPLAY_NAMES["bull_researcher"] == "Analista Comprador"
    assert DISPLAY_NAMES["bear_researcher"] == "Analista Vendedor"
    assert DISPLAY_NAMES["risk_manager"] == "Gestor de Risco"
    assert DISPLAY_NAMES["trading_decision"] == "Decisão de Negociação"


def test_all_prompts_require_pt_pt() -> None:
    assert "português de Portugal" in PT_PT_RULE
    for role, prompt in SYSTEM_PROMPTS.items():
        assert "português de Portugal" in prompt, role
        assert "português do Brasil" in prompt, role
