"""Compare providers on identical historical bars. No fabricated outputs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import load_agent_config
from app.ai.registry import get_provider
from app.models.agent import AgentConfig
from app.models.backtest import EvaluationRun
from app.models.market import Candle


def compare_providers_on_history(
    db: Session,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    providers: list[str],
    max_bars: int = 5,
) -> EvaluationRun:
    from app.agents.orchestrator import run_collaboration

    candles = list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.ts >= start,
                Candle.ts <= end,
            )
            .order_by(Candle.ts.asc())
        )
    )
    if not candles:
        raise ValueError("No candles in range.")

    original = {cfg.role: (cfg.provider, cfg.model) for cfg in db.scalars(select(AgentConfig))}
    picks = candles[50:: max(1, len(candles) // max(max_bars, 1))][:max_bars]
    results: dict[str, Any] = {}

    for provider_name in providers:
        try:
            provider = get_provider(provider_name)
        except Exception as exc:
            results[provider_name] = {"status": "unavailable", "error": str(exc), "decisions": []}
            continue
        if not provider.configured():
            results[provider_name] = {
                "status": "disabled",
                "error": f"{provider_name} is not configured.",
                "decisions": [],
            }
            continue
        model = provider.available_models()[0]
        for role in ("technical", "trend", "fundamental", "strategy", "risk_challenger", "coordinator"):
            cfg = load_agent_config(db, role)
            cfg.provider = provider_name
            cfg.model = model
        db.commit()
        decisions = []
        for bar in picks:
            out = run_collaboration(db, symbol, timeframe, as_of=bar.ts, execute=False)
            decisions.append(
                {
                    "as_of": bar.ts.isoformat(),
                    "recommendation": out["recommendation"],
                    "risk_approved": out["risk_approved"],
                    "decision_id": out["decision_id"],
                }
            )
        results[provider_name] = {"status": "ok", "model": model, "decisions": decisions}

    for role, (prov, model) in original.items():
        cfg = db.get(AgentConfig, role)
        if cfg:
            cfg.provider = prov
            cfg.model = model
    db.commit()

    run = EvaluationRun(
        name="provider-comparison",
        symbol=symbol,
        timeframe=timeframe,
        start_ts=start,
        end_ts=end,
        data_source=candles[0].source,
        providers_json=json.dumps(providers),
        results_json=json.dumps(results),
        notes="Identical bars were shown to each configured provider. Unconfigured providers were skipped.",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
