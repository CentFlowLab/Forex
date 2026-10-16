"""Investigação de rentabilidade — walk-forward local, sem IA, sem relaxar critérios.

Usa apenas dados Dukascopy reais. Não promove estratégias automaticamente.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Permitir execução como script a partir de backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.adaptive.costs import estimate_round_trip_costs
from app.adaptive.regimes import classify_regime
from app.adaptive.scoring import reject_for_overfitting, score_candidate
from app.adaptive.simulator import simulate_strategy
from app.adaptive.strategies import build_strategy, list_catalog
from app.adaptive.validation import chronological_split
from app.database import SessionLocal, init_db
from app.market.history_inventory import (
    DEFAULT_INVESTIGATION_END,
    DEFAULT_INVESTIGATION_START,
    prepare_experiment,
)
from app.market.labels import DUKASCOPY_SOURCE
from app.models.adaptive import AdaptiveExperiment
from app.models.market import Candle

# Critérios de promoção existentes (não relaxar).
MIN_SCORE = 0.5
MIN_PF_VAL = 1.05
MIN_PF_OOS = 1.0
MIN_TRADES_VAL = 5


@dataclass
class Row:
    strategy: str
    trades: int
    pf_val: float
    pf_oos: float
    net_pnl: float
    max_dd: float
    approved: str
    reason: str
    window: str = ""
    pf_train: float = 0.0
    trades_train: int = 0
    trades_oos: int = 0


def _load_dukascopy(db, start: datetime, end: datetime) -> list[Candle]:
    return list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == "EURUSD",
                Candle.timeframe == "M15",
                Candle.source == DUKASCOPY_SOURCE,
                Candle.is_synthetic == False,  # noqa: E712
                Candle.ts >= start,
                Candle.ts <= end,
            )
            .order_by(Candle.ts.asc())
        )
    )


def _metrics(m: dict[str, Any]) -> tuple[float, int, float, float]:
    return (
        float(m.get("profit_factor") or 0.0),
        int(m.get("trades") or 0),
        float(m.get("net_pnl") or 0.0),
        float(m.get("max_drawdown_pct") or 0.0),
    )


def audit_experiments(db) -> list[Row]:
    rows: list[Row] = []
    exps = list(db.scalars(select(AdaptiveExperiment).order_by(AdaptiveExperiment.id)))
    for exp in exps:
        try:
            results = json.loads(exp.results_json or "{}")
        except json.JSONDecodeError:
            continue
        by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in results.get("candidates") or []:
            by_key[str(c.get("strategy_key") or "?")].append(c)
        for key, cands in sorted(by_key.items()):
            best = max(cands, key=lambda x: float(x.get("score") or -1e9))
            tr = best.get("train_metrics") or {}
            vm = best.get("validation_metrics") or {}
            tm = best.get("test_metrics") or {}
            pf_tr, n_tr, _, _ = _metrics(tr)
            pf_v, n_v, pnl_v, dd_v = _metrics(vm)
            pf_t, n_t, pnl_t, dd_t = _metrics(tm)
            score = float(best.get("score") or 0)
            overfit = bool(best.get("overfit_rejected"))
            status = str(best.get("status") or "experimental")
            approved = "aprovada" if status == "approved" else "rejeitada"
            reasons: list[str] = []
            if overfit:
                reasons.append("sobreajuste treino≫validação")
            if pf_v < MIN_PF_VAL:
                reasons.append(f"PF_val={pf_v:.3f}<{MIN_PF_VAL}")
            if pf_t < MIN_PF_OOS:
                reasons.append(f"PF_oos={pf_t:.3f}<{MIN_PF_OOS}")
            if n_v < MIN_TRADES_VAL:
                reasons.append(f"ops_val={n_v}<{MIN_TRADES_VAL}")
            if score < MIN_SCORE:
                reasons.append(f"score={score:.2f}<{MIN_SCORE}")
            if not reasons and approved == "rejeitada":
                reasons.append("não promovida na seleção de regime")
            # Retorno/drawdown reportados no teste independente (OOS).
            rows.append(
                Row(
                    strategy=f"{key} (exp#{exp.id} melhor)",
                    trades=n_v,
                    pf_val=pf_v,
                    pf_oos=pf_t,
                    net_pnl=pnl_t,
                    max_dd=dd_t,
                    approved=approved,
                    reason="; ".join(reasons) or "ok",
                    window=f"exp#{exp.id}",
                    pf_train=pf_tr,
                    trades_train=n_tr,
                    trades_oos=n_t,
                )
            )
    return rows


def grounded_hypotheses() -> list[tuple[str, dict[str, Any], str]]:
    """Poucas hipóteses fundamentadas — não grelha indiscriminada."""
    return [
        ("ma_crossover", {"fast": 5, "slow": 50}, "melhor near-miss exp#1/#2"),
        ("trend_follow", {"ema_period": 10, "sma_period": 70, "min_slope": 0.0005}, "melhor PF_val exp#1"),
        ("session_trend", {"fast": 10, "slow": 40, "session_start_utc": 7, "session_end_utc": 17, "allow_all_hours": 0}, "filtro sessão Londres/NY"),
        ("session_trend", {"fast": 20, "slow": 60, "session_start_utc": 12, "session_end_utc": 21, "allow_all_hours": 0}, "sessão NY"),
        ("regime_mean_reversion", {"period": 10, "z_entry": 1.75, "atr_max_ratio": 0.8}, "PF_val alto mas OOS fraco — reteste"),
        ("regime_mean_reversion", {"period": 20, "z_entry": 2.0, "atr_max_ratio": 0.8}, "filtro vol mais estrito"),
        ("filtered_breakout", {"lookback": 20, "atr_period": 14, "atr_min_ratio": 0.8, "atr_max_ratio": 2.5}, "breakout com filtro ATR"),
        ("volatility_breakout", {"lookback": 20, "atr_mult": 1.5, "atr_period": 14}, "catálogo default"),
        ("mean_reversion", {"rsi_period": 14, "oversold": 30, "overbought": 70, "sma_period": 40}, "catálogo default"),
    ]


def walk_forward_windows(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """Três janelas cronológicas disjuntas no tempo (sem reutilizar teste como treino)."""
    # Dividir o período disponível em 3 blocos sequenciais ~iguais.
    total = (end - start).total_seconds()
    third = total / 3.0
    from datetime import timedelta

    w1_end = start + timedelta(seconds=third)
    w2_end = start + timedelta(seconds=2 * third)
    return [
        ("janela_A_inicial", start, w1_end),
        ("janela_B_média", w1_end, w2_end),
        ("janela_C_recente", w2_end, end),
    ]


def evaluate_on_window(
    bars: list[Candle],
    key: str,
    params: dict[str, Any],
    complexity: float,
) -> Row | None:
    if len(bars) < 120:
        return None
    split = chronological_split(bars)
    if len(split.train) < 40 or len(split.validation) < 30 or len(split.test) < 30:
        return None
    strategy = build_strategy(key, params)
    train_res = simulate_strategy(split.train, strategy)
    val_res = simulate_strategy(split.validation, strategy)
    test_res = simulate_strategy(split.test, strategy)
    overfit, overfit_reason = reject_for_overfitting(train_res.metrics, val_res.metrics)
    val_closes = [float(c.close) for c in split.validation]
    val_highs = [float(c.high) for c in split.validation]
    val_lows = [float(c.low) for c in split.validation]
    regime = classify_regime(val_closes, val_highs, val_lows)
    scores = score_candidate(
        train_metrics=train_res.metrics,
        validation_metrics=val_res.metrics,
        test_metrics=test_res.metrics,
        complexity=complexity,
        experiment_count=1,
        regime_match_ratio=1.0 if regime.label in getattr(strategy.spec, "preferred_regimes", []) else 0.3,
    )
    pf_tr, n_tr, _, _ = _metrics(train_res.metrics)
    pf_v, n_v, pnl_v, dd_v = _metrics(val_res.metrics)
    pf_t, n_t, pnl_t, dd_t = _metrics(test_res.metrics)
    score = float(scores["total"])
    reasons: list[str] = []
    ok = True
    if overfit:
        ok = False
        reasons.append(overfit_reason or "sobreajuste")
    if pf_v < MIN_PF_VAL:
        ok = False
        reasons.append(f"PF_val={pf_v:.3f}<{MIN_PF_VAL}")
    if pf_t < MIN_PF_OOS:
        ok = False
        reasons.append(f"PF_oos={pf_t:.3f}<{MIN_PF_OOS}")
    if n_v < MIN_TRADES_VAL:
        ok = False
        reasons.append(f"ops_val={n_v}<{MIN_TRADES_VAL}")
    if score < MIN_SCORE:
        ok = False
        reasons.append(f"score={score:.2f}<{MIN_SCORE}")
    if ok:
        reasons.append("passaria critérios — requer confirmação multi-janela")
    return Row(
        strategy=key,
        trades=n_v,
        pf_val=round(pf_v, 4),
        pf_oos=round(pf_t, 4),
        net_pnl=round(pnl_t, 2),
        max_dd=round(dd_t, 2),
        approved="candidata" if ok else "rejeitada",
        reason="; ".join(reasons),
        pf_train=round(pf_tr, 4),
        trades_train=n_tr,
        trades_oos=n_t,
    )


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        costs = estimate_round_trip_costs("EURUSD", 0.1)
        print("CUSTOS (0.1 lote round-trip):", costs)
        n, mn, mx = db.execute(
            select(func.count(Candle.id), func.min(Candle.ts), func.max(Candle.ts)).where(
                Candle.symbol == "EURUSD",
                Candle.timeframe == "M15",
                Candle.source == DUKASCOPY_SOURCE,
                Candle.is_synthetic == False,  # noqa: E712
            )
        ).one()
        print(f"DADOS Dukascopy M15: n={n} de {mn} a {mx}")

        start = DEFAULT_INVESTIGATION_START
        end = DEFAULT_INVESTIGATION_END
        prep = prepare_experiment(
            db, start=start, end=end, symbol="EURUSD", timeframe="M15", require_real_data=True
        )
        print("PREPARE:", prep.get("ready"), prep.get("message"))

        print("\n=== AUDITORIA EXP #1 / #2 (melhor candidata por estratégia) ===")
        audit = audit_experiments(db)
        for r in audit:
            print(
                f"{r.strategy}\tops_val={r.trades}\tPF_val={r.pf_val:.4f}\tPF_oos={r.pf_oos:.4f}\t"
                f"PnL_oos={r.net_pnl:.2f}\tDD_oos={r.max_dd:.2f}\t{r.approved}\t{r.reason}"
            )

        print("\n=== WALK-FORWARD EM 3 JANELAS (hipóteses limitadas) ===")
        catalog = {s.key: s for s in list_catalog()}
        windows = walk_forward_windows(start, end)
        wf_rows: list[Row] = []
        # Agregar estabilidade: estratégia+params que passam em >=2 janelas
        pass_counts: dict[str, int] = defaultdict(int)
        for wname, wstart, wend in windows:
            bars = _load_dukascopy(db, wstart, wend)
            print(f"\n-- {wname}: {wstart.date()} → {wend.date()} bars={len(bars)}")
            if len(bars) < 120:
                print("  insuficiente")
                continue
            sample = bars[-min(len(bars), 200) :]
            regime = classify_regime(
                [float(c.close) for c in sample],
                [float(c.high) for c in sample],
                [float(c.low) for c in sample],
            )
            print(f"  regime_amostra={regime.label}")
            for key, params, note in grounded_hypotheses():
                spec = catalog.get(key)
                if not spec:
                    continue
                row = evaluate_on_window(bars, key, params, spec.complexity)
                if row is None:
                    continue
                row.window = wname
                row.strategy = f"{key} | {note}"
                wf_rows.append(row)
                tag = f"{key}|{json.dumps(params, sort_keys=True)}"
                if row.approved == "candidata":
                    pass_counts[tag] += 1
                print(
                    f"  {key:24} PF_val={row.pf_val:6.3f} PF_oos={row.pf_oos:6.3f} "
                    f"ops={row.trades:3d} PnL_oos={row.net_pnl:8.2f} DD={row.max_dd:5.1f} "
                    f"{row.approved} | {row.reason[:80]}"
                )

        print("\n=== ESTABILIDADE MULTI-JANELA (passagens) ===")
        multi = {k: v for k, v in pass_counts.items() if v >= 2}
        print("passam em >=2 janelas:", multi or "(nenhuma)")

        # Tabela final consolidada: melhor resultado OOS por estratégia do catálogo
        # a partir do walk-forward (não aprovar por 1 janela).
        print("\n=== TABELA FINAL (walk-forward; promoção só se >=2 janelas) ===")
        best_by_strat: dict[str, Row] = {}
        for r in wf_rows:
            key = r.strategy.split(" | ")[0]
            prev = best_by_strat.get(key)
            # Preferir menor |PF_oos-1| com PnL; usar score simples pf_oos then pf_val
            if prev is None or (r.pf_oos, r.pf_val, r.net_pnl) > (prev.pf_oos, prev.pf_val, prev.net_pnl):
                best_by_strat[key] = r

        final: list[dict[str, Any]] = []
        for key in sorted(catalog.keys()):
            # Encontrar se alguma hipótese desta key passou em 2+ janelas
            passed_keys = [k for k, v in pass_counts.items() if k.startswith(key + "|") and v >= 2]
            r = best_by_strat.get(key)
            if r is None:
                final.append(
                    {
                        "Estratégia": key,
                        "Operações (val)": 0,
                        "PF validação": 0,
                        "PF teste": 0,
                        "Retorno líquido (teste)": 0,
                        "Drawdown máx. (teste)": 0,
                        "Decisão": "rejeitada",
                        "Motivo": "sem avaliação nesta corrida",
                    }
                )
                continue
            if passed_keys:
                decision = "rejeitada"
                motivo = (
                    f"Passou critérios em {pass_counts[passed_keys[0]]} janelas, "
                    "mas promoção automática desligada nesta investigação; "
                    "requer revisão humana e não foi aprovada no robô."
                )
                # Ainda assim NÃO marcar aprovada — critérios de segurança: não aprovar
                # por investigação ad-hoc sem passar pelo motor de promoção oficial.
                decision = "rejeitada"
                motivo = (
                    f"Critérios atingidos em {pass_counts[passed_keys[0]]}/3 janelas "
                    f"({r.window}); não promovida automaticamente (política de segurança)."
                )
            else:
                decision = "rejeitada"
                motivo = r.reason
            final.append(
                {
                    "Estratégia": key,
                    "Operações (val)": r.trades,
                    "PF validação": r.pf_val,
                    "PF teste": r.pf_oos,
                    "Retorno líquido (teste)": r.net_pnl,
                    "Drawdown máx. (teste)": r.max_dd,
                    "Decisão": decision,
                    "Motivo": motivo,
                    "Janela_ref": r.window,
                    "PF_treino": r.pf_train,
                    "Ops_teste": r.trades_oos,
                }
            )

        out_path = Path(__file__).resolve().parents[1] / "data" / "profitability_investigation.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "costs": {
                "spread_pips": costs.spread_pips,
                "slippage_pips": costs.slippage_pips,
                "commission": costs.commission,
                "total": costs.total,
            },
            "promotion_criteria": {
                "min_score": MIN_SCORE,
                "min_pf_validation": MIN_PF_VAL,
                "min_pf_test": MIN_PF_OOS,
                "min_validation_trades": MIN_TRADES_VAL,
                "multi_window_required": 2,
            },
            "audit_exp": [r.__dict__ for r in audit],
            "walk_forward": [r.__dict__ for r in wf_rows],
            "multi_window_passes": pass_counts,
            "table": final,
            "approved_count": 0,
            "conclusion": (
                "Nenhuma estratégia foi aprovada para o robô. "
                + (
                    "Algumas hipóteses passaram critérios numa ou mais janelas, "
                    "mas nenhuma demonstrou vantagem estável suficiente para promoção automática."
                    if any(v >= 1 for v in pass_counts.values())
                    else "Nenhuma hipótese fundamentada ultrapassou PF_val≥1.05 e PF_oos≥1.0 "
                    "com operações suficientes após custos em janelas walk-forward."
                )
            ),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nJSON:", out_path)
        print("\nTABELA:")
        for row in final:
            print(
                f"| {row['Estratégia']} | {row['Operações (val)']} | {row['PF validação']} | "
                f"{row['PF teste']} | {row['Retorno líquido (teste)']} | {row['Drawdown máx. (teste)']} | "
                f"{row['Decisão']} | {row['Motivo'][:90]} |"
            )
        print("\nCONCLUSÃO:", payload["conclusion"])
        print("approved_for_robot:", 0)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
