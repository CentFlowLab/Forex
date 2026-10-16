"""Cálculo auditável de EV líquido — separado de P(subida).

Fórmulas (por 0.1 lote, conta na moeda do pip_value):

  risk_pips   = |entry - SL| / pip_size
  reward_pips = |TP - entry| / pip_size
  RR          = reward_pips / risk_pips

  P_hit = P(atingir TP antes de SL)   # NÃO é P(subida)
  P_loss = 1 - P_hit                  # simplificação: timeout conta como perda
                                      # (conservador; walk-forward pode separar)

  gross_usd = pip_value_account(lots) * (P_hit * reward_pips - P_loss * risk_pips)
  cost_usd  = spread_pips * pip_value
              + 2 * slippage_pips * pip_value
              + commission_per_lot * lots
  EV_net    = gross_usd - cost_usd

Break-even (antes de custos): P_hit* > risk/(risk+reward) = 1/(1+RR)
Com RR=1.6 ⇒ P_hit* > 1/2.6 ≈ 0.3846

pip_value: preferir MT5 trade_tick_value/trade_tick_size; fallback InstrumentSpec.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.adaptive.costs import estimate_round_trip_costs
from app.config import get_settings
from app.risk.instruments import get_instrument

Side = Literal["BUY", "SELL"]

LOTS_DEFAULT = 0.10
SL_ATR_MULT = 1.0
TP_ATR_MULT = 1.6


@dataclass(frozen=True)
class PipValueAudit:
    symbol: str
    lots: float
    pip_size: float
    pip_value_account: float
    source: str  # mt5_tick | instrument_spec
    trade_tick_size: float | None = None
    trade_tick_value: float | None = None
    account_currency: str = "USD"
    mid: float | None = None
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvBreakdown:
    symbol: str
    side: Side
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    atr_price: float
    sl_atr_mult: float
    tp_atr_mult: float
    risk_pips: float
    reward_pips: float
    rr: float
    p_direction: float  # P(up) ou P(down) — NÃO usado como P_hit
    p_hit_tp_before_sl: float
    p_hit_source: str
    breakeven_p_hit: float
    pip_value: float
    pip_value_source: str
    spread_pips: float
    slippage_pips: float
    commission: float
    spread_cash: float
    slippage_cash: float
    cost_usd: float
    gross_usd: float
    expected_net_usd: float
    formula: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pip_value_from_mt5_specs(
    *,
    trade_tick_size: float,
    trade_tick_value: float,
    pip_size: float,
    lots: float,
) -> float | None:
    """USD (ou currency_profit) por 1 pip para `lots`.

    pip_value = (tick_value / tick_size) * pip_size * lots
    """
    if trade_tick_size <= 0 or trade_tick_value <= 0 or pip_size <= 0 or lots <= 0:
        return None
    per_lot = (trade_tick_value / trade_tick_size) * pip_size
    return float(per_lot * lots)


def resolve_pip_value(
    symbol: str,
    lots: float = LOTS_DEFAULT,
    *,
    mt5_specs: dict[str, Any] | None = None,
) -> PipValueAudit:
    settings = get_settings()
    acct = settings.account_currency
    try:
        spec = get_instrument(symbol)
    except ValueError:
        return PipValueAudit(
            symbol=symbol,
            lots=lots,
            pip_size=0.0001,
            pip_value_account=1.0,
            source="fallback",
            account_currency=acct,
            notes="instrumento desconhecido",
        )

    mid = None
    if mt5_specs and mt5_specs.get("ok"):
        mid = mt5_specs.get("mid")
        tsz = float(mt5_specs.get("trade_tick_size") or 0)
        tval = float(mt5_specs.get("trade_tick_value") or 0)
        # Preferir pip_size do spec canónico (0.01 JPY / 0.0001 outros);
        # se digits MT5 implicarem pip diferente, anotar.
        pv = pip_value_from_mt5_specs(
            trade_tick_size=tsz,
            trade_tick_value=tval,
            pip_size=spec.pip_size,
            lots=lots,
        )
        if pv is not None and pv > 0:
            note = ""
            # Sanity JPY: ~$0.67 por 0.1 lote a ~150 (≈6.67$/lote)
            if spec.quote == "JPY" and not (0.2 <= pv <= 2.5):
                note = f"pip_value JPY fora da faixa típica 0.2–2.5 para 0.1 lote (got {pv:.4f})"
            return PipValueAudit(
                symbol=symbol,
                lots=lots,
                pip_size=spec.pip_size,
                pip_value_account=round(pv, 6),
                source="mt5_tick",
                trade_tick_size=tsz,
                trade_tick_value=tval,
                account_currency=str(mt5_specs.get("currency_profit") or acct),
                mid=mid,
                notes=note,
            )

    pv_spec = float(spec.pip_value_account(lots, acct))
    return PipValueAudit(
        symbol=symbol,
        lots=lots,
        pip_size=spec.pip_size,
        pip_value_account=round(pv_spec, 6),
        source="instrument_spec",
        account_currency=acct,
        mid=mid,
        notes="fallback InstrumentSpec (FX quote→USD aproximado)",
    )


def breakeven_p_hit(risk_pips: float, reward_pips: float) -> float:
    denom = risk_pips + reward_pips
    if denom <= 0:
        return 1.0
    return risk_pips / denom


def levels_from_atr(
    *,
    side: Side,
    mid: float,
    atr_price: float,
    sl_mult: float = SL_ATR_MULT,
    tp_mult: float = TP_ATR_MULT,
) -> tuple[float, float, float]:
    sl_dist = sl_mult * atr_price
    tp_dist = tp_mult * atr_price
    if side == "BUY":
        entry = mid
        return entry, entry - sl_dist, entry + tp_dist
    entry = mid
    return entry, entry + sl_dist, entry - tp_dist


def compute_ev(
    *,
    symbol: str,
    side: Side,
    mid: float,
    atr_price: float,
    p_hit_tp_before_sl: float,
    p_hit_source: str,
    p_direction: float,
    spread_pips: float,
    lots: float = LOTS_DEFAULT,
    sl_mult: float = SL_ATR_MULT,
    tp_mult: float = TP_ATR_MULT,
    mt5_specs: dict[str, Any] | None = None,
    slippage_pips: float | None = None,
    commission_per_lot: float | None = None,
) -> EvBreakdown:
    """EV líquido auditável. p_hit ≠ p_direction."""
    settings = get_settings()
    spec = get_instrument(symbol)
    entry, sl, tp = levels_from_atr(side=side, mid=mid, atr_price=atr_price, sl_mult=sl_mult, tp_mult=tp_mult)
    risk_pips = abs(entry - sl) / spec.pip_size
    reward_pips = abs(tp - entry) / spec.pip_size
    rr = reward_pips / risk_pips if risk_pips > 0 else 0.0
    p_hit = max(0.0, min(1.0, float(p_hit_tp_before_sl)))
    be = breakeven_p_hit(risk_pips, reward_pips)

    pip_audit = resolve_pip_value(symbol, lots, mt5_specs=mt5_specs)
    pip_val = pip_audit.pip_value_account

    slip = slippage_pips if slippage_pips is not None else settings.sim_slippage_pips
    costs = estimate_round_trip_costs(
        symbol,
        lots,
        spread_pips=spread_pips,
        slippage_pips=slip,
        commission_per_lot=commission_per_lot,
    )
    # Recalcular custo com pip_value MT5 (estimate_round_trip usa InstrumentSpec).
    spread_cash = spread_pips * pip_val
    slippage_cash = float(slip) * 2.0 * pip_val
    commission = float(
        commission_per_lot if commission_per_lot is not None else settings.sim_commission_per_lot
    ) * lots
    cost_usd = spread_cash + slippage_cash + commission

    gross = pip_val * (p_hit * reward_pips - (1.0 - p_hit) * risk_pips)
    net = gross - cost_usd

    formula = (
        f"EV = pip_value×(P_hit×reward_pips − (1−P_hit)×risk_pips) − costs; "
        f"pip_value={pip_val:.6f} ({pip_audit.source}); "
        f"P_hit={p_hit:.4f} ({p_hit_source}) ≠ P_dir={p_direction:.4f}; "
        f"BE_P_hit={be:.4f}; costs=spread({spread_pips})+2×slip({slip})+comm"
    )
    return EvBreakdown(
        symbol=symbol,
        side=side,
        lots=lots,
        entry=round(entry, spec.digits),
        stop_loss=round(sl, spec.digits),
        take_profit=round(tp, spec.digits),
        atr_price=atr_price,
        sl_atr_mult=sl_mult,
        tp_atr_mult=tp_mult,
        risk_pips=round(risk_pips, 4),
        reward_pips=round(reward_pips, 4),
        rr=round(rr, 4),
        p_direction=round(float(p_direction), 4),
        p_hit_tp_before_sl=round(p_hit, 4),
        p_hit_source=p_hit_source,
        breakeven_p_hit=round(be, 4),
        pip_value=round(pip_val, 6),
        pip_value_source=pip_audit.source,
        spread_pips=float(spread_pips),
        slippage_pips=float(slip),
        commission=round(commission, 4),
        spread_cash=round(spread_cash, 4),
        slippage_cash=round(slippage_cash, 4),
        cost_usd=round(cost_usd, 4),
        gross_usd=round(gross, 4),
        expected_net_usd=round(net, 4),
        formula=formula,
    )


def simulate_barrier_hit(
    *,
    side: Side,
    entry: float,
    stop_loss: float,
    take_profit: float,
    future_highs: list[float],
    future_lows: list[float],
) -> Literal["tp", "sl", "timeout"]:
    """Primeiro toque TP vs SL nas barras futuras (OHLC). Sem look-ahead na entrada."""
    for h, l in zip(future_highs, future_lows):
        if side == "BUY":
            hit_sl = l <= stop_loss
            hit_tp = h >= take_profit
            if hit_sl and hit_tp:
                # Ambos na mesma barra — conservador: SL
                return "sl"
            if hit_sl:
                return "sl"
            if hit_tp:
                return "tp"
        else:
            hit_sl = h >= stop_loss
            hit_tp = l <= take_profit
            if hit_sl and hit_tp:
                return "sl"
            if hit_sl:
                return "sl"
            if hit_tp:
                return "tp"
    return "timeout"
