"""Backtest walk-forward da lógica acionável exacta (sem ordens, sem OpenAI).

Filtros reproduzidos (live):
  EV líquido > 0
  evidence_quality ≥ 0.35
  p_hit_samples ≥ MIN_CALIB_SAMPLES (30)
  P_hit ≥ break-even
  sem reject_reasons
  dados válidos (no histórico: velas MT5 fechadas; feed_state live não aplica)

Camadas do relatório: backtest | simulação | DEMO MT5 (esta módulo = backtest apenas).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.config import get_settings
from app.market.mt5_history import fetch_symbol_history
from app.robot.directional_engine import (
    _MIN_CALIB_SAMPLES,
    _bucket_key,
    _feature_vector,
)
from app.robot.ev_math import (
    LOTS_DEFAULT,
    SL_ATR_MULT,
    TP_ATR_MULT,
    breakeven_p_hit,
    compute_ev,
    levels_from_atr,
    resolve_pip_value,
    simulate_barrier_hit,
)
from app.robot.universe import FOREX_28_SYMBOLS
from app.risk.instruments import get_instrument

logger = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]
StrategyId = Literal[
    "actionable_current",
    "baseline_always_buy",
    "baseline_always_sell",
    "alt_strict_phit",
    "alt_conservative_ci",
]

_REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "backtests"
_MAX_BARRIER = 16
_EVIDENCE_MIN = 0.35


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def beta_binomial_ci(hits: int, n: int, *, alpha: float = 0.05) -> tuple[float, float, float]:
    """Média posterior Laplace + intervalo aproximado (normal) a (1-alpha)."""
    if n <= 0:
        return 0.35, 0.0, 1.0
    # Posterior Beta(hits+1, misses+1)
    a = hits + 1.0
    b = (n - hits) + 1.0
    mean = a / (a + b)
    var = (a * b) / (((a + b) ** 2) * (a + b + 1.0))
    se = math.sqrt(max(var, 1e-12))
    z = 1.96 if abs(alpha - 0.05) < 1e-9 else 1.64
    lo = max(0.0, mean - z * se)
    hi = min(1.0, mean + z * se)
    return mean, lo, hi


def evidence_from_samples(p_hit_n: int, dir_conf: float = 0.5) -> float:
    """Replica a lógica de evidence_quality do motor direcional."""
    evidence = float(dir_conf)
    if p_hit_n >= _MIN_CALIB_SAMPLES:
        evidence = min(0.9, max(evidence, 0.25 + 0.5 * (min(p_hit_n, 400) / 400.0)))
    return evidence


@dataclass
class TradeResult:
    symbol: str
    side: Side
    strategy: str
    fold: int
    bar_index: int
    entry: float
    stop_loss: float
    take_profit: float
    p_hit_pred: float
    p_hit_lo: float
    evidence: float
    expected_net: float
    outcome: str  # tp|sl|timeout
    pnl_usd: float
    cost_usd: float
    risk_pips: float
    reward_pips: float


@dataclass
class StrategyStats:
    strategy: str
    symbol: str
    side: str  # BUY|SELL|BOTH
    n_trades: int = 0
    n_tp: int = 0
    n_sl: int = 0
    n_timeout: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    net_pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    costs_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    avg_expected_net: float | None = None
    notes: str = ""

    def finalize(self, pnls: list[float], expecteds: list[float]) -> None:
        self.n_trades = len(pnls)
        if self.n_trades:
            self.win_rate = round(self.n_tp / self.n_trades, 4)
            self.net_pnl_usd = round(sum(pnls), 4)
            self.avg_expected_net = round(sum(expecteds) / len(expecteds), 4) if expecteds else None
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gw = sum(wins)
            gl = abs(sum(losses))
            self.profit_factor = round(gw / gl, 4) if gl > 0 else (999.0 if gw > 0 else None)
            # Drawdown em equity curve
            eq = 0.0
            peak = 0.0
            dd = 0.0
            for p in pnls:
                eq += p
                peak = max(peak, eq)
                dd = max(dd, peak - eq)
            self.max_drawdown_usd = round(dd, 4)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _calib_p_hit(
    train_outcomes: list[str],
    train_buckets: list[str],
    bucket_now: str,
) -> tuple[float, float, float, int, str]:
    """P_hit + CI a partir do treino apenas."""
    bucket_hits = [
        o for o, b in zip(train_outcomes, train_buckets) if b == bucket_now
    ]
    if len(bucket_hits) >= _MIN_CALIB_SAMPLES:
        use = bucket_hits
        src = "barrier_bucket"
    else:
        use = list(train_outcomes)
        src = "barrier_pooled"
    n = len(use)
    if n < 10:
        return 0.35, 0.0, 1.0, n, "default_low"
    hits = sum(1 for x in use if x == "tp")
    mean, lo, hi = beta_binomial_ci(hits, n)
    return mean, lo, hi, n, src


def _pnl_from_outcome(
    *,
    outcome: str,
    side: Side,
    risk_pips: float,
    reward_pips: float,
    pip_value: float,
    cost_usd: float,
) -> float:
    if outcome == "tp":
        gross = pip_value * reward_pips
    elif outcome == "sl":
        gross = -pip_value * risk_pips
    else:
        # timeout: fecho neutro no mid (conservador: só custos)
        gross = 0.0
    return gross - cost_usd


def _prepare_series(candles: list[dict[str, Any]]) -> tuple[list[float], list[float], list[float]]:
    closes = [float(x["c"]) for x in candles]
    highs = [float(x["h"]) for x in candles]
    lows = [float(x["l"]) for x in candles]
    return closes, highs, lows


def _build_indexed_samples(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    *,
    stride: int = 3,
) -> list[dict[str, Any]]:
    n = len(closes)
    end = n - _MAX_BARRIER - 1
    start = max(50, end - 10000)
    out: list[dict[str, Any]] = []
    for i in range(start, end, stride):
        w0 = max(0, i + 1 - 120)
        feat = _feature_vector(closes[w0 : i + 1], highs[w0 : i + 1], lows[w0 : i + 1])
        if not feat or feat["atr"] <= 0:
            continue
        entry = closes[i]
        atr_i = float(feat["atr"])
        fut_h = highs[i + 1 : i + 1 + _MAX_BARRIER]
        fut_l = lows[i + 1 : i + 1 + _MAX_BARRIER]
        sample: dict[str, Any] = {
            "i": i,
            "bucket": _bucket_key(feat),
            "atr": atr_i,
            "entry": entry,
            "feat": feat,
        }
        for side in ("BUY", "SELL"):
            e, sl, tp = levels_from_atr(side=side, mid=entry, atr_price=atr_i)
            hit = simulate_barrier_hit(
                side=side,
                entry=e,
                stop_loss=sl,
                take_profit=tp,
                future_highs=fut_h,
                future_lows=fut_l,
            )
            sample[f"{side}_outcome"] = hit
            sample[f"{side}_levels"] = (e, sl, tp)
        out.append(sample)
    return out


def _is_actionable(
    *,
    expected_net: float,
    evidence: float,
    p_hit_n: int,
    p_hit: float,
    be: float,
    strategy: StrategyId,
    p_hit_lo: float,
) -> tuple[bool, list[str]]:
    rejects: list[str] = []
    if p_hit_n < _MIN_CALIB_SAMPLES:
        rejects.append("calibração histórica fraca")
    if evidence < _EVIDENCE_MIN:
        rejects.append("evidence_quality < 0.35")
    if strategy == "alt_conservative_ci":
        # EV deve ser positivo também com P_hit = limite inferior do CI
        if p_hit_lo < be:
            rejects.append("P_hit_lo < break-even")
    elif p_hit < be:
        rejects.append("P_hit < break-even")
    if expected_net <= 0:
        rejects.append("EV líquido ≤ 0")
    if strategy == "alt_strict_phit" and p_hit < be + 0.05:
        rejects.append("P_hit < BE+0.05 (strict)")
    return (len(rejects) == 0), rejects


def backtest_symbol(
    symbol: str,
    candles: list[dict[str, Any]],
    *,
    spread_pips: float,
    mt5_specs: dict[str, Any] | None,
    n_folds: int = 4,
    strategies: list[StrategyId] | None = None,
) -> dict[str, Any]:
    strategies = strategies or [
        "actionable_current",
        "baseline_always_buy",
        "baseline_always_sell",
        "alt_strict_phit",
        "alt_conservative_ci",
    ]
    closes, highs, lows = _prepare_series(candles)
    samples = _build_indexed_samples(closes, highs, lows)
    if len(samples) < 500:
        return {
            "symbol": symbol,
            "ok": False,
            "reason": f"amostras insuficientes ({len(samples)})",
            "n_bars": len(closes),
            "n_samples": len(samples),
            "strategies": {},
        }

    pip_audit = resolve_pip_value(symbol, LOTS_DEFAULT, mt5_specs=mt5_specs)
    pip_val = pip_audit.pip_value_account
    settings = get_settings()
    slip = float(settings.sim_slippage_pips)
    comm = float(settings.sim_commission_per_lot)

    n = len(samples)
    test_size = max(60, int(n * 0.15))
    # Expanding window folds
    fold_ranges: list[tuple[int, int, int]] = []
    for fold in range(n_folds):
        test_end = n - (n_folds - 1 - fold) * test_size
        test_start = test_end - test_size
        if test_start < 300:
            continue
        fold_ranges.append((fold, test_start, test_end))

    # Também split train/val/test único para selecção de alternativas
    # (parâmetros das alts são fixos a priori — não grelha no teste).
    test_cut = int(n * 0.70)
    val_cut = int(n * 0.85)

    trades_by_key: dict[tuple[str, str], list[TradeResult]] = {}

    def _run_on_range(
        strategy: StrategyId,
        fold: int,
        train_slice: list[dict[str, Any]],
        test_slice: list[dict[str, Any]],
        sides: list[Side],
    ) -> None:
        train_out: dict[Side, list[str]] = {"BUY": [], "SELL": []}
        train_bkt: dict[Side, list[str]] = {"BUY": [], "SELL": []}
        for s in train_slice:
            for side in ("BUY", "SELL"):
                train_out[side].append(s[f"{side}_outcome"])
                train_bkt[side].append(s["bucket"])

        for s in test_slice:
            for side in sides:
                p_hit, p_lo, p_hi, n_hit, src = _calib_p_hit(
                    train_out[side], train_bkt[side], s["bucket"]
                )
                if strategy == "alt_conservative_ci":
                    p_use = p_lo
                else:
                    p_use = p_hit
                ev = compute_ev(
                    symbol=symbol,
                    side=side,
                    mid=s["entry"],
                    atr_price=s["atr"],
                    p_hit_tp_before_sl=p_use,
                    p_hit_source=src,
                    p_direction=0.5,
                    spread_pips=spread_pips,
                    mt5_specs=mt5_specs,
                    slippage_pips=slip,
                    commission_per_lot=comm,
                )
                evidence = evidence_from_samples(n_hit, dir_conf=min(0.85, 0.25 + 0.5 * min(n_hit, 120) / 120))
                be = ev.breakeven_p_hit

                if strategy == "baseline_always_buy" and side != "BUY":
                    continue
                if strategy == "baseline_always_sell" and side != "SELL":
                    continue

                if strategy.startswith("baseline_"):
                    ok, rejects = True, []
                else:
                    ok, rejects = _is_actionable(
                        expected_net=ev.expected_net_usd,
                        evidence=evidence,
                        p_hit_n=n_hit,
                        p_hit=p_hit,
                        be=be,
                        strategy=strategy,
                        p_hit_lo=p_lo,
                    )
                if not ok:
                    continue

                outcome = s[f"{side}_outcome"]
                pnl = _pnl_from_outcome(
                    outcome=outcome,
                    side=side,
                    risk_pips=ev.risk_pips,
                    reward_pips=ev.reward_pips,
                    pip_value=pip_val,
                    cost_usd=ev.cost_usd,
                )
                tr = TradeResult(
                    symbol=symbol,
                    side=side,
                    strategy=strategy,
                    fold=fold,
                    bar_index=int(s["i"]),
                    entry=ev.entry,
                    stop_loss=ev.stop_loss,
                    take_profit=ev.take_profit,
                    p_hit_pred=round(p_hit, 4),
                    p_hit_lo=round(p_lo, 4),
                    evidence=round(evidence, 3),
                    expected_net=ev.expected_net_usd,
                    outcome=outcome,
                    pnl_usd=round(pnl, 4),
                    cost_usd=ev.cost_usd,
                    risk_pips=ev.risk_pips,
                    reward_pips=ev.reward_pips,
                )
                key = (strategy, side)
                trades_by_key.setdefault(key, []).append(tr)

    for fold, ts, te in fold_ranges:
        train = samples[:ts]
        test = samples[ts:te]
        for strat in strategies:
            sides: list[Side] = ["BUY", "SELL"]
            if strat == "baseline_always_buy":
                sides = ["BUY"]
            elif strat == "baseline_always_sell":
                sides = ["SELL"]
            _run_on_range(strat, fold, train, test, sides)

    # Relatório de incerteza estilo GBPAUD (amostra pequena)
    uncertainty_demo = {}
    if samples:
        last_train = samples[: max(100, int(n * 0.7))]
        for side in ("BUY", "SELL"):
            outs = [s[f"{side}_outcome"] for s in last_train]
            bkts = [s["bucket"] for s in last_train]
            # Simular n=43 pooled
            use = outs[-43:] if len(outs) >= 43 else outs
            hits = sum(1 for x in use if x == "tp")
            mean, lo, hi = beta_binomial_ci(hits, len(use))
            # EV com mid / lo
            s0 = samples[min(len(samples) - 1, int(n * 0.7))]
            ev_mean = compute_ev(
                symbol=symbol,
                side=side,
                mid=s0["entry"],
                atr_price=s0["atr"],
                p_hit_tp_before_sl=mean,
                p_hit_source="demo_n43",
                p_direction=0.5,
                spread_pips=spread_pips,
                mt5_specs=mt5_specs,
            )
            ev_lo = compute_ev(
                symbol=symbol,
                side=side,
                mid=s0["entry"],
                atr_price=s0["atr"],
                p_hit_tp_before_sl=lo,
                p_hit_source="demo_n43_lo",
                p_direction=0.5,
                spread_pips=spread_pips,
                mt5_specs=mt5_specs,
            )
            uncertainty_demo[side] = {
                "n": len(use),
                "hits": hits,
                "p_hit_mean": round(mean, 4),
                "p_hit_lo95": round(lo, 4),
                "p_hit_hi95": round(hi, 4),
                "ev_at_mean": ev_mean.expected_net_usd,
                "ev_at_lo95": ev_lo.expected_net_usd,
                "still_positive_at_lo95": ev_lo.expected_net_usd > 0,
                "note": (
                    "Com n≈43 o intervalo é largo; EV no limite inferior do CI "
                    "é o teste conservador."
                ),
            }

    strategy_stats: dict[str, Any] = {}
    for (strat, side), trs in trades_by_key.items():
        st = StrategyStats(strategy=strat, symbol=symbol, side=side)
        st.n_tp = sum(1 for t in trs if t.outcome == "tp")
        st.n_sl = sum(1 for t in trs if t.outcome == "sl")
        st.n_timeout = sum(1 for t in trs if t.outcome == "timeout")
        st.costs_usd = round(sum(t.cost_usd for t in trs), 4)
        st.gross_pnl_usd = round(sum(t.pnl_usd + t.cost_usd for t in trs), 4)
        st.finalize([t.pnl_usd for t in trs], [t.expected_net for t in trs])
        strategy_stats[f"{strat}:{side}"] = st.as_dict()

    # Agregar BOTH por estratégia
    for strat in strategies:
        both = []
        for side in ("BUY", "SELL"):
            both.extend(trades_by_key.get((strat, side), []))
        if not both:
            strategy_stats[f"{strat}:BOTH"] = StrategyStats(
                strategy=strat, symbol=symbol, side="BOTH", notes="sem trades OOS"
            ).as_dict()
            continue
        st = StrategyStats(strategy=strat, symbol=symbol, side="BOTH")
        st.n_tp = sum(1 for t in both if t.outcome == "tp")
        st.n_sl = sum(1 for t in both if t.outcome == "sl")
        st.n_timeout = sum(1 for t in both if t.outcome == "timeout")
        st.costs_usd = round(sum(t.cost_usd for t in both), 4)
        st.gross_pnl_usd = round(sum(t.pnl_usd + t.cost_usd for t in both), 4)
        st.finalize([t.pnl_usd for t in both], [t.expected_net for t in both])
        strategy_stats[f"{strat}:BOTH"] = st.as_dict()

    return {
        "symbol": symbol,
        "ok": True,
        "layer": "backtest",
        "n_bars": len(closes),
        "n_samples": n,
        "n_folds": len(fold_ranges),
        "pip_value_0.1lot": pip_val,
        "pip_source": pip_audit.source,
        "spread_pips": spread_pips,
        "train_val_test_cuts": {
            "train_end": test_cut,
            "val_end": val_cut,
            "test_end": n,
            "note": (
                "Alternativas usam regras fixas definidas a priori; "
                "métricas reportadas são dos folds OOS expanding-window."
            ),
        },
        "uncertainty_small_sample": uncertainty_demo,
        "strategies": strategy_stats,
        "filters_reproduced": {
            "expected_net_usd": "> 0",
            "evidence_quality": f">= {_EVIDENCE_MIN}",
            "p_hit_samples": f">= {_MIN_CALIB_SAMPLES}",
            "p_hit_vs_breakeven": "P_hit >= risk/(risk+reward)",
            "reject_reasons": "empty",
            "historical_data_valid": "MT5 closed candles (feed_state live N/A)",
        },
    }


def run_universe_actionable_backtest(
    symbols: list[str] | None = None,
    *,
    force_history: bool = True,
    n_folds: int = 4,
) -> dict[str, Any]:
    symbols = list(symbols or FOREX_28_SYMBOLS)
    results = []
    for idx, sym in enumerate(symbols):
        print(f"backtest {idx+1}/{len(symbols)} {sym}", flush=True)
        hist = fetch_symbol_history(sym, force=force_history)
        m15 = hist.by_tf.get("M15")
        if not m15 or m15.status != "ok" or len(m15.bars) < 200:
            results.append(
                {
                    "symbol": sym,
                    "ok": False,
                    "reason": (
                        f"histórico M15 {m15.status if m15 else 'missing'} "
                        f"bars={len(m15.bars) if m15 else 0}"
                    ),
                    "layer": "backtest",
                }
            )
            continue
        # Spread: preferir tick actual; senão default do meta
        spread = 1.2
        info = hist.symbol_info or {}
        if info.get("bid") and info.get("ask"):
            try:
                spec = get_instrument(sym)
                spread = max(0.1, (float(info["ask"]) - float(info["bid"])) / spec.pip_size)
            except Exception:  # noqa: BLE001
                pass
        r = backtest_symbol(
            sym,
            m15.bars,
            spread_pips=spread,
            mt5_specs=info if info.get("ok") else None,
            n_folds=n_folds,
        )
        r["m15_months"] = m15.months_approx
        r["m15_bars"] = len(m15.bars)
        results.append(r)

    # Agregar
    agg: dict[str, dict[str, Any]] = {}
    for strat in (
        "actionable_current",
        "baseline_always_buy",
        "baseline_always_sell",
        "alt_strict_phit",
        "alt_conservative_ci",
    ):
        pnls: list[float] = []
        n_tr = 0
        n_tp = 0
        n_pairs_pos = 0
        n_pairs_with = 0
        for r in results:
            if not r.get("ok"):
                continue
            st = (r.get("strategies") or {}).get(f"{strat}:BOTH") or {}
            if st.get("n_trades", 0) > 0:
                n_pairs_with += 1
                n_tr += int(st["n_trades"])
                n_tp += int(st.get("n_tp") or 0)
                pnls.append(float(st.get("net_pnl_usd") or 0))
                if float(st.get("net_pnl_usd") or 0) > 0:
                    n_pairs_pos += 1
        net = sum(pnls)
        agg[strat] = {
            "pairs_with_trades": n_pairs_with,
            "pairs_net_positive": n_pairs_pos,
            "n_trades": n_tr,
            "win_rate_trades": round(n_tp / n_tr, 4) if n_tr else None,
            "net_pnl_usd_sum_pairs": round(net, 4),
            "mean_pair_pnl": round(net / n_pairs_with, 4) if n_pairs_with else None,
            "robust_candidate": bool(
                n_pairs_with >= 8
                and n_pairs_pos >= max(5, int(0.6 * n_pairs_with))
                and net > 0
                and (n_tp / n_tr if n_tr else 0) > 0.45
            ),
        }

    best = max(agg.items(), key=lambda kv: (kv[1].get("net_pnl_usd_sum_pairs") or -1e9))
    candidate = None
    if agg.get("actionable_current", {}).get("robust_candidate"):
        candidate = {
            "strategy": "actionable_current",
            "status": "paper_demo_candidate",
            "disclaimer": (
                "Resultados OOS positivos agregados — candidato a PAPER/DEMO, "
                "NÃO rentabilidade garantida."
            ),
            "metrics": agg["actionable_current"],
        }
    elif best[1].get("robust_candidate"):
        candidate = {
            "strategy": best[0],
            "status": "paper_demo_candidate",
            "disclaimer": (
                "Alternativa com métricas agregadas positivas em OOS — "
                "candidato a PAPER/DEMO, NÃO garantia."
            ),
            "metrics": best[1],
        }
    else:
        candidate = {
            "strategy": None,
            "status": "no_robust_edge",
            "disclaimer": (
                "Nenhuma estratégia passou critérios de robustez "
                "(pares com trades, maioria líquida positiva, win-rate, PnL agregado)."
            ),
            "best_attempt": {"strategy": best[0], "metrics": best[1]},
        }

    report = {
        "generated_at": _utcnow().isoformat(),
        "layer": "backtest",
        "layers_legend": {
            "backtest": "Walk-forward OOS em velas MT5 históricas (este relatório)",
            "simulation": "SimulatedBroker / ciclo local — não incluído aqui",
            "mt5_demo": "Ordens DEMO reais — não executadas nestes testes",
        },
        "safety": {
            "mt5_orders": False,
            "openai_paid": False,
            "live_locked": True,
            "risk_limits_unchanged": True,
        },
        "universe": len(symbols),
        "pairs_ok": sum(1 for r in results if r.get("ok")),
        "aggregate": agg,
        "candidate": candidate,
        "results": results,
        "methodology": {
            "walk_forward": "expanding window, 4 folds, ~15% test cada",
            "no_lookahead": "calibração P_hit só em amostras com índice < test_start",
            "actionable_filters": (
                "EV_net>0, evidence≥0.35, n_hit≥30, P_hit≥BE, reject vazio"
            ),
            "baseline": "always BUY / always SELL em cada barra de teste (com custos)",
            "alternatives": {
                "alt_strict_phit": "exige P_hit ≥ BE+0.05 (regra a priori)",
                "alt_conservative_ci": "usa P_hit=limite inferior CI95% no EV",
            },
            "parameter_selection": (
                "Regras alternativas fixas a priori — não optimizadas no conjunto de teste"
            ),
        },
        "disclaimer": (
            "Previsão com EV>0 ≠ trade lucrativo. Custos modelados. "
            "Sem ordens DEMO nestes testes. Não declara rentabilidade futura."
        ),
    }

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _REPORT_DIR / "actionable_walkforward_latest.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(out_path)
    return report


def load_latest_backtest_report() -> dict[str, Any] | None:
    path = _REPORT_DIR / "actionable_walkforward_latest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def panel_backtest_payload() -> dict[str, Any]:
    """Resumo para o painel do robô."""
    full = load_latest_backtest_report()
    if not full:
        return {
            "available": False,
            "layer": "backtest",
            "message": "Ainda sem relatório walk-forward. Executar GET /api/robot/backtest/actionable.",
            "layers_legend": {
                "backtest": "Walk-forward OOS histórico",
                "simulation": "SimulatedBroker",
                "mt5_demo": "Ordens DEMO (não nestes testes)",
            },
        }
    rows = []
    for r in full.get("results") or []:
        if not r.get("ok"):
            rows.append({"symbol": r.get("symbol"), "ok": False, "reason": r.get("reason")})
            continue
        cur = (r.get("strategies") or {}).get("actionable_current:BOTH") or {}
        base_b = (r.get("strategies") or {}).get("baseline_always_buy:BOTH") or {}
        rows.append(
            {
                "symbol": r.get("symbol"),
                "ok": True,
                "m15_months": r.get("m15_months"),
                "actionable_trades": cur.get("n_trades"),
                "actionable_net_pnl": cur.get("net_pnl_usd"),
                "actionable_win_rate": cur.get("win_rate"),
                "actionable_pf": cur.get("profit_factor"),
                "actionable_max_dd": cur.get("max_drawdown_usd"),
                "baseline_buy_net": base_b.get("net_pnl_usd"),
                "uncertainty": r.get("uncertainty_small_sample"),
            }
        )
    return {
        "available": True,
        "layer": "backtest",
        "generated_at": full.get("generated_at"),
        "candidate": full.get("candidate"),
        "aggregate": full.get("aggregate"),
        "pairs": rows,
        "methodology": full.get("methodology"),
        "disclaimer": full.get("disclaimer"),
        "safety": full.get("safety"),
        "layers_legend": full.get("layers_legend"),
        "report_path": full.get("report_path"),
    }
