from uuid import uuid4

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_handover_is_committed_and_replays_to_the_exact_leader_projection(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'governance-handover.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        incumbent_id, successor_id, organization_id = (uuid4() for _ in range(3))
        governance = GovernanceComponent(
            revision=2,
            members=[incumbent_id, successor_id],
            leader_id=incumbent_id,
            succession_policy="appointment",
            offices=[OfficeRecord(office_id="office.leader", title="Leader", holder_id=incumbent_id)],
        )
        initial_state = WorldState(player_id=incumbent_id, entities={
            incumbent_id: WorldEntity(id=incumbent_id, kind="character", name="Incumbent"),
            successor_id: WorldEntity(id=successor_id, kind="character", name="Successor"),
            organization_id: WorldEntity(id=organization_id, kind="organization", components={
                "governance": governance.model_dump(mode="json"),
            }),
        })
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        created = repository.create_world(scope, NewWorld(title="Appointment handover", state=initial_state))
        command = CommandEnvelope(
            actor_id=incumbent_id,
            idempotency_key="leadership-handover",
            expected_state_version=0,
            command_type="player.action",
        )
        result = RuleEngine().evaluate(
            initial_state,
            {"kind": "handover_leadership", "organization_id": str(organization_id), "successor_id": str(successor_id)},
            RuleContext(
                world_id=created.world.world_id,
                branch_id=created.branch.branch_id,
                command_id=command.command_id,
                actor_id=incumbent_id,
                ruleset_version=created.world.ruleset_version,
            ),
        )
        committed = repository.commit_command(scope, created.world.world_id, created.branch.branch_id, command, result.events)
        replay = repository.replay(scope, created.world.world_id, created.branch.branch_id)

        assert committed.last_seq == replay.last_seq == 1
        assert committed.state_hash == replay.state_hash == state_hash(apply_event(initial_state, result.events[0]))
        event_record = repository.list_events(scope, created.world.world_id, created.branch.branch_id).events[0]
        assert event_record.type == "core.governance_leadership_handed_over.v1"
        assert event_record.visibility == {"policy": "public"}
        assert replay.state.entities[organization_id].components["governance"]["leader_id"] == str(successor_id)
    finally:
        database.dispose()
