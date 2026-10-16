"""Persistência e avaliação de previsões direcionais (sem OpenAI)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.directional import DirectionalPrediction
from app.risk.instruments import get_instrument

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def persist_scan_predictions(
    db: Session,
    scan_result: dict[str, Any],
    *,
    decision_id: int = 0,
    openai_decision: str = "",
    max_rows: int = 40,
) -> str:
    """Guarda previsões do scan (pares + top oportunidades). Retorna scan_id."""
    scan_id = uuid.uuid4().hex[:16]
    now = _utcnow()
    rows = 0
    # Uma linha por par (horizonte 30m) + linhas das top oportunidades BUY/SELL.
    seen: set[tuple[str, str, int]] = set()

    def _add(
        *,
        symbol: str,
        side: str,
        horizon_min: int,
        most_likely: str,
        f: dict[str, Any],
        entry: float = 0.0,
        sl: float = 0.0,
        tp: float = 0.0,
        expected_net: float = 0.0,
        evidence: float = 0.0,
        mid: float = 0.0,
        features: dict[str, Any] | None = None,
        block: str = "",
    ) -> None:
        nonlocal rows
        key = (symbol, side, horizon_min)
        if key in seen or rows >= max_rows:
            return
        seen.add(key)
        db.add(
            DirectionalPrediction(
                symbol=symbol,
                side=side,
                horizon_min=horizon_min,
                most_likely=most_likely,
                p_up=float(f.get("p_up") or 0),
                p_down=float(f.get("p_down") or 0),
                p_flat=float(f.get("p_flat") or 0),
                expected_move_pips=float(f.get("expected_move_pips") or 0),
                expected_volatility_pips=float(f.get("expected_volatility_pips") or 0),
                confidence=float(f.get("confidence") or 0),
                uncertainty=float(f.get("uncertainty") or 0),
                calib_samples=int(f.get("calib_samples") or 0),
                calib_method=str(f.get("method") or ""),
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,
                expected_net_usd=expected_net,
                evidence_quality=evidence,
                mid_at_prediction=mid,
                features_json=json.dumps(features or {}, ensure_ascii=False, default=str)[:4000],
                forecast_json=json.dumps(f, ensure_ascii=False, default=str)[:4000],
                scan_id=scan_id,
                decision_id=decision_id,
                openai_decision=openai_decision,
                block_reason=block,
                predicted_at=now,
                resolve_at=now + timedelta(minutes=int(horizon_min)),
            )
        )
        rows += 1

    for v in scan_result.get("pair_views") or []:
        forecasts = v.get("forecasts") or []
        f30 = next((x for x in forecasts if x.get("horizon_min") == 30), None)
        if not f30:
            continue
        mid = 0.0
        buy = v.get("buy") or {}
        sell = v.get("sell") or {}
        if buy.get("entry"):
            mid = float(buy["entry"])
        elif sell.get("entry"):
            mid = float(sell["entry"])
        _add(
            symbol=str(v.get("symbol") or ""),
            side="",
            horizon_min=30,
            most_likely=str(v.get("most_likely_direction") or f30.get("most_likely") or "flat"),
            f=f30,
            mid=mid,
            features={"regime": v.get("regime"), "trend_strength": v.get("trend_strength")},
        )

    for o in (scan_result.get("top_opportunities") or [])[:10]:
        forecasts = o.get("forecasts") or []
        f = next((x for x in forecasts if x.get("horizon_min") == o.get("horizon_min", 30)), None)
        if not f and forecasts:
            f = forecasts[0]
        if not f:
            continue
        _add(
            symbol=str(o.get("symbol") or ""),
            side=str(o.get("side") or ""),
            horizon_min=int(o.get("horizon_min") or 30),
            most_likely=str(f.get("most_likely") or "flat"),
            f=f,
            entry=float(o.get("entry") or 0),
            sl=float(o.get("stop_loss") or 0),
            tp=float(o.get("take_profit") or 0),
            expected_net=float(o.get("expected_net_usd") or 0),
            evidence=float(o.get("evidence_quality") or 0),
            mid=float(o.get("entry") or 0),
            features=o.get("features") or {},
            block="; ".join(o.get("reject_reasons") or []),
        )

    if rows:
        db.commit()
    return scan_id


def _mid_now(symbol: str) -> float | None:
    try:
        from app.market.mt5_feed import get_mt5_feed

        q = get_mt5_feed().get_quote(symbol)
        if q is not None and getattr(q, "mid", None):
            return float(q.mid)
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_due_predictions(db: Session, *, limit: int = 80) -> dict[str, Any]:
    """Compara previsões vencidas com o mid atual (sem look-ahead no treino)."""
    now = _utcnow()
    due = list(
        db.scalars(
            select(DirectionalPrediction)
            .where(
                DirectionalPrediction.resolved == False,  # noqa: E712
                DirectionalPrediction.resolve_at <= now,
            )
            .order_by(DirectionalPrediction.resolve_at.asc())
            .limit(limit)
        )
    )
    resolved_n = 0
    for row in due:
        mid = _mid_now(row.symbol)
        if mid is None or not row.mid_at_prediction:
            continue
        try:
            pip = get_instrument(row.symbol).pip_size
        except ValueError:
            continue
        if pip <= 0:
            continue
        move = (mid - row.mid_at_prediction) / pip
        thr = max(2.0, 0.25 * float(row.expected_volatility_pips or 8.0))
        if move > thr:
            actual_dir = "up"
        elif move < -thr:
            actual_dir = "down"
        else:
            actual_dir = "flat"
        hit = actual_dir == (row.most_likely or "flat")
        row.mid_at_resolution = mid
        row.actual_move_pips = round(move, 2)
        row.actual_direction = actual_dir
        row.direction_hit = hit
        row.move_error_pips = round(abs(move - float(row.expected_move_pips or 0)), 2)
        row.resolved = True
        row.resolved_at = now
        resolved_n += 1
    if resolved_n:
        db.commit()
    return {"resolved": resolved_n, "pending_checked": len(due)}


def validation_metrics(db: Session, *, limit: int = 500) -> dict[str, Any]:
    """Métricas de calibração / acerto a partir de previsões já resolvidas."""
    rows = list(
        db.scalars(
            select(DirectionalPrediction)
            .where(DirectionalPrediction.resolved == True)  # noqa: E712
            .order_by(DirectionalPrediction.resolved_at.desc())
            .limit(limit)
        )
    )
    if not rows:
        return {
            "n_resolved": 0,
            "directional_accuracy": None,
            "mean_abs_move_error_pips": None,
            "by_horizon": {},
            "by_symbol": {},
            "calibration_note": "Sem previsões resolvidas ainda — aguardar horizontes.",
            "disclaimer": "Sem amostra resolvida não se declara edge nem rentabilidade.",
        }

    hits = [r for r in rows if r.direction_hit]
    errors = [float(r.move_error_pips) for r in rows if r.move_error_pips is not None]
    by_h: dict[str, dict[str, Any]] = {}
    by_sym: dict[str, dict[str, Any]] = {}
    for r in rows:
        hk = str(r.horizon_min)
        by_h.setdefault(hk, {"n": 0, "hits": 0})
        by_h[hk]["n"] += 1
        by_h[hk]["hits"] += 1 if r.direction_hit else 0
        sk = r.symbol
        by_sym.setdefault(sk, {"n": 0, "hits": 0})
        by_sym[sk]["n"] += 1
        by_sym[sk]["hits"] += 1 if r.direction_hit else 0
    for d in (by_h, by_sym):
        for k, v in d.items():
            v["accuracy"] = round(v["hits"] / v["n"], 4) if v["n"] else None

    # Calibração grosseira: entre previsões com p_up alto, % que foram up.
    high_up = [r for r in rows if r.p_up >= 0.45]
    high_up_ok = [r for r in high_up if r.actual_direction == "up"]
    return {
        "n_resolved": len(rows),
        "directional_accuracy": round(len(hits) / len(rows), 4),
        "mean_abs_move_error_pips": round(sum(errors) / len(errors), 3) if errors else None,
        "by_horizon": by_h,
        "by_symbol": dict(sorted(by_sym.items(), key=lambda x: -x[1]["n"])[:12]),
        "high_p_up_bucket": {
            "n": len(high_up),
            "actual_up_rate": round(len(high_up_ok) / len(high_up), 4) if high_up else None,
        },
        "calibration_note": (
            "Calibração empírica pós-horizonte; amostra pequena ⇒ incerteza elevada."
        ),
        "disclaimer": "Métricas descritivas — não constituem prova de lucro futuro.",
    }


def panel_predictions_payload(db: Session, scan_cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """Payload do painel de previsões (separado de decisões/ordens/PnL)."""
    try:
        resolve_due_predictions(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_due_predictions: %s", exc)
    metrics = validation_metrics(db)
    recent = list(
        db.scalars(
            select(DirectionalPrediction)
            .order_by(DirectionalPrediction.id.desc())
            .limit(20)
        )
    )
    scan = scan_cache or {}
    return {
        "label": "PREVISÕES DIRECIONAIS (motor quantitativo local)",
        "generated_at": scan.get("generated_at"),
        "ranking_metric": scan.get("ranking_metric") or "expected_net_usd_after_costs",
        "top_opportunities": scan.get("top_opportunities") or [],
        "top_buys": scan.get("top_buys") or [],
        "top_sells": scan.get("top_sells") or [],
        "pair_directions": [
            {
                "symbol": v.get("symbol"),
                "display": v.get("display"),
                "most_likely_direction": v.get("most_likely_direction"),
                "best_side": v.get("best_side"),
                "best_expected_net_usd": v.get("best_expected_net_usd"),
                "regime": v.get("regime"),
                "trend_strength": v.get("trend_strength"),
                "forecasts": v.get("forecasts") or [],
                "data_ok": v.get("data_ok"),
            }
            for v in (scan.get("pair_views") or [])
        ],
        "actionable_count": scan.get("actionable_count"),
        "validation": metrics,
        "recent_predictions": [
            {
                "id": r.id,
                "symbol": r.symbol,
                "side": r.side,
                "horizon_min": r.horizon_min,
                "most_likely": r.most_likely,
                "p_up": r.p_up,
                "p_down": r.p_down,
                "p_flat": r.p_flat,
                "expected_move_pips": r.expected_move_pips,
                "expected_net_usd": r.expected_net_usd,
                "evidence_quality": r.evidence_quality,
                "calib_method": r.calib_method,
                "predicted_at": _aware(r.predicted_at).isoformat() if r.predicted_at else None,
                "resolve_at": _aware(r.resolve_at).isoformat() if r.resolve_at else None,
                "resolved": r.resolved,
                "direction_hit": r.direction_hit,
                "actual_move_pips": r.actual_move_pips,
                "openai_decision": r.openai_decision or None,
                "block_reason": r.block_reason or None,
            }
            for r in recent
        ],
        "disclaimer": scan.get("disclaimer")
        or "Probabilidades locais calibradas; OpenAI não inventa estes números.",
        "layers": {
            "predictions": "motor quantitativo local",
            "decisions": "OpenAI analista (orçamento limitado)",
            "orders": "executor MT5 DEMO com gates",
            "results": "conta DEMO / trades fechados",
        },
    }
