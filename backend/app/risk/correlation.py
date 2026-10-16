"""Heurística de correlação entre pares — bloqueia exposições obviamente alinhadas."""

from __future__ import annotations

# Pares que partilham direção USD ou EUR de forma tipicamente elevada.
# Não é correlação estatística em tempo real — é salvaguarda determinística.
_HIGH_CORR_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}),  # risk-on vs USD
    frozenset({"USDJPY", "USDCHF", "USDCAD"}),  # USD base longs correlacionados
    frozenset({"EURUSD", "USDCHF"}),  # EUR/CHF implícito
)


def correlation_conflict(proposed: str, open_symbols: list[str]) -> list[str]:
    """Devolve razões se a nova posição conflituar com abertas."""
    prop = proposed.replace("/", "").upper()
    opens = {s.replace("/", "").upper() for s in open_symbols}
    reasons: list[str] = []
    if prop in opens:
        reasons.append(f"Já existe posição aberta em {prop}.")
        return reasons
    for group in _HIGH_CORR_GROUPS:
        if prop not in group:
            continue
        overlap = opens & group
        if overlap:
            reasons.append(
                f"Exposição correlacionada: {prop} no mesmo grupo que {', '.join(sorted(overlap))}."
            )
    return reasons
