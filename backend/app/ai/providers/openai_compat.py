"""Shared OpenAI-compatible chat completions client (OpenAI + xAI)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.base import AIMessage, AIProvider, AIResponse, ProviderUnavailable
from app.ai.costs import estimate_cost
from app.ai.retry import with_retries
from app.config import get_settings


class OpenAICompatProvider(AIProvider):
    def __init__(self, name: str, api_key: str, base_url: str, models: list[str]):
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._models = models

    def configured(self) -> bool:
        return bool(self._api_key and self._api_key.strip())

    def available_models(self) -> list[str]:
        return list(self._models)

    def _supports_json_schema(self, model: str) -> bool:
        # OpenAI oficial + modelos recentes; xAI usa json_object como fallback.
        if self.name != "openai":
            return False
        m = model.lower()
        return any(x in m for x in ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "o1", "o3", "o4"))

    def complete(
        self,
        messages: list[AIMessage],
        model: str,
        temperature: float,
        max_tokens: int,
        response_schema: dict | None = None,
    ) -> AIResponse:
        if not self.configured():
            raise ProviderUnavailable(f"{self.name} API key is not configured.")

        # Controlo centralizado — cada tentativa (incl. retries) passa pelo gate.
        from app.ai.control import AIAuthorizationError, AIDisabled, get_ai_control

        control = get_ai_control()
        attempt_box = {"n": 0}

        def _once() -> AIResponse:
            attempt_box["n"] += 1
            try:
                control.before_provider_call(provider=self.name, model=model)
            except (AIDisabled, AIAuthorizationError) as exc:
                raise ProviderUnavailable(str(exc)) from exc
            settings = get_settings()
            shaped: list[dict[str, str]] = []
            for m in messages:
                content = m.content
                if m.role == "system" and "português de Portugal" not in content[:200].lower():
                    content = (
                        "LANGUAGE LOCK: All narrative JSON text fields must be in European Portuguese (pt-PT). "
                        "Never write rationale/argument/evidence claims in English.\n\n"
                        + content
                    )
                shaped.append({"role": m.role, "content": content})
            payload: dict[str, Any] = {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": shaped,
            }
            if response_schema and self._supports_json_schema(model):
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": response_schema,
                }
            elif self.name in {"openai", "xai"}:
                payload["response_format"] = {"type": "json_object"}

            started = time.perf_counter()
            try:
                with httpx.Client(timeout=settings.ai_request_timeout_seconds) as client:
                    response = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except httpx.HTTPError as exc:
                control.record_call_result(
                    provider=self.name,
                    model=model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    estimated_cost_usd=0.0,
                    status="error",
                    error=str(exc),
                    attempt=attempt_box["n"],
                )
                raise ProviderUnavailable(f"{self.name} request failed: {exc}") from exc

            latency = int((time.perf_counter() - started) * 1000)
            # Se json_schema não for suportado pelo endpoint, repetir sem ele uma vez
            # (mesmo attempt — não é retry de rede; não volta a contar no gate).
            if response.status_code >= 400 and response_schema and "response_format" in str(response.text):
                payload.pop("response_format", None)
                payload["response_format"] = {"type": "json_object"}
                with httpx.Client(timeout=settings.ai_request_timeout_seconds) as client:
                    response = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                latency = int((time.perf_counter() - started) * 1000)

            if response.status_code >= 400:
                control.record_call_result(
                    provider=self.name,
                    model=model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    estimated_cost_usd=0.0,
                    status="error",
                    error=f"HTTP {response.status_code}",
                    attempt=attempt_box["n"],
                )
                raise ProviderUnavailable(
                    f"{self.name} returned HTTP {response.status_code}: {response.text[:300]}"
                )
            data = response.json()
            # Persistência durável IMEDIATA do corpo HTTP — antes de ORM/parsing.
            from app.ai.control import _auth_token, _call_reason, _call_role
            from app.ai.response_store import persist_raw_provider_response

            auth_id = _auth_token.get() or ""
            try:
                early_text = ""
                try:
                    early_text = str(data["choices"][0]["message"]["content"] or "")
                except (KeyError, IndexError, TypeError, AttributeError):
                    early_text = ""
                store_path = persist_raw_provider_response(
                    provider=self.name,
                    model=model,
                    raw_http=data if isinstance(data, dict) else {"raw": data},
                    text=early_text,
                    authorization_id=auth_id,
                    role=_call_role.get() or "",
                    reason=_call_reason.get() or "",
                    status="received",
                    request_messages=shaped,
                )
            except Exception:  # noqa: BLE001 — nunca perder o fluxo por falha de disco
                store_path = None

            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderUnavailable(f"{self.name} response missing message content.") from exc
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cost = estimate_cost(model, prompt_tokens, completion_tokens)
            control.record_call_result(
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                status="ok",
                attempt=attempt_box["n"],
                response_text=text or "",
                raw_http=data if isinstance(data, dict) else None,
                response_store_path=str(store_path) if store_path else "",
            )
            return AIResponse(
                text=text or "",
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                latency_ms=latency,
                persisted_path=str(store_path) if store_path else "",
            )

        return with_retries(_once, label=self.name)
