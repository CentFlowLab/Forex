"""Testes de segurança de custos de IA — sem chamadas reais a fornecedores."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control, reset_ai_control_for_tests
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.config import get_settings


@pytest.fixture()
def ai_ctrl():
    ctrl = reset_ai_control_for_tests()
    # Garantir predefinição desligada.
    ctrl._runtime_enabled = False
    ctrl._emergency_block = False
    yield ctrl
    reset_ai_control_for_tests()


def test_ai_disabled_by_default(ai_ctrl) -> None:
    # Predefinição de configuração (env/settings) — independente de overrides de runtime.
    assert get_settings().ai_enabled is False
    ai_ctrl._runtime_enabled = False
    ai_ctrl._emergency_block = False
    assert ai_ctrl.is_enabled() is False


def test_provider_blocked_when_ai_disabled(ai_ctrl) -> None:
    ai_ctrl._runtime_enabled = False
    provider = OpenAICompatProvider("openai", "sk-test", "https://example.invalid/v1", ["gpt-4o-mini"])
    with pytest.raises(Exception) as exc:
        provider.complete([], "gpt-4o-mini", 0.2, 100)
    assert "desligada" in str(exc.value).lower() or "bloqueada" in str(exc.value).lower()


def test_authorize_requires_enabled_and_confirm(ai_ctrl, db) -> None:
    ai_ctrl._runtime_enabled = False
    with pytest.raises(AIDisabled):
        ai_ctrl.create_authorization(db, confirmed=True)
    ai_ctrl.set_enabled(True)
    with pytest.raises(AIAuthorizationError):
        ai_ctrl.create_authorization(db, confirmed=False)


def test_authorization_single_use_and_call_limit(ai_ctrl, db) -> None:
    ai_ctrl.set_enabled(True)
    auth = ai_ctrl.create_authorization(db, confirmed=True, debate_rounds=1)
    token = auth["authorization_id"]
    ai_ctrl.begin_analysis_context(token, analysis_run_id=1)
    ai_ctrl.set_call_meta(role="technical", reason="test")
    # Consumir todas as chamadas autorizadas
    for _ in range(auth["max_calls"]):
        ai_ctrl.before_provider_call(provider="openai", model="gpt-4o-mini")
    with pytest.raises(AIAuthorizationError):
        ai_ctrl.before_provider_call(provider="openai", model="gpt-4o-mini")
    ai_ctrl.end_analysis_context(token)
    # Não reutilizável
    with pytest.raises(AIAuthorizationError):
        ai_ctrl.begin_analysis_context(token, analysis_run_id=2)


def test_emergency_block_cancels_auth(ai_ctrl, db) -> None:
    ai_ctrl.set_enabled(True)
    auth = ai_ctrl.create_authorization(db, confirmed=True)
    token = auth["authorization_id"]
    ai_ctrl.block_emergency()
    assert ai_ctrl.is_enabled() is False
    with pytest.raises((AIAuthorizationError, AIDisabled)):
        ai_ctrl.begin_analysis_context(token, analysis_run_id=9)


def test_concurrent_analyses_blocked(ai_ctrl, db) -> None:
    settings = get_settings()
    object.__setattr__(settings, "ai_max_concurrent_analyses", 1)
    ai_ctrl.set_enabled(True)
    a1 = ai_ctrl.create_authorization(db, confirmed=True)
    ai_ctrl.begin_analysis_context(a1["authorization_id"], analysis_run_id=1)
    ai_ctrl.bind_authorization(a1["authorization_id"], 1)
    with pytest.raises(AIAuthorizationError):
        ai_ctrl.create_authorization(db, confirmed=True)


def test_dashboard_endpoints_do_not_call_openai(client, ai_ctrl) -> None:
    ai_ctrl._runtime_enabled = False
    with patch("app.ai.providers.openai_compat.httpx.Client") as client_cls:
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/market/quote-feed").status_code == 200
        assert client.get("/api/adaptive/dashboard").status_code == 200
        assert client.get("/api/ai/status").status_code == 200
        client_cls.assert_not_called()


def test_analysis_start_requires_authorization(client, ai_ctrl) -> None:
    ai_ctrl._runtime_enabled = False
    res = client.post(
        "/api/analysis/start",
        json={"symbol": "EURUSD", "timeframe": "M15", "execute": False},
    )
    assert res.status_code == 403


def test_analysis_start_with_auth_offline_no_http(client, ai_ctrl, db) -> None:
    from tests.ai_test_helpers import _strip_provider_keys

    ai_ctrl.set_enabled(True)
    _strip_provider_keys()
    auth = client.post(
        "/api/ai/authorize",
        json={"confirmed": True, "debate_rounds": 0},
    )
    assert auth.status_code == 200
    token = auth.json()["authorization_id"]
    with patch("app.ai.providers.openai_compat.httpx.Client") as client_cls:
        res = client.post(
            "/api/analysis/start",
            json={
                "symbol": "EURUSD",
                "timeframe": "M15",
                "execute": False,
                "debate_rounds": 0,
                "authorization_id": token,
                "source": "synthetic",
            },
        )
        # Pode ser 200 (agentes offline) — nunca HTTP real.
        assert res.status_code == 200
        client_cls.assert_not_called()


def test_block_endpoint(client, ai_ctrl) -> None:
    ai_ctrl.set_enabled(True)
    res = client.post("/api/ai/block")
    assert res.status_code == 200
    assert res.json()["ai_enabled"] is False
    assert get_ai_control().is_enabled() is False
