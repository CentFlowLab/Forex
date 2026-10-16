"""Reserva atómica cross-process — dois processos Python, um único vencedor."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


WORKER = textwrap.dedent(
    r"""
    import os, sys, json
    from datetime import datetime, timezone
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    db_url = os.environ["CLAIM_TEST_DB"]
    backend = os.environ["CLAIM_TEST_BACKEND"]
    if backend not in sys.path:
        sys.path.insert(0, backend)

    from app.database import Base
    from app.models.account import AppSetting  # noqa: F401
    from app.models.robot import AIDecisionLog  # noqa: F401
    from app.robot.openai_autonomous import (
        FLAG_LAST_RUN_AT,
        FLAG_NEXT_AT,
        atomic_claim_autonomous_slot,
        _set_flag,
    )

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 60.0},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
        c.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    # Garantir flags limpas só no arranque do ficheiro (pai já limpou).
    now = datetime.now(timezone.utc)
    ok = atomic_claim_autonomous_slot(db, interval_s=900.0, now=now)
    print(json.dumps({"claimed": bool(ok), "pid": os.getpid()}))
    db.close()
    """
)


@pytest.fixture()
def claim_db_file(tmp_path: Path) -> Path:
    path = tmp_path / "claim_atomic.sqlite"
    # Criar schema + flags vazias no processo pai.
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.database import Base
    from app.models.account import AppSetting  # noqa: F401
    from app.models.robot import AIDecisionLog  # noqa: F401
    from app.robot.openai_autonomous import FLAG_LAST_RUN_AT, FLAG_NEXT_AT, _set_flag

    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 60.0},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):  # noqa: ANN001
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
        c.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    _set_flag(db, FLAG_LAST_RUN_AT, "")
    _set_flag(db, FLAG_NEXT_AT, "")
    db.close()
    engine.dispose()
    return path


def test_two_processes_only_one_claims(claim_db_file: Path) -> None:
    backend = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["CLAIM_TEST_DB"] = f"sqlite:///{claim_db_file.as_posix()}"
    env["CLAIM_TEST_BACKEND"] = str(backend)
    # Manter isolamento TEST: nunca cair no default operacional.
    env["GROK_TEST_ISOLATION"] = "1"
    env["APP_ENV"] = "test"
    # DATABASE_URL da suite pytest (temp) — o worker usa CLAIM_TEST_DB à parte.
    assert env.get("DATABASE_URL"), "DATABASE_URL de teste em falta no ambiente do pai"
    assert "grokforex.db" not in env["DATABASE_URL"] or "pytest_" in env["DATABASE_URL"]

    script = claim_db_file.parent / "claim_worker.py"
    script.write_text(WORKER, encoding="utf-8")

    procs = [
        subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(backend),
        )
        for _ in range(2)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, f"stderr={err!r} stdout={out!r}"
        results.append(__import__("json").loads(out.strip().splitlines()[-1]))

    claimed = [r for r in results if r["claimed"]]
    denied = [r for r in results if not r["claimed"]]
    assert len(claimed) == 1, results
    assert len(denied) == 1, results
