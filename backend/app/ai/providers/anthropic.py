"""Anthropic Messages API client."""

from __future__ import annotations

import time

import httpx

from app.ai.base import AIMessage, AIProvider, AIResponse, ProviderUnavailable
from app.ai.costs import estimate_cost
from app.ai.retry import with_retries
from app.config import get_settings


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str, models: list[str]):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._models = models

    def configured(self) -> bool:
        return bool(self._api_key and self._api_key.strip())

    def available_models(self) -> list[str]:
        return list(self._models)

    def complete(
        self,
        messages: list[AIMessage],
        model: str,
        temperature: float,
        max_tokens: int,
        response_schema: dict | None = None,
    ) -> AIResponse:
        if not self.configured():
            raise ProviderUnavailable("anthropic API key is not configured.")
        _ = response_schema  # Anthropic: JSON via prompt; sem json_schema nativo aqui.

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
            system = "\n\n".join(m.content for m in messages if m.role == "system")
            chat = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": chat,
            }
            started = time.perf_counter()
            try:
                with httpx.Client(timeout=settings.ai_request_timeout_seconds) as client:
                    response = client.post(
                        f"{self._base_url}/v1/messages",
                        headers={
                            "x-api-key": self._api_key,
                            "anthropic-version": "2023-06-01",
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
                raise ProviderUnavailable(f"anthropic request failed: {exc}") from exc

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
                    f"anthropic returned HTTP {response.status_code}: {response.text[:300]}"
                )
            data = response.json()
            from app.ai.control import _auth_token, _call_reason, _call_role
            from app.ai.response_store import persist_raw_provider_response

            blocks = data.get("content") or []
            text = ""
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text") or ""
            store_path = None
            try:
                store_path = persist_raw_provider_response(
                    provider=self.name,
                    model=model,
                    raw_http=data if isinstance(data, dict) else {"raw": data},
                    text=text,
                    authorization_id=_auth_token.get() or "",
                    role=_call_role.get() or "",
                    reason=_call_reason.get() or "",
                    status="received",
                )
            except Exception:  # noqa: BLE001
                store_path = None
            if not text:
                raise ProviderUnavailable("anthropic response contained no text block.")
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("output_tokens") or 0)
            cost = estimate_cost(model, prompt_tokens, completion_tokens)
            control.record_call_result(
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                status="ok",
                attempt=attempt_box["n"],
                response_text=text,
                raw_http=data if isinstance(data, dict) else None,
                response_store_path=str(store_path) if store_path else "",
            )
            return AIResponse(
                text=text,
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                latency_ms=latency,
                persisted_path=str(store_path) if store_path else "",
            )

        return with_retries(_once, label="anthropic")
