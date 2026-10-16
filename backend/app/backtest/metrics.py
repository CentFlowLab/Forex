from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class PerformanceMetrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    max_drawdown_pct: float
    ending_equity: float
    starting_equity: float

    def as_dict(self) -> dict:
        return asdict(self)


def compute_metrics(pnls: list[float], starting_equity: float) -> PerformanceMetrics:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit else 0.0)
    return PerformanceMetrics(
        trades=len(pnls),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(pnls) * 100.0) if pnls else 0.0,
        net_pnl=sum(pnls),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=round(pf, 4),
        max_drawdown_pct=round(max_dd, 4),
        ending_equity=round(starting_equity + sum(pnls), 2),
        starting_equity=starting_equity,
    )
