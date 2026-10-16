"""Qualidade da decisão multimeda OpenAI vs candidatos do radar.

Não força BUY/SELL. Só exige explicação verificável face aos dados enviados.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Frases genéricas típicas (#573/#989) sem referência a dados concretos.
_GENERIC_HOLD_MARKERS = (
    "mercado lateral",
    "características semelhantes",
    "assimetria clara",
    "não há uma assimetria",
    "nenhum par apresenta uma vantagem",
    "vantagem significativa",
    "sem sinais claros",
    "movimentos inesperados",
)

_SYMBOL_RE = re.compile(
    r"\b(?:EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY)\s*/?\s*(?:USD|JPY|CHF|CAD|GBP|EUR|AUD|NZD)\b",
    re.IGNORECASE,
)
_DIGIT_RE = re.compile(r"\d")


@dataclass
class QualityResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    ai_decision_inconsistent: bool = False
    ev_inconsistency: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        status = "ok" if self.ok else "insufficient"
        return {
            "ok": self.ok,
            "status": status,
            "decision_quality": status,
            "issues": list(self.issues),
            "ai_decision_inconsistent": bool(self.ai_decision_inconsistent),
            "ev_inconsistency": dict(self.ev_inconsistency or {}),
        }


def is_decision_quality_insufficient(quality: dict[str, Any] | None) -> bool:
    """True se a análise paga não cumpriu o contrato de comparação/justificação."""
    if not isinstance(quality, dict) or not quality:
        return False
    if quality.get("status") == "insufficient":
        return True
    if quality.get("decision_quality") == "insufficient":
        return True
    if quality.get("ok") is False:
        return True
    return False


def _norm_sym(raw: str | None) -> str:
    return (raw or "").replace("/", "").replace(" ", "").upper()


def _top_symbols_from_radar(radar: dict[str, Any] | None) -> list[str]:
    if not radar:
        return []
    hint = radar.get("local_priority_hint") or []
    out: list[str] = []
    for item in hint[:3]:
        if isinstance(item, dict) and item.get("symbol"):
            out.append(_norm_sym(str(item["symbol"])))
        elif isinstance(item, str):
            out.append(_norm_sym(item))
    if out:
        return out
    pairs = radar.get("pairs_for_comparison") or []
    ranked = sorted(
        (p for p in pairs if isinstance(p, dict) and p.get("symbol")),
        key=lambda p: float(p.get("score") or 0),
        reverse=True,
    )
    return [_norm_sym(str(p["symbol"])) for p in ranked[:3]]


def _comparison_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("top_candidates_comparison") or []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _row_complete(row: dict[str, Any]) -> bool:
    needed = (
        "arguments_for",
        "arguments_against",
        "trading_costs",
        "missing_for_entry",
        "verdict_reason",
    )
    if not _norm_sym(str(row.get("symbol") or "")):
        return False
    for key in needed:
        if not str(row.get(key) or "").strip():
            return False
    return True


def _text_mentions_symbols(text: str, symbols: list[str]) -> list[str]:
    blob = (text or "").upper().replace("/", "")
    found: list[str] = []
    for sym in symbols:
        if sym and sym in blob:
            found.append(sym)
    # Também capturar menções com barra (USD/CHF).
    for m in _SYMBOL_RE.findall(text or ""):
        n = _norm_sym(m)
        if n and n not in found:
            found.append(n)
    return found


def _looks_generic_hold(text: str) -> bool:
    low = (text or "").lower()
    if not low.strip():
        return True
    hits = sum(1 for m in _GENERIC_HOLD_MARKERS if m in low)
    has_digit = bool(_DIGIT_RE.search(low))
    mentioned = _text_mentions_symbols(text, [])
    # Genérico se tem marcadores e não cita símbolos nem números.
    if hits >= 1 and not has_digit and len(mentioned) < 1:
        return True
    if hits >= 2 and len(mentioned) < 2:
        return True
    return False


def _dir_ops_from_radar(radar: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not radar:
        return []
    ops = radar.get("directional_opportunities") or []
    return [o for o in ops if isinstance(o, dict)]


def _net_from_opp(opp: dict[str, Any]) -> float | None:
    for key in (
        "expected_net_usd_after_costs",
        "expected_net_usd_0.1lot",
        "expected_net_usd",
    ):
        if opp.get(key) is not None:
            try:
                return float(opp[key])
            except (TypeError, ValueError):
                continue
    return None


def _cost_from_opp(opp: dict[str, Any]) -> float | None:
    for key in ("cost_usd", "trading_costs_usd"):
        if opp.get(key) is not None:
            try:
                return float(opp[key])
            except (TypeError, ValueError):
                continue
    return None


def detect_ev_double_count(
    payload: dict[str, Any],
    *,
    radar: dict[str, Any] | None = None,
    tol: float = 0.02,
) -> dict[str, Any]:
    """Deteta se a OpenAI re-subtraiu custos a um EV já líquido (#1118).

    Padrão típico: afirma 'EV líquido = X' onde X ≈ expected_net − cost_usd,
    enquanto o motor enviou expected_net_usd_after_costs já líquido.
    Não altera a decisão — só assinala inconsistência.
    """
    ops = _dir_ops_from_radar(radar)
    by_sym: dict[str, dict[str, Any]] = {}
    for o in ops:
        sym = _norm_sym(str(o.get("symbol") or ""))
        if sym:
            by_sym[sym] = o

    cited_net = payload.get("cited_expected_net_usd")
    try:
        cited_f = float(cited_net) if cited_net is not None else None
    except (TypeError, ValueError):
        cited_f = None

    texts: list[str] = [
        str(payload.get("justification") or ""),
        str(payload.get("rejected_notes") or ""),
        str(payload.get("why_preferred") or ""),
    ]
    for row in _comparison_rows(payload):
        texts.append(str(row.get("verdict_reason") or ""))
        texts.append(str(row.get("arguments_against") or ""))
    blob = " ".join(texts)

    # Capturar "EV líquido de 0.9388" / "EV líquido 0.9388"
    claimed: list[tuple[str, float]] = []
    for m in re.finditer(
        r"(?:EV\s*l[ií]quido|expected_net)[^\d\-]{0,24}(-?\d+(?:[.,]\d+)?)",
        blob,
        flags=re.IGNORECASE,
    ):
        raw = m.group(1).replace(",", ".")
        try:
            claimed.append((m.group(0), float(raw)))
        except ValueError:
            continue

    # Para cada claim, associar ao símbolo cujo (net−cost) está mais próximo.
    matches: list[dict[str, Any]] = []
    used_syms: set[str] = set()
    for claim_txt, claim_v in claimed:
        best: dict[str, Any] | None = None
        best_err = None
        for sym, opp in by_sym.items():
            net = _net_from_opp(opp)
            cost = _cost_from_opp(opp)
            if net is None or cost is None:
                continue
            double = net - cost
            err = abs(claim_v - double)
            if err <= tol and abs(claim_v - net) > tol:
                if best_err is None or err < best_err:
                    best_err = err
                    best = {
                        "symbol": sym,
                        "side": opp.get("side"),
                        "motor_expected_net_after_costs": net,
                        "motor_cost_usd": cost,
                        "openai_claimed_ev": claim_v,
                        "openai_claim_snippet": claim_txt[:80],
                        "equals_net_minus_cost": round(double, 4),
                        "diagnosis": (
                            "OpenAI tratou expected_net (já após custos) como bruto "
                            "e subtraiu cost_usd outra vez."
                        ),
                    }
        if best and best["symbol"] not in used_syms:
            used_syms.add(str(best["symbol"]))
            # cited alinhado com motor mas claim = net−cost
            net_b = float(best["motor_expected_net_after_costs"])
            if cited_f is not None and abs(cited_f - net_b) <= tol:
                best["cited_expected_net_usd"] = cited_f
                best["diagnosis"] = (
                    "cited_expected_net_usd está alinhado com o motor, "
                    "mas a justificação usa net−cost (dupla contagem)."
                )
            matches.append(best)

    inconsistent = bool(matches)
    return {
        "ai_decision_inconsistent": inconsistent,
        "inconsistency_type": "ev_costs_double_counted" if inconsistent else None,
        "matches": matches,
        "real_block_reason": (
            "Justificação OpenAI matematicamente contraditória (custos descontados "
            "duas vezes). A decisão HOLD original NÃO é convertida em BUY/SELL; "
            "bloqueio por inconsistência de análise, não por EV≤0 do motor."
            if inconsistent
            else None
        ),
    }


def assess_radar_decision_quality(
    payload: dict[str, Any],
    *,
    radar: dict[str, Any] | None = None,
) -> QualityResult:
    """Valida se a resposta explica aceitação/rejeição dos top candidatos."""
    issues: list[str] = []
    decision = str(payload.get("decision") or "HOLD").upper()
    justification = str(payload.get("justification") or "").strip()
    entry_conditions = str(payload.get("entry_conditions") or "").strip()
    rows = _comparison_rows(payload)
    top = _top_symbols_from_radar(radar)
    ev_check = detect_ev_double_count(payload, radar=radar)

    if len(rows) < 2:
        issues.append(
            "top_candidates_comparison deve ter pelo menos 2 candidatos "
            "(idealmente os 5 do local_priority_hint)."
        )
    incomplete = [str(r.get("symbol") or "?") for r in rows if not _row_complete(r)]
    if incomplete:
        issues.append(
            "Comparação incompleta (faltam a favor/contra/custos/condições/motivo) em: "
            + ", ".join(incomplete[:5])
        )

    row_syms = {_norm_sym(str(r.get("symbol"))) for r in rows}
    if top:
        missing_top = [s for s in top if s not in row_syms]
        if len(missing_top) >= 3:
            issues.append(
                "Os top candidatos do radar não foram comparados: "
                + ", ".join(missing_top)
            )

    if decision == "HOLD":
        if _looks_generic_hold(justification):
            issues.append(
                "HOLD com justificação genérica — cite símbolos e dados "
                "(ATR, spread, slope, sma, score) dos candidatos rejeitados."
            )
        mentioned = _text_mentions_symbols(
            justification + " " + str(payload.get("rejected_notes") or ""),
            top or list(row_syms),
        )
        if len(mentioned) < 2 and len(rows) < 3:
            issues.append(
                "HOLD deve nomear pelo menos 2 candidatos e explicar a rejeição de cada um."
            )
        if not entry_conditions and not str(payload.get("missing_information") or "").strip():
            issues.append(
                "HOLD deve indicar entry_conditions ou missing_information "
                "(o que faltaria para entrar)."
            )
        accepts = [
            r for r in rows if str(r.get("verdict") or "").lower() == "accept"
        ]
        if accepts:
            issues.append("HOLD não pode marcar candidatos com verdict=accept.")
    elif decision in {"BUY", "SELL"}:
        sym = _norm_sym(str(payload.get("symbol") or ""))
        if not sym:
            issues.append("BUY/SELL exige symbol.")
        if payload.get("entry_price") is None or payload.get("stop_loss") is None:
            issues.append("BUY/SELL exige entry_price e stop_loss.")
        if not str(payload.get("why_preferred") or "").strip() and not any(
            _norm_sym(str(r.get("symbol"))) == sym
            and str(r.get("verdict") or "").lower() == "accept"
            for r in rows
        ):
            issues.append(
                "BUY/SELL deve ter why_preferred ou um candidato accept na comparação."
            )
        if not entry_conditions:
            issues.append("BUY/SELL deve indicar entry_conditions (nível/gatilho).")

    if ev_check.get("ai_decision_inconsistent"):
        issues.append(
            "ai_decision_inconsistent: "
            + (ev_check.get("real_block_reason") or "EV/custos contraditórios")
        )
        for m in (ev_check.get("matches") or [])[:3]:
            issues.append(
                f"dupla_contagem {m.get('symbol')}: motor_net={m.get('motor_expected_net_after_costs')} "
                f"cost={m.get('motor_cost_usd')} claimed={m.get('openai_claimed_ev')} "
                f"(=net−cost {m.get('equals_net_minus_cost')})"
            )

    return QualityResult(
        ok=not issues,
        issues=issues,
        ai_decision_inconsistent=bool(ev_check.get("ai_decision_inconsistent")),
        ev_inconsistency=ev_check,
    )


def discordance_summary(
    *,
    radar_best: dict[str, Any] | None,
    openai_decision: str,
    openai_symbol: str | None,
    justification: str,
    entry_conditions: str,
    quality_issues: list[str] | None = None,
) -> dict[str, Any]:
    """Texto para o painel: radar vs OpenAI."""
    best_sym = _norm_sym(str((radar_best or {}).get("symbol") or ""))
    best_score = (radar_best or {}).get("score")
    oa = (openai_decision or "HOLD").upper()
    oa_sym = _norm_sym(openai_symbol)
    if not best_sym:
        reason = "Sem candidato de radar persistido para esta decisão."
    elif oa == "HOLD":
        reason = (
            f"Radar priorizou {best_sym}"
            + (f" (score {best_score})" if best_score is not None else "")
            + " como atenção local; OpenAI devolveu AGUARDAR. "
            "O ranking do radar não é ordem — a OpenAI deve explicar a rejeição com dados."
        )
        if _looks_generic_hold(justification):
            reason += " Justificação atual é genérica face aos candidatos enviados."
    elif oa_sym and best_sym and oa_sym != best_sym:
        reason = (
            f"Discordância de par: radar destacou {best_sym}"
            + (f" (score {best_score})" if best_score is not None else "")
            + f"; OpenAI escolheu {oa} {oa_sym}."
        )
    elif oa_sym and best_sym and oa_sym == best_sym:
        reason = (
            f"Alinhado no par {best_sym}: radar priorizou atenção; "
            f"OpenAI confirmou {oa}."
        )
    else:
        reason = "Decisão OpenAI sem símbolo face ao ranking do radar."

    return {
        "radar_best_candidate": best_sym or None,
        "radar_best_score": best_score,
        "openai_decision": oa,
        "openai_symbol": oa_sym or None,
        "discordance_reason": reason,
        "entry_conditions": entry_conditions
        or "Não indicado pela IA — condições de entrada em falta.",
        "quality_issues": list(quality_issues or []),
        "note": (
            "local_priority_hint é triagem de atenção/custo, não sinal de execução. "
            "Só BUY/SELL OpenAI válidos podem ir ao executor DEMO."
        ),
    }
