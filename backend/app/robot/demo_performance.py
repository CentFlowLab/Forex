"""Avaliação de decisões/operações DEMO — separada de resultados sintéticos.

Não declara rentabilidade sem amostra suficiente.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.robot import AIDecisionLog

try:
    from app.models.demo_execution import DemoExecutionAttempt
except Exception:  # noqa: BLE001
    DemoExecutionAttempt = None  # type: ignore

_MIN_SAMPLE_FOR_EDGE = 30


def _is_synthetic(log: AIDecisionLog) -> bool:
    import json

    try:
        ctx = json.loads(log.context_json or "{}")
    except Exception:  # noqa: BLE001
        ctx = {}
    if ctx.get("synthetic_test") or ctx.get("synthetic") or ctx.get("test_harness"):
        return True
    mode = (log.mode or "").lower()
    if mode.startswith("simulated"):
        return True
    if float(log.estimated_cost_usd or 0) <= 0 and mode.startswith("openai"):
        return True
    return False


def compute_demo_performance(db: Session, *, limit: int = 500) -> dict[str, Any]:
    logs = list(
        db.scalars(
            select(AIDecisionLog).order_by(AIDecisionLog.id.desc()).limit(limit)
        )
    )
    real = [x for x in logs if not _is_synthetic(x)]
    synthetic = [x for x in logs if _is_synthetic(x)]

    decisions = {"BUY": 0, "SELL": 0, "HOLD": 0, "other": 0}
    for x in real:
        d = (x.decision or "").upper()
        if d in decisions:
            decisions[d] += 1
        else:
            decisions["other"] += 1

    executed = [x for x in real if x.executed]
    attempts: list[Any] = []
    if DemoExecutionAttempt is not None:
        try:
            attempts = list(
                db.scalars(
                    select(DemoExecutionAttempt)
                    .order_by(DemoExecutionAttempt.id.desc())
                    .limit(limit)
                )
            )
        except Exception:  # noqa: BLE001
            attempts = []

    wins = losses = 0
    net = 0.0
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    exec_costs = 0.0

    for a in attempts:
        pnl = float(getattr(a, "realized_pnl", 0) or getattr(a, "pnl", 0) or 0)
        cost = float(getattr(a, "commission", 0) or 0) + float(
            getattr(a, "swap", 0) or 0
        )
        status = str(getattr(a, "status", "") or "").lower()
        if status not in {"filled", "closed", "done", "ok"} and pnl == 0:
            continue
        net += pnl - cost
        exec_costs += cost
        equity += pnl - cost
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += abs(pnl)

    closed_n = wins + losses
    hit_rate = (wins / closed_n) if closed_n else None
    profit_factor = (
        (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit <= 0 else None)
    )
    sample_ok = closed_n >= _MIN_SAMPLE_FOR_EDGE

    rejected_reasons: dict[str, int] = {}
    for x in real:
        if (x.decision or "").upper() != "HOLD":
            continue
        key = (x.risk_reasons or x.justification or "HOLD")[:120]
        rejected_reasons[key] = rejected_reasons.get(key, 0) + 1

    return {
        "opportunities_analyzed": len(real),
        "synthetic_excluded": len(synthetic),
        "decisions": decisions,
        "trades_executed": len(executed),
        "demo_attempts": len(attempts),
        "closed_trades_with_pnl": closed_n,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "net_pnl": round(net, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "execution_costs": round(exec_costs, 2),
        "sample_sufficient_for_edge_claim": sample_ok,
        "min_sample_required": _MIN_SAMPLE_FOR_EDGE,
        "disclaimer": (
            "Amostra insuficiente para declarar rentabilidade."
            if not sample_ok
            else "Amostra mínima atingida — interpretar com cautela (DEMO)."
        ),
        "top_hold_reasons": sorted(
            rejected_reasons.items(), key=lambda kv: kv[1], reverse=True
        )[:8],
        "source": "demo_real_paid_openai_only",
    }
