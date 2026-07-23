"""SQLAlchemy engine / session setup."""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from .config import settings

log = logging.getLogger("db.slow_sql")

_database_settings = settings.database
_is_sqlite = _database_settings.url.startswith("sqlite")

_engine_kwargs = {
    "pool_pre_ping": True,
    "future": True,
}

if _is_sqlite:
    # Test/dev SQLite does not support QueuePool tuning in the same way as
    # PostgreSQL. StaticPool keeps in-memory DBs usable across sessions; file
    # SQLite avoids stale pooled handles with NullPool.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    _engine_kwargs["poolclass"] = StaticPool if _database_settings.url in {"sqlite://", "sqlite:///:memory:"} else NullPool
else:
    _engine_kwargs.update(
        {
            "poolclass": QueuePool,
            "pool_size": int(_database_settings.pool_size),
            "max_overflow": int(_database_settings.max_overflow),
            "pool_timeout": int(_database_settings.pool_timeout_seconds),
            "pool_recycle": int(_database_settings.pool_recycle_seconds),
        }
    )

engine = create_engine(
    _database_settings.url,
    **_engine_kwargs,
)


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    original_autocommit = getattr(dbapi_connection, "autocommit", None)
    if original_autocommit is not None:
        dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
        if original_autocommit is not None:
            dbapi_connection.autocommit = original_autocommit


def configure_sqlite_foreign_keys(target_engine) -> None:
    """Match PostgreSQL foreign-key enforcement on every SQLite connection."""
    if target_engine.dialect.name == "sqlite" and not event.contains(
        target_engine, "connect", _enable_sqlite_foreign_keys
    ):
        event.listen(target_engine, "connect", _enable_sqlite_foreign_keys)


configure_sqlite_foreign_keys(engine)


@event.listens_for(engine, "before_cursor_execute")
def _record_query_start(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
    context._query_start_time = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _log_slow_query(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
    threshold_ms = int(getattr(settings, "db_slow_query_threshold_ms", 0) or 0)
    if threshold_ms <= 0:
        return
    start = getattr(context, "_query_start_time", None)
    if start is None:
        return
    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms < threshold_ms:
        return
    compact_sql = " ".join(str(statement or "").split())
    log.warning(
        "slow SQL %.1fms threshold=%sms executemany=%s sql=%s",
        elapsed_ms,
        threshold_ms,
        bool(executemany),
        compact_sql[:1000],
    )


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
