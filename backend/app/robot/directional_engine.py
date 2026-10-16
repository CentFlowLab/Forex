"""Motor quantitativo de previsão direcional (local, sem OpenAI).

Estima P(subida)/P(descida)/P(lateral) e oportunidades BUY/SELL a partir de
indicadores + calibração histórica nas velas fechadas (sem look-ahead).
Não força execução; ranking por expectativa líquida após custos.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.adaptive.regimes import classify_regime
from app.adaptive.costs import estimate_round_trip_costs
from app.market.indicators import compute_snapshot, sma
from app.risk.instruments import get_instrument
from app.robot.pair_snapshot import build_pair_snapshot
from app.robot.universe import FOREX_28_SYMBOLS, OPENAI_TOP_CANDIDATES

try:
    from app.robot.mt5_symbol_discovery import symbols_for_radar as _symbols_for_radar
except Exception:  # noqa: BLE001
    _symbols_for_radar = None

Side = Literal["BUY", "SELL"]
HorizonMin = Literal[15, 30, 60]

# Barras M15 por horizonte.
_HORIZON_BARS: dict[int, int] = {15: 1, 30: 2, 60: 4}
_FLAT_ATR_FRAC = 0.25  # |retorno| < 0.25*ATR ⇒ lateral
_MIN_CALIB_SAMPLES = 30
_TOP_OPPS = OPENAI_TOP_CANDIDATES


@dataclass
class HorizonForecast:
    horizon_min: int
    p_up: float
    p_down: float
    p_flat: float
    expected_move_pips: float
    expected_volatility_pips: float
    confidence: float
    uncertainty: float
    calib_samples: int
    most_likely: Literal["up", "down", "flat"]
    method: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DirectionalOpportunity:
    symbol: str
    display: str
    side: Side
    entry: float
    stop_loss: float
    take_profit: float
    risk_pips: float
    reward_pips: float
    rr: float
    p_hit_tp_before_sl: float
    expected_net_usd: float  # por 0.1 lote, após custos round-trip
    cost_usd: float
    evidence_quality: float  # 0–1
    horizon_min: int
    forecasts: list[HorizonForecast] = field(default_factory=list)
    regime: str = ""
    trend_strength: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    quote_source: str = ""
    feed_state: str = ""
    reasons: list[str] = field(default_factory=list)
    reject_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["forecasts"] = [f.as_dict() if hasattr(f, "as_dict") else f for f in self.forecasts]
        return d


@dataclass
class PairDirectionalView:
    symbol: str
    display: str
    most_likely_direction: Literal["up", "down", "flat"]
    forecasts: list[HorizonForecast]
    buy: DirectionalOpportunity | None
    sell: DirectionalOpportunity | None
    best_side: Side | None
    best_expected_net_usd: float
    indicators: dict[str, Any]
    regime: str
    trend_strength: str
    data_ok: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "display": self.display,
            "most_likely_direction": self.most_likely_direction,
            "forecasts": [f.as_dict() for f in self.forecasts],
            "buy": self.buy.as_dict() if self.buy else None,
            "sell": self.sell.as_dict() if self.sell else None,
            "best_side": self.best_side,
            "best_expected_net_usd": self.best_expected_net_usd,
            "indicators": self.indicators,
            "regime": self.regime,
            "trend_strength": self.trend_strength,
            "data_ok": self.data_ok,
            "notes": self.notes,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _softmax3(a: float, b: float, c: float) -> tuple[float, float, float]:
    m = max(a, b, c)
    ea, eb, ec = math.exp(a - m), math.exp(b - m), math.exp(c - m)
    s = ea + eb + ec
    return ea / s, eb / s, ec / s


def _trend_strength(slope: float | None, adx: float | None, regime: str) -> str:
    if regime in {"tendencia_alta", "tendencia_baixa"} and (adx or 0) >= 1.5:
        return "forte"
    if regime in {"tendencia_alta", "tendencia_baixa"}:
        return "fraca"
    if regime == "mercado_lateral" or regime == "volatilidade_reduzida":
        return "consolidacao"
    if regime == "volatilidade_elevada" and slope is not None and abs(slope) > 0.001:
        return "reversao_provavel" if (adx or 0) < 1.0 else "forte"
    return "indefinida"


def _feature_vector(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> dict[str, float] | None:
    """Features leves (sem MACD O(n²)) para calibração em massa."""
    from app.market.indicators import atr as atr_fn
    from app.market.indicators import rsi as rsi_fn

    if len(closes) < 40:
        return None
    regime = classify_regime(closes, highs, lows)
    slope = float(regime.trend_slope or 0.0)
    rsi_v = float(rsi_fn(closes, 14) or 50.0)
    atr_v = float(atr_fn(highs, lows, closes, 14) or 0.0)
    last = float(closes[-1])
    if atr_v <= 0 or last <= 0:
        return None
    s20 = sma(closes, 20)
    s50 = sma(closes, min(50, len(closes)))
    above20 = 1.0 if s20 and last > s20 else -1.0
    above50 = 1.0 if s50 and last > s50 else -1.0
    ret4 = (closes[-1] - closes[-5]) / atr_v if len(closes) >= 5 else 0.0
    return {
        "slope": slope,
        "rsi": rsi_v,
        "macd_hist": 0.0,  # omitido na calibração em massa (custo); painel usa compute_snapshot
        "atr_pct": atr_v / last,
        "above20": above20,
        "above50": above50,
        "ret4_atr": ret4,
        "adx_proxy": float(regime.adx_proxy or 0.0),
        "atr": atr_v,
    }


def _bucket_key(feat: dict[str, float]) -> str:
    slope_b = "up" if feat["slope"] > 0.0005 else ("dn" if feat["slope"] < -0.0005 else "flat")
    rsi_b = "os" if feat["rsi"] < 35 else ("ob" if feat["rsi"] > 65 else "mid")
    mom_b = "up" if feat["ret4_atr"] > 0.3 else ("dn" if feat["ret4_atr"] < -0.3 else "flat")
    return f"{slope_b}|{rsi_b}|{mom_b}"


def _calibrate_horizons(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    pip_size: float,
) -> dict[int, dict[str, Any]]:
    """Calibração empírica walk-forward nas velas fechadas (sem usar o futuro do índice atual)."""
    out: dict[int, dict[str, Any]] = {}
    n = len(closes)
    if n < 60:
        return out
    # Limitar custo: janela recente + stride (sem look-ahead).
    max_h = max(_HORIZON_BARS.values())
    start = max(40, n - 8000)
    stride = 4 if (n - start) > 2000 else 1
    buckets: dict[str, dict[int, list[str]]] = {}
    for i in range(start, n - max_h, stride):
        # Features só com passado; janela curta para indicadores (não série completa).
        w0 = max(0, i + 1 - 120)
        feat = _feature_vector(closes[w0 : i + 1], highs[w0 : i + 1], lows[w0 : i + 1])
        if not feat:
            continue
        key = _bucket_key(feat)
        atr_i = feat["atr"]
        for h_min, bars in _HORIZON_BARS.items():
            fut = closes[i + bars] - closes[i]
            thr = _FLAT_ATR_FRAC * atr_i
            if fut > thr:
                lab = "up"
            elif fut < -thr:
                lab = "down"
            else:
                lab = "flat"
            buckets.setdefault(key, {}).setdefault(h_min, []).append(lab)

    # Features atuais (último índice fechado) — janela curta.
    w_now = max(0, n - 120)
    feat_now = _feature_vector(closes[w_now:], highs[w_now:], lows[w_now:])
    if not feat_now:
        return out
    key_now = _bucket_key(feat_now)
    atr_now = feat_now["atr"]

    for h_min, bars in _HORIZON_BARS.items():
        labels = buckets.get(key_now, {}).get(h_min, [])
        # Suavização: misturar com labels globais do mesmo horizonte se amostra pequena.
        global_labs: list[str] = []
        for bk, by_h in buckets.items():
            global_labs.extend(by_h.get(h_min, []))
        samples = labels if len(labels) >= _MIN_CALIB_SAMPLES else (labels + global_labs[:80])
        n_s = len(samples)
        if n_s < 10:
            # Prior fraco a partir de features (não inventar vantagem estatística).
            score_up = 0.15 * feat_now["slope"] * 1000 + 0.02 * (feat_now["rsi"] - 50) / 10
            score_dn = -score_up
            score_flat = 0.8
            p_up, p_down, p_flat = _softmax3(score_up, score_dn, score_flat)
            method = "feature_prior_low_sample"
            conf = 0.2
        else:
            ups = samples.count("up") / n_s
            dns = samples.count("down") / n_s
            flats = samples.count("flat") / n_s
            # Shrinkage Laplace
            a = 1.0
            p_up = (samples.count("up") + a) / (n_s + 3 * a)
            p_down = (samples.count("down") + a) / (n_s + 3 * a)
            p_flat = (samples.count("flat") + a) / (n_s + 3 * a)
            # Ajuste suave com sinal de feature (peso baixo).
            adj = 0.08 * max(-1.0, min(1.0, feat_now["ret4_atr"]))
            p_up = max(0.05, min(0.9, p_up + adj))
            p_down = max(0.05, min(0.9, p_down - adj))
            s = p_up + p_down + p_flat
            p_up, p_down, p_flat = p_up / s, p_down / s, p_flat / s
            method = "empirical_bucket" if len(labels) >= _MIN_CALIB_SAMPLES else "empirical_pooled"
            conf = min(0.85, 0.25 + 0.5 * (min(n_s, 120) / 120.0))

        # Movimento esperado em pips (assinado: + subida).
        exp_move = (p_up - p_down) * (atr_now / pip_size) * math.sqrt(bars)
        exp_vol = (atr_now / pip_size) * math.sqrt(bars)
        unc = 1.0 - conf
        ml: Literal["up", "down", "flat"]
        if p_up >= p_down and p_up >= p_flat:
            ml = "up"
        elif p_down >= p_up and p_down >= p_flat:
            ml = "down"
        else:
            ml = "flat"
        out[h_min] = {
            "p_up": round(p_up, 4),
            "p_down": round(p_down, 4),
            "p_flat": round(p_flat, 4),
            "expected_move_pips": round(exp_move, 2),
            "expected_volatility_pips": round(exp_vol, 2),
            "confidence": round(conf, 3),
            "uncertainty": round(unc, 3),
            "calib_samples": n_s,
            "most_likely": ml,
            "method": method,
            "atr": atr_now,
        }
    return out


def _calibrate_barrier_p_hit(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    *,
    side: Side,
    bucket_now: str,
    max_barrier_bars: int = 16,
) -> tuple[float, int, str]:
    """P(TP antes SL) empírica no passado (≠ P_up). Timeout = não-TP."""
    from app.robot.ev_math import levels_from_atr, simulate_barrier_hit

    n = len(closes)
    if n < 80:
        return 0.35, 0, "default_low_history"
    outcomes: list[str] = []
    bucket_outcomes: list[str] = []
    # Excluir as últimas max_barrier_bars para não usar futuro da barra atual.
    end = n - max_barrier_bars - 1
    start = max(50, end - 6000)
    stride = 3 if (end - start) > 1500 else 1
    for i in range(start, end, stride):
        w0 = max(0, i + 1 - 120)
        feat = _feature_vector(closes[w0 : i + 1], highs[w0 : i + 1], lows[w0 : i + 1])
        if not feat:
            continue
        atr_i = float(feat["atr"])
        if atr_i <= 0:
            continue
        entry, sl, tp = levels_from_atr(side=side, mid=closes[i], atr_price=atr_i)
        hit = simulate_barrier_hit(
            side=side,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            future_highs=highs[i + 1 : i + 1 + max_barrier_bars],
            future_lows=lows[i + 1 : i + 1 + max_barrier_bars],
        )
        outcomes.append(hit)
        if _bucket_key(feat) == bucket_now:
            bucket_outcomes.append(hit)
    use = bucket_outcomes if len(bucket_outcomes) >= _MIN_CALIB_SAMPLES else outcomes
    src = "barrier_bucket" if len(bucket_outcomes) >= _MIN_CALIB_SAMPLES else "barrier_pooled"
    if len(use) < 10:
        return 0.35, len(use), "default_low_sample"
    p = (sum(1 for x in use if x == "tp") + 1.0) / (len(use) + 2.0)
    return float(p), len(use), src


def _build_side_opportunity(
    *,
    symbol: str,
    display: str,
    side: Side,
    mid: float,
    spread_pips: float,
    pip_size: float,
    atr_price: float,
    forecasts: list[HorizonForecast],
    regime: str,
    trend_strength: str,
    features: dict[str, Any],
    quote_source: str,
    feed_state: str,
    costs_total: float,
    p_hit: float,
    p_hit_source: str,
    p_hit_samples: int,
    mt5_specs: dict[str, Any] | None = None,
) -> DirectionalOpportunity | None:
    if mid <= 0 or atr_price <= 0 or pip_size <= 0:
        return None
    from app.robot.ev_math import compute_ev

    f30 = next((f for f in forecasts if f.horizon_min == 30), None)
    p_dir = float(
        (f30.p_up if side == "BUY" else f30.p_down) if f30 else 0.33
    )
    evidence = float(f30.confidence) if f30 else 0.2
    # Evidência também depende da amostra de barreiras.
    if p_hit_samples >= _MIN_CALIB_SAMPLES:
        evidence = min(0.9, max(evidence, 0.25 + 0.5 * min(p_hit_samples, 400) / 400.0))

    ev = compute_ev(
        symbol=symbol,
        side=side,
        mid=mid,
        atr_price=atr_price,
        p_hit_tp_before_sl=p_hit,
        p_hit_source=p_hit_source,
        p_direction=p_dir,
        spread_pips=spread_pips,
        mt5_specs=mt5_specs,
    )
    reasons = [
        f"regime={regime}",
        f"trend={trend_strength}",
        f"p_dir_30m={p_dir:.2f}",
        f"p_hit={ev.p_hit_tp_before_sl:.2f}≠p_dir ({p_hit_source}, n={p_hit_samples})",
        f"RR={ev.rr:.2f}",
        f"BE_p_hit={ev.breakeven_p_hit:.2f}",
        f"pip_value={ev.pip_value:.4f} ({ev.pip_value_source})",
        f"calib_conf={evidence:.2f}",
    ]
    reject: list[str] = []
    if evidence < 0.35 or p_hit_samples < _MIN_CALIB_SAMPLES:
        reject.append("calibração histórica fraca — não afirmar edge estatístico")
    if ev.expected_net_usd <= 0:
        reject.append("expectativa líquida ≤ 0 após custos")
    if ev.p_hit_tp_before_sl < ev.breakeven_p_hit:
        reject.append(
            f"P_hit {ev.p_hit_tp_before_sl:.2f} < break-even {ev.breakeven_p_hit:.2f}"
        )
    if spread_pips > 3.5:
        reject.append(f"spread elevado ({spread_pips:.1f} pips)")
    if quote_source != "mt5" or feed_state != "recente":
        reject.append("cotação não MT5 recente — não executável")

    feat_out = dict(features)
    feat_out["ev_audit"] = {
        "formula": ev.formula,
        "gross_usd": ev.gross_usd,
        "cost_usd": ev.cost_usd,
        "pip_value_source": ev.pip_value_source,
        "p_hit_source": p_hit_source,
        "p_hit_samples": p_hit_samples,
    }

    return DirectionalOpportunity(
        symbol=symbol,
        display=display,
        side=side,
        entry=ev.entry,
        stop_loss=ev.stop_loss,
        take_profit=ev.take_profit,
        risk_pips=round(ev.risk_pips, 2),
        reward_pips=round(ev.reward_pips, 2),
        rr=round(ev.rr, 3),
        p_hit_tp_before_sl=ev.p_hit_tp_before_sl,
        expected_net_usd=ev.expected_net_usd,
        cost_usd=ev.cost_usd,
        evidence_quality=round(evidence, 3),
        horizon_min=30,
        forecasts=forecasts,
        regime=regime,
        trend_strength=trend_strength,
        features=feat_out,
        quote_source=quote_source,
        feed_state=feed_state,
        reasons=reasons,
        reject_reasons=reject,
    )


def _tf_trend(candles: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(x["c"]) for x in candles]
    highs = [float(x["h"]) for x in candles]
    lows = [float(x["l"]) for x in candles]
    if len(closes) < 20:
        return {"label": "desconhecido", "slope": None, "above_sma20": None}
    reg = classify_regime(closes, highs, lows)
    s20 = sma(closes, 20)
    return {
        "label": reg.label,
        "slope": reg.trend_slope,
        "adx_proxy": reg.adx_proxy,
        "above_sma20": bool(s20 and closes[-1] > s20),
        "hh_hl": bool(len(highs) >= 6 and highs[-1] > highs[-3] and lows[-1] > lows[-3]),
        "lh_ll": bool(len(highs) >= 6 and highs[-1] < highs[-3] and lows[-1] < lows[-3]),
    }


def analyze_pair_directional(snap: dict[str, Any]) -> PairDirectionalView | None:
    symbol = str(snap.get("symbol") or "")
    display = str(snap.get("display") or symbol)
    q = snap.get("quote") or {}
    dq = snap.get("data_quality") or {}
    cc = snap.get("candles_closed") or {}
    candles = cc.get("M15") or []
    if not candles or not dq.get("data_ok"):
        return PairDirectionalView(
            symbol=symbol,
            display=display,
            most_likely_direction="flat",
            forecasts=[],
            buy=None,
            sell=None,
            best_side=None,
            best_expected_net_usd=0.0,
            indicators={},
            regime="desconhecido",
            trend_strength="indefinida",
            data_ok=False,
            notes=["dados insuficientes"],
        )
    closes = [float(x["c"]) for x in candles]
    highs = [float(x["h"]) for x in candles]
    lows = [float(x["l"]) for x in candles]
    try:
        spec = get_instrument(symbol)
        pip_size = spec.pip_size
    except ValueError:
        return None

    ind = compute_snapshot([], highs, lows, closes)
    regime = classify_regime(closes, highs, lows)
    strength = _trend_strength(regime.trend_slope, regime.adx_proxy, regime.label)
    trends = {
        "M15": _tf_trend(candles),
        "H1": _tf_trend(cc.get("H1") or []),
        "H4": _tf_trend(cc.get("H4") or []),
    }
    # Alinhar força se H1/H4 discordarem do M15.
    labels = {trends["M15"]["label"], trends["H1"]["label"], trends["H4"]["label"]}
    if "tendencia_alta" in labels and "tendencia_baixa" in labels:
        strength = "reversao_provavel"
    calib = _calibrate_horizons(closes, highs, lows, pip_size)
    forecasts: list[HorizonForecast] = []
    for h in (15, 30, 60):
        c = calib.get(h)
        if not c:
            continue
        forecasts.append(
            HorizonForecast(
                horizon_min=h,
                p_up=c["p_up"],
                p_down=c["p_down"],
                p_flat=c["p_flat"],
                expected_move_pips=c["expected_move_pips"],
                expected_volatility_pips=c["expected_volatility_pips"],
                confidence=c["confidence"],
                uncertainty=c["uncertainty"],
                calib_samples=c["calib_samples"],
                most_likely=c["most_likely"],
                method=c["method"],
            )
        )
    if not forecasts:
        return PairDirectionalView(
            symbol=symbol,
            display=display,
            most_likely_direction="flat",
            forecasts=[],
            buy=None,
            sell=None,
            best_side=None,
            best_expected_net_usd=0.0,
            indicators=ind.__dict__ if hasattr(ind, "__dict__") else {},
            regime=regime.label,
            trend_strength=strength,
            data_ok=True,
            notes=["calibração indisponível (histórico curto)"],
        )

    # Direção mais provável no horizonte 30m (ou o de maior confiança).
    primary = next((f for f in forecasts if f.horizon_min == 30), forecasts[0])
    most = primary.most_likely

    mid = float(q.get("mid") or closes[-1])
    spread = float(q.get("spread_pips") or 1.2)
    costs = snap.get("trading_costs") or {}
    cost_total = float(costs.get("total") or estimate_round_trip_costs(symbol, 0.10, spread_pips=spread).total)
    feat = _feature_vector(closes, highs, lows) or {}
    feat["trends"] = trends
    atr_v = float(ind.atr_14 or feat.get("atr") or 0.0)
    bucket_now = _bucket_key(feat) if feat else "flat|mid|flat"
    mt5_specs = snap.get("mt5_symbol_info") if isinstance(snap.get("mt5_symbol_info"), dict) else None
    p_hit_buy, n_hit_buy, src_buy = _calibrate_barrier_p_hit(
        closes, highs, lows, side="BUY", bucket_now=bucket_now
    )
    p_hit_sell, n_hit_sell, src_sell = _calibrate_barrier_p_hit(
        closes, highs, lows, side="SELL", bucket_now=bucket_now
    )

    buy = _build_side_opportunity(
        symbol=symbol,
        display=display,
        side="BUY",
        mid=mid,
        spread_pips=spread,
        pip_size=pip_size,
        atr_price=atr_v,
        forecasts=forecasts,
        regime=regime.label,
        trend_strength=strength,
        features=feat,
        quote_source=str(q.get("source") or ""),
        feed_state=str(q.get("feed_state") or ""),
        costs_total=cost_total,
        p_hit=p_hit_buy,
        p_hit_source=src_buy,
        p_hit_samples=n_hit_buy,
        mt5_specs=mt5_specs,
    )
    sell = _build_side_opportunity(
        symbol=symbol,
        display=display,
        side="SELL",
        mid=mid,
        spread_pips=spread,
        pip_size=pip_size,
        atr_price=atr_v,
        forecasts=forecasts,
        regime=regime.label,
        trend_strength=strength,
        features=feat,
        quote_source=str(q.get("source") or ""),
        feed_state=str(q.get("feed_state") or ""),
        costs_total=cost_total,
        p_hit=p_hit_sell,
        p_hit_source=src_sell,
        p_hit_samples=n_hit_sell,
        mt5_specs=mt5_specs,
    )

    best_side: Side | None = None
    best_net = 0.0
    if buy and sell:
        if buy.expected_net_usd >= sell.expected_net_usd:
            best_side, best_net = "BUY", buy.expected_net_usd
        else:
            best_side, best_net = "SELL", sell.expected_net_usd
    elif buy:
        best_side, best_net = "BUY", buy.expected_net_usd
    elif sell:
        best_side, best_net = "SELL", sell.expected_net_usd

    return PairDirectionalView(
        symbol=symbol,
        display=display,
        most_likely_direction=most,
        forecasts=forecasts,
        buy=buy,
        sell=sell,
        best_side=best_side,
        best_expected_net_usd=round(best_net, 4),
        indicators={
            "rsi_14": ind.rsi_14,
            "sma_20": ind.sma_20,
            "sma_50": ind.sma_50,
            "ema_12": ind.ema_12,
            "macd": ind.macd,
            "macd_signal": ind.macd_signal,
            "macd_histogram": ind.macd_histogram,
            "atr_14": ind.atr_14,
            "support": ind.support,
            "resistance": ind.resistance,
            "volatility_regime": ind.volatility_regime,
            "trends": trends,
        },
        regime=regime.label,
        trend_strength=strength,
        data_ok=True,
        notes=[],
    )


def _rank_directional(
    views: list[PairDirectionalView],
    *,
    universe: int,
    top_n: int = _TOP_OPPS,
) -> dict[str, Any]:
    opps: list[DirectionalOpportunity] = []
    for view in views:
        if view.buy:
            opps.append(view.buy)
        if view.sell:
            opps.append(view.sell)
    opps_sorted = sorted(
        opps,
        key=lambda o: (o.expected_net_usd, o.evidence_quality, o.rr),
        reverse=True,
    )
    actionable = [
        o
        for o in opps_sorted
        if o.expected_net_usd > 0
        and o.evidence_quality >= 0.35
        and o.quote_source == "mt5"
        and o.feed_state == "recente"
        and not o.reject_reasons
    ]
    top = (actionable or opps_sorted)[:top_n]
    buys = [o for o in opps_sorted if o.side == "BUY"][:5]
    sells = [o for o in opps_sorted if o.side == "SELL"][:5]
    return {
        "generated_at": _utcnow().isoformat(),
        "universe": universe,
        "pairs_analyzed": len(views),
        "top_opportunities": [o.as_dict() for o in top],
        "top_buys": [o.as_dict() for o in buys],
        "top_sells": [o.as_dict() for o in sells],
        "pair_views": [v.as_dict() for v in views],
        "ranking_metric": "expected_net_usd_after_costs",
        "disclaimer": (
            "Probabilidades calibradas em histórico local de velas fechadas. "
            "Amostra baixa ⇒ uncertainty elevada; não afirmar edge estatístico. "
            "Não é garantia de lucro. OpenAI não inventa estas probabilidades."
        ),
        "actionable_count": len(actionable),
    }


def directional_from_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    top_n: int = _TOP_OPPS,
) -> dict[str, Any]:
    """Ranking a partir de snapshots já construídos (evita rebuild no radar)."""
    views: list[PairDirectionalView] = []
    for snap in snapshots:
        view = analyze_pair_directional(snap)
        if view is not None:
            views.append(view)
    return _rank_directional(views, universe=len(snapshots), top_n=top_n)


def scan_directional_opportunities(
    db: Session,
    *,
    symbols: list[str] | None = None,
    top_n: int = _TOP_OPPS,
) -> dict[str, Any]:
    """Varre o universo, produz previsões e ranking BUY/SELL por EV líquido."""
    if symbols is None:
        if _symbols_for_radar is not None:
            try:
                symbols = _symbols_for_radar(prefer_mt5=True)
            except Exception:  # noqa: BLE001
                symbols = list(FOREX_28_SYMBOLS)
        else:
            symbols = list(FOREX_28_SYMBOLS)
    snaps = [build_pair_snapshot(db, sym) for sym in symbols]
    return directional_from_snapshots(snaps, top_n=top_n)


def opportunities_for_openai(scan_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Compacta as top oportunidades para o contexto OpenAI (sem milhares de tokens)."""
    out = []
    for o in scan_result.get("top_opportunities") or []:
        f30 = next((f for f in (o.get("forecasts") or []) if f.get("horizon_min") == 30), {})
        net = o.get("expected_net_usd")
        cost = o.get("cost_usd")
        try:
            gross = (
                round(float(net) + float(cost), 4)
                if net is not None and cost is not None
                else (o.get("features") or {}).get("ev_audit", {}).get("gross_usd")
            )
        except (TypeError, ValueError):
            gross = None
        out.append(
            {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "entry": o.get("entry"),
                "stop_loss": o.get("stop_loss"),
                "take_profit": o.get("take_profit"),
                "rr": o.get("rr"),
                "p_hit_tp_before_sl": o.get("p_hit_tp_before_sl"),
                # Nome explícito: JÁ é líquido após custos — NÃO voltar a subtrair cost_usd.
                "expected_net_usd_after_costs": net,
                "expected_net_usd_0.1lot": net,  # alias legado
                "expected_gross_usd_before_costs": gross,
                "cost_usd": cost,
                "costs_already_included_in_net": True,
                "ev_note": (
                    "expected_net_usd_after_costs = gross − cost_usd. "
                    "PROIBIDO calcular net−cost outra vez. "
                    "expected_move_pips_30m é movimento direcional esperado — "
                    "NÃO é o EV em dólares."
                ),
                "evidence_quality": o.get("evidence_quality"),
                "regime": o.get("regime"),
                "trend_strength": o.get("trend_strength"),
                "p_up_30m": f30.get("p_up"),
                "p_down_30m": f30.get("p_down"),
                "p_flat_30m": f30.get("p_flat"),
                "expected_move_pips_30m": f30.get("expected_move_pips"),
                "calib_samples_30m": f30.get("calib_samples"),
                "calib_method": f30.get("method"),
                "reject_reasons": o.get("reject_reasons") or [],
                "reasons": o.get("reasons") or [],
            }
        )
    return out
