import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config

from world_engine.domain.models import AccessScope, NewWorld, WorldState
from world_engine.storage.repository import WorldRepository


def test_initial_migration_creates_repository_schema(tmp_path) -> None:
    database_path = tmp_path / "migrated.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config_path = "backend/migrations/alembic.ini"
    config = Config(config_path)
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    command.upgrade(config, "head")

    repository = WorldRepository(database_url)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        created = repository.create_world(scope, NewWorld(title="Migration test", state=WorldState()))
        reopened = repository.read_world(scope, created.world.world_id)
        assert reopened.world.title == "Migration test"
        assert reopened.branch.head_seq == 0
    finally:
        repository.database.dispose()

def test_movement_index_upgrades_existing_databases_and_serves_branch_lookup(tmp_path) -> None:
    database = tmp_path / "previous.db"
    config = Config("backend/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}".replace("%", "%%"))
    command.upgrade(config, "c8d04ac3f19a")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        ddl, = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'ix_events_direct_move_branch_sequence'"
        ).fetchone()
        assert "WHERE event_type = 'core.movement_completed.v1'" in ddl
        query = """EXPLAIN QUERY PLAN SELECT payload_json FROM world_events
                   WHERE world_id=? AND branch_id=? AND sequence<=? AND event_type=?
                     AND json_extract(payload_json,'$.actor_id')=? AND json_extract(payload_json,'$.to_place_id')=?
                   ORDER BY sequence LIMIT 1"""
        plan = connection.execute(query, ("world", "branch", 1000, "core.movement_completed.v1", "actor", "place"))
        assert any("ix_events_direct_move_branch_sequence" in row[-1] for row in plan)
    command.downgrade(config, "c8d04ac3f19a")
    with sqlite3.connect(database) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'ix_events_direct_move_branch_sequence'"
        ).fetchone()
