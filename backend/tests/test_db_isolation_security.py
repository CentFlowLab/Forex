"""Segurança: isolamento estrutural da BD operacional e artefactos.

Não apaga dados operacionais. Não envia ordens MT5. Não chama OpenAI paga.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.db_isolation import (
    OperationalDatabaseIsolationError,
    assert_ai_responses_dir_allowed,
    assert_database_url_allowed,
    assert_live_locked,
    is_operational_db_path,
    operational_ai_responses_dir,
    operational_db_path,
)
from app.db_isolation import test_isolation_active as isolation_is_active


def test_isolation_flags_active():
    assert isolation_is_active() is True
    assert os.environ.get("GROK_TEST_ISOLATION") == "1"
    assert os.environ.get("APP_ENV") == "test"
    assert_live_locked()


def test_sessionlocal_is_not_operational():
    from app.database import engine

    url = str(engine.url)
    assert is_operational_db_path(url) is False
    assert "pytest_grokforex" in url or "grokforex_pytest_" in url
    assert_database_url_allowed(url, context="test_sessionlocal")


def test_operational_url_is_rejected_immediately():
    op = operational_db_path()
    url = f"sqlite:///{op.as_posix()}"
    with pytest.raises(OperationalDatabaseIsolationError, match="BD operacional"):
        assert_database_url_allowed(url, context="security_test")


def test_operational_relative_default_rejected():
    with pytest.raises(OperationalDatabaseIsolationError):
        assert_database_url_allowed("sqlite:///./data/grokforex.db", context="relative")


def test_operational_ai_responses_rejected():
    with pytest.raises(OperationalDatabaseIsolationError, match="ai_responses"):
        assert_ai_responses_dir_allowed(operational_ai_responses_dir(), context="sec")


def test_create_engine_to_operational_fails():
    op = operational_db_path()
    url = f"sqlite:///{op.as_posix()}"
    with pytest.raises(OperationalDatabaseIsolationError):
        # Mesmo caminho que um teste mal escrito usaria.
        from app.database import _resolve_sqlite_url

        _resolve_sqlite_url(url)


def test_subprocess_cannot_open_operational_db():
    """Subprocesso com isolamento ativo falha ao importar engine operacional."""
    backend = Path(__file__).resolve().parents[1]
    op = operational_db_path()
    script = textwrap.dedent(
        f"""
        import os, sys
        os.environ["GROK_TEST_ISOLATION"] = "1"
        os.environ["APP_ENV"] = "test"
        # Forçar URL operacional de propósito — deve falhar.
        os.environ["DATABASE_URL"] = "sqlite:///{op.as_posix()}"
        sys.path.insert(0, {str(backend)!r})
        try:
            import app.config as cfg
            cfg.get_settings.cache_clear()
            import app.database as dbmod
            print("FAIL_OPENED", dbmod.engine.url)
            sys.exit(2)
        except Exception as exc:
            name = type(exc).__name__
            if "OperationalDatabaseIsolationError" in name or "operacional" in str(exc).lower():
                print("BLOCKED_OK", name)
                sys.exit(0)
            print("OTHER", name, exc)
            sys.exit(3)
        """
    )
    env = os.environ.copy()
    env["GROK_TEST_ISOLATION"] = "1"
    env["APP_ENV"] = "test"
    env["DATABASE_URL"] = f"sqlite:///{op.as_posix()}"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(backend),
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "BLOCKED_OK" in proc.stdout


def test_subprocess_memory_db_allowed():
    backend = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import os, sys
        os.environ["GROK_TEST_ISOLATION"] = "1"
        os.environ["APP_ENV"] = "test"
        os.environ["DATABASE_URL"] = "sqlite://"
        sys.path.insert(0, os.environ["CLAIM_TEST_BACKEND"])
        import app.config as cfg
        cfg.get_settings.cache_clear()
        # :memory: / sqlite:// não é operacional
        from app.db_isolation import assert_database_url_allowed
        assert_database_url_allowed("sqlite://", context="mem")
        print("MEM_OK")
        """
    )
    env = os.environ.copy()
    env["GROK_TEST_ISOLATION"] = "1"
    env["CLAIM_TEST_BACKEND"] = str(backend)
    env["DATABASE_URL"] = "sqlite://"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(backend),
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MEM_OK" in proc.stdout


def test_live_remains_locked():
    from app.config import get_settings

    s = get_settings()
    assert s.live_trading_locked is True
    assert s.live_authorized is False
    assert s.trading_environment != "LIVE" or s.live_trading_locked


def test_response_store_not_operational(tmp_path, monkeypatch):
    from app.ai import response_store

    monkeypatch.setenv("GROK_AI_RESPONSES_DIR", str(tmp_path / "iso_ai"))
    d = response_store.store_dir()
    assert operational_ai_responses_dir() not in d.parents
    assert d.resolve() != operational_ai_responses_dir()


def test_paid_openai_http_blocked():
    import httpx

    with pytest.raises(RuntimeError, match="HTTP pago bloqueado"):
        with httpx.Client() as client:
            client.post("https://api.openai.com/v1/chat/completions", json={})
