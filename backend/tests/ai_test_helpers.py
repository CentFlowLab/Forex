"""Helpers para testes offline de IA sem créditos reais."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.control import get_ai_control, reset_ai_control_for_tests
from app.config import get_settings


def _strip_provider_keys() -> None:
    """Impede HTTP real mesmo que .env tenha chaves — testes nunca gastam créditos."""
    settings = get_settings()
    object.__setattr__(settings, "openai_api_key", "")
    object.__setattr__(settings, "xai_api_key", "")
    object.__setattr__(settings, "anthropic_api_key", "")


def enable_ai_offline_no_approval() -> None:
    """Ativa IA em memória e dispensa autorização (só para testes sem HTTP real)."""
    ctrl = reset_ai_control_for_tests()
    ctrl.set_enabled(True)
    settings = get_settings()
    object.__setattr__(settings, "ai_require_manual_approval", False)
    object.__setattr__(settings, "ai_enabled", True)
    _strip_provider_keys()


def authorize_via_api(client) -> str:
    ctrl = get_ai_control()
    ctrl.set_enabled(True)
    settings = get_settings()
    object.__setattr__(settings, "ai_require_manual_approval", True)
    object.__setattr__(settings, "ai_enabled", True)
    _strip_provider_keys()
    res = client.post("/api/ai/authorize", json={"confirmed": True, "debate_rounds": 0})
    assert res.status_code == 200, res.text
    return res.json()["authorization_id"]
