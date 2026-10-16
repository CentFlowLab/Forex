"""Persistência durável de respostas de fornecedores de IA — antes de qualquer ORM.

Garante que uma resposta HTTP válida não se perde se o registo SQL falhar.
Não faz chamadas pagas. Reprocessamento é apenas local.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

# backend/data/ai_responses/ — sobrescrito por GROK_AI_RESPONSES_DIR em testes.
_STORE_DIR = Path(__file__).resolve().parents[2] / "data" / "ai_responses"


def store_dir() -> Path:
    override = (os.environ.get("GROK_AI_RESPONSES_DIR") or "").strip()
    directory = Path(override) if override else _STORE_DIR
    from app.db_isolation import assert_ai_responses_dir_allowed

    assert_ai_responses_dir_allowed(directory, context="response_store.store_dir")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_token(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "", (value or "")[:48])
    return cleaned or "noauth"


def persist_raw_provider_response(
    *,
    provider: str,
    model: str,
    raw_http: dict[str, Any] | None,
    text: str,
    authorization_id: str = "",
    role: str = "",
    reason: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    call_log_id: int | None = None,
    status: str = "ok",
    extra: dict[str, Any] | None = None,
    request_messages: list[dict[str, Any]] | None = None,
) -> Path:
    """Grava a resposta de forma atómica (tmp → rename). Devolve o caminho do ficheiro."""
    directory = store_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    auth = _safe_token(authorization_id)
    file_id = f"{stamp}_{auth}_{uuid.uuid4().hex[:8]}"
    path = directory / f"{file_id}.json"
    tmp = directory / f"{file_id}.json.tmp"

    # Extrair texto da mensagem se raw_http existir e text vazio.
    body_text = text or ""
    if not body_text and isinstance(raw_http, dict):
        try:
            body_text = str(raw_http["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            body_text = ""

    # Truncar mensagens de pedido para não inflar disco (mantém auditoria do contexto).
    stored_messages: list[dict[str, Any]] = []
    if request_messages:
        for m in request_messages:
            content = str(m.get("content") or "")
            if len(content) > 48000:
                content = content[:48000] + "…[truncated]"
            stored_messages.append({"role": m.get("role"), "content": content})

    payload: dict[str, Any] = {
        "file_id": file_id,
        "saved_at": _utcnow_iso(),
        "provider": provider,
        "model": model,
        "authorization_id": authorization_id or "",
        "role": role or "",
        "reason": reason or "",
        "status": status,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "estimated_cost_usd": float(estimated_cost_usd or 0.0),
        "call_log_id": call_log_id,
        "text": body_text,
        "raw_http": raw_http,
        "request_messages": stored_messages,
        "reprocessable": True,
        "processed": False,
        "process_error": "",
    }
    if extra:
        payload["extra"] = extra

    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    with _LOCK:
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)
    return path


def update_persisted_meta(path: Path, **fields: Any) -> Path:
    """Atualiza metadados (ex.: call_log_id) sem perder o texto/raw_http."""
    with _LOCK:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if key in {"text", "raw_http"} and value is None:
                continue
            payload[key] = value
        payload["updated_at"] = _utcnow_iso()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    return path


def load_persisted(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def find_by_authorization(authorization_id: str) -> list[Path]:
    auth = (authorization_id or "").strip()
    if not auth:
        return []
    directory = store_dir()
    found: list[Path] = []
    for p in sorted(directory.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("authorization_id") == auth:
            found.append(p)
    return found


def find_by_call_log_id(call_log_id: int) -> Path | None:
    directory = store_dir()
    for p in sorted(directory.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("call_log_id") == call_log_id:
            return p
    return None


def reprocess_json_decision(path: Path | str, schema_model: type) -> tuple[Any | None, str]:
    """Reprocessa localmente o JSON da resposta — sem chamada paga.

    schema_model: tipicamente AIRadarDecision ou AITradeProposal.
    Devolve (instância validada | None, erro).
    """
    from app.agents.structured import validate_agent_payload

    data = load_persisted(path)
    raw_text = data.get("text") or ""
    if not raw_text and isinstance(data.get("raw_http"), dict):
        try:
            raw_text = str(data["raw_http"]["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raw_text = ""
    if not raw_text:
        return None, "Resposta persistida sem texto."
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw_text)
        if not m:
            return None, "Resposta persistida sem JSON."
        try:
            raw = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            return None, f"JSON inválido: {exc}"
    parsed, err = validate_agent_payload(schema_model, raw if isinstance(raw, dict) else {})
    if not parsed:
        update_persisted_meta(Path(path), processed=False, process_error=str(err or "validação falhou"))
        return None, str(err or "validação falhou")
    obj = schema_model.model_validate(parsed)
    update_persisted_meta(
        Path(path),
        processed=True,
        process_error="",
        processed_decision=getattr(obj, "decision", None),
        processed_symbol=getattr(obj, "symbol", None),
    )
    return obj, ""
