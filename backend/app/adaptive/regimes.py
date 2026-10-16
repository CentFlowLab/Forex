"""Classificador determinístico de regimes de mercado (sem look-ahead)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.market.indicators import atr, sma

RegimeLabel = Literal[
    "tendencia_alta",
    "tendencia_baixa",
    "mercado_lateral",
    "volatilidade_elevada",
    "volatilidade_reduzida",
    "desconhecido",
]

REGIME_LABELS_PT: dict[str, str] = {
    "tendencia_alta": "Tendência de alta",
    "tendencia_baixa": "Tendência de baixa",
    "mercado_lateral": "Mercado lateral",
    "volatilidade_elevada": "Volatilidade elevada",
    "volatilidade_reduzida": "Volatilidade reduzida",
    "desconhecido": "Regime desconhecido",
}


@dataclass(frozen=True)
class RegimeSnapshot:
    label: RegimeLabel
    label_pt: str
    trend_slope: float | None
    atr_ratio: float | None
    adx_proxy: float | None
    bars_used: int

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "label_pt": self.label_pt,
            "trend_slope": self.trend_slope,
            "atr_ratio": self.atr_ratio,
            "adx_proxy": self.adx_proxy,
            "bars_used": self.bars_used,
        }


def classify_regime(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    *,
    lookback: int = 50,
) -> RegimeSnapshot:
    """Classifica o regime usando apenas os dados fornecidos (já cortados até ao momento)."""
    n = len(closes)
    if n < max(30, lookback // 2) or len(highs) != n or len(lows) != n:
        return RegimeSnapshot("desconhecido", REGIME_LABELS_PT["desconhecido"], None, None, None, n)

    window = min(lookback, n)
    c = closes[-window:]
    h = highs[-window:]
    l = lows[-window:]

    sma_fast = sma(c, min(20, window))
    sma_slow = sma(c, min(50, window)) if window >= 50 else sma(c, min(30, window))
    atr_now = atr(h, l, c, period=min(14, window - 1))
    atr_ref = atr(h[: max(15, window // 2)], l[: max(15, window // 2)], c[: max(15, window // 2)], period=min(14, window // 2 - 1))

    if sma_fast is None or sma_slow is None or atr_now is None or c[-1] <= 0:
        return RegimeSnapshot("desconhecido", REGIME_LABELS_PT["desconhecido"], None, None, None, n)

    slope = (sma_fast - sma_slow) / c[-1]
    atr_ratio = (atr_now / atr_ref) if atr_ref and atr_ref > 0 else None

    # Proxy simples de força direcional: |slope| normalizado.
    adx_proxy = abs(slope) * 1000.0

    # Prioridade: volatilidade extrema → tendência → lateral → volatilidade baixa.
    if atr_ratio is not None and atr_ratio >= 1.6:
        label: RegimeLabel = "volatilidade_elevada"
    elif atr_ratio is not None and atr_ratio <= 0.65 and adx_proxy < 1.2:
        label = "volatilidade_reduzida"
    elif slope > 0.0008 and adx_proxy >= 1.0:
        label = "tendencia_alta"
    elif slope < -0.0008 and adx_proxy >= 1.0:
        label = "tendencia_baixa"
    elif adx_proxy < 1.0:
        label = "mercado_lateral"
    else:
        label = "desconhecido"

    return RegimeSnapshot(
        label=label,
        label_pt=REGIME_LABELS_PT[label],
        trend_slope=round(slope, 8),
        atr_ratio=round(atr_ratio, 4) if atr_ratio is not None else None,
        adx_proxy=round(adx_proxy, 4),
        bars_used=window,
    )
