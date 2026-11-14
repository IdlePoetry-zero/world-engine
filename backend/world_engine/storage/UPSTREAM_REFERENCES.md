# Upstream implementation notes

These references were checked on 2026-11-15. No upstream implementation code was copied.

## Adopted patterns

- SQLAlchemy sessions use a short `Session.begin()` context around each repository operation. SQLAlchemy commits when the context exits successfully and rolls back on an exception, which keeps event rows, projections, ledger postings, idempotency receipts, and outbox messages in one transaction: [Transactions and Connection Management](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html).
- The SQLite engine enables foreign-key enforcement, WAL, a bounded lock wait, and `synchronous=FULL`. SQLAlchemy documents SQLite driver transaction quirks and its WAL setup; SQLite documents that WAL permits concurrent readers but only one writer and is intended for processes on the same host, not network filesystems: [SQLAlchemy SQLite dialect](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html), [SQLite WAL](https://www.sqlite.org/wal.html).
- Migrations use self-contained Alembic operations generated from the first version of SQLAlchemy metadata, with the generated revision checked into `backend/migrations/versions/`. Alembic's tutorial documents revision scripts and `op.create_table()` as the migration interface: [Alembic Tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html).
- Domain inputs are parsed with Pydantic v2 `model_validate()` and a closed discriminated event registry; reducer state is serialized to JSON and revalidated so nested model instances cannot bypass validators: [Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/).

## Runtime note

The available Python 3.12.10 build reports SQLite 3.49.1. SQLite's official WAL documentation reports a rare WAL-reset race in versions through 3.51.2, fixed in 3.51.3 and later. The engine remains in WAL mode to match the architecture decision, so the packaged runtime should be moved to a fixed SQLite build before high-concurrency or long-run acceptance. The note is a runtime dependency finding, not evidence of a reproduced failure in the current tests.
