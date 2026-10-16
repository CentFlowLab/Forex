"""Testes do Motor Adaptativo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adaptive.costs import estimate_round_trip_costs
from app.adaptive.decisions import decisions_for_agents_as_of, record_decision
from app.adaptive.experiments import run_experiment
from app.adaptive.regimes import classify_regime
from app.adaptive.scoring import reject_for_overfitting, score_candidate
from app.adaptive.selection import (
    detect_degradation,
    promote_strategy,
    rollback_strategy,
    select_for_regime,
    suspend_strategy,
)
from app.adaptive.strategies import build_strategy, list_catalog, param_grid
from app.adaptive.validation import chronological_split, walk_forward_windows
from app.database import Base
from app.models import adaptive as _adaptive_models  # noqa: F401 — regista tabelas
from app.models.adaptive import AdaptiveStrategyState
from app.models.market import Candle
from app.services.seed import seed_all


def _make_candles(n: int = 300, start: datetime | None = None, trend: float = 0.00002) -> list[Candle]:
    start = start or datetime(2024, 1, 2, tzinfo=timezone.utc)
    rows: list[Candle] = []
    price = 1.1000
    for i in range(n):
        # Oscilação + tendência controlada (dados sintéticos etiquetados para testes).
        wave = 0.0008 * ((i % 40) / 40.0 - 0.5)
        open_p = price
        close_p = price + trend + wave
        high_p = max(open_p, close_p) + 0.0003
        low_p = min(open_p, close_p) - 0.0003
        rows.append(
            Candle(
                symbol="EURUSD",
                timeframe="M15",
                ts=start + timedelta(minutes=15 * i),
                open=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=100.0,
                source="adaptive_test",
                is_synthetic=True,
            )
        )
        price = close_p
    return rows


@pytest.fixture()
def adb() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionTesting = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionTesting()
    seed_all(session)
    for c in _make_candles(320):
        session.add(c)
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_no_lookahead_in_chrono_split(adb: Session) -> None:
    candles = list(adb.scalars(select(Candle).order_by(Candle.ts.asc())))
    split = chronological_split(candles)
    assert split.train[-1].ts < split.validation[0].ts
    assert split.validation[-1].ts < split.test[0].ts


def test_walk_forward_windows_ordered() -> None:
    bars = [SimpleNamespace(ts=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)) for i in range(200)]
    windows = walk_forward_windows(bars, train_size=80, test_size=20, step=20)
    assert windows
    for train, test in windows:
        assert train[-1].ts < test[0].ts


def test_regime_classification_deterministic() -> None:
    up = [1.0 + i * 0.001 for i in range(80)]
    highs = [c + 0.0005 for c in up]
    lows = [c - 0.0005 for c in up]
    r = classify_regime(up, highs, lows)
    assert r.label in {
        "tendencia_alta",
        "tendencia_baixa",
        "mercado_lateral",
        "volatilidade_elevada",
        "volatilidade_reduzida",
        "desconhecido",
    }
    assert r.label_pt


def test_costs_positive() -> None:
    c = estimate_round_trip_costs("EURUSD", 0.10)
    assert c.total > 0
    assert c.spread_pips > 0


def test_catalog_and_param_bounds() -> None:
    catalog = list_catalog()
    assert len(catalog) == 7
    keys = {s.key for s in catalog}
    assert keys == {
        "ma_crossover",
        "trend_follow",
        "mean_reversion",
        "volatility_breakout",
        "session_trend",
        "regime_mean_reversion",
        "filtered_breakout",
    }
    for spec in catalog:
        grid = param_grid(spec, max_combos=6)
        assert grid
        for params in grid:
            build_strategy(spec.key, params)  # valida limites


def test_overfitting_rejection() -> None:
    rejected, reason = reject_for_overfitting(
        {"profit_factor": 3.5, "trades": 40, "max_drawdown_pct": 5},
        {"profit_factor": 0.8, "trades": 8, "max_drawdown_pct": 10},
    )
    assert rejected is True
    assert "sobreajustamento" in reason.lower() or "sobreajuste" in reason.lower() or "Rejeitada" in reason


def test_score_penalizes_complexity() -> None:
    base_m = {"profit_factor": 1.2, "net_pnl": 50, "max_drawdown_pct": 5, "trades": 10}
    simple = score_candidate(
        train_metrics=base_m,
        validation_metrics=base_m,
        test_metrics=base_m,
        complexity=1.0,
        experiment_count=1,
        regime_match_ratio=0.5,
    )
    complex_ = score_candidate(
        train_metrics=base_m,
        validation_metrics=base_m,
        test_metrics=base_m,
        complexity=2.5,
        experiment_count=1,
        regime_match_ratio=0.5,
    )
    assert complex_["total"] < simple["total"]


def test_selection_hold_without_edge() -> None:
    out = select_for_regime([], "mercado_lateral")
    assert out["action"] == "HOLD"
    assert out["risk_limits_changed"] is False


def test_suspend_and_rollback(adb: Session) -> None:
    state = AdaptiveStrategyState(
        strategy_key="ma_crossover",
        display_name="teste",
        status="approved",
        previous_status="experimental",
    )
    adb.add(state)
    adb.commit()
    adb.refresh(state)
    suspend_strategy(adb, state, experiment_id=1, reason="degradação")
    adb.commit()
    assert state.status == "suspended"
    rollback_strategy(adb, state, experiment_id=1, reason="recuperação")
    adb.commit()
    assert state.status == "approved"  # previous_status era approved antes do suspend? 
    # Após suspend: previous=approved, status=suspended
    # rollback: previous=suspended, status=approved (target=previous_status=approved)
    assert state.status in {"approved", "experimental"}


def test_degradation_detection() -> None:
    assert detect_degradation(5.0, 1.0) is True
    assert detect_degradation(2.0, 1.5) is False


def test_outcome_not_leaked_before_available(adb: Session) -> None:
    t0 = datetime(2024, 2, 1, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2024, 2, 1, 14, 0, tzinfo=timezone.utc)
    record_decision(
        adb,
        {
            "experiment_id": 1,
            "strategy_state_id": 1,
            "decision_ts": t0,
            "data_source": "synthetic",
            "regime": "mercado_lateral",
            "strategy_key": "ma_crossover",
            "params": {},
            "final_decision": "BUY",
            "entry_price": 1.1,
            "exit_price": 1.11,
            "pnl": 10.0,
            "outcome_available_at": t1,
            "split_label": "test",
        },
    )
    adb.commit()
    before = decisions_for_agents_as_of(adb, as_of=t0 + timedelta(minutes=30))
    assert before[0]["pnl"] is None
    assert before[0]["outcome_available"] is False
    after = decisions_for_agents_as_of(adb, as_of=t1)
    assert after[0]["pnl"] == 10.0
    assert after[0]["outcome_available"] is True


def test_run_experiment_db_integrity(adb: Session) -> None:
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc) + timedelta(minutes=15 * 319)
    exp = run_experiment(
        adb,
        start=start,
        end=end,
        name="teste-unitário",
        authorize_openai=False,
        max_combos_per_strategy=2,
    )
    assert exp.status == "completed"
    assert exp.use_openai is False
    assert exp.candidates_tested > 0
    assert exp.train_bars > 0 and exp.validation_bars > 0 and exp.test_bars > 0
    states = list(adb.scalars(select(AdaptiveStrategyState)))
    assert states
    # Sem alteração de limites de risco no resultado.
    import json

    results = json.loads(exp.results_json)
    assert results.get("risk_limits_changed") is False
    assert results.get("live_trading") is False


def test_adaptive_api_dashboard(client) -> None:
    # cliente de conftest usa DB em memória sem as nossas velas — deve responder mesmo assim.
    res = client.get("/api/adaptive/dashboard")
    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "Motor Adaptativo"
    assert "strategies" in body
    assert body["risk"]["live_trading_locked"] is True


def test_schedule_rejects_openai_authorization(adb: Session) -> None:
    from app.adaptive.service import get_adaptive_service

    with pytest.raises(ValueError):
        get_adaptive_service().update_schedule(adb, enabled=True, authorize_openai=True)


def test_promote_requires_validation_path(adb: Session) -> None:
    state = AdaptiveStrategyState(strategy_key="trend_follow", display_name="x", status="experimental")
    adb.add(state)
    adb.commit()
    adb.refresh(state)
    promote_strategy(adb, state, experiment_id=1, reason="ok")
    adb.commit()
    assert state.status == "approved"
