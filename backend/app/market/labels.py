"""Etiquetas canónicas da proveniência dos dados de mercado (pt-PT)."""

from __future__ import annotations

SYNTHETIC_LABEL = "SINTÉTICO — não são preços de mercado em tempo real"
REAL_HISTORICAL_LABEL = "DADOS HISTÓRICOS REAIS — Dukascopy (negociação apenas simulada)"
MT5_LABEL = "METATRADER 5 — cotações/velas do terminal (verificar timestamps vs UTC)"
IMPORTED_LABEL = "DADOS HISTÓRICOS IMPORTADOS — não são preços em tempo real"
DUKASCOPY_SOURCE = "dukascopy"
MT5_SOURCE = "mt5"


def label_for(source: str, is_synthetic: bool) -> str:
    if is_synthetic or source == "synthetic":
        return SYNTHETIC_LABEL
    if source == MT5_SOURCE:
        return MT5_LABEL
    if source == DUKASCOPY_SOURCE:
        return REAL_HISTORICAL_LABEL
    if source in {"imported", "csv"}:
        return IMPORTED_LABEL
    return f"DADOS HISTÓRICOS — {source}"
