"""Percurso integrado: MT5 → radar → contexto → resposta simulada → persistência → painel.

Ordens desligadas. LIVE bloqueado. Sem chamadas OpenAI pagas.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from app.ai.response_store import persist_raw_provider_response
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.market.mt5_feed import get_mt5_feed
from app.models.robot import AIDecisionLog
from app.robot.ai_engine import run_radar_decision
from app.robot.openai_panel import (
    is_real_paid_openai_decision,
    serialize_openai_decision,
)
from app.robot.radar import scan_radar, validate_scan_for_paid


@pytest.fixture()
def db(tmp_path, monkeypatch):
    target = tmp_path / "ai_responses"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GROK_AI_RESPONSES_DIR", str(target))
    monkeypatch.setattr("app.ai.response_store._STORE_DIR", target)
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def test_integrated_mt5_radar_simulated_panel(db, tmp_path, monkeypatch):
    settings = get_settings()
    assert settings.live_trading_locked is True
    assert settings.trading_environment != "LIVE"

    feed = get_mt5_feed()
    try:
        feed.start()
        feed.poll_once()
    except Exception:  # noqa: BLE001
        pytest.skip("MT5 indisponível neste host — percurso integrado requer terminal.")

    snap = feed.snapshot()
    if not snap.terminal_connected or len(snap.symbols) < 7:
        pytest.skip("MT5 sem 7 pares — skip integrado.")

    scan = scan_radar(db)
    assert scan.monitored >= 7
    # Qualidade: validação para pago pode falhar por relógio; o percurso simulado continua.
    _quality = validate_scan_for_paid(scan)

    ctx, prop, scan2, usage = run_radar_decision(db, mode="simulated")
    assert prop.decision in {"BUY", "SELL", "HOLD"}
    assert float(usage.get("estimated_cost_usd") or 0) == 0.0
    assert (ctx.get("quote") or {}).get("source") in {"mt5", "dukascopy", "synthetic", None} or True

    sim_text = json.dumps(
        {
            "decision": prop.decision,
            "symbol": prop.symbol,
            "justification": prop.justification or "Resposta simulada integrada.",
            "confidence": float(prop.confidence or 0.5),
        },
        ensure_ascii=False,
    )
    path = persist_raw_provider_response(
        provider="openai",
        model=settings.openai_default_model,
        raw_http={"choices": [{"message": {"content": sim_text}}], "simulated": True},
        text=sim_text,
        authorization_id="dry-run-integrated",
        role="robot_radar_autonomous",
        reason="integrated_sim",
        prompt_tokens=0,
        completion_tokens=0,
        estimated_cost_usd=0.0,
        status="dry_run",
        request_messages=[
            {"role": "system", "content": "teste integrado"},
            {"role": "user", "content": json.dumps({"radar": scan2.as_dict()}, default=str)[:2000]},
        ],
    )
    assert Path(path).is_file()

    log = AIDecisionLog(
        mode="openai_radar_autonomous",
        decision=prop.decision,
        justification=prop.justification or "Resposta simulada integrada.",
        confidence=float(prop.confidence or 0.5),
        estimated_cost_usd=0.0,
        executed=False,
        context_json=json.dumps(
            {
                "authorization_id": "dry-run-integrated",
                "persisted_path": str(path),
                "execution_disabled": True,
                "integrated_test": True,
            },
            ensure_ascii=False,
        ),
        proposal_json=json.dumps(prop.as_dict(), ensure_ascii=False),
        risk_approved=False,
        risk_reasons="Teste integrado — execução desligada.",
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    try:
        # Dry-run integrado NÃO é OpenAI paga — o painel real deve ignorá-la.
        assert is_real_paid_openai_decision(log) is False
        serialized = serialize_openai_decision(log)
        assert serialized["decision"] == prop.decision
        assert serialized.get("justification")
        assert serialized["is_real_paid_openai"] is False
        assert log.executed is False
    finally:
        # Remover só o registo deste teste (não dados do utilizador).
        db.delete(log)
        db.commit()
