"""Validação determinística independente da proposta IA."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.autonomy.timeutil import as_utc, utcnow
from app.config import get_settings
from app.models.trading import Position
from app.risk.correlation import correlation_conflict
from app.risk.engine import ProposedTrade, RiskEngine
from app.risk.instruments import get_instrument
from app.robot.ai_schema import AITradeProposal


@dataclass
class AIValidationResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    lots: float = 0.0
    risk_amount: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reasons": self.reasons,
            "lots": self.lots,
            "risk_amount": self.risk_amount,
        }


def validate_ai_proposal(
    db: Session,
    proposal: AITradeProposal,
    context: dict[str, Any],
    *,
    allow_delayed: bool = False,
) -> AIValidationResult:
    """A IA propõe; este módulo decide se pode executar na conta simulada.

    Por defeito allow_delayed=False: cotações históricas atrasadas NÃO executam.
    """
    settings = get_settings()
    reasons: list[str] = []
    quote = context.get("quote") or {}
    symbol = (proposal.symbol or context.get("symbol") or "EURUSD").replace("/", "").upper()

    if settings.trading_environment == "LIVE" or settings.live_authorized:
        return AIValidationResult(False, ["Negociação real bloqueada."])

    if proposal.decision == "HOLD":
        return AIValidationResult(True, ["AGUARDAR — sem execução."], 0.0, 0.0)

    if proposal.decision not in {"BUY", "SELL"}:
        return AIValidationResult(False, ["Decisão IA inválida."])

    try:
        spec = get_instrument(symbol)
    except ValueError as exc:
        return AIValidationResult(False, [str(exc)])

    mid = quote.get("mid")
    if mid is None:
        return AIValidationResult(False, ["Sem cotação mid utilizável."])
    if quote.get("block_entries"):
        return AIValidationResult(False, ["Entradas bloqueadas: cotação obsoleta ou feed desligado."])
    if quote.get("is_synthetic"):
        return AIValidationResult(False, ["Cotação sintética — execução bloqueada."])
    if quote.get("channel") == "candle_derived":
        return AIValidationResult(False, ["Mid derivado de vela — não é preço executável."])
    # MT5 recente: permitido em simulação mesmo sem claims_realtime (relógio servidor ≠ UTC).
    mt5_recent = (
        quote.get("source") == "mt5"
        and quote.get("feed_state") == "recente"
        and not quote.get("is_synthetic")
    )
    # Regra dura: sem operações sobre histórico atrasado (Dukascopy/bi5).
    if not quote.get("claims_realtime") and not mt5_recent:
        return AIValidationResult(
            False,
            ["Cotação histórica/atrasada — execução proibida (só análise)."],
        )
    if not allow_delayed and not quote.get("claims_realtime") and not mt5_recent:
        return AIValidationResult(
            False,
            ["Cotação não é tempo real — execução bloqueada."],
        )

    open_syms = [p.symbol for p in db.scalars(select(Position).limit(20))]
    corr = correlation_conflict(symbol, open_syms)
    if corr:
        return AIValidationResult(False, corr)

    entry = proposal.entry_price
    stop = proposal.stop_loss
    take = proposal.take_profit
    if entry is None or stop is None or take is None:
        return AIValidationResult(False, ["Proposta incompleta: faltam entrada, stop ou take."])
    if entry <= 0 or stop <= 0 or take <= 0:
        return AIValidationResult(False, ["Preços da proposta inválidos (≤0)."])

    # Tolerância: 30 pips do instrumento.
    tol = 30 * spec.pip_size
    if abs(float(entry) - float(mid)) > tol:
        reasons.append("Entrada proposta demasiado afastada do mid atual.")

    if proposal.decision == "BUY":
        if not (stop < entry < take):
            reasons.append("BUY exige stop < entrada < take.")
    else:
        if not (take < entry < stop):
            reasons.append("SELL exige take < entrada < stop.")

    spread = float(quote.get("spread_pips") or settings.sim_spread_pips)
    risk = RiskEngine(db).evaluate(
        ProposedTrade(
            symbol=symbol,
            side=proposal.decision,
            entry_price=float(entry),
            stop_loss=float(stop),
            take_profit=float(take),
            spread_pips=spread,
            expected_slippage_pips=settings.sim_slippage_pips,
            market_ts=as_utc(utcnow()) or utcnow(),
            broker_connected=True,
            data_is_stale=bool(quote.get("block_entries")) or not bool(quote.get("claims_realtime")),
        )
    )
    if not risk.approved:
        reasons.extend(risk.reasons)
        return AIValidationResult(False, reasons, 0.0, risk.risk_amount)

    if reasons:
        return AIValidationResult(False, reasons, 0.0, risk.risk_amount)

    return AIValidationResult(True, ["Validação de risco OK."], risk.lots, risk.risk_amount)
