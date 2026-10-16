"""Avaliação de decisões IA — simulado; não apresenta rentabilidade sem evidência."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.robot import AIDecisionLog
from app.models.trading import ClosedTrade


def sync_outcomes(db: Session) -> int:
    """Associa P&L de trades fechados a logs IA executados (por client_order_id)."""
    updated = 0
    logs = list(
        db.scalars(
            select(AIDecisionLog).where(
                AIDecisionLog.executed == True,  # noqa: E712
                AIDecisionLog.outcome_pnl.is_(None),
            )
        )
    )
    for log in logs:
        if not log.client_order_id:
            continue
        trade = db.scalar(
            select(ClosedTrade).where(ClosedTrade.client_order_id == log.client_order_id).limit(1)
        )
        if trade is None:
            continue
        log.outcome_pnl = float(trade.pnl)
        updated += 1
    if updated:
        db.commit()
    return updated


def evaluation_summary(db: Session) -> dict[str, Any]:
    """Métricas simples: IA vs AGUARDAR e referências. Sem alegar lucro real."""
    sync_outcomes(db)
    logs = list(db.scalars(select(AIDecisionLog).order_by(AIDecisionLog.id.desc()).limit(200)))
    if not logs:
        return {
            "decisions": 0,
            "executed": 0,
            "net_pnl_simulated": 0.0,
            "hold_baseline_pnl": 0.0,
            "vs_hold": "sem dados",
            "max_drawdown": 0.0,
            "consistency_note": "Sem decisões registadas.",
            "disclaimer": "Resultados simulados — não são lucros reais nem prova de rentabilidade.",
        }

    executed = [x for x in logs if x.executed and x.outcome_pnl is not None]
    holds = [x for x in logs if x.decision == "HOLD"]
    buys = sum(1 for x in logs if x.decision == "BUY")
    sells = sum(1 for x in logs if x.decision == "SELL")
    pnls = [float(x.outcome_pnl or 0) for x in executed]
    net = sum(pnls)
    # Baseline AGUARDAR: 0 por definição (sem operação).
    hold_baseline = 0.0
    # Referência ingénua: sempre comprar o mesmo tamanho não é modelada aqui —
    # só comparamos com AGUARDAR (PnL=0).
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in reversed(pnls):  # ordem cronológica aproximada (lista desc → reverse)
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    wins = sum(1 for p in pnls if p > 0)
    consistency = (
        f"{wins}/{len(pnls)} operações com P&L>0"
        if pnls
        else "Nenhuma operação fechada com resultado associado."
    )
    vs = "acima de AGUARDAR" if net > 0 else ("igual a AGUARDAR" if abs(net) < 1e-9 else "abaixo de AGUARDAR")

    return {
        "decisions": len(logs),
        "buy": buys,
        "sell": sells,
        "hold": len(holds),
        "executed": len([x for x in logs if x.executed]),
        "with_outcome": len(executed),
        "net_pnl_simulated": round(net, 2),
        "hold_baseline_pnl": hold_baseline,
        "vs_hold": vs,
        "max_drawdown": round(max_dd, 2),
        "consistency_note": consistency,
        "disclaimer": (
            "Resultados simulados na conta virtual. "
            "Não são lucros reais. Sistema não é considerado rentável sem evidência robusta."
        ),
    }
