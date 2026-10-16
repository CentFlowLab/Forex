"""Apresentação das decisões OpenAI reais a partir de registos persistidos.

Não faz chamadas pagas. Não altera o motor de decisão nem a execução.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.autonomy.timeutil import iso_utc
from app.models.robot import AIDecisionLog
from app.robot.ai_schema import DECISION_PT

NOT_INDICATED = "Não indicado pela IA"

# Modos persistidos que correspondem a chamadas OpenAI reais (não simuladas).
_OPENAI_MODE_PREFIX = "openai"


def is_openai_decision_mode(mode: str | None) -> bool:
    return (mode or "").strip().lower().startswith(_OPENAI_MODE_PREFIX)


def is_synthetic_or_test_decision(log: AIDecisionLog) -> bool:
    """Stubs de teste / regressão / auditoria — nunca no histórico OpenAI do painel."""
    ctx = _safe_json(log.context_json)
    if ctx.get("synthetic_test") or ctx.get("synthetic") or ctx.get("test_harness"):
        return True
    if ctx.get("integrated_test") or ctx.get("dry_run"):
        return True
    auth = str(ctx.get("authorization_id") or "").strip().lower()
    if auth.startswith("test-") or auth.startswith("audit-"):
        return True
    if auth in {
        "dry-run-autonomous",
        "test-auth-hold-path",
        "dry-run-integrated",
    }:
        return True
    just = (log.justification or "").strip().lower()
    if just in {"aguardar", "teste", "test", "wait"}:
        return True
    if "assimetria teste" in just or "sem edge claro (teste)" in just:
        return True
    if "teste auditoria" in just or "justificação única de teste" in just:
        return True
    if "teste" in just and not str(ctx.get("persisted_path") or "").strip():
        return True
    # Decisão openai_* sem ficheiro de resposta persistido não conta como paga real.
    path = str(ctx.get("persisted_path") or "").strip()
    if not path:
        return True
    if not Path(path).is_file():
        return True
    # Respostas dry_run / mock no store.
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("status") in {"dry_run", "mock", "test"}:
            return True
        if (data.get("raw_http") or {}).get("dry_run") or (data.get("raw_http") or {}).get(
            "simulated"
        ):
            return True
        if float(data.get("estimated_cost_usd") or 0) <= 0 and float(
            log.estimated_cost_usd or 0
        ) <= 0:
            return True
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return False


def is_real_paid_openai_decision(log: AIDecisionLog) -> bool:
    if not is_openai_decision_mode(log.mode):
        return False
    if is_synthetic_or_test_decision(log):
        return False
    if float(log.estimated_cost_usd or 0) <= 0:
        return False
    return True


def _safe_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _text_or_na(value: Any) -> str:
    if value is None:
        return NOT_INDICATED
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return NOT_INDICATED
    return text


def _symbol_or_na(prop: dict[str, Any], ctx: dict[str, Any]) -> str:
    for key in ("symbol",):
        for src in (prop, ctx):
            val = src.get(key)
            if val is not None and str(val).strip() and str(val).strip().lower() != "null":
                return str(val).strip()
    return NOT_INDICATED


def _confidence_or_na(prop: dict[str, Any], log: AIDecisionLog) -> str:
    """Só mostra confiança se o modelo tiver fornecido um valor > 0."""
    conf: Any = None
    if "confidence" in prop and prop.get("confidence") is not None:
        conf = prop.get("confidence")
    elif log.confidence is not None and float(log.confidence or 0) > 0:
        conf = log.confidence
    try:
        c = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        return NOT_INDICATED
    if c <= 0:
        return NOT_INDICATED
    if c <= 1.0:
        return f"{round(c * 100)}%"
    return f"{round(c)}%"


def _model_from_store(persisted_path: str) -> str:
    if not persisted_path:
        return ""
    path = Path(persisted_path)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("model") or "").strip()
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def _payload_from_persisted_store(persisted_path: str) -> dict[str, Any]:
    """Recupera decision/justification do JSON original quando o ORM ficou vazio."""
    if not persisted_path:
        return {}
    path = Path(persisted_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    raw_text = data.get("text") or ""
    if not raw_text:
        return {}
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    from app.agents.structured import normalize_agent_payload

    return normalize_agent_payload(raw)


def _compact_from_store(persisted_path: str) -> dict[str, Any]:
    """JSON compacto do user message (radar) no response_store.

    O user message começa com texto + 'Top quantitativo: [...]' antes do
    objecto compacto — não usar o primeiro '{' (é o array). Procurar o
    objecto com 'directional_opportunities' ou 'monitored'.
    """
    if not persisted_path:
        return {}
    path = Path(persisted_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    msgs = data.get("request_messages") or []
    for msg in msgs:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        # Preferir o bloco que contém o contexto radar completo.
        for key in (
            '"directional_opportunities"',
            '"monitored"',
            '"local_priority_hint"',
        ):
            marker = content.rfind(key)
            if marker < 0:
                continue
            start = content.rfind("{", 0, marker)
            if start < 0:
                continue
            try:
                compact = json.loads(content[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(compact, dict) and (
                compact.get("directional_opportunities") is not None
                or compact.get("monitored") is not None
                or compact.get("local_priority_hint") is not None
            ):
                return compact
    return {}


def _radar_hint_from_store(persisted_path: str) -> list[dict[str, Any]]:
    """Extrai local_priority_hint do request_messages persistido."""
    compact = _compact_from_store(persisted_path)
    hint = compact.get("local_priority_hint") or []
    if isinstance(hint, list) and hint:
        return [h for h in hint if isinstance(h, dict)]
    return []


def _directional_ops_from_store(persisted_path: str) -> list[dict[str, Any]]:
    """Snapshot exacto das directional_opportunities enviadas à OpenAI."""
    compact = _compact_from_store(persisted_path)
    ops = compact.get("directional_opportunities") or []
    if ops:
        return [o for o in ops if isinstance(o, dict)]
    # Fallback: o compacto pode estar truncado (:22000); o preâmbulo
    # "Top quantitativo: [...]" costuma estar completo.
    if not persisted_path:
        return []
    path = Path(persisted_path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    for msg in data.get("request_messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        key = "Top quantitativo:"
        i = content.find(key)
        if i < 0:
            continue
        rest = content[i + len(key) :].lstrip()
        if not rest.startswith("["):
            continue
        # Extrair array JSON balanceado.
        depth = 0
        end = None
        for j, ch in enumerate(rest):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:
            continue
        try:
            arr = json.loads(rest[:end])
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list):
            return [o for o in arr if isinstance(o, dict)]
    return []


def _radar_vs_openai_block(
    *,
    ctx: dict[str, Any],
    prop: dict[str, Any],
    store_payload: dict[str, Any],
    persisted: str,
    decision: str,
    symbol: str,
    justification: str,
) -> dict[str, Any]:
    from app.robot.radar_decision_quality import (
        assess_radar_decision_quality,
        discordance_summary,
        is_decision_quality_insufficient,
    )

    hint = ctx.get("local_priority_hint")
    if not isinstance(hint, list) or not hint:
        hint = _radar_hint_from_store(persisted)
    best = ctx.get("radar_best_candidate")
    if not isinstance(best, dict) or not best.get("symbol"):
        best = hint[0] if hint else None
    entry_conditions = _text_or_na(
        prop.get("entry_conditions")
        or store_payload.get("entry_conditions")
        or prop.get("missing_information")
        or store_payload.get("missing_information")
    )
    comparisons = (
        prop.get("top_candidates_comparison")
        or store_payload.get("top_candidates_comparison")
        or []
    )
    # Snapshot direcional do momento da decisão (contexto) ou do request_store.
    dir_ops = ctx.get("directional_opportunities")
    if not isinstance(dir_ops, list) or not dir_ops:
        dir_ops = _directional_ops_from_store(persisted)
    quality = ctx.get("decision_quality")
    # Reavaliar sempre a consistência EV (deteta dupla contagem #1118 mesmo se
    # a quality antiga foi gravada sem este check).
    q = assess_radar_decision_quality(
        {
            **prop,
            **({} if not store_payload else store_payload),
            "decision": decision,
            "justification": justification
            if justification != NOT_INDICATED
            else prop.get("justification") or "",
            "top_candidates_comparison": comparisons,
            "entry_conditions": (
                ""
                if entry_conditions == NOT_INDICATED
                else entry_conditions
            ),
            "cited_expected_net_usd": prop.get("cited_expected_net_usd")
            or store_payload.get("cited_expected_net_usd"),
        },
        radar={
            "local_priority_hint": hint,
            "directional_opportunities": dir_ops,
        },
    )
    quality = q.as_dict()
    disc = discordance_summary(
        radar_best=best if isinstance(best, dict) else None,
        openai_decision=decision,
        openai_symbol=None if symbol == NOT_INDICATED else symbol,
        justification="" if justification == NOT_INDICATED else justification,
        entry_conditions="" if entry_conditions == NOT_INDICATED else entry_conditions,
        quality_issues=list(quality.get("issues") or []),
    )
    disc["top_candidates_comparison"] = comparisons if isinstance(comparisons, list) else []
    disc["local_priority_hint"] = hint
    disc["directional_opportunities_sent"] = dir_ops
    disc["justification_quality_ok"] = bool(quality.get("ok"))
    disc["decision_quality"] = quality.get("status") or (
        "ok" if quality.get("ok") else "insufficient"
    )
    disc["analysis_incomplete"] = is_decision_quality_insufficient(quality)
    disc["ai_decision_inconsistent"] = bool(quality.get("ai_decision_inconsistent"))
    disc["ev_inconsistency"] = quality.get("ev_inconsistency") or {}
    disc["quality_failure_reasons"] = list(quality.get("issues") or [])
    # Motivo exacto visível — não esconder a resposta original (justification no painel).
    if disc["ai_decision_inconsistent"]:
        disc["quality_failure_summary"] = (
            "ai_decision_inconsistent — "
            + str(
                (disc["ev_inconsistency"] or {}).get("real_block_reason")
                or "; ".join(disc["quality_failure_reasons"][:3])
            )
        )
        # HOLD permanece HOLD; o bloqueio real é inconsistência matemática, não EV≤0.
        disc["discordance_reason"] = (
            (disc.get("discordance_reason") or "")
            + " | BLOQUEIO: justificação OpenAI contradiz o EV líquido do motor "
            "(custos descontados duas vezes). Decisão NÃO convertida para BUY/SELL."
        )
    elif disc["analysis_incomplete"]:
        disc["quality_failure_summary"] = (
            "decision_quality=insufficient — " + "; ".join(disc["quality_failure_reasons"][:5])
            if disc["quality_failure_reasons"]
            else "decision_quality=insufficient"
        )
    else:
        disc["quality_failure_summary"] = ""
    return disc


def serialize_openai_decision(log: AIDecisionLog) -> dict[str, Any]:
    """Serializa um AIDecisionLog OpenAI para o painel (campos em falta → NA)."""
    prop = _safe_json(log.proposal_json)
    ctx = _safe_json(log.context_json)
    persisted = str(ctx.get("persisted_path") or "")
    model = _model_from_store(persisted) or _text_or_na(ctx.get("model"))
    if model == NOT_INDICATED:
        model = NOT_INDICATED

    # Hidratar do response_store se ORM/proposal vazios (envelope #573) ou sem comparação.
    store_payload: dict[str, Any] = {}
    needs_store = (
        not str(log.justification or prop.get("justification") or "").strip()
        or not (prop.get("top_candidates_comparison") or [])
    )
    if needs_store and persisted:
        store_payload = _payload_from_persisted_store(persisted)

    def _coalesce(*values: Any) -> Any:
        for v in values:
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            if isinstance(v, (list, dict)) and not v:
                continue
            if isinstance(v, (int, float)) and v == 0:
                continue
            return v
        return None

    # Com hidratação do store (ORM vazio), o store tem precedência sobre defaults Pydantic.
    if store_payload:
        decision = str(
            _coalesce(
                store_payload.get("decision"),
                log.decision,
                prop.get("decision"),
                "HOLD",
            )
        ).upper()
        justification = _text_or_na(
            _coalesce(
                store_payload.get("justification"),
                log.justification,
                prop.get("justification"),
            )
        )
        risks = _text_or_na(
            _coalesce(store_payload.get("risks"), log.risks, prop.get("risks"))
        )
        invalidating = _text_or_na(
            _coalesce(
                store_payload.get("invalidating_scenario"),
                log.invalidating_scenario,
                prop.get("invalidating_scenario"),
            )
        )
        favoring = _text_or_na(
            _coalesce(
                store_payload.get("favoring_scenario"),
                log.favoring_scenario,
                prop.get("favoring_scenario"),
            )
        )
        why_preferred = _text_or_na(
            _coalesce(store_payload.get("why_preferred"), prop.get("why_preferred"))
        )
    else:
        decision = str(
            _coalesce(log.decision, prop.get("decision"), "HOLD")
        ).upper()
        justification = _text_or_na(
            _coalesce(log.justification, prop.get("justification"))
        )
        risks = _text_or_na(_coalesce(log.risks, prop.get("risks")))
        invalidating = _text_or_na(
            _coalesce(log.invalidating_scenario, prop.get("invalidating_scenario"))
        )
        favoring = _text_or_na(
            _coalesce(log.favoring_scenario, prop.get("favoring_scenario"))
        )
        why_preferred = _text_or_na(prop.get("why_preferred"))
    if decision not in DECISION_PT:
        decision = "HOLD"

    conf_prop = dict(prop)
    if store_payload.get("confidence") and not conf_prop.get("confidence"):
        conf_prop["confidence"] = store_payload.get("confidence")
    compared = (
        prop.get("compared_symbols")
        or store_payload.get("compared_symbols")
        or []
    )

    mt5_ticket = ctx.get("mt5_ticket") or ctx.get("order_ticket") or ""
    execution_attempted = bool(log.executed) or bool(ctx.get("execution_attempted"))
    real_paid = is_real_paid_openai_decision(log)
    hydrated_from_store = bool(store_payload) and justification != NOT_INDICATED
    symbol = _symbol_or_na(prop, ctx)
    if symbol == NOT_INDICATED:
        symbol = _symbol_or_na(store_payload, ctx)
    return {
        "id": log.id,
        "available": True,
        "source": "openai_persisted" if real_paid else "synthetic_or_test",
        "is_real_paid_openai": real_paid,
        "is_synthetic_test": not real_paid,
        "hydrated_from_response_store": hydrated_from_store,
        "mode": log.mode,
        "decision": decision,
        "decision_pt": DECISION_PT.get(decision, "AGUARDAR"),
        "symbol": symbol,
        "justification": justification,
        "confidence": _confidence_or_na(conf_prop, log),
        "risks": risks,
        "invalidating_scenario": invalidating,
        "favoring_scenario": favoring,
        "why_preferred": why_preferred,
        "missing_information": _text_or_na(
            _coalesce(
                log.missing_information,
                prop.get("missing_information"),
                store_payload.get("missing_information"),
            )
        ),
        "time_horizon": _text_or_na(
            _coalesce(
                log.time_horizon,
                prop.get("time_horizon"),
                store_payload.get("time_horizon"),
            )
        ),
        "model": model if model and model != NOT_INDICATED else NOT_INDICATED,
        "estimated_cost_usd": round(float(log.estimated_cost_usd or 0.0), 6),
        "executed": bool(log.executed),
        "execution_attempted": execution_attempted,
        "risk_approved": bool(log.risk_approved),
        "risk_reasons": (log.risk_reasons or "").strip(),
        "mt5_ticket": str(mt5_ticket) if mt5_ticket else None,
        "analyzed_at": iso_utc(log.created_at),
        "persisted_path": persisted or "",
        "compared_symbols": compared if isinstance(compared, list) else [],
        "radar_vs_openai": _radar_vs_openai_block(
            ctx=ctx,
            prop=prop,
            store_payload=store_payload,
            persisted=persisted,
            decision=decision,
            symbol=symbol,
            justification=justification,
        ),
        "entry_conditions": _text_or_na(
            prop.get("entry_conditions")
            or store_payload.get("entry_conditions")
            or prop.get("missing_information")
            or store_payload.get("missing_information")
        ),
        "execution_note": (
            f"Ordem MT5 ticket {mt5_ticket}."
            if mt5_ticket
            else (
                "Tentativa de execução registada — ver histórico de ordens."
                if execution_attempted
                else "Decisão e execução são eventos distintos — nenhuma ordem enviada."
            )
        ),
    }


def empty_openai_decision() -> dict[str, Any]:
    return {
        "available": False,
        "source": "none",
        "decision": None,
        "decision_pt": None,
        "symbol": NOT_INDICATED,
        "justification": "Ainda não existe nenhuma decisão OpenAI real persistida.",
        "confidence": NOT_INDICATED,
        "risks": NOT_INDICATED,
        "invalidating_scenario": NOT_INDICATED,
        "favoring_scenario": NOT_INDICATED,
        "why_preferred": NOT_INDICATED,
        "missing_information": NOT_INDICATED,
        "time_horizon": NOT_INDICATED,
        "model": NOT_INDICATED,
        "estimated_cost_usd": None,
        "executed": False,
        "analyzed_at": None,
        "persisted_path": "",
        "execution_note": "Observação apenas — nenhuma ordem foi executada.",
        "entry_conditions": NOT_INDICATED,
        "radar_vs_openai": {
            "radar_best_candidate": None,
            "openai_decision": None,
            "discordance_reason": "Sem decisão OpenAI real para comparar com o radar.",
            "entry_conditions": NOT_INDICATED,
            "note": (
                "local_priority_hint é triagem de atenção/custo, não sinal de execução. "
                "Só BUY/SELL OpenAI válidos podem ir ao executor DEMO."
            ),
        },
        "message": "Sem decisão OpenAI real guardada. O radar local e a IA simulada não substituem uma análise paga.",
    }


def list_openai_decisions(db: Session, *, limit: int = 5) -> list[AIDecisionLog]:
    """Últimas N decisões OpenAI pagas reais (ignora simuladas e stubs de teste)."""
    lim = max(0, int(limit))
    if lim == 0:
        return []
    # Busca alargada e filtra em Python — stubs usam o mesmo mode prefix.
    candidates = list(
        db.scalars(
            select(AIDecisionLog)
            .where(AIDecisionLog.mode.like("openai%"))
            .order_by(AIDecisionLog.id.desc())
            .limit(max(lim * 12, 40))
        )
    )
    real = [r for r in candidates if is_real_paid_openai_decision(r)]
    return real[:lim]


def openai_panel_payload(db: Session, *, history_limit: int = 5) -> dict[str, Any]:
    """Última decisão OpenAI real + histórico recente (só leitura)."""
    rows = list_openai_decisions(db, limit=history_limit)
    if not rows:
        empty = empty_openai_decision()
        return {
            "available": False,
            "latest": empty,
            "recent": [],
            "count": 0,
            "message": empty["message"],
        }
    recent = [serialize_openai_decision(r) for r in rows]
    return {
        "available": True,
        "latest": recent[0],
        "recent": recent,
        "count": len(recent),
        "message": "",
    }


def authorization_label(prep: dict[str, Any]) -> str:
    """Rótulo de autorização a partir do estado real do backend (não inferido de custos)."""
    suspend = str(prep.get("suspend_reason") or "").strip()
    if suspend and "auditoria" in suspend.lower():
        return "A aguardar autorização (auditoria — confirme novamente)"
    if suspend and "desativ" in suspend.lower() and not prep.get("confirmed"):
        return f"Desativada — {suspend}"
    if not prep.get("confirmed"):
        return "A aguardar autorização"
    if prep.get("scheduling_active") or (
        prep.get("active") and prep.get("robot_running") and not suspend
    ):
        return "OpenAI autónoma ativa"
    if not prep.get("robot_running"):
        return "Autorizada — agendamento parado"
    if suspend:
        return f"Autorizada — agendamento suspenso ({suspend})"
    blockers = prep.get("blockers") or []
    if blockers:
        return f"Autorizada — inativa ({blockers[0]})"
    return "Autorizada — aguardar condições"


def as_last_result_shape(decision: dict[str, Any]) -> dict[str, Any]:
    """Compatível com openai_autonomous.last_result no painel."""
    if not decision.get("available"):
        return {}
    return {
        "ok": True,
        "decision": decision.get("decision"),
        "decision_pt": decision.get("decision_pt"),
        "symbol": decision.get("symbol"),
        "justification": decision.get("justification"),
        "risks": decision.get("risks"),
        "invalidating_scenario": decision.get("invalidating_scenario"),
        "favoring_scenario": decision.get("favoring_scenario"),
        "actual_cost_usd": decision.get("estimated_cost_usd"),
        "persisted_path": decision.get("persisted_path"),
        "decision_log_id": decision.get("id"),
        "executed": False,
        "observed_at": decision.get("analyzed_at"),
        "model": decision.get("model"),
        "from_persisted": True,
    }
