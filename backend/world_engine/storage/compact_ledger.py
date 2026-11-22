"""Opt-in SQLite storage for the existing logical ledger contract.

Only an empty initialized database may switch layouts. The compatibility view
retains original posting IDs and amounts. A posting's timestamp is the immutable
commit timestamp of its event and is checked on every insert.
"""

from sqlalchemy import Connection
from sqlalchemy.exc import DBAPIError


_DDL = (
    """CREATE TABLE ledger_compact_events (
        event_key INTEGER PRIMARY KEY,
        event_id CHAR(32) NOT NULL UNIQUE REFERENCES world_events(event_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE ledger_compact_resources (
        resource_key INTEGER PRIMARY KEY,
        resource_id TEXT NOT NULL UNIQUE CHECK(length(resource_id) BETWEEN 1 AND 160)
    )""",
    """CREATE TABLE ledger_compact_postings (
        event_key INTEGER NOT NULL REFERENCES ledger_compact_events(event_key) ON DELETE CASCADE,
        posting_index INTEGER NOT NULL CHECK(posting_index >= 0),
        posting_uuid BLOB NOT NULL UNIQUE CHECK(length(posting_uuid) = 16),
        account_uuid BLOB NOT NULL CHECK(length(account_uuid) = 16),
        resource_key INTEGER NOT NULL REFERENCES ledger_compact_resources(resource_key),
        delta TEXT NOT NULL CHECK(length(delta) BETWEEN 1 AND 64),
        PRIMARY KEY(event_key, posting_index)
    ) WITHOUT ROWID""",
    """CREATE INDEX ix_ledger_compact_account_resource
        ON ledger_compact_postings(account_uuid, resource_key)""",
    "DROP TABLE ledger_postings",
    """CREATE VIEW ledger_postings AS
        SELECT lower(hex(p.posting_uuid)) AS posting_id, k.event_id, p.posting_index,
            lower(hex(p.account_uuid)) AS account_id, r.resource_id, p.delta, e.committed_at AS created_at
        FROM ledger_compact_events AS k
        JOIN world_events AS e ON e.event_id = k.event_id
        JOIN ledger_compact_postings AS p ON p.event_key = k.event_key
        JOIN ledger_compact_resources AS r ON r.resource_key = p.resource_key""",
    """CREATE TRIGGER ledger_compact_append INSTEAD OF INSERT ON ledger_postings BEGIN
        SELECT RAISE(ABORT, 'Ledger posting must match its committed event timestamp')
        WHERE NOT EXISTS (
            SELECT 1 FROM world_events e WHERE e.event_id = NEW.event_id AND e.committed_at = NEW.created_at
        );
        INSERT INTO ledger_compact_events(event_id) VALUES (NEW.event_id)
            ON CONFLICT(event_id) DO NOTHING;
        INSERT INTO ledger_compact_resources(resource_id) VALUES (NEW.resource_id)
            ON CONFLICT(resource_id) DO NOTHING;
        INSERT INTO ledger_compact_postings(event_key, posting_index, posting_uuid, account_uuid, resource_key, delta)
            SELECT e.event_key, NEW.posting_index, unhex(NEW.posting_id), unhex(NEW.account_id), r.resource_key, NEW.delta
            FROM ledger_compact_events AS e, ledger_compact_resources AS r
            WHERE e.event_id = NEW.event_id AND r.resource_id = NEW.resource_id;
    END""",
)


def enable_compact_ledger(connection: Connection) -> bool:
    """Install the alternate layout atomically, or confirm an existing installation."""
    if connection.dialect.name != "sqlite":
        raise ValueError("Compact ledger storage requires SQLite.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        existing = {name: kind for name, kind in connection.exec_driver_sql("""
            SELECT name, type FROM sqlite_schema
            WHERE name IN ('worlds', 'world_events', 'ledger_postings', 'ledger_compact_events',
                'ledger_compact_resources', 'ledger_compact_postings', 'ledger_compact_append',
                'ix_ledger_compact_account_resource')
        """)}
        compact_objects = {
            "ledger_compact_events": "table", "ledger_compact_resources": "table",
            "ledger_compact_postings": "table", "ledger_compact_append": "trigger",
            "ix_ledger_compact_account_resource": "index",
        }
        if existing.get("ledger_postings") == "view":
            if any(existing.get(name) != kind for name, kind in compact_objects.items()):
                raise ValueError("Compact ledger schema is incomplete.")
            connection.commit()
            return False
        if (
            any(existing.get(name) != "table" for name in ("worlds", "world_events", "ledger_postings"))
            or any(name in existing for name in compact_objects)
        ):
            raise ValueError("Expected an initialized world database with the original ledger schema.")
        for table in ("worlds", "world_events", "ledger_postings"):
            if connection.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").scalar() is not None:
                raise ValueError("Compact ledger installation requires an empty world database.")
        try:
            uuid_bytes = connection.exec_driver_sql("SELECT length(unhex('00112233445566778899aabbccddeeff'))").scalar()
        except DBAPIError:
            uuid_bytes = None
        if uuid_bytes != 16:
            raise ValueError("Compact ledger requires SQLite hexadecimal conversion support.")
        for statement in _DDL:
            connection.exec_driver_sql(statement)
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise
