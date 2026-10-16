"""Validação e normalização de saídas estruturadas dos agentes LLM."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Campos de viés/tendência: o modelo por vezes devolve "unclear" (válido só em structure/momentum).
_BIAS_FIELDS = frozenset(
    {"bias", "m5_trend", "m15_trend", "h1_trend", "h4_trend"}
)
_BIAS_ALIASES = {
    "unclear": "NEUTRAL",
    "unknown": "NEUTRAL",
    "sideways": "NEUTRAL",
    "range": "NEUTRAL",
    "flat": "NEUTRAL",
    "mixed": "NEUTRAL",
    "bull": "BULLISH",
    "bear": "BEARISH",
    "bullish": "BULLISH",
    "bearish": "BEARISH",
    "neutral": "NEUTRAL",
}
_REC_ALIASES = {
    "long": "BUY",
    "short": "SELL",
    "buy": "BUY",
    "sell": "SELL",
    "hold": "HOLD",
    "wait": "HOLD",
    "aguardar": "HOLD",
    "comprar": "BUY",
    "vender": "SELL",
}


def unwrap_schema_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Extrai o payload se o modelo ecoou o envelope JSON Schema.

    Caso real (#573): {"description": "...", "properties": {"decision": "HOLD", ...}}
    em vez de {"decision": "HOLD", ...}. Sem unwrap, o Pydantic aceita defaults vazios.
    """
    if not isinstance(raw, dict):
        return raw
    props = raw.get("properties")
    if not isinstance(props, dict):
        return raw
    has_decision = any(k in props for k in ("decision", "recommendation", "final_recommendation"))
    if not has_decision and "justification" not in props:
        return raw
    root_just = str(raw.get("justification") or "").strip()
    prop_just = str(props.get("justification") or "").strip()
    root_decision = raw.get("decision")
    # Envelope típico: description/type + properties, sem decision útil na raiz.
    if "description" in raw or raw.get("type") == "object" or "title" in raw:
        if root_decision is None or (not root_just and prop_just):
            return dict(props)
    # Raiz HOLD vazia + properties com conteúdo real.
    if (
        (root_decision in {None, "HOLD", "WAIT"} or root_decision == "")
        and not root_just
        and (prop_just or props.get("decision") is not None)
    ):
        return dict(props)
    return raw


def normalize_agent_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normaliza aliases comuns antes da validação Pydantic (não inventa dados de mercado)."""
    data = unwrap_schema_envelope(dict(raw))
    for key in list(data.keys()):
        val = data[key]
        if key in _BIAS_FIELDS and isinstance(val, str):
            mapped = _BIAS_ALIASES.get(val.strip().lower())
            if mapped:
                data[key] = mapped
            else:
                data[key] = val.strip().upper()
        elif key == "recommendation" and isinstance(val, str):
            mapped = _REC_ALIASES.get(val.strip().lower())
            data[key] = mapped or val.strip().upper()
        elif key == "final_recommendation" and isinstance(val, str):
            mapped = _REC_ALIASES.get(val.strip().lower())
            data[key] = mapped or val.strip().upper()
        elif key == "decision" and isinstance(val, str):
            mapped = _REC_ALIASES.get(val.strip().lower())
            if mapped:
                data[key] = mapped
        elif key == "volatility_regime" and isinstance(val, str):
            v = val.strip().lower()
            data[key] = v if v in {"low", "normal", "high", "unknown"} else "unknown"
    return data


def validate_agent_payload(schema: type[T], raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    try:
        normalized = normalize_agent_payload(raw if isinstance(raw, dict) else {})
        parsed = schema.model_validate(normalized)
        dumped = parsed.model_dump()
        # Defesa: se ainda ficou vazio mas o envelope original tinha properties, falhar.
        if (
            isinstance(raw, dict)
            and isinstance(raw.get("properties"), dict)
            and not str(dumped.get("justification") or "").strip()
            and str((raw.get("properties") or {}).get("justification") or "").strip()
        ):
            return None, (
                "Resposta em envelope JSON Schema não desembrulhada — "
                "justification presente em properties mas ausente no payload validado."
            )
        return dumped, ""
    except ValidationError as exc:
        return None, str(exc)


def pydantic_to_openai_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """
    Converte o modelo Pydantic para json_schema utilizável em response_format OpenAI.
    Em modo não estrito (strict=false) para aceitar campos opcionais do nosso contrato.
    """
    json_schema = schema.model_json_schema()
    # Remover $defs aninhados problemáticos se vazios; OpenAI aceita $defs.
    return {
        "name": schema.__name__,
        "strict": False,
        "schema": json_schema,
    }


def repair_instruction(validation_error: str) -> str:
    return (
        "\n\nCORREÇÃO OBRIGATÓRIA DE ESQUEMA JSON:\n"
        "A resposta anterior falhou a validação estruturada.\n"
        f"Erro: {validation_error[:1200]}\n"
        "Devolve APENAS um único objeto JSON válido.\n"
        "Para m5_trend/m15_trend/h1_trend/h4_trend/bias usa SOMENTE BULLISH, BEARISH ou NEUTRAL "
        "(nunca 'unclear' — 'unclear' só é permitido em structure e momentum).\n"
        "Todos os campos de texto em português de Portugal (pt-PT).\n"
    )
