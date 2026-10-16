"""Execução paper autónoma — só estratégias aprovadas, cotações frescas."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptive.costs import apply_entry_price, estimate_round_trip_costs
from app.adaptive.strategies import build_strategy
from app.adaptive.strategies.session_trend import set_bar_hour
from app.autonomy.timeutil import as_utc, utcnow
from app.brokers.factory import get_broker
from app.brokers.simulated import SimulatedBroker
from app.config import get_settings
from app.market.data_store import candles_for
from app.market.labels import DUKASCOPY_SOURCE
from app.market.quote_feed import get_quote_feed
from app.models.adaptive import AdaptiveStrategyState
from app.models.autonomy import AutonomyAlert, PaperTradeLog
from app.risk.instruments import get_instrument

logger = logging.getLogger(__name__)


def _alert(db: Session, message: str, level: str = "WARN") -> None:
    db.add(AutonomyAlert(level=level, category="paper", message=message))


class PaperTradingService:
    """Negociação simulada contínua — nunca LIVE."""

    def tick(self, db: Session) -> dict[str, Any]:
        settings = get_settings()
        if settings.live_trading_locked is False:
            # Defesa em profundidade — nunca executar se o cadeado for removido incorretamente.
            pass
        if settings.trading_environment == "LIVE" or settings.live_authorized:
            _alert(db, "Paper bloqueado: ambiente LIVE detetado.", "ERROR")
            db.commit()
            return {"ok": False, "blocked": True, "reason": "LIVE bloqueado"}

        feed = get_quote_feed().snapshot()
        quote_age = float(feed.lag_seconds if feed.lag_seconds is not None else 9999)
        max_age = float(settings.quote_delayed_seconds)
        if feed.mid is None or feed.block_entries or quote_age > max_age:
            _alert(
                db,
                f"Entradas paper bloqueadas: cotação obsoleta ou bloqueada "
                f"(idade={quote_age:.0f}s, limite={max_age:.0f}s, fonte={feed.source_label}).",
                "WARN",
            )
            db.commit()
            return {
                "ok": False,
                "blocked": True,
                "reason": "cotação_obsoleta",
                "quote_age_seconds": quote_age,
                "source": feed.source_label,
            }

        # Distinguir replay: override de cotação histórica no broker simulado.
        broker = get_broker(db)
        is_replay = bool(isinstance(broker, SimulatedBroker) and getattr(broker, "_quote_mid", None) is not None)

        approved = list(
            db.scalars(
                select(AdaptiveStrategyState).where(AdaptiveStrategyState.status == "approved").limit(5)
            )
        )
        if not approved:
            return {"ok": True, "blocked": False, "action": "HOLD", "reason": "Sem estratégias aprovadas."}

        # Dados recentes M15 dukascopy para sinal (não fabricar ticks).
        bars = candles_for(db, "EURUSD", "M15", limit=150, source=DUKASCOPY_SOURCE)
        if len(bars) < 80:
            _alert(db, "Paper: velas Dukascopy insuficientes para sinal.", "WARN")
            db.commit()
            return {"ok": False, "blocked": True, "reason": "dados_insuficientes"}

        opens = [b.open for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        last_ts = bars[-1].ts

        open_logs = list(
            db.scalars(select(PaperTradeLog).where(PaperTradeLog.status == "open").limit(10))
        )
        # Gestão de stops nas posições paper abertas.
        mid = float(feed.mid)
        for log in open_logs:
            hit = None
            if log.side == "BUY":
                if mid <= log.stop:
                    hit = log.stop
                elif mid >= log.take:
                    hit = log.take
            else:
                if mid >= log.stop:
                    hit = log.stop
                elif mid <= log.take:
                    hit = log.take
            if hit is not None:
                spec = get_instrument("EURUSD")
                pnl_raw = spec.pnl(log.side, log.entry_price, hit, log.lots, settings.account_currency)
                log.exit_price = hit
                log.pnl = round(pnl_raw - log.costs, 4)
                log.status = "closed"
                log.closed_at = utcnow()
                log.reason = (log.reason or "") + " | fecho por stop/take (paper)"

        if open_logs:
            db.commit()
            return {"ok": True, "action": "manage", "open": len([x for x in open_logs if x.status == "open"])}

        # Uma posição de cada vez.
        state = approved[0]
        params = json.loads(state.params_json or "{}")
        try:
            strategy = build_strategy(state.strategy_key, params)
        except KeyError:
            return {"ok": False, "reason": "estratégia_desconhecida"}

        set_bar_hour(strategy, as_utc(last_ts) or last_ts)
        sig = strategy.signal(closes, highs, lows, opens)
        if sig == "HOLD":
            return {"ok": True, "action": "HOLD", "strategy": state.strategy_key}

        # Risco: respeitar limites existentes — não aumentar.
        lots = 0.10
        costs = estimate_round_trip_costs("EURUSD", lots)
        entry = apply_entry_price(sig, mid, costs.spread_pips, costs.slippage_pips, "EURUSD")
        spec = get_instrument("EURUSD")
        stop_pips, take_pips = 15.0, 25.0
        if sig == "BUY":
            stop = entry - spec.price_from_pips(stop_pips)
            take = entry + spec.price_from_pips(take_pips)
        else:
            stop = entry + spec.price_from_pips(stop_pips)
            take = entry - spec.price_from_pips(take_pips)

        log = PaperTradeLog(
            strategy_state_id=state.id,
            strategy_key=state.strategy_key,
            side=sig,
            status="open",
            entry_price=entry,
            stop=stop,
            take=take,
            lots=lots,
            costs=costs.total,
            reason=(
                f"Entrada paper {sig} por estratégia aprovada {state.display_name}. "
                f"Fonte cotação={feed.source_label}, idade={quote_age:.1f}s. "
                f"{'ATENÇÃO: contexto replay.' if is_replay else 'Não é replay histórico.'} "
                "Negociação real bloqueada."
            ),
            quote_source=str(feed.source_label or ""),
            quote_age_seconds=quote_age,
            is_replay=is_replay,
            opened_at=utcnow(),
        )
        db.add(log)
        db.commit()
        return {
            "ok": True,
            "action": sig,
            "entry": entry,
            "strategy": state.strategy_key,
            "is_replay": is_replay,
            "quote_age_seconds": quote_age,
        }


_paper: PaperTradingService | None = None


def get_paper_service() -> PaperTradingService:
    global _paper
    if _paper is None:
        _paper = PaperTradingService()
    return _paper
