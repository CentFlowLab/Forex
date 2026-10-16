"""Motor de previsão direcional — local, sem OpenAI, sem ordens."""

from __future__ import annotations

import math

from app.robot.directional_engine import (
    analyze_pair_directional,
    directional_from_snapshots,
    opportunities_for_openai,
)
from app.robot.ai_schema import AIRadarDecision


def _synth_candles(n: int = 80, *, drift: float = 0.0001, seed: float = 1.1) -> list[dict]:
    """Série sintética com tendência ligeira (sem look-ahead no motor)."""
    out = []
    price = 1.1000
    for i in range(n):
        # Oscilação + drift determinístico
        wobble = 0.0003 * math.sin(i / 5.0 + seed)
        o = price
        c = price + drift + wobble
        h = max(o, c) + 0.0002
        l = min(o, c) - 0.0002
        out.append(
            {
                "ts": f"2024-01-01T{i % 24:02d}:{(i * 15) % 60:02d}:00+00:00",
                "o": round(o, 5),
                "h": round(h, 5),
                "l": round(l, 5),
                "c": round(c, 5),
            }
        )
        price = c
    return out


def _snap(symbol: str = "EURUSD", *, drift: float = 0.00012) -> dict:
    m15 = _synth_candles(80, drift=drift)
    h1 = _synth_candles(40, drift=drift * 2, seed=2.0)
    h4 = _synth_candles(20, drift=drift * 3, seed=3.0)
    mid = m15[-1]["c"]
    return {
        "symbol": symbol,
        "display": symbol[:3] + "/" + symbol[3:],
        "quote": {
            "mid": mid,
            "bid": mid - 0.00005,
            "ask": mid + 0.00005,
            "spread_pips": 1.0,
            "source": "mt5",
            "feed_state": "recente",
            "update_age_s": 2.0,
        },
        "candles_closed": {"M15": m15, "H1": h1, "H4": h4},
        "candles_counts": {"M15": len(m15), "H1": len(h1), "H4": len(h4)},
        "trading_costs": {"total": 2.5, "spread_pips": 1.0},
        "data_quality": {"data_ok": True, "executable": True},
    }


def test_probabilities_sum_to_one_and_horizons():
    view = analyze_pair_directional(_snap())
    assert view is not None
    assert view.data_ok
    assert view.forecasts
    for f in view.forecasts:
        s = f.p_up + f.p_down + f.p_flat
        assert abs(s - 1.0) < 1e-3
        assert f.horizon_min in {15, 30, 60}
        assert f.most_likely in {"up", "down", "flat"}
        assert 0.0 <= f.confidence <= 1.0
    # HOLD permitido no painel, mas direção mais provável sempre presente
    assert view.most_likely_direction in {"up", "down", "flat"}


def test_buy_and_sell_evaluated_with_rr_and_ev():
    view = analyze_pair_directional(_snap("GBPUSD", drift=0.00015))
    assert view is not None
    assert view.buy is not None
    assert view.sell is not None
    assert view.buy.rr >= 1.0
    assert view.sell.rr >= 1.0
    assert view.buy.risk_pips > 0
    assert view.buy.reward_pips > view.buy.risk_pips * 0.9
    assert isinstance(view.buy.expected_net_usd, float)
    assert view.buy.p_hit_tp_before_sl > 0


def test_ranking_by_expected_net_not_saturated_score():
    snaps = [
        _snap("EURUSD", drift=0.0002),
        _snap("GBPUSD", drift=-0.00018),
        _snap("AUDUSD", drift=0.00005),
    ]
    result = directional_from_snapshots(snaps, top_n=5)
    assert result["pairs_analyzed"] == 3
    assert result["ranking_metric"] == "expected_net_usd_after_costs"
    tops = result["top_opportunities"]
    assert tops
    # Scores heurísticos antigos saturavam em 0.98 — aqui a chave é EV líquido
    evs = [float(t["expected_net_usd"]) for t in tops]
    assert evs == sorted(evs, reverse=True)
    openai_ops = opportunities_for_openai(result)
    assert len(openai_ops) <= 5
    assert "p_up_30m" in openai_ops[0]
    assert "expected_net_usd_0.1lot" in openai_ops[0]


def test_schema_accepts_directional_fields():
    d = AIRadarDecision(
        decision="HOLD",
        justification="EV líquido ≤0 nos top candidatos",
        most_likely_direction="up",
        cited_p_up=0.41,
        cited_p_down=0.33,
        cited_expected_net_usd=-0.12,
        top_candidates_comparison=[],
    )
    assert d.most_likely_direction == "up"
    assert d.cited_p_up == 0.41


def test_persist_and_metrics_empty(db):
    from app.robot.prediction_store import persist_scan_predictions, validation_metrics

    snaps = [_snap("EURUSD"), _snap("GBPUSD", drift=-0.0001)]
    scan = directional_from_snapshots(snaps)
    sid = persist_scan_predictions(db, scan)
    assert sid
    metrics = validation_metrics(db)
    assert metrics["n_resolved"] == 0
    assert "Sem previsões resolvidas" in (metrics.get("calibration_note") or "")
