"""Configuração global pytest — isolamento obrigatório da BD operacional.

Este ficheiro corre ANTES dos testes. Define GROK_TEST_ISOLATION e DATABASE_URL
para uma SQLite temporária da sessão, antes de importar a aplicação.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 1) Ambiente TEST — ANTES de qualquer import de app.*
# ---------------------------------------------------------------------------
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_OPERATIONAL_DB = (BACKEND / "data" / "grokforex.db").resolve()
_OPERATIONAL_AI = (BACKEND / "data" / "ai_responses").resolve()

# Diretório temporário da sessão de testes (partilhado por SessionLocal).
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="grokforex_pytest_"))
_TEST_DB = _TEST_ROOT / "pytest_grokforex.db"
_TEST_AI = _TEST_ROOT / "ai_responses"
_TEST_AI.mkdir(parents=True, exist_ok=True)

os.environ["GROK_TEST_ISOLATION"] = "1"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["GROK_AI_RESPONSES_DIR"] = str(_TEST_AI)
# Sem rede / sem custos / LIVE bloqueado.
os.environ["DUKASCOPY_QUOTE_FEED_ENABLED"] = "false"
os.environ["DUKASCOPY_TCP_ENABLED"] = "false"
os.environ["MT5_QUOTE_FEED_ENABLED"] = "false"
os.environ["AI_ENABLED"] = "false"
os.environ["AI_REQUIRE_MANUAL_APPROVAL"] = "true"
os.environ["OPENAI_API_KEY"] = ""
os.environ["XAI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["LIVE_TRADING_ENABLED"] = "false"
os.environ["LIVE_TRADING_LOCKED"] = "true"
os.environ["LIVE_MANUAL_AUTHORIZATION"] = ""
os.environ["TRADING_ENVIRONMENT"] = "PAPER"

# Expor caminhos para testes de segurança / subprocessos.
os.environ["GROK_TEST_DB_PATH"] = str(_TEST_DB)
os.environ["GROK_TEST_ROOT"] = str(_TEST_ROOT)
os.environ["GROK_OPERATIONAL_DB_PATH"] = str(_OPERATIONAL_DB)
os.environ["GROK_OPERATIONAL_AI_RESPONSES"] = str(_OPERATIONAL_AI)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Limpar cache de settings antes do primeiro import da app.
from app.config import get_settings

get_settings.cache_clear()

from app.database import Base, SessionLocal, engine, get_db, init_db  # noqa: E402
from app.db_isolation import (  # noqa: E402
    OperationalDatabaseIsolationError,
    assert_database_url_allowed,
    assert_live_locked,
    blocked_access_attempts,
    clear_blocked_access_attempts,
    is_operational_db_path,
    operational_ai_responses_dir,
    operational_db_path,
    test_isolation_active,
)
from app.main import app  # noqa: E402
from app.services.seed import seed_all  # noqa: E402

# Garantir que o engine global NÃO é a BD operacional.
assert test_isolation_active(), "GROK_TEST_ISOLATION deve estar ativo"
assert_database_url_allowed(str(engine.url), context="conftest.import")
assert not is_operational_db_path(str(engine.url)), str(engine.url)
assert_live_locked()
init_db()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ai_responses_fingerprint(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(directory.glob("*.json")):
        digest = _file_sha256(p)
        if digest:
            out[p.name] = digest
    return out


def _operational_fingerprint() -> dict[str, object]:
    db = operational_db_path()
    return {
        "db": _file_sha256(db),
        "db_wal": _file_sha256(Path(str(db) + "-wal")),
        "db_shm": _file_sha256(Path(str(db) + "-shm")),
        "ai_responses": _ai_responses_fingerprint(operational_ai_responses_dir()),
        "db_mtime_ns": db.stat().st_mtime_ns if db.is_file() else None,
        "db_size": db.stat().st_size if db.is_file() else None,
    }


@pytest.fixture(scope="session", autouse=True)
def _session_isolation_guard():
    """No início/fim da suite: BD operacional e ai_responses intactos."""
    clear_blocked_access_attempts()
    before = _operational_fingerprint()
    # Garantir SessionLocal da suite está inicializada e isolada.
    assert_database_url_allowed(str(engine.url), context="session_start")
    yield
    after = _operational_fingerprint()
    # Tentativas *bloqueadas* (raises do guard) são esperadas nos testes de
    # segurança — não são escritas. A prova de isolamento é o fingerprint.
    # Se o servidor uvicorn operacional estiver a correr em paralelo, o ficheiro
    # pode mudar — nesse caso falhamos com mensagem explícita.
    assert before["db"] == after["db"], (
        "HASH da BD operacional alterou-se durante a suite pytest. "
        "Isto indica escrita indevida OU um processo operacional (uvicorn) "
        "a modificar a BD em paralelo. Pare o servidor e volte a correr.\n"
        f"antes={before['db']} depois={after['db']}\n"
        f"blocked_attempts={blocked_access_attempts()}"
    )
    assert before["ai_responses"] == after["ai_responses"], (
        "Ficheiros em data/ai_responses operacionais foram alterados durante a suite."
    )
    assert before["db_size"] == after["db_size"]
    assert_database_url_allowed(str(engine.url), context="session_end")


@pytest.fixture(autouse=True)
def _block_real_mt5_orders_and_paid_openai(monkeypatch: pytest.MonkeyPatch):
    """Nenhum teste pode order_send no terminal real nem chamar OpenAI paga."""

    def _boom_order_send(*_a, **_k):
        raise RuntimeError(
            "ISOLAMENTO TESTE: order_send MT5 real bloqueado na suite pytest."
        )

    def _boom_paid(*_a, **_k):
        raise RuntimeError(
            "ISOLAMENTO TESTE: chamada OpenAI paga bloqueada na suite pytest."
        )

    # Gateway protegido da app.
    monkeypatch.setattr(
        "app.brokers.mt5_demo_executor.protected_order_send",
        _boom_order_send,
        raising=False,
    )
    # Se MetaTrader5 estiver instalado, bloquear símbolo global.
    try:
        import MetaTrader5 as mt5  # type: ignore

        monkeypatch.setattr(mt5, "order_send", _boom_order_send, raising=False)
    except Exception:  # noqa: BLE001
        pass

    # Providers HTTP — bloquear hosts pagos (TestClient ASGI local continua OK).
    try:
        import httpx

        real_request = httpx.Client.request

        def _guarded_request(self, method, url, *args, **kwargs):  # noqa: ANN001
            u = str(url)
            if any(
                host in u
                for host in ("api.openai.com", "api.x.ai", "api.anthropic.com")
            ):
                raise RuntimeError(f"ISOLAMENTO TESTE: HTTP pago bloqueado para {u}")
            return real_request(self, method, url, *args, **kwargs)

        monkeypatch.setattr(httpx.Client, "request", _guarded_request, raising=False)
    except Exception:  # noqa: BLE001
        pass

    # Registry complete — só se existir; testes com mock local podem sobrescrever.
    try:
        from app.ai.registry import get_provider

        _orig = get_provider

        def _guarded_provider(name: str):  # noqa: ANN001
            prov = _orig(name)
            if hasattr(prov, "complete"):
                orig_complete = prov.complete

                def _wrapped(*a, **k):
                    # Permitir se já estiver mockado pelo teste (MagicMock).
                    if type(prov).__name__ == "MagicMock" or getattr(
                        orig_complete, "_mock_name", None
                    ):
                        return orig_complete(*a, **k)
                    # Chamadas reais bloqueadas.
                    raise RuntimeError(
                        f"ISOLAMENTO TESTE: provider {name}.complete bloqueado"
                    )

                # Não substituir se o teste já fez monkeypatch fino — só documentar via httpx.
            return prov

        # Preferir bloqueio HTTP; não substituir get_provider (evita falsos negativos).
        _ = _guarded_provider
        _ = _boom_paid
    except Exception:  # noqa: BLE001
        pass

    yield


@pytest.fixture()
def db() -> Session:
    """BD em memória por teste — nunca a operacional, nunca a SessionLocal partilhada."""
    assert_database_url_allowed("sqlite://", context="fixture.db")
    mem = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(mem)
    SessionTesting = sessionmaker(bind=mem, autoflush=False, autocommit=False)
    session = SessionTesting()
    seed_all(session)
    try:
        yield session
    finally:
        session.close()
        mem.dispose()


@pytest.fixture()
def client(db: Session) -> TestClient:
    def _override() -> Session:
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def suite_db() -> Session:
    """Sessão na SQLite temporária da suite (SessionLocal isolada)."""
    assert_database_url_allowed(str(engine.url), context="fixture.suite_db")
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# Reexport para testes de segurança.
__all__ = [
    "OperationalDatabaseIsolationError",
    "assert_database_url_allowed",
    "operational_db_path",
    "is_operational_db_path",
]
