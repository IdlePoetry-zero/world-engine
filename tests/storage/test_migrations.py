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
