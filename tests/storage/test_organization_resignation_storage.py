from uuid import uuid4

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_member_resignation_commits_and_replays_as_a_public_versioned_event(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'member-resignation.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        actor_id, leader_id, organization_id = (uuid4() for _ in range(3))
        governance = GovernanceComponent(
            revision=3,
            members=[actor_id, leader_id],
            leader_id=leader_id,
            offices=[OfficeRecord(office_id="steward", title="Steward", holder_id=actor_id)],
        )
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        initial_state = WorldState(
            player_id=actor_id,
            entities={
                actor_id: WorldEntity(id=actor_id, kind="character", name="Member"),
                leader_id: WorldEntity(id=leader_id, kind="character", name="Leader"),
                organization_id: WorldEntity(
                    id=organization_id,
                    kind="organization",
                    name="Guild",
                    components={"governance": governance.model_dump(mode="json")},
                ),
            },
        )
        created = repository.create_world(scope, NewWorld(title="Member exit", state=initial_state))
        command = CommandEnvelope(
            actor_id=actor_id,
            idempotency_key="leave-guild",
            expected_state_version=0,
            command_type="player.action",
        )
        result = RuleEngine().evaluate(
            initial_state,
            {"kind": "leave_organization", "organization_id": str(organization_id)},
            RuleContext(
                world_id=created.world.world_id,
                branch_id=created.branch.branch_id,
                command_id=command.command_id,
                actor_id=actor_id,
                ruleset_version=created.world.ruleset_version,
            ),
        )
        assert result.outcome.status == "succeeded"
        committed = repository.commit_command(
            scope, created.world.world_id, created.branch.branch_id, command, result.events,
        )
        replay = repository.replay(scope, created.world.world_id, created.branch.branch_id)

        assert committed.state_hash == replay.state_hash
        assert committed.last_seq == replay.last_seq == 1
        persisted = repository.list_events(scope, created.world.world_id, created.branch.branch_id).events[0]
        assert persisted.type == "core.organization_member_resigned.v1"
        assert persisted.visibility == {"policy": "public"}
        assert replay.state_hash == state_hash(apply_event(initial_state, result.events[0]))
        assert replay.state_hash == committed.state_hash
        updated = GovernanceComponent.model_validate(replay.state.entities[organization_id].components["governance"])
        assert actor_id not in updated.members
        assert updated.offices[0].holder_id is None
    finally:
        database.dispose()
