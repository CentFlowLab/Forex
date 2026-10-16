from __future__ import annotations

from app.agents.orchestrator import run_collaboration
from app.config import get_settings
from tests.ai_test_helpers import enable_ai_offline_no_approval


def test_collaboration_without_keys_does_not_fabricate(db) -> None:
    enable_ai_offline_no_approval()
    result = run_collaboration(db, execute=False)
    assert result["recommendation"] in {"BUY", "SELL", "HOLD"}
    assert result["executed"] is False
    settings = get_settings()
    any_key = any(
        settings.provider_configured(p) for p in ("openai", "xai", "anthropic")
    )
    for report in result["reports"]:
        assert report["status"] in {
            "unavailable",
            "disabled",
            "error",
            "ok",
            "invalid_json",
            "language_rejected",
        }
        if report["status"] != "ok":
            assert report.get("error") or report["status"] in {"disabled", "unavailable"}
            # Agente inválido não contribui com BUY/SELL utilizável
            assert report["recommendation"] == "HOLD"
            assert report.get("valid_for_analysis", False) is False
    if not any_key:
        assert result["recommendation"] == "HOLD"
        # is_synthetic=False é válido com MT5/histórico real — o que importa é
        # não fabricar recomendações BUY/SELL sem chaves (relatórios inválidos = HOLD).
        assert result.get("executed") is False
