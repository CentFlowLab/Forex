"""Isolamento TEST vs OPERACIONAL da base SQLite e artefactos persistidos.

Em modo teste (GROK_TEST_ISOLATION=1 ou PYTEST_CURRENT_TEST), é proibido
abrir/escrever a BD operacional ou o diretório operacional de ai_responses.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from app.config import BACKEND_DIR, DATA_DIR

# Caminhos canónicos da instalação operacional (nunca usar em pytest).
OPERATIONAL_DB_PATH = (DATA_DIR / "grokforex.db").resolve()
OPERATIONAL_AI_RESPONSES_DIR = (DATA_DIR / "ai_responses").resolve()
OPERATIONAL_DB_STEMS = {
    OPERATIONAL_DB_PATH,
    Path(str(OPERATIONAL_DB_PATH) + "-wal").resolve(),
    Path(str(OPERATIONAL_DB_PATH) + "-shm").resolve(),
    Path(str(OPERATIONAL_DB_PATH) + "-journal").resolve(),
}

_lock = threading.Lock()
_blocked_access_attempts: list[str] = []


class OperationalDatabaseIsolationError(RuntimeError):
    """Tentativa de aceder à BD/artefactos operacionais durante testes."""


def test_isolation_active() -> bool:
    if os.environ.get("GROK_TEST_ISOLATION", "").strip() in {"1", "true", "TRUE", "yes"}:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get("APP_ENV", "").strip().lower() == "test":
        return True
    return False


def operational_db_path() -> Path:
    return OPERATIONAL_DB_PATH


def operational_ai_responses_dir() -> Path:
    return OPERATIONAL_AI_RESPONSES_DIR


def _normalize_sqlite_path(url_or_path: str | Path) -> Path | None:
    raw = str(url_or_path or "").strip()
    if not raw:
        return None
    if raw in {"sqlite://", "sqlite:///:memory:", "sqlite:///:memory"}:
        return None
    if raw.startswith("sqlite:///"):
        path_part = raw[len("sqlite:///") :]
        if path_part in {":memory:", ""}:
            return None
        # Windows: sqlite:///C:/... or sqlite:////C:/...
        p = Path(path_part)
        if not p.is_absolute() and raw.startswith("sqlite:///./"):
            p = (BACKEND_DIR / path_part).resolve()
        else:
            p = p.resolve()
        return p
    if raw.startswith("sqlite://"):
        # relative forms already handled; memory etc.
        return None
    p = Path(raw)
    try:
        return p.resolve()
    except OSError:
        return p


def is_operational_db_path(url_or_path: str | Path) -> bool:
    p = _normalize_sqlite_path(url_or_path)
    if p is None:
        return False
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    if resolved in OPERATIONAL_DB_STEMS:
        return True
    # Comparar também pelo ficheiro principal mesmo se -wal for passado
    if resolved.name.startswith("grokforex.db") and resolved.parent.resolve() == DATA_DIR.resolve():
        return True
    return False


def is_operational_ai_responses_path(path: str | Path) -> bool:
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    op = OPERATIONAL_AI_RESPONSES_DIR
    if p == op:
        return True
    try:
        p.relative_to(op)
        return True
    except ValueError:
        return False


def record_blocked_attempt(detail: str) -> None:
    with _lock:
        _blocked_access_attempts.append(detail)


def blocked_access_attempts() -> list[str]:
    with _lock:
        return list(_blocked_access_attempts)


def clear_blocked_access_attempts() -> None:
    with _lock:
        _blocked_access_attempts.clear()


def assert_database_url_allowed(url: str, *, context: str = "database") -> None:
    """Falha imediatamente se, em modo teste, o URL apontar à BD operacional."""
    if not test_isolation_active():
        return
    if is_operational_db_path(url):
        msg = (
            f"ISOLAMENTO TESTE: proibido usar a BD operacional "
            f"({OPERATIONAL_DB_PATH}) em {context}. url={url!r}"
        )
        record_blocked_attempt(msg)
        raise OperationalDatabaseIsolationError(msg)


def assert_ai_responses_dir_allowed(path: str | Path, *, context: str = "ai_responses") -> None:
    if not test_isolation_active():
        return
    if is_operational_ai_responses_path(path):
        msg = (
            f"ISOLAMENTO TESTE: proibido usar ai_responses operacional "
            f"({OPERATIONAL_AI_RESPONSES_DIR}) em {context}. path={path!r}"
        )
        record_blocked_attempt(msg)
        raise OperationalDatabaseIsolationError(msg)


def assert_live_locked() -> None:
    from app.config import get_settings

    settings = get_settings()
    if not settings.live_trading_locked:
        raise OperationalDatabaseIsolationError(
            "ISOLAMENTO TESTE: live_trading_locked deve ser True."
        )
    if settings.live_authorized:
        raise OperationalDatabaseIsolationError(
            "ISOLAMENTO TESTE: live_authorized não pode ser True."
        )
