"""Pré-verificação de dados antes de qualquer chamada OpenAI paga."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.autonomy.timeutil import as_utc, iso_utc, utcnow
from app.config import get_settings
from app.market.quote_feed import get_quote_feed
from app.robot.context import count_closed_candles

AnalysisKind = Literal["unavailable", "too_stale", "historical", "fresh"]


@dataclass
class DataReadiness:
    ok_for_paid: bool
    analysis_kind: AnalysisKind
    allow_execution: bool
    reason: str
    quote_valid: bool
    quote_ts: str | None
    lag_seconds: float | None
    connection_status: str
    channel: str
    candles_ok: bool
    candles_counts: dict[str, int]
    last_candle_age_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok_for_paid": self.ok_for_paid,
            "analysis_kind": self.analysis_kind,
            "allow_execution": self.allow_execution,
            "reason": self.reason,
            "quote_valid": self.quote_valid,
            "quote_ts": self.quote_ts,
            "lag_seconds": self.lag_seconds,
            "connection_status": self.connection_status,
            "channel": self.channel,
            "candles_ok": self.candles_ok,
            "candles_counts": self.candles_counts,
            "last_candle_age_seconds": self.last_candle_age_seconds,
        }


# Limiares (segundos) — horizonte de decisão intradía.
_MIN_M15 = 20
_MIN_H1 = 10
_MIN_H4 = 5
# Acima disto: dados históricos atrasados — análise histórica sem execução.
_HISTORICAL_LAG = 120.0
# Acima disto: demasiado antigo para análise paga (nem histórica).
_TOO_STALE_LAG = 6 * 3600.0  # 6 h


def assess_data_readiness(db: Session, *, recover_feed: bool = True) -> DataReadiness:
    """Avalia se há dados suficientes para alimentar o cérebro IA."""
    settings = get_settings()
    feed_svc = get_quote_feed()
    if recover_feed:
        try:
            snap_pre = feed_svc.snapshot()
            if snap_pre.mid is None or snap_pre.status in {"desligado", "obsoleto"}:
                feed_svc.recover()
        except Exception:  # noqa: BLE001
            pass

    snap = feed_svc.snapshot()
    counts, last_m15_ts = count_closed_candles(db)
    candles_ok = (
        counts.get("M15", 0) >= _MIN_M15
        and counts.get("H1", 0) >= _MIN_H1
        and counts.get("H4", 0) >= _MIN_H4
    )

    quote_valid = bool(snap.mid is not None and not snap.is_synthetic)
    lag = float(snap.lag_seconds) if snap.lag_seconds is not None else None
    quote_ts = iso_utc(snap.quote_ts) if snap.quote_ts else None

    candle_age: float | None = None
    if last_m15_ts is not None:
        ts = as_utc(last_m15_ts) or last_m15_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        candle_age = (utcnow() - ts).total_seconds()

    # Idade efetiva: cotação se existir; senão idade da última vela M15.
    effective_age = lag if lag is not None else candle_age

    if not quote_valid and not candles_ok:
        return DataReadiness(
            ok_for_paid=False,
            analysis_kind="unavailable",
            allow_execution=False,
            reason=(
                "Dados indisponíveis: sem cotação válida e velas fechadas insuficientes "
                f"(M15={counts.get('M15', 0)}, H1={counts.get('H1', 0)}, H4={counts.get('H4', 0)})."
            ),
            quote_valid=False,
            quote_ts=quote_ts,
            lag_seconds=lag,
            connection_status=snap.status,
            channel=snap.channel,
            candles_ok=False,
            candles_counts=counts,
            last_candle_age_seconds=candle_age,
        )

    if effective_age is not None and effective_age > _TOO_STALE_LAG:
        return DataReadiness(
            ok_for_paid=False,
            analysis_kind="too_stale",
            allow_execution=False,
            reason=(
                f"Dados demasiado atrasados para o horizonte de decisão "
                f"(atraso≈{effective_age:.0f}s > {_TOO_STALE_LAG:.0f}s). "
                "API paga não será chamada."
            ),
            quote_valid=quote_valid,
            quote_ts=quote_ts,
            lag_seconds=lag,
            connection_status=snap.status,
            channel=snap.channel,
            candles_ok=candles_ok,
            candles_counts=counts,
            last_candle_age_seconds=candle_age,
        )

    if not candles_ok:
        return DataReadiness(
            ok_for_paid=False,
            analysis_kind="unavailable",
            allow_execution=False,
            reason=(
                "Velas fechadas insuficientes para análise "
                f"(M15={counts.get('M15', 0)}/{_MIN_M15}, "
                f"H1={counts.get('H1', 0)}/{_MIN_H1}, "
                f"H4={counts.get('H4', 0)}/{_MIN_H4})."
            ),
            quote_valid=quote_valid,
            quote_ts=quote_ts,
            lag_seconds=lag,
            connection_status=snap.status,
            channel=snap.channel,
            candles_ok=False,
            candles_counts=counts,
            last_candle_age_seconds=candle_age,
        )

    fresh = bool(
        quote_valid
        and snap.claims_realtime
        and lag is not None
        and lag <= settings.quote_fresh_seconds
    )
    if fresh:
        return DataReadiness(
            ok_for_paid=True,
            analysis_kind="fresh",
            allow_execution=False,  # execução continua bloqueada pelo teste/risco
            reason="Cotação recente verificada — análise permitida (execução sob validação de risco).",
            quote_valid=True,
            quote_ts=quote_ts,
            lag_seconds=lag,
            connection_status=snap.status,
            channel=snap.channel,
            candles_ok=True,
            candles_counts=counts,
            last_candle_age_seconds=candle_age,
        )

    # Histórico válido mas atrasado (ex.: bi5) — análise histórica sem execução.
    age_txt = f"{effective_age:.0f}s" if effective_age is not None else "desconhecido"
    return DataReadiness(
        ok_for_paid=True,
        analysis_kind="historical",
        allow_execution=False,
        reason=(
            f"Dados históricos/atrasados válidos (atraso≈{age_txt}, canal={snap.channel}, "
            f"estado={snap.status_label}). Análise histórica permitida — sem execução de operações."
        ),
        quote_valid=quote_valid,
        quote_ts=quote_ts,
        lag_seconds=lag,
        connection_status=snap.status,
        channel=snap.channel,
        candles_ok=True,
        candles_counts=counts,
        last_candle_age_seconds=candle_age,
    )


def hold_for_readiness(ready: DataReadiness):
    """Proposta AGUARDAR local — zero custo OpenAI."""
    from app.robot.ai_schema import AITradeProposal

    return AITradeProposal(
        decision="HOLD",
        justification=ready.reason,
        favoring_scenario="Cotação válida e suficientemente recente com velas fechadas.",
        invalidating_scenario="Feed desligado, dados obsoletos ou velas em falta.",
        entry_price=None,
        stop_loss=None,
        take_profit=None,
        time_horizon="—",
        risks="Chamar a API paga sem dados utilizáveis desperdiça orçamento.",
        missing_information=ready.reason,
        confidence=0.95,
    )
