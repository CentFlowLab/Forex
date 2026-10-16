"""Deterministic risk engine. AI consensus cannot bypass these checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models.account import Account, AppSetting
from app.models.trading import Order, Position
from app.risk.instruments import InstrumentSpec, get_instrument


@dataclass
class ProposedTrade:
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float | None
    spread_pips: float
    expected_slippage_pips: float
    market_ts: datetime
    broker_connected: bool
    data_is_stale: bool
    lots_hint: float | None = None


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    lots: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    risk_reward: float | None = None


class RiskEngine:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()

    def _account(self) -> Account | None:
        return self.db.scalar(select(Account).limit(1))

    def evaluate(self, proposal: ProposedTrade) -> RiskDecision:
        reasons: list[str] = []
        settings = self.settings

        flag = self.db.get(AppSetting, "kill_switch")
        kill = settings.kill_switch or (flag is not None and flag.value.lower() == "true")
        if kill:
            reasons.append("A paragem de emergência está ativa — novas ordens desativadas.")
        if settings.trading_environment == "LIVE" and not settings.live_authorized:
            reasons.append("A negociação real não está autorizada.")
        if not settings.live_authorized and settings.trading_environment == "LIVE":
            reasons.append("Ambiente LIVE bloqueado pelos controlos de segurança predefinidos.")

        if proposal.side not in {"BUY", "SELL"}:
            reasons.append("O lado tem de ser BUY ou SELL.")
        if proposal.side == "HOLD":
            reasons.append("HOLD não é um lado executável.")

        if not proposal.broker_connected:
            reasons.append("A sessão da corretora está desligada.")
        if proposal.data_is_stale:
            reasons.append("Os dados de mercado estão desatualizados.")

        try:
            spec = get_instrument(proposal.symbol)
        except ValueError as exc:
            return RiskDecision(False, [str(exc)])

        if proposal.stop_loss <= 0:
            reasons.append("Falta o stop-loss (limite de perda) obrigatório.")
        else:
            if proposal.side == "BUY" and proposal.stop_loss >= proposal.entry_price:
                reasons.append("O stop-loss de BUY tem de ficar abaixo da entrada.")
            if proposal.side == "SELL" and proposal.stop_loss <= proposal.entry_price:
                reasons.append("O stop-loss de SELL tem de ficar acima da entrada.")

        if proposal.spread_pips > settings.max_spread_pips:
            reasons.append(
                f"Spread {proposal.spread_pips:.2f} excede o máximo de {settings.max_spread_pips:.2f} pips."
            )
        if proposal.expected_slippage_pips > settings.max_slippage_pips:
            reasons.append(
                f"Slippage esperado {proposal.expected_slippage_pips:.2f} excede o máximo de {settings.max_slippage_pips:.2f}."
            )

        open_count = self.db.scalar(select(func.count(Position.id))) or 0
        if open_count >= settings.max_concurrent_positions:
            reasons.append(
                f"Limite de posições concorrentes atingido ({settings.max_concurrent_positions})."
            )

        account = self._account()
        equity = account.equity if account else settings.starting_equity
        currency = account.currency if account else settings.account_currency
        daily_pnl = account.daily_realized_pnl if account else 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        if account and account.daily_pnl_date != today:
            daily_pnl = 0.0

        if equity <= 0:
            reasons.append("O capital da conta não é positivo.")

        daily_loss_limit = equity * (settings.max_daily_loss_pct / 100.0)
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            reasons.append(
                f"Limite de prejuízo diário atingido ({settings.max_daily_loss_pct}% / {daily_loss_limit:.2f})."
            )

        sl_pips = 0.0
        if proposal.stop_loss > 0:
            sl_pips = abs(spec.pips_from_price_delta(proposal.entry_price - proposal.stop_loss))
        if sl_pips <= 0:
            reasons.append("A distância do stop-loss é zero ou inválida.")

        risk_budget = equity * (settings.max_risk_per_trade_pct / 100.0)
        pip_value_per_lot = spec.pip_value_account(1.0, currency)
        lots = 0.0
        if sl_pips > 0 and pip_value_per_lot > 0:
            raw = risk_budget / (sl_pips * pip_value_per_lot)
            # Always round DOWN so lot-step rounding cannot exceed the risk cap.
            steps = int(raw / spec.lot_step)
            lots = round(max(0.0, steps * spec.lot_step), 2)
        if lots < spec.min_lot:
            reasons.append("A dimensão da posição calculada fica abaixo do lote mínimo após o limite de risco.")

        rr: float | None = None
        if proposal.take_profit and proposal.stop_loss:
            reward = abs(proposal.take_profit - proposal.entry_price)
            risk = abs(proposal.entry_price - proposal.stop_loss)
            if risk > 0:
                rr = reward / risk

        # No martingale / no size increase after losses.
        last_loss = self.db.scalar(
            select(Order)
            .where(Order.status == "FILLED")
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        _ = last_loss  # size is always recomputed from equity * fixed %, never increased after loss

        if reasons:
            return RiskDecision(False, reasons, 0.0, 0.0, 0.0, rr)

        risk_amount = sl_pips * spec.pip_value_account(lots, currency)
        risk_pct = (risk_amount / equity) * 100.0 if equity else 0.0
        if risk_pct > settings.max_risk_per_trade_pct + 1e-6:
            return RiskDecision(
                False,
                ["O risco por operação excede o máximo configurado."],
                0.0,
                risk_amount,
                risk_pct,
                rr,
            )

        return RiskDecision(True, ["Passou as verificações determinísticas de risco."], lots, risk_amount, risk_pct, rr)


def lots_for_risk(spec: InstrumentSpec, equity: float, risk_pct: float, stop_pips: float, currency: str = "USD") -> float:
    budget = equity * (risk_pct / 100.0)
    pip_val = spec.pip_value_account(1.0, currency)
    if stop_pips <= 0 or pip_val <= 0:
        return 0.0
    return spec.normalize_lots(budget / (stop_pips * pip_val))
