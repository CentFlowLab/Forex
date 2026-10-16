"""Retries com backoff exponencial limitado para fornecedores de IA."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from app.ai.base import ProviderUnavailable
from app.config import get_settings

T = TypeVar("T")


def with_retries(operation: Callable[[], T], *, label: str = "AI") -> T:
    settings = get_settings()
    attempts = max(1, int(settings.ai_max_retries))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except ProviderUnavailable as exc:
            last_exc = exc
            msg = str(exc).lower()
            # Não repetir erros de configuração / autenticação / bloqueio de custos.
            if any(
                x in msg
                for x in (
                    "not configured",
                    "401",
                    "403",
                    "invalid api key",
                    "authentication",
                    "ia desligada",
                    "ai_enabled",
                    "autorização",
                    "autorizacao",
                    "orçamento",
                    "orcamento",
                    "bloqueada",
                    "bloqueado",
                )
            ):
                raise
            if attempt >= attempts - 1:
                raise
            delay = min(8.0, 0.5 * (2**attempt))
            time.sleep(delay)
        except Exception as exc:  # noqa: BLE001 — última linha de defesa
            last_exc = exc
            if attempt >= attempts - 1:
                raise ProviderUnavailable(f"{label} falhou após {attempts} tentativas: {exc}") from exc
            time.sleep(min(8.0, 0.5 * (2**attempt)))
    raise ProviderUnavailable(f"{label} falhou: {last_exc}")
