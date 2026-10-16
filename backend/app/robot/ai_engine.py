"""Motor central de decisão IA — simulado por defeito; OpenAI só com autorização explícita."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.ai.base import AIMessage, ProviderUnavailable
from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control
from app.ai.costs import estimate_cost
from app.ai.registry import get_provider
from app.agents.structured import (
    pydantic_to_openai_json_schema,
    repair_instruction,
    validate_agent_payload,
)
from app.config import get_settings
from app.robot.ai_schema import AIRadarDecision, AITradeProposal
from app.robot.context import build_market_context, compact_context_for_ai, estimate_context_tokens
from app.robot.radar import RadarScan, scan_radar
from app.robot.radar_decision_quality import assess_radar_decision_quality
from app.robot.readiness import assess_data_readiness, hold_for_readiness
from app.risk.instruments import get_instrument

logger = logging.getLogger(__name__)

Mode = Literal["simulated", "openai"]


SYSTEM_PROMPT = """És um analista de Forex autónomo para EUR/USD.
Recebes um contexto JSON compacto com cotações, velas já fechadas, custos e conta simulada.
Regras obrigatórias:
- Não inventes preços, notícias nem velas.
- Não uses informação futura.
- Se analysis_kind for historical ou data_disclaimer indicar atraso, trata como análise histórica — podes decidir BUY/SELL/HOLD livremente, mas reconhece que não é tempo real.
- Cotações atrasadas NÃO são tempo real — se o atraso for elevado, prefere AGUARDAR e explica as limitações.
- Responde APENAS JSON válido conforme o esquema.
- Textos em português de Portugal (pt-PT).
- decision deve ser BUY (COMPRAR), SELL (VENDER) ou HOLD (AGUARDAR).
- favoring_scenario = cenário de subida / que favorece a decisão.
- invalidating_scenario = cenário de descida / que invalida a decisão.
- entry_price, stop_loss e take_profit só se a decisão for BUY ou SELL e fizer sentido; caso contrário null.
- Em missing_information e risks, menciona limitações causadas pelo atraso dos dados quando aplicável.
"""

RADAR_SYSTEM_PROMPT = """És um analista/decisor de Forex multimeda (até 28 cruzamentos).
Recebes pairs_for_comparison, directional_opportunities (motor quantitativo local),
other_pairs_brief, data_quality e temporal_disclaimer.
Regras obrigatórias:
- Não inventes preços, notícias, velas NEM probabilidades.
- As probabilidades p_up/p_down/p_flat, expected_net_usd_after_costs e calib_* vêm do motor —
  CITA-AS; não as alters nem declares vantagem estatística sem evidence_quality/calib_samples.
- expected_net_usd_after_costs JÁ inclui spread/slippage/comissão (costs_already_included_in_net=true).
  PROIBIDO subtrair cost_usd outra vez. Se rejeitares, diz que o EV líquido do motor é X
  (cita o número exacto) e justifica com riscos/evidência — não inventes net−cost.
- expected_move_pips_30m ≠ EV em dólares; não os mistures.
- Compara BUY e SELL nas directional_opportunities; escolhe a melhor ou HOLD.
- HOLD não é preferido por defeito. Avalia EV líquido, evidência, frescura e risco.
  Se nenhuma entrada tiver vantagem demonstrável, HOLD é obrigatório.
- Mesmo em HOLD: preenche most_likely_direction, cited_p_* e cited_expected_net_usd
  com os valores exactos do motor.
- Não uses informação futura.
- Origem e frescura: data_quality, quote.source, feed_state, update_age_s.
- Com cotações atrasadas, não assumes que o preço de entrada ainda é executável; valida frescura e risco antes de BUY/SELL.
- claims_realtime=false ou clock_anomaly NÃO significam automaticamente atraso de preço.
- local_priority_hint é ranking de atenção — NÃO é ordem.
- local_regime_hint é apenas uma heurística: não copies o rótulo como conclusão sem verificar os dados.
- Analisa os 5 melhores candidatos disponíveis; não inventes candidatos em falta.
- top_candidates_comparison: arguments_for/against, trading_costs, missing_for_entry,
  quantitative_probs_note, verdict accept|reject + motivo com EV/probs/ATR/spread.
- HOLD se EV líquido ≤0, evidência fraca ou riscos superiores — justifica rejeição concreta.
- BUY/SELL só com edge demonstrável nos números fornecidos; why_preferred obrigatório.
  Não forces operações para aumentar actividade.
- Responde APENAS JSON válido conforme o esquema (pt-PT).
"""


def estimate_single_analysis_usd(
    model: str | None = None,
    *,
    db: Session | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    model = model or settings.openai_default_model
    control = get_ai_control()
    token_info: dict[str, Any] = {}
    if db is not None:
        try:
            ctx = build_market_context(db, analysis_kind="historical")
            token_info = estimate_context_tokens(ctx)
            prompt_tokens = int(token_info["estimated_prompt_tokens"])
            completion = int(token_info["estimated_completion_tokens_cap"])
        except Exception:  # noqa: BLE001
            prompt_tokens, completion = 4500, min(1200, int(settings.ai_max_tokens))
    else:
        # Compacto típico pós-redução: ~3–5k prompt.
        prompt_tokens, completion = 4500, min(1200, int(settings.ai_max_tokens))
    cost = estimate_cost(model, prompt_tokens, completion)
    capped = round(min(cost, settings.ai_max_cost_per_analysis_usd), 6)
    return {
        "model": model,
        "max_calls": 1,
        "estimated_cost_usd": capped,
        "estimated_max_cost_usd": capped,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_completion_tokens_cap": completion,
        "daily_budget_usd": settings.ai_daily_budget_usd,
        "session_budget_usd": settings.ai_session_budget_usd,
        "ai_enabled": control.is_enabled(),
        "context_token_estimate": token_info or None,
        "disclaimer": (
            "Estimativa local conservadora para UMA análise (contexto compacto). "
            "Não é faturação confirmada. Chamadas automáticas pagas estão desligadas."
        ),
    }


def analyze_simulated(context: dict[str, Any]) -> AITradeProposal:
    """Resposta IA simulada — zero custo, determinística, para testar o percurso completo."""
    quote = context.get("quote") or {}
    structure = context.get("structure") or {}
    mid = quote.get("mid")
    lag = float(quote.get("lag_seconds") or 9999)
    block = bool(quote.get("block_entries"))
    claims_rt = bool(quote.get("claims_realtime"))

    if mid is None or block:
        return AITradeProposal(
            decision="HOLD",
            justification=(
                "Cotação indisponível ou bloqueada para entradas. "
                f"Estado={quote.get('status_label')}, atraso≈{lag:.0f}s."
            ),
            favoring_scenario="Cotação verificada e suficientemente recente.",
            invalidating_scenario="Feed desligado ou cotação obsoleta.",
            time_horizon="—",
            risks="Sem preço executável fiável.",
            missing_information="Cotação utilizável.",
            confidence=0.9,
        )

    if not claims_rt and lag > 1800:
        return AITradeProposal(
            decision="HOLD",
            justification=(
                f"Cotação classificada como {quote.get('data_class')} com atraso≈{lag:.0f}s "
                "(não é tempo real). Em modo cauteloso a IA simulada aguarda."
            ),
            favoring_scenario="Atraso baixo via TCP ou fonte fresca.",
            invalidating_scenario="Atraso elevado em bi5 horário.",
            entry_price=float(mid),
            time_horizon="intradia",
            risks="Executar sobre dados atrasados distorce o contexto.",
            missing_information="Feed de baixa latência.",
            confidence=0.7,
        )

    last = structure.get("last_close") or mid
    sma20 = structure.get("sma20")
    sma50 = structure.get("sma50")
    swing_hi = structure.get("swing_high_20")
    swing_lo = structure.get("swing_low_20")
    costs = (context.get("trading_costs") or {}).get("total_round_trip_usd") or 2.5

    # Heurística simples só para testes do pipeline (não é estratégia aprovada).
    if sma20 and sma50 and last > sma20 > sma50 and swing_lo and last > swing_lo:
        entry = float(mid) + 0.00006
        return AITradeProposal(
            decision="BUY",
            justification=(
                "IA simulada: estrutura M15 com fechos acima de SMA20/SMA50. "
                f"Custo round-trip ref.≈{costs} USD/0.1 lote. Modo simulado — sem OpenAI."
            ),
            favoring_scenario="Continuação da tendência de curta duração acima das médias.",
            invalidating_scenario="Rutura abaixo da SMA50 ou aumento brusco de volatilidade.",
            entry_price=round(entry, 5),
            stop_loss=round(entry - 0.0015, 5),
            take_profit=round(entry + 0.0025, 5),
            time_horizon="algumas horas (M15)",
            risks="Sinal simulado; não constitui evidência de rentabilidade.",
            missing_information=context.get("news_note") or "",
            confidence=0.55,
        )
    if sma20 and sma50 and last < sma20 < sma50 and swing_hi and last < swing_hi:
        entry = float(mid) - 0.00006
        return AITradeProposal(
            decision="SELL",
            justification=(
                "IA simulada: estrutura M15 abaixo de SMA20/SMA50. "
                f"Custo round-trip ref.≈{costs} USD/0.1 lote. Modo simulado — sem OpenAI."
            ),
            favoring_scenario="Continuação da pressão vendedora abaixo das médias.",
            invalidating_scenario="Recuperação acima da SMA50.",
            entry_price=round(entry, 5),
            stop_loss=round(entry + 0.0015, 5),
            take_profit=round(entry - 0.0025, 5),
            time_horizon="algumas horas (M15)",
            risks="Sinal simulado; não constitui evidência de rentabilidade.",
            missing_information=context.get("news_note") or "",
            confidence=0.55,
        )

    return AITradeProposal(
        decision="HOLD",
        justification=(
            "IA simulada: sem alinhamento claro de médias/estrutura. "
            f"Mid={mid}, atraso≈{lag:.0f}s, classe={quote.get('data_class')}."
        ),
        favoring_scenario="Estrutura direcional clara com cotação aceitável.",
        invalidating_scenario="Mercado lateral ou dados insuficientes.",
        entry_price=float(mid),
        time_horizon="—",
        risks="Forçar entrada sem estrutura aumenta custos relativos.",
        missing_information=context.get("news_note") or "",
        confidence=0.6,
    )


def analyze_openai(
    db: Session, context: dict[str, Any], *, auth_token: str
) -> tuple[AITradeProposal, dict[str, Any]]:
    """Uma única chamada OpenAI — requer AI_ENABLED + autorização explícita."""
    settings = get_settings()
    control = get_ai_control()
    if not control.is_enabled():
        raise AIDisabled("IA desligada — ative e autorize antes de uma chamada paga.")

    control.begin_analysis_context(auth_token, analysis_run_id=0)
    try:
        provider = get_provider("openai")
        if not provider.configured():
            raise ProviderUnavailable("OpenAI não configurada (chave em falta).")
        model = settings.openai_default_model
        schema = pydantic_to_openai_json_schema(AITradeProposal)
        compact = compact_context_for_ai(context)
        user = (
            "Analisa livremente o EUR/USD com base apenas neste contexto compacto. "
            "Devolve decisão estruturada (BUY/SELL/HOLD).\n\n"
            + json.dumps(compact, ensure_ascii=False, default=str)[:24000]
        )
        from app.ai.control import _call_reason, _call_role

        role = _call_role.set("robot_ai_trader")
        reason = _call_reason.set("decisão única do robô Forex (manual)")
        try:
            resp = provider.complete(
                [
                    AIMessage(role="system", content=SYSTEM_PROMPT),
                    AIMessage(role="user", content=user),
                ],
                model=model,
                temperature=float(settings.ai_temperature),
                max_tokens=min(1200, int(settings.ai_max_tokens)),
                response_schema=schema,
            )
        finally:
            _call_role.reset(role)
            _call_reason.reset(reason)
        raw_text = getattr(resp, "text", None) or getattr(resp, "content", None) or ""
        usage = {
            "provider": getattr(resp, "provider", "openai"),
            "model": getattr(resp, "model", model),
            "prompt_tokens": int(getattr(resp, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(resp, "completion_tokens", 0) or 0),
            "estimated_cost_usd": float(getattr(resp, "estimated_cost_usd", 0) or 0),
            "latency_ms": int(getattr(resp, "latency_ms", 0) or 0),
            "raw_status": getattr(resp, "raw_status", "ok"),
            "raw_text": raw_text,
            "persisted_path": str(getattr(resp, "persisted_path", "") or ""),
        }
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", raw_text)
            if not m:
                raise ValueError("Resposta OpenAI sem JSON válido.") from None
            raw = json.loads(m.group(0))
        parsed, err = validate_agent_payload(AITradeProposal, raw if isinstance(raw, dict) else {})
        if not parsed:
            raise ValueError(f"Validação da proposta IA falhou: {err}")
        return AITradeProposal.model_validate(parsed), usage
    finally:
        control.end_analysis_context(auth_token)


def run_ai_decision(
    db: Session,
    *,
    mode: Mode = "simulated",
    auth_token: str | None = None,
    recover_feed: bool = True,
) -> tuple[dict[str, Any], AITradeProposal, dict[str, Any]]:
    ready = assess_data_readiness(db, recover_feed=recover_feed)
    context = build_market_context(db, analysis_kind=ready.analysis_kind)
    context["readiness"] = ready.as_dict()
    usage: dict[str, Any] = {
        "mode": mode,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost_usd": 0.0,
        "skipped_paid": False,
        "readiness": ready.as_dict(),
        "token_estimate": estimate_context_tokens(context),
    }
    if mode == "openai":
        if not auth_token:
            raise AIAuthorizationError("Token de autorização em falta para chamada OpenAI.")
        if not ready.ok_for_paid:
            # Não gastar dinheiro sem dados utilizáveis.
            usage["skipped_paid"] = True
            usage["mode"] = "blocked_no_data"
            return context, hold_for_readiness(ready), usage
        proposal, usage = analyze_openai(db, context, auth_token=auth_token)
        usage["readiness"] = ready.as_dict()
        usage["skipped_paid"] = False
    else:
        if ready.analysis_kind in {"unavailable", "too_stale"}:
            proposal = hold_for_readiness(ready)
        else:
            proposal = analyze_simulated(context)
    return context, proposal, usage


def estimate_radar_analysis_usd(scan: RadarScan | None = None) -> dict[str, Any]:
    """Estimativa de UMA chamada conjunta multi-par (sem executar)."""
    settings = get_settings()
    model = settings.openai_default_model
    if scan is not None:
        compact = scan.compact_for_ai()
        payload = json.dumps(compact, ensure_ascii=False, default=str)
        prompt_tokens = max(1, len(payload) // 4) + 700
        n_pairs = len(compact.get("pairs_for_comparison") or [])
        n_brief = len(compact.get("other_pairs_brief") or [])
    else:
        # top-5 detalhado + brief dos restantes
        prompt_tokens = 4800
        n_pairs = 5
        n_brief = 0
    completion = min(1600, int(settings.ai_max_tokens))
    cost = estimate_cost(model, prompt_tokens, completion)
    capped = round(min(cost, settings.ai_max_cost_per_analysis_usd), 6)
    return {
        "model": model,
        "max_calls": 1,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_completion_tokens_cap": completion,
        "estimated_cost_usd": capped,
        "estimated_max_cost_usd": capped,
        "pairs_in_context": n_pairs,
        "other_pairs_brief": n_brief if scan is not None else 0,
        "candidates": len(scan.candidates) if scan else 0,
        "disclaimer": (
            "Estimativa local para UMA análise conjunta multi-par. "
            "Chamadas automáticas pagas desligadas."
        ),
    }


def analyze_radar_simulated(scan: RadarScan) -> AIRadarDecision:
    """Decisão simulada multi-par — compara candidatos sem OpenAI e sem estratégias fixas."""
    if not scan.candidates:
        return AIRadarDecision(
            decision="HOLD",
            symbol=None,
            justification=(
                f"Radar: {scan.monitored} pares monitorizados, {scan.with_valid_data} com dados. "
                "Nenhum candidato prioritário — AGUARDAR."
            ),
            why_preferred="Nenhum par passou a triagem local com prioridade suficiente.",
            favoring_scenario="Dados válidos e estrutura com deslocamento relativo relevante.",
            invalidating_scenario="Ausência de candidatos ou dados sintéticos/insuficientes.",
            compared_symbols=[],
            rejected_notes="; ".join(
                f"{r['symbol']}:{r['reason']}" for r in scan.rejected[:5]
            ),
            risks="Forçar entrada sem candidatos aumenta custos sem evidência.",
            missing_information="Candidatos com dados fiáveis.",
            confidence=0.8,
        )

    # Escolher o melhor score; se nenhum for executável, ainda pode AGUARDAR para execução.
    best = scan.candidates[0]
    others = [c.symbol for c in scan.candidates[1:]]
    snap = best.snapshot
    q = snap.get("quote") or {}
    s = snap.get("structure") or {}
    mid = q.get("mid")
    lag = float(q.get("lag_seconds") or 9999)
    executable = bool((snap.get("data_quality") or {}).get("executable"))

    compared = [c.symbol for c in scan.candidates]
    why = (
        f"Prioridade local: {best.symbol} (score={best.score:.2f}) vs "
        + (", ".join(f"{c.symbol}:{c.score:.2f}" for c in scan.candidates[1:]) or "sem rivais")
        + ". Triagem não é ordem — só ranking de atenção."
    )

    # Sem preço executável fresco → AGUARDAR (análise histórica ok, execução não).
    if not executable or mid is None or q.get("is_synthetic"):
        return AIRadarDecision(
            decision="HOLD",
            symbol=best.symbol,
            justification=(
                f"Melhor candidato {best.display}, mas cotação não executável "
                f"(atraso≈{lag:.0f}s, classe={q.get('data_class')}). "
                f"IA simulada compara e AGUARDA. {why}"
            ),
            why_preferred=why,
            favoring_scenario="Cotação fresca e executável no par prioritário.",
            invalidating_scenario="Atraso histórico ou mid derivado de vela.",
            compared_symbols=compared,
            rejected_notes="; ".join(f"{r['symbol']}:{r['reason']}" for r in scan.rejected[:4]),
            entry_price=float(mid) if mid else None,
            time_horizon="—",
            risks="Operar sobre histórico atrasado está bloqueado pelo risco.",
            missing_information="Feed executável (MT5 recente) por par.",
            confidence=0.7,
        )

    # Decisão livre simulada no melhor par (heurística de teste, não as 7 estratégias).
    try:
        spec = get_instrument(best.symbol)
    except ValueError:
        spec = get_instrument("EURUSD")
    last = s.get("last_close") or mid
    sma20 = s.get("sma20")
    sma50 = s.get("sma50")
    costs = (snap.get("trading_costs") or {}).get("total") or 2.5

    if sma20 and sma50 and last > sma20 > sma50:
        entry = float(mid) + spec.price_from_pips(0.6)
        return AIRadarDecision(
            decision="BUY",
            symbol=best.symbol,
            justification=(
                f"IA simulada escolhe {best.display} entre {len(compared)} candidatos. "
                f"Estrutura acima das médias; custo≈{costs} USD/0.1 lote. {why}"
            ),
            why_preferred=why + f" Alternativas: {', '.join(others) or 'nenhuma'}." ,
            favoring_scenario="Continuação da pressão compradora no par escolhido.",
            invalidating_scenario="Rutura abaixo da SMA50 ou aumento de spread.",
            entry_price=round(entry, spec.digits),
            stop_loss=round(entry - spec.price_from_pips(15), spec.digits),
            take_profit=round(entry + spec.price_from_pips(25), spec.digits),
            time_horizon="intradia",
            compared_symbols=compared,
            risks="Simulado — sem evidência de rentabilidade multi-par.",
            missing_information="",
            confidence=0.55,
        )
    if sma20 and sma50 and last < sma20 < sma50:
        entry = float(mid) - spec.price_from_pips(0.6)
        return AIRadarDecision(
            decision="SELL",
            symbol=best.symbol,
            justification=(
                f"IA simulada escolhe {best.display} entre {len(compared)} candidatos. "
                f"Estrutura abaixo das médias; custo≈{costs} USD/0.1 lote. {why}"
            ),
            why_preferred=why + f" Alternativas: {', '.join(others) or 'nenhuma'}.",
            favoring_scenario="Continuação da pressão vendedora.",
            invalidating_scenario="Recuperação acima da SMA50.",
            entry_price=round(entry, spec.digits),
            stop_loss=round(entry + spec.price_from_pips(15), spec.digits),
            take_profit=round(entry - spec.price_from_pips(25), spec.digits),
            time_horizon="intradia",
            compared_symbols=compared,
            risks="Simulado — sem evidência de rentabilidade multi-par.",
            missing_information="",
            confidence=0.55,
        )

    return AIRadarDecision(
        decision="HOLD",
        symbol=best.symbol,
        justification=(
            f"Candidato top {best.display}, mas sem assimetria clara face aos outros. "
            f"AGUARDAR. {why}"
        ),
        why_preferred=why,
        favoring_scenario="Assimetria clara num único par com cotação executável.",
        invalidating_scenario="Mercado lateral em todos os candidatos.",
        compared_symbols=compared,
        entry_price=float(mid) if mid else None,
        time_horizon="—",
        risks="Entrar sem preferência clara aumenta custos relativos.",
        missing_information="",
        confidence=0.6,
    )


def run_radar_decision(
    db: Session,
    *,
    mode: Mode = "simulated",
    auth_token: str | None = None,
    call_role: str = "robot_radar",
) -> tuple[dict[str, Any], AIRadarDecision, RadarScan, dict[str, Any]]:
    """Percurso multimeda: radar local → decisão IA (simulada por defeito)."""
    scan = scan_radar(db)
    context = {
        "radar": scan.compact_for_ai(),
        "symbol": scan.candidates[0].symbol if scan.candidates else None,
        "quote": (scan.candidates[0].snapshot.get("quote") if scan.candidates else {}),
        "analysis_kind": "radar_multi",
        "live_trading_locked": True,
    }
    usage: dict[str, Any] = {
        "mode": mode,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost_usd": 0.0,
        "skipped_paid": False,
        "token_estimate": estimate_radar_analysis_usd(scan),
        "radar": {
            "monitored": scan.monitored,
            "with_valid_data": scan.with_valid_data,
            "candidates": [c.symbol for c in scan.candidates],
        },
    }
    if mode == "openai":
        if not auth_token:
            raise AIAuthorizationError("Token de autorização em falta.")
        if not scan.all_valid and not scan.candidates:
            usage["skipped_paid"] = True
            usage["mode"] = "blocked_no_candidates"
            return context, analyze_radar_simulated(scan), scan, usage
        # Chamada paga conjunta — só com autorização; schema multi-par.
        control = get_ai_control()
        if not control.is_enabled():
            raise AIDisabled("IA desligada.")
        compact = scan.compact_for_ai()
        context["radar"] = compact
        settings = get_settings()
        control.begin_analysis_context(auth_token, analysis_run_id=0)
        try:
            provider = get_provider("openai")
            schema = pydantic_to_openai_json_schema(AIRadarDecision)
            top_hint = compact.get("local_priority_hint") or []
            dir_opps = compact.get("directional_opportunities") or []
            user = (
                "Análise multimeda Forex (universo até 28 pares). "
                "Prioriza directional_opportunities (EV líquido após custos + probs calibradas). "
                "Compara também pairs_for_comparison. "
                "PROIBIDO inventar p_up/p_down/expected_net — cita os do motor. "
                "expected_net_usd_after_costs JÁ é líquido (não subtraias cost_usd outra vez). "
                f"Top quantitativo: {json.dumps(dir_opps[:5], ensure_ascii=False, default=str)[:4500]}. "
                f"local_priority_hint (atenção): {json.dumps(top_hint, ensure_ascii=False)}. "
                "top_candidates_comparison (5): a favor/contra, custos, probs citadas, "
                "verdict accept|reject com EV/evidence. "
                "HOLD com most_likely_direction preenchida se rejeitares entradas. "
                "BUY/SELL com entry/SL/TP alinhados ao candidato aceite. "
                "Não forces trades. Devolve JSON do esquema.\n\n"
                + json.dumps(compact, ensure_ascii=False, default=str)[:22000]
            )
            from app.ai.control import _call_reason, _call_role
            from pathlib import Path

            from app.ai.response_store import update_persisted_meta

            def _parse_raw(text: str) -> dict[str, Any]:
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    m = re.search(r"\{[\s\S]*\}", text)
                    if not m:
                        raise ValueError("Resposta OpenAI sem JSON.") from None
                    obj = json.loads(m.group(0))
                if not isinstance(obj, dict):
                    raise ValueError("Resposta OpenAI não é objeto JSON.")
                return obj

            def _complete(messages: list[AIMessage], *, call_reason: str):
                role_tok = _call_role.set(call_role or "robot_radar")
                reason_tok = _call_reason.set(call_reason)
                try:
                    return provider.complete(
                        messages,
                        model=settings.openai_default_model,
                        temperature=float(settings.ai_temperature),
                        max_tokens=min(1600, int(settings.ai_max_tokens)),
                        response_schema=schema,
                    )
                finally:
                    _call_role.reset(role_tok)
                    _call_reason.reset(reason_tok)

            resp = _complete(
                [
                    AIMessage(role="system", content=RADAR_SYSTEM_PROMPT),
                    AIMessage(role="user", content=user),
                ],
                call_reason="decisão multimeda única",
            )
            raw_text = getattr(resp, "text", None) or ""
            persisted = str(getattr(resp, "persisted_path", "") or "")
            usage = {
                "provider": getattr(resp, "provider", "openai"),
                "model": getattr(resp, "model", settings.openai_default_model),
                "prompt_tokens": int(getattr(resp, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(resp, "completion_tokens", 0) or 0),
                "estimated_cost_usd": float(getattr(resp, "estimated_cost_usd", 0) or 0),
                "latency_ms": int(getattr(resp, "latency_ms", 0) or 0),
                "raw_text": raw_text,
                "persisted_path": persisted,
                "skipped_paid": False,
                "token_estimate": estimate_radar_analysis_usd(scan),
                "quality_repair_used": False,
            }
            try:
                raw = _parse_raw(raw_text)
            except ValueError:
                if persisted:
                    try:
                        update_persisted_meta(
                            Path(persisted),
                            processed=False,
                            process_error="Resposta OpenAI sem JSON.",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                raise
            parsed, err = validate_agent_payload(
                AIRadarDecision, raw if isinstance(raw, dict) else {}
            )
            if not parsed:
                if persisted:
                    try:
                        update_persisted_meta(
                            Path(persisted),
                            processed=False,
                            process_error=str(err or "validação falhou"),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                raise ValueError(f"Validação radar IA falhou: {err}")

            quality = assess_radar_decision_quality(parsed, radar=compact)
            # Por defeito: uma única chamada paga. Reparação só se
            # openai_allow_quality_repair_call=True (autorização manual explícita).
            allow_repair = bool(
                getattr(settings, "openai_allow_quality_repair_call", False)
            )
            usage["quality_repair_used"] = False
            usage["paid_calls_this_analysis"] = 1
            original_parsed = dict(parsed)
            if not quality.ok and allow_repair:
                repair_user = (
                    user
                    + repair_instruction("; ".join(quality.issues))
                    + "\nCorrige mantendo decisão honesta (podes continuar HOLD), "
                    "mas preenche top_candidates_comparison e justifica com dados."
                )
                resp2 = _complete(
                    [
                        AIMessage(role="system", content=RADAR_SYSTEM_PROMPT),
                        AIMessage(role="user", content=repair_user),
                    ],
                    call_reason="correção comparação candidatos (manual)",
                )
                usage["paid_calls_this_analysis"] = 2
                usage["quality_repair_used"] = True
                usage["prompt_tokens"] = int(usage["prompt_tokens"]) + int(
                    getattr(resp2, "prompt_tokens", 0) or 0
                )
                usage["completion_tokens"] = int(usage["completion_tokens"]) + int(
                    getattr(resp2, "completion_tokens", 0) or 0
                )
                usage["estimated_cost_usd"] = float(usage["estimated_cost_usd"]) + float(
                    getattr(resp2, "estimated_cost_usd", 0) or 0
                )
                persisted2 = str(getattr(resp2, "persisted_path", "") or "") or persisted
                usage["persisted_path"] = persisted2
                usage["raw_text"] = getattr(resp2, "text", None) or ""
                persisted = persisted2
                try:
                    raw2 = _parse_raw(usage["raw_text"])
                    parsed2, err2 = validate_agent_payload(AIRadarDecision, raw2)
                    if parsed2:
                        parsed = parsed2
                        quality = assess_radar_decision_quality(parsed2, radar=compact)
                    else:
                        logger.warning("Correção radar: validação falhou: %s", err2)
                        parsed = original_parsed
                except ValueError as exc:
                    logger.warning("Correção radar: JSON inválido: %s", exc)
                    parsed = original_parsed
            elif not quality.ok:
                # Ciclo autónomo / defeito: guardar original, marcar insufficient, sem 2.ª chamada.
                parsed = original_parsed

            # Nunca transformar HOLD↔BUY/SELL por qualidade; painel mostra issues.
            quality_dict = quality.as_dict()
            proposal = AIRadarDecision.model_validate(parsed)
            context["decision_quality"] = quality_dict
            context["local_priority_hint"] = top_hint
            usage["decision_quality"] = quality_dict
            if persisted:
                try:
                    update_persisted_meta(
                        Path(persisted),
                        processed=True,
                        process_error=(
                            ""
                            if quality.ok
                            else "qualidade_insuficiente: " + "; ".join(quality.issues)[:500]
                        ),
                        processed_decision=proposal.decision,
                        processed_symbol=proposal.symbol,
                    )
                except Exception:  # noqa: BLE001
                    pass
        finally:
            control.end_analysis_context(auth_token)
    else:
        proposal = analyze_radar_simulated(scan)

    # Anexar quote do par escolhido ao contexto para validação de risco.
    if proposal.symbol:
        for c in scan.candidates:
            if c.symbol == proposal.symbol:
                context["quote"] = c.snapshot.get("quote") or {}
                context["symbol"] = c.symbol
                context["structure"] = c.snapshot.get("structure")
                context["trading_costs"] = c.snapshot.get("trading_costs")
                break
    return context, proposal, scan, usage
