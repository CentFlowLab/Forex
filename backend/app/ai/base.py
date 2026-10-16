"""Model-agnostic AI provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderUnavailable(Exception):
    """Raised when a provider has no credentials or cannot be reached.

    Callers must surface this to the user. Never invent a model response.
    """


@dataclass
class AIMessage:
    role: str
    content: str


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    raw_status: str = "ok"
    # Caminho do ficheiro durável (response_store) — preenchido pelo provider.
    persisted_path: str = ""


class AIProvider(ABC):
    name: str

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def available_models(self) -> list[str]: ...

    @abstractmethod
    def complete(
        self,
        messages: list[AIMessage],
        model: str,
        temperature: float,
        max_tokens: int,
        response_schema: dict | None = None,
    ) -> AIResponse: ...
