"""Database session handling."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def init_engine(database_url: Optional[str] = None) -> Engine:
    global _engine, _session_factory
    url = database_url or get_settings().database_url
    kwargs = {"future": True}
    if url.startswith("sqlite"):
        # The control plane runs scenarios in background tasks on the same
        # process, so more than one thread touches the session factory.
        # SQLite's default same-thread check would refuse that.
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # WAL lets the run thread write live statistics while requests read,
        # busy_timeout turns "database is locked" into a short wait, and
        # foreign_keys makes ON DELETE SET NULL on runs.target_id real: SQLite
        # ignores it otherwise, and SQLAlchemy would try to null a NOT NULL
        # column instead.
        @event.listens_for(_engine, "connect")
        def _pragmas(connection, _record):  # noqa: ANN001
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Additive migration: columns the model has that the database does not.

    create_all creates missing tables and nothing else, so a column added to
    a model after a deployment has stored data would fail every query. This
    adds them with their defaults. Removing or retyping a column is not
    covered and would need a real migration, which this schema has not needed.
    """
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                kind = column.type.compile(dialect=engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {kind}')
                )


_init_lock = threading.Lock()


def get_engine() -> Engine:
    # Under a lock: a fleet supervisor thread and a request can both find the
    # engine missing at the same instant, and two concurrent create_all calls
    # on one SQLite file race each other into "table already exists".
    if _engine is None:
        with _init_lock:
            if _engine is None:
                init_engine()
    assert _engine is not None
    return _engine


def reset_engine() -> None:
    """Test seam."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
