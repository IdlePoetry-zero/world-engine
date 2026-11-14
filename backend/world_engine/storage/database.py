"""SQLAlchemy engine and session setup for SQLite and compatible backends."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def create_engine_for_url(database_url: str, *, echo: bool = False) -> Engine:
    """Create a synchronous SQLAlchemy engine with safe SQLite defaults."""
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite:"):
        connect_args = {"check_same_thread": False, "timeout": 30.0}
    engine = create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
            finally:
                cursor.close()

    return engine


class Database:
    """Owns a SQLAlchemy engine and short-lived session factory."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.engine = create_engine_for_url(database_url, echo=echo)
        self.session_factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    def create_schema(self) -> None:
        """Create current metadata directly; intended for tests and scratch DBs only."""
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()
