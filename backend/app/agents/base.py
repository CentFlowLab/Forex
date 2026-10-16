"""Agent runner: calls a configured provider and validates structured JSON."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.language import (
    PT_PT_RETRY_SYSTEM,
    PT_PT_USER_WRAPPER,
    assess_payload_language,
)
from app.agents.prompts import DISPLAY_NAMES, SYSTEM_PROMPTS
from app.agents.structured import (
    pydantic_to_openai_json_schema,
    repair_instruction,
    validate_agent_payload,
)
from app.ai.base import AIMessage, ProviderUnavailable
from app.ai.budget import BudgetExceeded, assert_budget_allows
from app.ai.registry import get_provider
from app.config import get_settings
from app.models.agent import AgentConfig
from app.models.audit import ApiUsage

T = TypeVar("T", bound=BaseModel)

# Estados que NÃO contam como análise válida para o coordenador / debate.
INVALID_ANALYSIS_STATUSES = frozenset(
    {
        "unavailable",
        "disabled",
        "error",
        "invalid_json",
        "language_rejected",
    }
)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError("O modelo não devolveu JSON válido.")


def load_agent_config(db: Session, role: str) -> AgentConfig:
    row = db.get(AgentConfig, role)
    if row:
        return row
    settings = get_settings()
    provider, model = settings.agent_assignment(role)
    row = AgentConfig(
        role=role,
        display_name=DISPLAY_NAMES[role],
        provider=provider,
        model=model,
        enabled=True,
        temperature=settings.ai_temperature,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _call_provider(
    provider: Any,
    model: str,
    temperature: float,
    max_tokens: int,
    system: str,
    user_content: str,
    response_schema: dict | None,
) -> Any:
    messages = [
        AIMessage("system", system),
        AIMessage("user", user_content),
    ]
    return provider.complete(
        messages,
        model,
        temperature,
        max_tokens,
        response_schema=response_schema,
    )


def run_agent(
    db: Session,
    role: str,
    schema: type[T],
    user_payload: dict[str, Any],
    extra_system: str = "",
) -> dict[str, Any]:
    cfg = load_agent_config(db, role)
    now = datetime.now(timezone.utc).isoformat()
    user_payload = {
        **user_payload,
        "timestamp_utc": now,
        "response_language": "pt-PT",
        "idioma_obrigatorio": "português de Portugal",
    }
    result: dict[str, Any] = {
        "role": role,
        "display_name": cfg.display_name,
        "provider": cfg.provider,
        "model": cfg.model,
        "status": "unavailable",
        "bias": "NEUTRAL",
        "recommendation": "HOLD",
        "confidence": 0.0,
        "uncertainty": 1.0,
        "summary": "",
        "payload": {},
        "error": "",
        "error_code": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost_usd": 0.0,
        "latency_ms": 0,
        "language_ok": True,
        "language_warning": "",
        "valid_for_analysis": False,
    }
    from app.i18n_pt import MSG

    if not cfg.enabled:
        result["error"] = MSG["agent_disabled"]
        result["status"] = "disabled"
        result["error_code"] = "disabled"
        return result

    try:
        provider = get_provider(cfg.provider)
    except ProviderUnavailable as exc:
        result["error"] = str(exc)
        result["error_code"] = "provider_unavailable"
        return result
    if not provider.configured():
        result["error"] = MSG["provider_not_configured"].format(provider=cfg.provider)
        result["error_code"] = "provider_not_configured"
        return result

    settings = get_settings()
    system = SYSTEM_PROMPTS[role] + extra_system
    user_content = PT_PT_USER_WRAPPER + json.dumps(user_payload, default=str, ensure_ascii=False)
    openai_schema = None
    if cfg.provider == "openai":
        try:
            openai_schema = pydantic_to_openai_json_schema(schema)
        except Exception:  # noqa: BLE001
            openai_schema = None

    def _budget_or_fail() -> str | None:
        try:
            assert_budget_allows(db)
            return None
        except BudgetExceeded as exc:
            return str(exc)

    from app.ai.control import get_ai_control

    control = get_ai_control()
    if not control.is_enabled():
        result["status"] = "error"
        result["error"] = "IA desligada — nenhuma chamada ao fornecedor."
        result["error_code"] = "ai_disabled"
        result["summary"] = "Chamada de IA bloqueada (AI_ENABLED=false)."
        return result

    budget_err = _budget_or_fail()
    if budget_err:
        result["status"] = "error"
        result["error"] = budget_err
        result["error_code"] = "budget_exceeded"
        result["summary"] = "Chamada de IA bloqueada por limite de orçamento."
        return result

    control.set_call_meta(role=role, reason="agent_analysis")

    total_prompt = 0
    total_completion = 0
    total_cost = 0.0
    total_latency = 0

    try:
        response = _call_provider(
            provider,
            cfg.model,
            cfg.temperature,
            settings.ai_max_tokens,
            system,
            user_content,
            openai_schema,
        )
    except ProviderUnavailable as exc:
        result["error"] = str(exc)
        result["status"] = "error"
        result["error_code"] = "provider_error"
        db.add(ApiUsage(provider=cfg.provider, model=cfg.model, role=role, status="error"))
        db.commit()
        return result

    total_prompt += response.prompt_tokens
    total_completion += response.completion_tokens
    total_cost += response.estimated_cost_usd
    total_latency += response.latency_ms

    def _parse(text: str) -> tuple[dict[str, Any] | None, str]:
        try:
            raw = _extract_json(text)
        except ValueError as exc:
            return None, str(exc)
        return validate_agent_payload(schema, raw)

    payload, parse_err = _parse(response.text)
    repair_used = False

    # Uma única tentativa de correção se JSON/esquema inválido (respeita orçamento).
    if payload is None:
        budget_err = _budget_or_fail()
        if not budget_err:
            repair_used = True
            try:
                repair = _call_provider(
                    provider,
                    cfg.model,
                    cfg.temperature,
                    settings.ai_max_tokens,
                    system + repair_instruction(parse_err),
                    user_content,
                    openai_schema,
                )
                total_prompt += repair.prompt_tokens
                total_completion += repair.completion_tokens
                total_cost += repair.estimated_cost_usd
                total_latency += repair.latency_ms
                payload, parse_err = _parse(repair.text)
            except ProviderUnavailable as exc:
                parse_err = f"{parse_err}; correção falhou: {exc}"

    lang = assess_payload_language(payload) if payload else {"ok": True, "warning": "", "likely_english": False}

    # Uma correção de idioma (só se o JSON já for válido e ainda houver orçamento).
    if payload and lang.get("likely_english") and not repair_used:
        budget_err = _budget_or_fail()
        if not budget_err:
            try:
                retry = _call_provider(
                    provider,
                    cfg.model,
                    cfg.temperature,
                    settings.ai_max_tokens,
                    system + PT_PT_RETRY_SYSTEM,
                    user_content,
                    openai_schema,
                )
                total_prompt += retry.prompt_tokens
                total_completion += retry.completion_tokens
                total_cost += retry.estimated_cost_usd
                total_latency += retry.latency_ms
                retry_payload, retry_err = _parse(retry.text)
                if retry_payload:
                    payload = retry_payload
                    parse_err = ""
                    lang = assess_payload_language(payload)
                elif retry_err:
                    parse_err = retry_err
                    payload = None
            except ProviderUnavailable:
                pass

    result.update(
        {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "estimated_cost_usd": total_cost,
            "latency_ms": total_latency,
        }
    )

    usage_status = "ok"
    if payload is None:
        usage_status = "invalid_json"
    elif lang.get("likely_english"):
        usage_status = "language_rejected"

    db.add(
        ApiUsage(
            provider=cfg.provider,
            model=cfg.model,
            role=role,
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            estimated_cost_usd=total_cost,
            status=usage_status,
        )
    )

    if payload is None:
        result.update(
            {
                "status": "error",
                "error_code": "invalid_json",
                "error": f"Validação estruturada falhou: {parse_err}",
                "summary": (
                    "ERRO: a saída do modelo é inválida após tentativa de correção. "
                    "Este relatório não é utilizado como análise válida."
                ),
                "payload": {},
                "recommendation": "HOLD",
                "bias": "NEUTRAL",
                "confidence": 0.0,
                "uncertainty": 1.0,
                "valid_for_analysis": False,
            }
        )
        db.commit()
        return result

    if lang.get("likely_english"):
        result.update(
            {
                "status": "error",
                "error_code": "language_rejected",
                "bias": "NEUTRAL",
                "recommendation": "HOLD",
                "confidence": 0.0,
                "uncertainty": 1.0,
                "summary": (
                    "ERRO: resposta rejeitada — o modelo respondeu em inglês após reforço pt-PT. "
                    "Este relatório não é utilizado como análise válida."
                ),
                "payload": {},
                "language_ok": False,
                "language_warning": str(lang.get("warning") or "Resposta em inglês rejeitada."),
                "error": "Resposta em inglês rejeitada — obrigatório português de Portugal (pt-PT).",
                "valid_for_analysis": False,
            }
        )
        db.commit()
        return result

    result.update(
        {
            "status": "ok",
            "bias": payload.get("bias", "NEUTRAL"),
            "recommendation": payload.get("recommendation", "HOLD"),
            "confidence": payload.get("confidence", 0.0),
            "uncertainty": payload.get("uncertainty", 1.0),
            "summary": payload.get("rationale", "")[:2000],
            "payload": payload,
            "language_ok": bool(lang.get("ok", True)),
            "language_warning": str(lang.get("warning") or ""),
            "valid_for_analysis": True,
            "error_code": "",
        }
    )
    if lang.get("warning"):
        result["error"] = result["error"] or str(lang["warning"])
    db.commit()
    return result


def is_valid_analysis(report: dict[str, Any]) -> bool:
    return bool(report.get("valid_for_analysis")) or report.get("status") == "ok"
