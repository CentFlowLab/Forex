"""Execução de experiências locais do Motor Adaptativo (sem OpenAI por defeito)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptive.costs import estimate_round_trip_costs
from app.adaptive.decisions import record_decision
from app.adaptive.regimes import classify_regime
from app.adaptive.scoring import reject_for_overfitting, score_candidate
from app.adaptive.selection import (
    detect_degradation,
    promote_strategy,
    select_for_regime,
    suspend_strategy,
)
from app.adaptive.simulator import simulate_strategy
from app.adaptive.strategies import build_strategy, list_catalog, param_grid
from app.adaptive.validation import chronological_split
from app.config import get_settings
from app.market.history_inventory import prepare_experiment
from app.market.labels import DUKASCOPY_SOURCE
from app.models.adaptive import AdaptiveExperiment, AdaptiveStrategyState
from app.models.market import Candle


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_candles(
    db: Session,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[Candle]:
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


ProgressCb = Callable[[dict[str, Any]], None]


def run_experiment(
    db: Session,
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    start: datetime,
    end: datetime,
    name: str = "experiência-adaptativa",
    authorize_openai: bool = False,
    max_combos_per_strategy: int = 8,
    require_real_data: bool = False,
    experiment_id: int | None = None,
    progress_cb: ProgressCb | None = None,
) -> AdaptiveExperiment:
    """Corre investigação + validação independente.

    Por defeito NÃO chama OpenAI. authorize_openai apenas regista a autorização;
    hipóteses de agentes não alteram código nem limites de risco.
    Com require_real_data=True, bloqueia sintéticos e cobertura insuficiente.
    """
    settings = get_settings()
    # Segurança de custos: experiências do motor nunca invocam fornecedores de IA.
    authorize_openai = False
    openai_note = "Experiência local sem chamadas OpenAI (AI_ENABLED respeitado; motor 100% local)."

    if require_real_data:
        readiness = prepare_experiment(
            db,
            start=start,
            end=end,
            symbol=symbol,
            timeframe=timeframe,
            require_real_data=True,
        )
        if not readiness["ready"]:
            raise ValueError(readiness["message"] + " " + " ".join(readiness["reasons"]))

    if experiment_id is not None:
        exp = db.get(AdaptiveExperiment, experiment_id)
        if exp is None:
            raise ValueError(f"Experiência #{experiment_id} não encontrada.")
        exp.status = "running"
        exp.started_at = exp.started_at or _utcnow()
        exp.notes = openai_note
        exp.progress_message = "A carregar dados históricos…"
        db.commit()
    else:
        exp = AdaptiveExperiment(
            name=name,
            symbol=symbol,
            timeframe=timeframe,
            start_ts=start,
            end_ts=end,
            status="running",
            use_openai=False,
            openai_authorized=False,
            notes=openai_note,
            started_at=_utcnow(),
            progress_message="A carregar dados históricos…",
        )
        db.add(exp)
        db.flush()

    def _report(**kwargs: Any) -> None:
        if progress_cb:
            progress_cb(kwargs)

    try:
        candles = _load_candles(db, symbol, timeframe, start, end)
        if len(candles) < 90:
            raise ValueError(
                f"Dados insuficientes ({len(candles)} velas). "
                "Descarregue histórico Dukascopy real antes de executar a experiência."
            )

        # Uma única fonte contínua — evita sobreposição temporal entre fontes mistas.
        by_source: dict[str, list[Candle]] = {}
        for c in candles:
            by_source.setdefault(c.source, []).append(c)

        if require_real_data:
            duk = [
                c
                for c in by_source.get(DUKASCOPY_SOURCE, [])
                if not c.is_synthetic
            ]
            if len(duk) < 90:
                raise ValueError(
                    f"Fonte Dukascopy real insuficiente ({len(duk)} velas). "
                    "Substituição silenciosa por sintéticos está bloqueada."
                )
            candles = duk
            chosen_source = DUKASCOPY_SOURCE
            is_synthetic = False
        else:
            preferred_order = ("dukascopy", "adaptive_test", "synthetic")
            chosen_source = None
            for key in preferred_order:
                if key in by_source and len(by_source[key]) >= 90:
                    chosen_source = key
                    break
            if chosen_source is None:
                chosen_source = max(by_source.keys(), key=lambda k: len(by_source[k]))
            candles = by_source[chosen_source]
            if len(candles) < 90:
                raise ValueError(
                    f"Dados insuficientes na fonte {chosen_source} ({len(candles)} velas)."
                )
            is_synthetic = all(c.is_synthetic for c in candles)

        exp.data_source = chosen_source
        exp.is_synthetic = is_synthetic
        exp.bars_total = len(candles)

        split = chronological_split(candles)
        exp.train_bars = len(split.train)
        exp.validation_bars = len(split.validation)
        exp.test_bars = len(split.test)

        # Regime atual = última barra disponível (sem futuro).
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        regime = classify_regime(closes, highs, lows)
        exp.current_regime = regime.label

        cost_info = estimate_round_trip_costs(symbol).as_dict()
        candidate_rows: list[dict[str, Any]] = []
        tested = 0
        work: list[tuple[Any, dict[str, Any]]] = []
        for spec in list_catalog():
            for params in param_grid(spec, max_combos=max_combos_per_strategy):
                work.append((spec, params))
        total = len(work)
        exp.candidates_total = total
        exp.bars_total = len(candles)
        exp.progress_pct = 2.0
        exp.progress_message = f"Dados prontos ({len(candles)} velas) · {total} combinações a testar"
        db.commit()
        _report(
            tested=0,
            total=total,
            progress_pct=2.0,
            message=exp.progress_message,
            bars_total=len(candles),
        )

        for spec, params in work:
            tested += 1
            strategy = build_strategy(spec.key, params)
            _report(
                tested=tested - 1,
                total=total,
                progress_pct=round(5.0 + 90.0 * (tested - 1) / max(1, total), 2),
                message=f"A simular {spec.display_name} ({tested}/{total})…",
                bars_total=len(candles),
            )
            train_res = simulate_strategy(list(split.train), strategy, symbol=symbol, split_label="train")
            val_res = simulate_strategy(list(split.validation), strategy, symbol=symbol, split_label="validation")
            test_res = simulate_strategy(list(split.test), strategy, symbol=symbol, split_label="test")

            # Afinidade de regime: fração de trades de validação no regime preferido.
            pref = set(spec.preferred_regimes)
            val_regimes = [t.regime_at_entry for t in val_res.trades]
            match_ratio = (
                sum(1 for r in val_regimes if r in pref) / len(val_regimes) if val_regimes else 0.0
            )

            # Contagem global de experiências desta família (controlo de sobreajuste).
            prior = (
                db.scalar(
                    select(AdaptiveStrategyState)
                    .where(AdaptiveStrategyState.strategy_key == spec.key)
                    .order_by(AdaptiveStrategyState.experiment_count.desc())
                    .limit(1)
                )
            )
            exp_count = int(prior.experiment_count + 1) if prior else 1

            overfit, overfit_reason = reject_for_overfitting(train_res.metrics, val_res.metrics)
            scores = score_candidate(
                train_metrics=train_res.metrics,
                validation_metrics=val_res.metrics,
                test_metrics=test_res.metrics,
                complexity=strategy.complexity_score(),
                experiment_count=exp_count,
                regime_match_ratio=match_ratio,
            )

            state = AdaptiveStrategyState(
                strategy_key=spec.key,
                display_name=spec.display_name,
                params_json=json.dumps(params, ensure_ascii=False),
                status="experimental",
                regime_affinity_json=json.dumps(list(spec.preferred_regimes), ensure_ascii=False),
                complexity_score=strategy.complexity_score(),
                experiment_count=exp_count,
                in_sample_score=float(scores["base"]),
                validation_score=float(scores["total"]),
                out_of_sample_score=float(scores["oos_component"]),
                max_drawdown_pct=float(test_res.metrics.get("max_drawdown_pct") or 0.0),
                net_pnl=float(test_res.metrics.get("net_pnl") or 0.0),
                trading_costs=float(test_res.total_costs + val_res.total_costs + train_res.total_costs),
                reason=overfit_reason or "Candidata experimental — aguarda promoção.",
            )
            db.add(state)
            db.flush()

            # Persistir decisões de teste (resultado só após outcome_available_at).
            equity = settings.starting_equity
            peak = equity
            for d in test_res.decisions:
                equity += float(d["pnl"])
                peak = max(peak, equity)
                dd = ((peak - equity) / peak * 100.0) if peak else 0.0
                age = 0.0
                record_decision(
                    db,
                    {
                        "experiment_id": exp.id,
                        "strategy_state_id": state.id,
                        "decision_ts": d["decision_ts"],
                        "data_source": exp.data_source,
                        "data_age_seconds": age,
                        "regime": d["regime"],
                        "strategy_key": spec.key,
                        "params": params,
                        "agent_arguments": {},
                        "final_decision": d["final_decision"],
                        "entry_price": d["entry_price"],
                        "exit_price": d["exit_price"],
                        "spread_pips": d["spread_pips"],
                        "slippage_pips": d["slippage_pips"],
                        "commission": d["commission"],
                        "estimated_costs": d["estimated_costs"],
                        "pnl": d["pnl"],
                        "drawdown_pct": dd,
                        "outcome_available_at": d["outcome_available_at"],
                        "outcome_label": "lucro" if d["pnl"] > 0 else "prejuízo" if d["pnl"] < 0 else "nulo",
                        "split_label": "test",
                    },
                )

            if overfit:
                suspend_strategy(db, state, experiment_id=exp.id, reason=overfit_reason)
            elif (
                scores["total"] >= 0.5
                and float(val_res.metrics.get("profit_factor") or 0) >= 1.05
                and float(test_res.metrics.get("profit_factor") or 0) >= 1.0
                and int(val_res.metrics.get("trades") or 0) >= 5
            ):
                # Passa por validação e promoção apenas com evidência fora do treino.
                state.status = "validating"
                promote_strategy(
                    db,
                    state,
                    experiment_id=exp.id,
                    reason=(
                        f"Promovida após validação independente "
                        f"(score={scores['total']}, PF_val={val_res.metrics.get('profit_factor')}, "
                        f"PF_oos={test_res.metrics.get('profit_factor')}). "
                        "Não é garantia de lucro futuro."
                    ),
                )

            # Degradação vs melhor estado anterior aprovado da mesma chave.
            prev_approved = db.scalar(
                select(AdaptiveStrategyState).where(
                    AdaptiveStrategyState.strategy_key == spec.key,
                    AdaptiveStrategyState.status == "approved",
                    AdaptiveStrategyState.id != state.id,
                ).order_by(AdaptiveStrategyState.out_of_sample_score.desc()).limit(1)
            )
            if (
                prev_approved
                and state.status == "approved"
                and detect_degradation(prev_approved.out_of_sample_score, state.out_of_sample_score)
            ):
                suspend_strategy(
                    db,
                    state,
                    experiment_id=exp.id,
                    reason="Suspensa por degradação significativa face à versão anterior.",
                )

            status = state.status

            candidate_rows.append(
                {
                    "id": state.id,
                    "strategy_key": spec.key,
                    "display_name": spec.display_name,
                    "params": params,
                    "status": status,
                    "score": scores["total"],
                    "scores": scores,
                    "preferred_regimes": list(spec.preferred_regimes),
                    "regime_affinity_json": state.regime_affinity_json,
                    "overfit_rejected": overfit,
                    "train_metrics": train_res.metrics,
                    "validation_metrics": val_res.metrics,
                    "test_metrics": test_res.metrics,
                    "complexity": strategy.complexity_score(),
                    "experiment_count": exp_count,
                    "trading_costs": state.trading_costs,
                    "reason": state.reason,
                }
            )
            exp.candidates_tested = tested
            exp.progress_pct = round(5.0 + 90.0 * tested / max(1, total), 2)
            exp.progress_message = f"Concluída combinação {tested}/{total}: {spec.display_name}"
            db.commit()
            _report(
                tested=tested,
                total=total,
                progress_pct=exp.progress_pct,
                message=exp.progress_message,
                bars_total=len(candles),
            )

        selection = select_for_regime(candidate_rows, regime.label)
        exp.candidates_tested = tested
        exp.progress_pct = 98.0
        exp.progress_message = "A finalizar seleção de regime…"
        exp.selected_strategy_id = int(selection.get("strategy_state_id") or 0)
        exp.selected_action = "HOLD" if selection["action"] == "HOLD" else "SIMULATE"
        exp.results_json = json.dumps(
            {
                "disclaimer": (
                    "Resultados históricos simulados — não constituem previsão de lucro futuro "
                    "nem evidência estatística independente de rentabilidade."
                ),
                "regime": regime.as_dict(),
                "split": split.as_dict(),
                "costs_model": cost_info,
                "selection": selection,
                "candidates": candidate_rows,
                "openai_authorized": bool(authorize_openai),
                "risk_limits_changed": False,
                "live_trading": False,
            },
            ensure_ascii=False,
        )
        exp.status = "completed"
        exp.progress_pct = 100.0
        exp.progress_message = f"Concluída — {tested} combinações testadas"
        exp.completed_at = _utcnow()
        db.commit()
        db.refresh(exp)
        _report(
            tested=tested,
            total=total,
            progress_pct=100.0,
            message=exp.progress_message,
            bars_total=len(candles),
        )
        return exp
    except Exception as exc:  # noqa: BLE001
        exp.status = "failed"
        exp.error = str(exc)
        exp.completed_at = _utcnow()
        db.commit()
        db.refresh(exp)
        return exp
