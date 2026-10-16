"""Reproducible historical testing. Prevents look-ahead bias."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.baseline import sma_crossover_backtest
from app.backtest.metrics import compute_metrics
from app.config import get_settings
from app.models.backtest import BacktestRun
from app.models.market import Candle
from app.models.trading import ClosedTrade


def _candles(db: Session, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
    return list(
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


def run_baseline_backtest(
    db: Session,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    walk_forward: bool = False,
    in_sample_ratio: float = 0.7,
) -> BacktestRun:
    settings = get_settings()
    candles = _candles(db, symbol, timeframe, start, end)
    if not candles:
        raise ValueError("No candles in the requested range.")

    is_synthetic = all(c.is_synthetic for c in candles)
    source = candles[0].source
    split = int(len(candles) * in_sample_ratio) if walk_forward else len(candles)
    in_sample = candles[:split]
    oos = candles[split:] if walk_forward else []

    trades_is = sma_crossover_backtest(
        in_sample,
        symbol=symbol,
        spread_pips=settings.sim_spread_pips,
        commission_per_lot=settings.sim_commission_per_lot,
    )
    trades_oos = (
        sma_crossover_backtest(
            oos,
            symbol=symbol,
            spread_pips=settings.sim_spread_pips,
            commission_per_lot=settings.sim_commission_per_lot,
        )
        if oos
        else []
    )
    all_trades = trades_is + trades_oos
    metrics = compute_metrics([t.pnl for t in all_trades], settings.starting_equity)
    oos_metrics = compute_metrics([t.pnl for t in trades_oos], settings.starting_equity) if trades_oos else None

    notes = (
        "DADOS SINTÉTICOS — não reivindique rentabilidade de mercado real."
        if is_synthetic
        else "Importação histórica. Os resultados são execuções simuladas, não desempenho real."
    )
    run = BacktestRun(
        name="sma20-50-baseline",
        symbol=symbol,
        timeframe=timeframe,
        start_ts=start,
        end_ts=end,
        engine="baseline",
        data_source=source,
        is_synthetic=is_synthetic,
        walk_forward=walk_forward,
        in_sample_bars=len(in_sample),
        out_of_sample_bars=len(oos),
        trades=metrics.trades,
        win_rate=metrics.win_rate,
        profit_factor=metrics.profit_factor,
        max_drawdown_pct=metrics.max_drawdown_pct,
        net_pnl=metrics.net_pnl,
        ending_equity=metrics.ending_equity,
        metrics_json=json.dumps(
            {
                "all": metrics.as_dict(),
                "out_of_sample": oos_metrics.as_dict() if oos_metrics else None,
            }
        ),
        notes=notes,
    )
    db.add(run)
    db.flush()
    for t in all_trades:
        db.add(
            ClosedTrade(
                symbol=symbol,
                side=t.side,
                volume_lots=0.10,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                stop_loss=0.0,
                take_profit=0.0,
                pnl=t.pnl,
                pips=t.pips,
                environment="BACKTEST",
                is_simulated=True,
                close_reason="BASELINE",
                opened_at=candles[t.entry_index].ts,
                closed_at=candles[t.exit_index].ts,
                backtest_run_id=run.id,
            )
        )
    db.commit()
    db.refresh(run)
    return run


def run_ai_sample_backtest(
    db: Session,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    max_bars: int = 12,
    step: int = 20,
    workflow: str = "classic",
) -> dict[str, Any]:
    """
    Amostragem AI walk-forward: em cada barra os agentes só veem dados até essa barra.
    Limitado por defeito porque cada barra custa várias chamadas ao modelo.
    Sem fornecedor configurado, os relatórios ficam unavailable — nunca fabricados.
    """
    from app.agents.orchestrator import run_collaboration

    candles = _candles(db, symbol, timeframe, start, end)
    if not candles:
        raise ValueError("Sem velas no intervalo solicitado.")
    picks = candles[50::step][:max_bars]
    results = []
    api_cost = 0.0
    for bar in picks:
        r = run_collaboration(
            db,
            symbol,
            timeframe,
            as_of=bar.ts,
            execute=False,
            workflow=workflow,
            debate_rounds=1 if workflow == "tradingagents" else None,
        )
        results.append(r)
        for rep in r.get("reports") or []:
            api_cost += float(rep.get("estimated_cost_usd") or 0)
        if r.get("package"):
            api_cost = max(api_cost, float(r["package"].get("estimated_cost_usd") or 0))

    recs = [r["recommendation"] for r in results]
    return {
        "engine": f"ai-sample:{workflow}",
        "workflow": workflow,
        "bars_evaluated": len(results),
        "is_synthetic": all(c.is_synthetic for c in candles),
        "disclaimer": (
            "Decisões de IA amostradas em barras históricas (sem look-ahead). "
            "Não é um backtest completo trade-a-trade. "
            "Não reivindique rentabilidade a partir de dados sintéticos ou amostras insuficientes."
        ),
        "recommendation_counts": {
            "BUY": recs.count("BUY"),
            "SELL": recs.count("SELL"),
            "HOLD": recs.count("HOLD"),
        },
        "ai_api_cost_usd": round(api_cost, 6),
        "results": [
            {
                "decision_id": r["decision_id"],
                "as_of": (r.get("market") or {}).get("as_of"),
                "recommendation": r["recommendation"],
                "risk_approved": r["risk_approved"],
                "data_source": r["data_source"],
                "workflow": r.get("workflow"),
            }
            for r in results
        ],
    }


def compare_workflows(
    db: Session,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    max_bars: int = 6,
    step: int = 20,
) -> dict[str, Any]:
    """
    Compara A) SMA20/50 baseline, B) fluxo clássico de 6 agentes, C) debate TradingAgents.
    Mesmo intervalo e dataset; amostragem cronológica out-of-sample para IA.
    """
    settings = get_settings()
    baseline = run_baseline_backtest(db, symbol, timeframe, start, end, walk_forward=True)
    classic = run_ai_sample_backtest(
        db, symbol, timeframe, start, end, max_bars=max_bars, step=step, workflow="classic"
    )
    debate = run_ai_sample_backtest(
        db, symbol, timeframe, start, end, max_bars=max_bars, step=step, workflow="tradingagents"
    )
    txn_cost_note = (
        f"Spread simulado {settings.sim_spread_pips} pips · "
        f"comissão {settings.sim_commission_per_lot} USD/lote · "
        f"slippage {settings.sim_slippage_pips} pips"
    )
    return {
        "engine": "compare-workflows",
        "symbol": symbol,
        "timeframe": timeframe,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "transaction_costs": txn_cost_note,
        "disclaimer": (
            "Comparação amostrada. O baseline SMA produz operações simuladas completas; "
            "os fluxos de IA reportam recomendações amostradas (sem look-ahead). "
            "Não reivindique rentabilidade de mercado real a partir de dados sintéticos "
            "ou testes insuficientes."
        ),
        "A_baseline_sma20_50": {
            "net_virtual_pnl": baseline.net_pnl,
            "win_rate": baseline.win_rate,
            "profit_factor": baseline.profit_factor,
            "max_drawdown_pct": baseline.max_drawdown_pct,
            "trades": baseline.trades,
            "ending_equity": baseline.ending_equity,
            "is_synthetic": baseline.is_synthetic,
            "ai_api_cost_usd": 0.0,
        },
        "B_classic_six_agents": {
            "recommendation_counts": classic["recommendation_counts"],
            "bars_evaluated": classic["bars_evaluated"],
            "ai_api_cost_usd": classic["ai_api_cost_usd"],
            "is_synthetic": classic["is_synthetic"],
            "note": "Amostragem de recomendações; não equivale a P&L trade-a-trade completo.",
        },
        "C_tradingagents_debate": {
            "recommendation_counts": debate["recommendation_counts"],
            "bars_evaluated": debate["bars_evaluated"],
            "ai_api_cost_usd": debate["ai_api_cost_usd"],
            "is_synthetic": debate["is_synthetic"],
            "note": "Debate Bull/Bear com rondas limitadas; custos de API tipicamente superiores.",
        },
    }
