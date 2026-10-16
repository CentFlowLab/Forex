"""Simulador barra-a-barra sem look-ahead bias."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.adaptive.costs import apply_entry_price, estimate_round_trip_costs
from app.adaptive.regimes import classify_regime
from app.adaptive.strategies.base import Strategy
from app.backtest.metrics import compute_metrics
from app.config import get_settings
from app.risk.instruments import get_instrument


@dataclass
class SimTrade:
    side: str
    entry_index: int
    exit_index: int
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    pnl: float
    costs: float
    regime_at_entry: str
    stop: float
    take: float


@dataclass
class SimResult:
    trades: list[SimTrade] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    total_costs: float = 0.0
    regimes_seen: dict[str, int] = field(default_factory=dict)


def simulate_strategy(
    candles: list[Any],
    strategy: Strategy,
    *,
    symbol: str = "EURUSD",
    stop_pips: float = 15.0,
    take_pips: float = 25.0,
    lots: float = 0.10,
    split_label: str = "",
) -> SimResult:
    """Executa sinal do fecho[i] no open[i+1]. Nunca usa barras futuras nos indicadores."""
    if len(candles) < 60:
        return SimResult(metrics=compute_metrics([], get_settings().starting_equity).as_dict())

    settings = get_settings()
    spec = get_instrument(symbol)
    costs_tpl = estimate_round_trip_costs(symbol, lots)
    result = SimResult()
    position: dict[str, Any] | None = None

    opens = [float(c.open) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    closes = [float(c.close) for c in candles]
    # Janela limitada — evita fatias crescentes O(n²) em 12 meses de M15.
    lookback = 120

    for i in range(50, len(candles) - 1):
        lo = max(0, i + 1 - lookback)
        c_win = closes[lo : i + 1]
        h_win = highs[lo : i + 1]
        l_win = lows[lo : i + 1]
        o_win = opens[lo : i + 1]
        regime = classify_regime(c_win, h_win, l_win)
        result.regimes_seen[regime.label] = result.regimes_seen.get(regime.label, 0) + 1

        # Gestão da posição aberta no bar seguinte (i+1).
        if position is not None:
            bar = candles[i + 1]
            hit = None
            if position["side"] == "BUY":
                if float(bar.low) <= position["stop"]:
                    hit = position["stop"]
                elif float(bar.high) >= position["take"]:
                    hit = position["take"]
            else:
                if float(bar.high) >= position["stop"]:
                    hit = position["stop"]
                elif float(bar.low) <= position["take"]:
                    hit = position["take"]
            if hit is not None:
                pnl_raw = spec.pnl(position["side"], position["entry"], hit, lots, settings.account_currency)
                pnl = pnl_raw - position["costs"]
                trade = SimTrade(
                    side=position["side"],
                    entry_index=position["index"],
                    exit_index=i + 1,
                    entry_ts=candles[position["index"]].ts,
                    exit_ts=bar.ts,
                    entry_price=position["entry"],
                    exit_price=hit,
                    pnl=pnl,
                    costs=position["costs"],
                    regime_at_entry=position["regime"],
                    stop=position["stop"],
                    take=position["take"],
                )
                result.trades.append(trade)
                result.total_costs += position["costs"]
                result.decisions.append(
                    {
                        "decision_ts": candles[position["index"]].ts,
                        "regime": position["regime"],
                        "final_decision": position["side"],
                        "entry_price": position["entry"],
                        "exit_price": hit,
                        "spread_pips": costs_tpl.spread_pips,
                        "slippage_pips": costs_tpl.slippage_pips,
                        "commission": costs_tpl.commission,
                        "estimated_costs": position["costs"],
                        "pnl": pnl,
                        "outcome_available_at": bar.ts,
                        "split_label": split_label,
                        "strategy_key": strategy.spec.key,
                        "params": dict(strategy.params),
                    }
                )
                position = None

        if position is not None:
            continue

        # Sessão UTC da barra atual (sem futuro) — estratégias com filtro horário.
        from app.adaptive.strategies.session_trend import set_bar_hour

        set_bar_hour(strategy, getattr(candles[i], "ts", None))
        sig = strategy.signal(c_win, h_win, l_win, o_win)
        if sig == "HOLD":
            continue

        next_open = opens[i + 1]
        entry = apply_entry_price(
            sig,
            next_open,
            costs_tpl.spread_pips,
            costs_tpl.slippage_pips,
            symbol,
        )
        if sig == "BUY":
            stop = entry - spec.price_from_pips(stop_pips)
            take = entry + spec.price_from_pips(take_pips)
        else:
            stop = entry + spec.price_from_pips(stop_pips)
            take = entry - spec.price_from_pips(take_pips)

        position = {
            "side": sig,
            "entry": entry,
            "stop": stop,
            "take": take,
            "index": i + 1,
            "costs": costs_tpl.total,
            "regime": regime.label,
        }

    pnls = [t.pnl for t in result.trades]
    result.metrics = compute_metrics(pnls, settings.starting_equity).as_dict()
    result.metrics["total_costs"] = round(result.total_costs, 4)
    result.metrics["regimes_seen"] = dict(result.regimes_seen)
    return result
