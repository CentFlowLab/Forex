"""Validação walk-forward fora da amostra do motor direcional.

Treino → calibração de P(dir) e P(TP antes SL) por barreira OHLC.
Teste → só índices posteriores (sem fuga temporal).
Não envia ordens nem chama OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.robot.ev_math import (
    LOTS_DEFAULT,
    SL_ATR_MULT,
    TP_ATR_MULT,
    compute_ev,
    levels_from_atr,
    simulate_barrier_hit,
)
from app.robot.directional_engine import (
    _HORIZON_BARS,
    _FLAT_ATR_FRAC,
    _MIN_CALIB_SAMPLES,
    _bucket_key,
    _feature_vector,
)

Side = Literal["BUY", "SELL"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _label_move(ret: float, thr: float) -> str:
    if ret > thr:
        return "up"
    if ret < -thr:
        return "down"
    return "flat"


@dataclass
class FoldResult:
    fold: int
    train_end: int
    test_start: int
    test_end: int
    n_test: int
    dir_accuracy: float | None
    brier_up: float | None
    p_hit_buy_pred_mean: float | None
    p_hit_buy_real: float | None
    p_hit_sell_pred_mean: float | None
    p_hit_sell_real: float | None
    mean_ev_buy: float | None
    mean_ev_sell: float | None
    positive_ev_buy: int
    positive_ev_sell: int


def _build_samples(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    *,
    horizon_min: int = 30,
    max_barrier_bars: int = 16,
) -> list[dict[str, Any]]:
    """Uma amostra por barra (features só com passado; labels/barreira com futuro)."""
    bars_h = _HORIZON_BARS[horizon_min]
    n = len(closes)
    out: list[dict[str, Any]] = []
    end = n - max(bars_h, max_barrier_bars) - 1
    start = max(50, end - 8000)
    stride = 4 if (end - start) > 2000 else 1
    for i in range(start, end, stride):
        w0 = max(0, i + 1 - 120)
        feat = _feature_vector(closes[w0 : i + 1], highs[w0 : i + 1], lows[w0 : i + 1])
        if not feat:
            continue
        atr_i = float(feat["atr"])
        if atr_i <= 0:
            continue
        thr = _FLAT_ATR_FRAC * atr_i
        fut = closes[i + bars_h] - closes[i]
        lab = _label_move(fut, thr)
        entry = closes[i]
        fut_h = highs[i + 1 : i + 1 + max_barrier_bars]
        fut_l = lows[i + 1 : i + 1 + max_barrier_bars]
        buy_e, buy_sl, buy_tp = levels_from_atr(side="BUY", mid=entry, atr_price=atr_i)
        sell_e, sell_sl, sell_tp = levels_from_atr(side="SELL", mid=entry, atr_price=atr_i)
        buy_hit = simulate_barrier_hit(
            side="BUY",
            entry=buy_e,
            stop_loss=buy_sl,
            take_profit=buy_tp,
            future_highs=fut_h,
            future_lows=fut_l,
        )
        sell_hit = simulate_barrier_hit(
            side="SELL",
            entry=sell_e,
            stop_loss=sell_sl,
            take_profit=sell_tp,
            future_highs=fut_h,
            future_lows=fut_l,
        )
        out.append(
            {
                "i": i,
                "bucket": _bucket_key(feat),
                "feat": feat,
                "label": lab,
                "atr": atr_i,
                "entry": entry,
                "buy_outcome": buy_hit,  # tp|sl|timeout
                "sell_outcome": sell_hit,
            }
        )
    return out


def _calib_dir(train: list[dict[str, Any]], bucket: str) -> tuple[float, float, float, int, str]:
    labels = [s["label"] for s in train if s["bucket"] == bucket]
    method = "empirical_bucket"
    if len(labels) < _MIN_CALIB_SAMPLES:
        labels = [s["label"] for s in train]
        method = "empirical_pooled"
    n = len(labels)
    if n < 10:
        return 1 / 3, 1 / 3, 1 / 3, n, "uniform_low_sample"
    a = 1.0
    p_up = (labels.count("up") + a) / (n + 3 * a)
    p_down = (labels.count("down") + a) / (n + 3 * a)
    p_flat = (labels.count("flat") + a) / (n + 3 * a)
    return p_up, p_down, p_flat, n, method


def _calib_p_hit(train: list[dict[str, Any]], bucket: str, side: Side) -> tuple[float, int, str]:
    """P(TP antes SL) empírica; timeout conta como não-TP (conservador)."""
    key = "buy_outcome" if side == "BUY" else "sell_outcome"
    rows = [s for s in train if s["bucket"] == bucket]
    src = "bucket"
    if len(rows) < _MIN_CALIB_SAMPLES:
        rows = list(train)
        src = "pooled"
    if not rows:
        return 0.35, 0, "default"
    hits = sum(1 for s in rows if s[key] == "tp")
    n = len(rows)
    # Laplace
    p = (hits + 1.0) / (n + 2.0)
    return p, n, src


def walk_forward_symbol(
    symbol: str,
    candles_m15: list[dict[str, Any]],
    *,
    spread_pips: float,
    mt5_specs: dict[str, Any] | None = None,
    n_folds: int = 4,
    min_train: int = 400,
    test_frac: float = 0.15,
) -> dict[str, Any]:
    closes = [float(x["c"]) for x in candles_m15]
    highs = [float(x["h"]) for x in candles_m15]
    lows = [float(x["l"]) for x in candles_m15]
    samples = _build_samples(closes, highs, lows)
    if len(samples) < min_train + 50:
        return {
            "symbol": symbol,
            "ok": False,
            "reason": f"amostras insuficientes ({len(samples)}) para walk-forward",
            "n_samples": len(samples),
            "n_bars": len(closes),
            "folds": [],
        }

    n = len(samples)
    test_size = max(40, int(n * test_frac))
    folds: list[dict[str, Any]] = []
    # Expanding window: train [0, cut), test [cut, cut+test)
    for fold in range(n_folds):
        # Últimos n_folds blocos de teste
        test_end = n - (n_folds - 1 - fold) * test_size
        test_start = test_end - test_size
        if test_start < min_train:
            continue
        train = samples[:test_start]
        test = samples[test_start:test_end]
        if len(test) < 20:
            continue

        dir_hits = 0
        brier = 0.0
        ph_buy_preds: list[float] = []
        ph_sell_preds: list[float] = []
        buy_real_hits = 0
        sell_real_hits = 0
        ev_buys: list[float] = []
        ev_sells: list[float] = []
        pos_buy = 0
        pos_sell = 0

        for s in test:
            p_up, p_down, p_flat, _, _ = _calib_dir(train, s["bucket"])
            ml = "up" if p_up >= p_down and p_up >= p_flat else ("down" if p_down >= p_up else "flat")
            if ml == s["label"]:
                dir_hits += 1
            y_up = 1.0 if s["label"] == "up" else 0.0
            brier += (p_up - y_up) ** 2

            p_hit_b, _, src_b = _calib_p_hit(train, s["bucket"], "BUY")
            p_hit_s, _, src_s = _calib_p_hit(train, s["bucket"], "SELL")
            ph_buy_preds.append(p_hit_b)
            ph_sell_preds.append(p_hit_s)
            if s["buy_outcome"] == "tp":
                buy_real_hits += 1
            if s["sell_outcome"] == "tp":
                sell_real_hits += 1

            ev_b = compute_ev(
                symbol=symbol,
                side="BUY",
                mid=s["entry"],
                atr_price=s["atr"],
                p_hit_tp_before_sl=p_hit_b,
                p_hit_source=f"wf_train_{src_b}",
                p_direction=p_up,
                spread_pips=spread_pips,
                mt5_specs=mt5_specs,
            )
            ev_s = compute_ev(
                symbol=symbol,
                side="SELL",
                mid=s["entry"],
                atr_price=s["atr"],
                p_hit_tp_before_sl=p_hit_s,
                p_hit_source=f"wf_train_{src_s}",
                p_direction=p_down,
                spread_pips=spread_pips,
                mt5_specs=mt5_specs,
            )
            ev_buys.append(ev_b.expected_net_usd)
            ev_sells.append(ev_s.expected_net_usd)
            if ev_b.expected_net_usd > 0:
                pos_buy += 1
            if ev_s.expected_net_usd > 0:
                pos_sell += 1

        nt = len(test)
        folds.append(
            {
                "fold": fold,
                "train_end": test_start,
                "test_start": test_start,
                "test_end": test_end,
                "n_test": nt,
                "dir_accuracy": round(dir_hits / nt, 4),
                "brier_up": round(brier / nt, 4),
                "p_hit_buy_pred_mean": round(sum(ph_buy_preds) / nt, 4),
                "p_hit_buy_real": round(buy_real_hits / nt, 4),
                "p_hit_sell_pred_mean": round(sum(ph_sell_preds) / nt, 4),
                "p_hit_sell_real": round(sell_real_hits / nt, 4),
                "mean_ev_buy": round(sum(ev_buys) / nt, 4),
                "mean_ev_sell": round(sum(ev_sells) / nt, 4),
                "positive_ev_buy": pos_buy,
                "positive_ev_sell": pos_sell,
            }
        )

    if not folds:
        return {
            "symbol": symbol,
            "ok": False,
            "reason": "não foi possível formar folds",
            "n_samples": n,
            "n_bars": len(closes),
            "folds": [],
        }

    # Agregar
    def _avg(key: str) -> float | None:
        vals = [f[key] for f in folds if f.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "symbol": symbol,
        "ok": True,
        "n_samples": n,
        "n_bars": len(closes),
        "n_folds": len(folds),
        "folds": folds,
        "oos_summary": {
            "dir_accuracy": _avg("dir_accuracy"),
            "brier_up": _avg("brier_up"),
            "p_hit_buy_pred_mean": _avg("p_hit_buy_pred_mean"),
            "p_hit_buy_real": _avg("p_hit_buy_real"),
            "p_hit_sell_pred_mean": _avg("p_hit_sell_pred_mean"),
            "p_hit_sell_real": _avg("p_hit_sell_real"),
            "mean_ev_buy": _avg("mean_ev_buy"),
            "mean_ev_sell": _avg("mean_ev_sell"),
            "calibration_gap_buy": (
                round((_avg("p_hit_buy_pred_mean") or 0) - (_avg("p_hit_buy_real") or 0), 4)
                if _avg("p_hit_buy_pred_mean") is not None
                else None
            ),
            "calibration_gap_sell": (
                round((_avg("p_hit_sell_pred_mean") or 0) - (_avg("p_hit_sell_real") or 0), 4)
                if _avg("p_hit_sell_pred_mean") is not None
                else None
            ),
        },
        "disclaimer": (
            "Resultados OOS descritivos; não constituem prova de lucro futuro. "
            "Timeouts tratados como não-TP (conservador)."
        ),
    }


def run_universe_validation(
    history_by_symbol: dict[str, dict[str, Any]],
    *,
    spreads: dict[str, float] | None = None,
) -> dict[str, Any]:
    """history_by_symbol[sym] = {bars_m15, spread_pips, symbol_info}"""
    results = []
    spreads = spreads or {}
    for sym, payload in history_by_symbol.items():
        bars = payload.get("bars_m15") or []
        sp = float(payload.get("spread_pips") or spreads.get(sym) or 1.2)
        specs = payload.get("symbol_info")
        r = walk_forward_symbol(sym, bars, spread_pips=sp, mt5_specs=specs)
        results.append(r)

    ok = [r for r in results if r.get("ok")]
    return {
        "generated_at": _utcnow().isoformat(),
        "pairs_requested": len(history_by_symbol),
        "pairs_validated": len(ok),
        "pairs_skipped": len(results) - len(ok),
        "results": results,
        "aggregate": {
            "mean_dir_accuracy": _mean_key(ok, "oos_summary", "dir_accuracy"),
            "mean_ev_buy": _mean_key(ok, "oos_summary", "mean_ev_buy"),
            "mean_ev_sell": _mean_key(ok, "oos_summary", "mean_ev_sell"),
            "mean_p_hit_buy_real": _mean_key(ok, "oos_summary", "p_hit_buy_real"),
            "mean_p_hit_sell_real": _mean_key(ok, "oos_summary", "p_hit_sell_real"),
            "mean_calibration_gap_buy": _mean_key(ok, "oos_summary", "calibration_gap_buy"),
        },
        "ev_formula": (
            "EV_net = pip_value(lots)×(P_hit×reward_pips − (1−P_hit)×risk_pips) "
            "− (spread_pips + 2×slippage_pips)×pip_value − commission_per_lot×lots; "
            "P_hit = taxa empírica treino de TP-antes-SL (≠ P_up/P_down); "
            f"SL={SL_ATR_MULT}×ATR, TP={TP_ATR_MULT}×ATR, lots={LOTS_DEFAULT}"
        ),
    }


def _mean_key(rows: list[dict], *path: str) -> float | None:
    vals = []
    for r in rows:
        cur: Any = r
        for p in path:
            cur = (cur or {}).get(p) if isinstance(cur, dict) else None
        if cur is not None:
            vals.append(float(cur))
    return round(sum(vals) / len(vals), 4) if vals else None
