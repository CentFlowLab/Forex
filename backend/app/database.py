"""SQLAlchemy engine, session and base model."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import BACKEND_DIR, get_settings
from app.db_isolation import (
    assert_database_url_allowed,
    is_operational_db_path,
    test_isolation_active,
)


class Base(DeclarativeBase):
    pass


def _resolve_sqlite_url(url: str) -> str:
    assert_database_url_allowed(url, context="_resolve_sqlite_url")
    if url.startswith("sqlite:///./"):
        relative = url.replace("sqlite:///./", "", 1)
        absolute = (BACKEND_DIR / relative).resolve()
        # Em teste, nunca criar o diretório operacional por engano.
        if test_isolation_active() and is_operational_db_path(absolute):
            assert_database_url_allowed(str(absolute), context="mkdir_operational")
        absolute.parent.mkdir(parents=True, exist_ok=True)
        resolved = f"sqlite:///{absolute.as_posix()}"
        assert_database_url_allowed(resolved, context="_resolve_sqlite_url.resolved")
        return resolved
    if url.startswith("sqlite:///"):
        path = Path(url.replace("sqlite:///", "", 1))
        if path.name not in {":memory:", ""}:
            assert_database_url_allowed(url, context="_resolve_sqlite_url.absolute")
            path.parent.mkdir(parents=True, exist_ok=True)
    return url


def _build_engine():
    settings = get_settings()
    db_url = _resolve_sqlite_url(settings.database_url)
    assert_database_url_allowed(db_url, context="create_engine")
    engine_kwargs: dict = {"echo": False, "future": True}
    if db_url.startswith("sqlite"):
        engine_kwargs.update(
            {
                "connect_args": {"check_same_thread": False, "timeout": 60.0},
                "poolclass": NullPool,
            }
        )
    else:
        engine_kwargs["connect_args"] = {"timeout": 60.0}
    eng = create_engine(db_url, **engine_kwargs)

    @event.listens_for(eng, "connect")
    def _sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        # Revalidar em cada ligação (subprocessos / engines reutilizados).
        assert_database_url_allowed(str(eng.url), context="engine.connect")
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return eng, db_url


engine, _db_url = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    assert_database_url_allowed(str(engine.url), context="get_db")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    assert_database_url_allowed(str(engine.url), context="init_db")
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()


def _ensure_sqlite_columns() -> None:
    """Migração leve para colunas novas em SQLite já existente."""
    if not str(engine.url).startswith("sqlite"):
        return
    assert_database_url_allowed(str(engine.url), context="_ensure_sqlite_columns")
    alterations = [
        ("download_jobs", "timeframe", "VARCHAR(8) DEFAULT 'M15'"),
        ("download_jobs", "cancel_requested", "BOOLEAN DEFAULT 0"),
        ("download_jobs", "hours_total", "INTEGER DEFAULT 0"),
        ("download_jobs", "hours_done", "INTEGER DEFAULT 0"),
        ("download_jobs", "resume_hour", "DATETIME"),
        ("adaptive_experiments", "candidates_total", "INTEGER DEFAULT 0"),
        ("adaptive_experiments", "progress_pct", "FLOAT DEFAULT 0"),
        ("adaptive_experiments", "progress_message", "TEXT DEFAULT ''"),
        ("adaptive_experiments", "started_at", "DATETIME"),
        ("autonomy_state", "run_status", "VARCHAR(24) DEFAULT 'off'"),
        ("robot_state", "decision_mode", "VARCHAR(24) DEFAULT 'simulated'"),
        ("robot_state", "last_justification", "TEXT DEFAULT ''"),
        ("robot_state", "last_ai_decision_id", "INTEGER DEFAULT 0"),
        ("robot_state", "paid_ai_calls", "INTEGER DEFAULT 0"),
    ]
    with engine.begin() as conn:
        for table, column, decl in alterations:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            names = {r[1] for r in rows}
            if column not in names:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
