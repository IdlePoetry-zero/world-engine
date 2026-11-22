from io import BytesIO
from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.ruleset_migrations import require_ruleset_upgrade
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.versions import LEGACY_RULESET_VERSION, MAGIC_GATE_RULESET_VERSION
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.mark.parametrize(
    ("ruleset_version", "magic_enabled", "should_commit"),
    [
        (LEGACY_RULESET_VERSION, False, True),
        (MAGIC_GATE_RULESET_VERSION, True, True),
        (MAGIC_GATE_RULESET_VERSION, False, False),
    ],
)
def test_native_breakthrough_commit_enforces_the_pinned_ruleset(ruleset_version, magic_enabled, should_commit, tmp_path):
    database = Database("sqlite:///:memory:")
    try:
        database.create_schema()
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        actor_id, holder_id, sink_id, lot_id = (uuid4() for _ in range(4))
        state = WorldState(
            player_id=actor_id,
            metadata={
                "pack_id": "long_life_cultivation",
                "content_rules": {"magic": {"enabled": magic_enabled}},
            },
            entities={
                actor_id: WorldEntity(id=actor_id, kind="character", components={
                    "capabilities": {"grants": ["cultivation.practice"]},
                    "skills": {"skills": {"skill.cultivation_practice": {"level": "100"}}},
                    "progression": {},
                    "health": {"current": 50, "maximum": 100},
                    "inventory": {"lots": [{
                        "lot_id": str(lot_id), "account_id": str(holder_id),
                        "resource_id": "cultivation.qi", "quantity": "2",
                    }]},
                }),
            },
            resources={
                holder_id: ResourceAccount(resource_id="cultivation.qi", balance="2", owner_id=actor_id),
                sink_id: ResourceAccount(resource_id="cultivation.qi", balance="0", account_type="consumption_sink"),
            },
        )
        created = repository.create_world(scope, NewWorld(
            title="Breakthrough ruleset",
            state=state,
            ruleset_version=ruleset_version,
        ))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        intent = {"kind": "attempt_cultivation_breakthrough", "policy_id": "cultivation.qi_gathering.v1"}
        command = CommandEnvelope(
            actor_id=actor_id,
            command_type="action.attempt_cultivation_breakthrough",
            payload=intent,
            idempotency_key="native-breakthrough",
            expected_state_version=created.branch.state_version,
        )
        context = RuleContext(
            world_id=world_id, branch_id=branch_id, actor_id=actor_id,
            command_id=command.command_id, ruleset_version=ruleset_version,
        )
        enabled_state = state.model_copy(update={"metadata": {
            **state.metadata, "content_rules": {"magic": {"enabled": True}},
        }})
        events = RuleEngine().evaluate(enabled_state, intent, context).events
        assert any(event.type == "core.cultivation_breakthrough_resolved.v1" for event in events)

        if not should_commit:
            before = repository.read_world(scope, world_id, branch_id)
            with pytest.raises(InvalidEvent, match="disable cultivation breakthroughs"):
                repository.commit_command(scope, world_id, branch_id, command, events)
            after = repository.read_world(scope, world_id, branch_id)
            assert (after.state_hash, after.last_seq) == (before.state_hash, before.last_seq)
            assert after.branch.state_version == before.branch.state_version
            return

        with pytest.raises(InvalidEvent, match="exact registered time advance"):
            repository.commit_command(scope, world_id, branch_id, command, events[1:])
        committed = repository.commit_command(scope, world_id, branch_id, command, events)
        replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == committed.state_hash

        if ruleset_version == LEGACY_RULESET_VERSION:
            backup = BackupApplication(repository, scratch_directory=tmp_path / "backup-spool")
            archive = backup.export_world(scope, world_id, format_version="2.0")
            upgrade = require_ruleset_upgrade(LEGACY_RULESET_VERSION, MAGIC_GATE_RULESET_VERSION)
            with backup.validate_archive_file_streaming(BytesIO(archive)) as verified:
                destination = Database("sqlite:///:memory:")
                try:
                    destination.create_schema()
                    copied = WorldRepository(destination)
                    WorldBackupImporter(copied).import_verified_backup(scope, upgrade.bind_copy(verified))
                    restored = copied.read_world(scope, world_id, branch_id)
                    history = copied.list_events(scope, world_id, branch_id).events
                    assert restored.world.ruleset_version == MAGIC_GATE_RULESET_VERSION
                    assert restored.state_hash == committed.state_hash
                    assert {event.ruleset_version for event in history} == {LEGACY_RULESET_VERSION}
                    copied_replay = copied.replay(scope, world_id, branch_id, from_genesis=True)
                    assert copied_replay.state_hash == committed.state_hash
                finally:
                    destination.dispose()
    finally:
        database.dispose()
