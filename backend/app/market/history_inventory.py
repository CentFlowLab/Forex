"""Inventário e cobertura de histórico Dukascopy — sem fabricar velas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.market.labels import DUKASCOPY_SOURCE, label_for
from app.market.validation import TIMEFRAME_DELTA, validate_candle_row, validate_series
from app.models.market import Candle

# Período inicial de investigação (12 meses) — não descarrega automaticamente.
DEFAULT_INVESTIGATION_START = datetime(2025, 10, 1, 0, 0, tzinfo=timezone.utc)
DEFAULT_INVESTIGATION_END = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

# Cobertura mínima para experiências com dados reais.
MIN_REAL_BARS = 500
MIN_COVERAGE_PCT = 70.0
MIN_SPLIT_BARS = 30


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def is_forex_weekend_gap(ts: datetime) -> bool:
    """Ausência legítima: mercado FX tipicamente fechado Sex 22:00–Dom 22:00 UTC."""
    ts = _aware(ts)
    wd = ts.weekday()
    if wd == 5:
        return True
    if wd == 6:
        return ts.hour < 22
    if wd == 4:
        return ts.hour >= 22
    return False


def iter_expected_slots(start: datetime, end: datetime, timeframe: str) -> list[datetime]:
    delta = TIMEFRAME_DELTA.get(timeframe)
    if delta is None:
        raise ValueError(f"Intervalo temporal não suportado: {timeframe}")
    start = _aware(start)
    end = _aware(end)
    # Alinhar ao bucket do timeframe.
    epoch = int(start.timestamp())
    step = int(delta.total_seconds())
    aligned = epoch - (epoch % step)
    cursor = datetime.fromtimestamp(aligned, tz=timezone.utc)
    if cursor < start:
        cursor += delta
    slots: list[datetime] = []
    while cursor < end:
        if not is_forex_weekend_gap(cursor):
            slots.append(cursor)
        cursor += delta
    return slots


def _row_dict(c: Candle) -> dict[str, Any]:
    return {
        "symbol": c.symbol,
        "timeframe": c.timeframe,
        "ts": _aware(c.ts),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
        "source": c.source,
        "is_synthetic": c.is_synthetic,
    }


def cache_bounds(
    db: Session,
    *,
    symbol: str = "EURUSD",
    source: str = DUKASCOPY_SOURCE,
) -> dict[str, Any]:
    """Extremos reais no cache por timeframe — só fonte pedida."""
    out: dict[str, Any] = {"symbol": symbol, "source": source, "by_timeframe": {}}
    for tf in ("M5", "M15", "H1", "H4"):
        first = db.scalar(
            select(func.min(Candle.ts)).where(
                Candle.symbol == symbol, Candle.timeframe == tf, Candle.source == source
            )
        )
        last = db.scalar(
            select(func.max(Candle.ts)).where(
                Candle.symbol == symbol, Candle.timeframe == tf, Candle.source == source
            )
        )
        count = int(
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol, Candle.timeframe == tf, Candle.source == source
                )
            )
            or 0
        )
        synthetic_count = int(
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol,
                    Candle.timeframe == tf,
                    Candle.source == source,
                    Candle.is_synthetic.is_(True),
                )
            )
            or 0
        )
        out["by_timeframe"][tf] = {
            "bars": count,
            "oldest": first.isoformat() if first else None,
            "newest": last.isoformat() if last else None,
            "synthetic_bars": synthetic_count,
            "real_bars": max(0, count - synthetic_count),
            "is_synthetic_source": source == "synthetic",
        }
    return out


def analyze_coverage(
    db: Session,
    *,
    start: datetime,
    end: datetime,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    source: str = DUKASCOPY_SOURCE,
) -> dict[str, Any]:
    """Cobertura percentual, lacunas e integridade — sem assumir validade só por contagem."""
    start, end = _aware(start), _aware(end)
    if end <= start:
        raise ValueError("A data de fim tem de ser posterior ao início.")

    rows = list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.source == source,
                Candle.ts >= start,
                Candle.ts < end,
            )
            .order_by(Candle.ts.asc())
        )
    )

    dicts = [_row_dict(c) for c in rows]
    cleaned, report = validate_series(dicts, timeframe) if dicts else ([], None)

    # Rejeitar velas sintéticas etiquetadas mesmo sob fonte dukascopy.
    real_cleaned = [r for r in cleaned if not r.get("is_synthetic")]
    synthetic_in_range = sum(1 for r in dicts if r.get("is_synthetic"))

    present = {_aware(r["ts"]) for r in real_cleaned}
    expected = iter_expected_slots(start, end, timeframe)
    missing_slots = [s for s in expected if s not in present]
    # Lacunas de falha = slots esperados em sessão sem vela válida.
    failure_gaps = missing_slots
    weekend_expected_skipped = 0
    # Contar slots de fim de semana no período (para transparência).
    delta = TIMEFRAME_DELTA[timeframe]
    cursor = start
    epoch = int(cursor.timestamp())
    step = int(delta.total_seconds())
    cursor = datetime.fromtimestamp(epoch - (epoch % step), tz=timezone.utc)
    while cursor < end:
        if is_forex_weekend_gap(cursor):
            weekend_expected_skipped += 1
        cursor += delta

    expected_n = len(expected)
    present_n = len(present & set(expected))
    coverage_pct = round(100.0 * present_n / expected_n, 2) if expected_n else 0.0

    # Continuidade: gaps entre velas consecutivas (excluindo fins de semana).
    continuity_gaps: list[str] = []
    sorted_ts = sorted(present)
    for i in range(1, len(sorted_ts)):
        gap = sorted_ts[i] - sorted_ts[i - 1]
        if gap <= delta * 2:
            continue
        # Se o intervalo atravessa fim de semana, é legítimo.
        probe = sorted_ts[i - 1] + delta
        crossed_weekend = False
        while probe < sorted_ts[i]:
            if is_forex_weekend_gap(probe):
                crossed_weekend = True
                break
            probe += delta
        if crossed_weekend:
            continue
        missing_n = int(gap / delta) - 1
        if missing_n > 0 and len(continuity_gaps) < 20:
            continuity_gaps.append(
                f"{sorted_ts[i-1].isoformat()} → {sorted_ts[i].isoformat()} (~{missing_n} velas)"
            )

    ohlc_errors = 0
    for r in dicts:
        if validate_candle_row(r):
            ohlc_errors += 1

    oldest = sorted_ts[0] if sorted_ts else None
    newest = sorted_ts[-1] if sorted_ts else None

    # Horas em falta para descarregamento incremental (agrupar slots em horas UTC).
    missing_hours: list[datetime] = []
    seen_h: set[datetime] = set()
    for slot in failure_gaps:
        hour = slot.replace(minute=0, second=0, microsecond=0)
        if hour not in seen_h:
            seen_h.add(hour)
            missing_hours.append(hour)
    missing_hours.sort()

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": source,
        "label": label_for(source, source == "synthetic"),
        "is_synthetic_source": source == "synthetic" or synthetic_in_range > 0 and present_n == 0,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "oldest_available": oldest.isoformat() if oldest else None,
        "newest_available": newest.isoformat() if newest else None,
        "bars_in_db": len(rows),
        "bars_valid_real": present_n,
        "bars_synthetic_in_range": synthetic_in_range,
        "expected_trading_bars": expected_n,
        "coverage_pct": coverage_pct,
        "weekend_slots_excluded": weekend_expected_skipped,
        "missing_trading_slots": len(failure_gaps),
        "missing_hours_count": len(missing_hours),
        "missing_hours_sample": [h.isoformat() for h in missing_hours[:30]],
        "continuity_gap_examples": continuity_gaps,
        "ohlc_errors": ohlc_errors,
        "duplicate_timestamps": report.duplicate_timestamps if report else 0,
        "validation_notes": report.notes if report else ["Sem velas na fonte pedida para o período."],
        "distinction": {
            "real_dukascopy": source == DUKASCOPY_SOURCE and present_n > 0 and synthetic_in_range == 0,
            "has_synthetic_contamination": synthetic_in_range > 0,
            "synthetic_only": present_n == 0 and synthetic_in_range > 0,
        },
        "fills_gaps_with_synthetic": False,
    }


def estimate_download(
    *,
    missing_hours: int,
    seconds_per_hour: float = 0.35,
) -> dict[str, Any]:
    """Estimativa fundamentada no nº de horas em falta e ritmo conservador."""
    hours = max(0, missing_hours)
    seconds = hours * seconds_per_hour
    # bi5 típico ~50–200 KB; estimativa conservadora 120 KB/hora com ticks.
    size_mb = round(hours * 0.12, 2)
    return {
        "missing_hours": hours,
        "estimated_duration_seconds": int(seconds),
        "estimated_duration_label": _duration_label(seconds),
        "estimated_size_mb": size_mb,
        "disclaimer": (
            "Estimativa local conservadora com base nas horas em falta. "
            "O tempo real depende da rede e do datafeed Dukascopy."
        ),
    }


def _duration_label(seconds: float) -> str:
    if seconds < 60:
        return f"~{int(seconds)} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"~{int(minutes)} min"
    hours = minutes / 60
    return f"~{hours:.1f} h"


def prepare_experiment(
    db: Session,
    *,
    start: datetime,
    end: datetime,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    require_real_data: bool = True,
    min_bars: int = MIN_REAL_BARS,
    min_coverage_pct: float = MIN_COVERAGE_PCT,
) -> dict[str, Any]:
    """Estado de preparação — bloqueia experiências com cobertura insuficiente."""
    coverage = analyze_coverage(
        db, start=start, end=end, symbol=symbol, timeframe=timeframe, source=DUKASCOPY_SOURCE
    )
    reasons: list[str] = []
    ready = True

    if require_real_data:
        if coverage["bars_valid_real"] < min_bars:
            ready = False
            reasons.append(
                f"Velas reais Dukascopy insuficientes ({coverage['bars_valid_real']} < {min_bars}). "
                "Use «Descarregar histórico em falta»."
            )
        if coverage["coverage_pct"] < min_coverage_pct:
            ready = False
            reasons.append(
                f"Cobertura {coverage['coverage_pct']}% abaixo do mínimo {min_coverage_pct}% "
                "no período selecionado (sessão FX)."
            )
        if coverage["distinction"]["has_synthetic_contamination"]:
            ready = False
            reasons.append(
                "Contaminação com velas sintéticas no período — não é permitido substituir "
                "histórico real por sintético."
            )
        if coverage["bars_valid_real"] == 0:
            ready = False
            reasons.append("Não há velas Dukascopy validadas neste intervalo.")

        # Separação treino/val/teste mínima.
        n = coverage["bars_valid_real"]
        train_n = int(n * 0.60)
        val_n = int(n * 0.20)
        test_n = n - train_n - val_n
        if train_n < MIN_SPLIT_BARS or val_n < MIN_SPLIT_BARS or test_n < MIN_SPLIT_BARS:
            ready = False
            reasons.append(
                f"Separação treino/validação/teste insuficiente "
                f"(treino={train_n}, validação={val_n}, teste={test_n}; mínimo {MIN_SPLIT_BARS} cada)."
            )
    else:
        # Modo teste/local: ainda exige mínimo absoluto de velas (qualquer fonte).
        total = int(
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.ts >= _aware(start),
                    Candle.ts < _aware(end),
                )
            )
            or 0
        )
        if total < 90:
            ready = False
            reasons.append(f"Dados insuficientes ({total} velas; mínimo 90).")

    syn_count = int(
        db.scalar(
            select(func.count(Candle.id)).where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.source == "synthetic",
                Candle.ts >= _aware(start),
                Candle.ts < _aware(end),
            )
        )
        or 0
    )

    missing_hours = coverage["missing_hours_count"]
    estimate = estimate_download(missing_hours=missing_hours)

    return {
        "ready": ready,
        "blocked": not ready,
        "reasons": reasons,
        "require_real_data": require_real_data,
        "coverage": coverage,
        "synthetic_bars_other_source": syn_count,
        "download_estimate": estimate,
        "default_investigation": {
            "start": DEFAULT_INVESTIGATION_START.isoformat(),
            "end": DEFAULT_INVESTIGATION_END.isoformat(),
            "symbol": "EURUSD",
            "timeframe": "M15",
            "label": "01/10/2025 00:00 UTC → 01/10/2026 00:00 UTC (12 meses)",
        },
        "ai_calls": False,
        "message": (
            "Experiência pronta para execução local (sem OpenAI)."
            if ready
            else "Experiência bloqueada: " + (reasons[0] if reasons else "dados insuficientes.")
        ),
    }


def hours_already_satisfied(
    db: Session,
    hour: datetime,
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    source: str = DUKASCOPY_SOURCE,
) -> bool:
    """Hora já coberta por velas reais validadas (evita re-descarregar)."""
    hour = _aware(hour).replace(minute=0, second=0, microsecond=0)
    end = hour + timedelta(hours=1)
    rows = list(
        db.scalars(
            select(Candle).where(
                and_(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.source == source,
                    Candle.is_synthetic.is_(False),
                    Candle.ts >= hour,
                    Candle.ts < end,
                )
            )
        )
    )
    if not rows:
        return False
    for r in rows:
        if validate_candle_row(_row_dict(r)):
            return False
    return True
