"""Seguimento de tendência com filtro de sessão Forex (UTC)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.adaptive.strategies.base import Signal, Strategy, StrategySpec
from app.market.indicators import sma


def _hour_utc_from_len(n: int) -> int | None:
    # Sem timestamp nas listas — o filtro de sessão é aplicado no paper/ciclo
    # via parâmetro session_start/session_end em horas UTC sobre o sinal "ativo".
    # Aqui só produzimos sinal de tendência; o filtro horário é um gate opcional
    # quando `allow_all_hours` = 0 e o caller passa `current_hour_utc`.
    return None


class SessionTrendStrategy(Strategy):
    spec = StrategySpec(
        key="session_trend",
        display_name="Tendência com filtro de sessão",
        description=(
            "Médias móveis com confirmação de inclinação; parâmetros de sessão UTC "
            "restringem entradas (Londres/NY típicas)."
        ),
        default_params={
            "fast": 20,
            "slow": 60,
            "session_start_utc": 7,
            "session_end_utc": 17,
            "allow_all_hours": 0,
        },
        param_bounds={
            "fast": (10, 30, 5),
            "slow": (40, 100, 20),
            "session_start_utc": (0, 12, 1),
            "session_end_utc": (12, 23, 1),
            "allow_all_hours": (0, 1, 1),
        },
        preferred_regimes=("tendencia_alta", "tendencia_baixa"),
        complexity=1.3,
    )

    # Preenchido pelo simulador/paper quando disponível (anti look-ahead: hora da barra atual).
    current_hour_utc: int | None = None

    def signal(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        opens: list[float],
    ) -> Signal:
        fast = int(self.params["fast"])
        slow = int(self.params["slow"])
        if fast >= slow or len(closes) < slow + 2:
            return "HOLD"
        allow_all = int(self.params.get("allow_all_hours") or 0) == 1
        if not allow_all and self.current_hour_utc is not None:
            start_h = int(self.params["session_start_utc"])
            end_h = int(self.params["session_end_utc"])
            h = self.current_hour_utc
            if start_h <= end_h:
                if not (start_h <= h < end_h):
                    return "HOLD"
            else:
                # sessão que atravessa meia-noite
                if not (h >= start_h or h < end_h):
                    return "HOLD"

        f_now = sma(closes, fast)
        s_now = sma(closes, slow)
        f_prev = sma(closes[:-1], fast)
        s_prev = sma(closes[:-1], slow)
        if None in (f_now, s_now, f_prev, s_prev):
            return "HOLD"
        # Confirmação: cruzamento + fecho acima/abaixo da lenta.
        if f_prev <= s_prev and f_now > s_now and closes[-1] > s_now:
            return "BUY"
        if f_prev >= s_prev and f_now < s_now and closes[-1] < s_now:
            return "SELL"
        return "HOLD"


def set_bar_hour(strategy: Strategy, ts: datetime | None) -> None:
    if isinstance(strategy, SessionTrendStrategy) and ts is not None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        strategy.current_hour_utc = ts.astimezone(timezone.utc).hour
