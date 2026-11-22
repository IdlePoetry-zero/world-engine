from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, HealthComponent, LifeComponent, OfficeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import BackgroundDeprivationResolvedEvent, GovernanceSuccessionEvent, LifeEndedEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_deprivation_death_lookup_excludes_survivors_and_other_branches(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'deprivation-lineage.db').as_posix()}")
    try:
        database.create_schema()
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        actor, dead, survivor = (uuid4() for _ in range(3))
        boundary = 86_400
        state = WorldState(current_tick=boundary, player_id=actor, entities={
            actor: WorldEntity(id=actor, kind="character"),
            **{entity_id: WorldEntity(id=entity_id, kind="character", components={
                "health": {"current": health, "maximum": 10},
                "needs": {"food": {"value": 0, "last_critical_damage_tick": 0}},
            }) for entity_id, health in ((dead, 1), (survivor, 10))},
        })
        created = repository.create_world(scope, NewWorld(title="Evidence lineage", state=state))
        wid, bid = created.world.world_id, created.branch.branch_id
        before = repository.fork(scope, wid, bid, 0, "before")
        command = CommandEnvelope(actor_id=actor, idempotency_key="boundary", expected_state_version=0,
                                  command_type="simulation.window")
        event = BackgroundDeprivationResolvedEvent(
            actor_id=actor, resolved_tick=boundary,
            subjects=[{
                "entity_id": subject, "boundary_tick": boundary, "expected_needs_revision": 1,
                "expected_health_revision": 1, "expected_damage": 1,
                "terminal_status": "dead" if subject == dead else "active",
            } for subject in sorted((dead, survivor), key=str)],
            provenance=[{"kind": "rule", "reference_id": "background.deprivation"}],
        )
        wrong_actor = event.model_copy(update={"actor_id": uuid4()})
        with pytest.raises(InvalidEvent, match="command's acting character"):
            repository.commit_command(scope, wid, bid, command, [wrong_actor])
        committed = repository.commit_command(scope, wid, bid, command, [event])
        after = repository.fork(scope, wid, bid, committed.last_seq, "after")
        assert repository.find_death_events(scope, wid, before.branch_id, {dead, survivor}) == {}
        assert repository.find_death_events(scope, wid, after.branch_id, {dead, survivor}) == {dead: event.event_id}
        replayed = repository.replay(scope, wid, bid)
        assert replayed.state_hash == committed.state_hash
        assert replayed.state.entities[dead].status == "dead"
        assert replayed.state.entities[survivor].status == "active"
    finally:
        database.dispose()


def test_succession_must_cite_latest_committed_actual_death_event(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'succession-causation.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        tenant_id, owner_id = uuid4(), uuid4()
        scope = AccessScope(tenant_id=tenant_id, user_id=owner_id)
        predecessor_id, successor_id, organization_id = uuid4(), uuid4(), uuid4()
        death_event_id = uuid4()
        predecessor = WorldEntity(
            id=predecessor_id,
            kind="character",
            name="Former chair",
            components={
                "life": LifeComponent(birth_tick=0, lifespan_ticks=10),
                "health": HealthComponent(current=100, maximum=100),
            },
        )
        successor = WorldEntity(id=successor_id, kind="character", name="Successor")
        organization = WorldEntity(
            id=organization_id,
            kind="organization",
            name="Council",
            components={
                "governance": GovernanceComponent(
                    members=[predecessor_id, successor_id],
                    leader_id=predecessor_id,
                    offices=[OfficeRecord(office_id="office.leader", title="Chair", holder_id=predecessor_id)],
                    succession_policy="election",
                )
            },
        )
        created = repository.create_world(
            scope,
            NewWorld(
                title="Succession test",
                state=WorldState(entities={
                    predecessor_id: predecessor,
                    successor_id: successor,
                    organization_id: organization,
                }),
            ),
        )
        repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            CommandEnvelope(
                actor_id=successor_id,
                idempotency_key="record-death",
                expected_state_version=0,
                command_type="simulation.death",
            ),
            [
                TimeAdvancedEvent(to_tick=10),
                LifeEndedEvent(
                    event_id=death_event_id,
                    entity_id=predecessor_id,
                    expected_life_revision=1,
                ),
            ],
        )

        unrelated_event_id = uuid4()
        repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            CommandEnvelope(
                actor_id=successor_id,
                idempotency_key="later-tick",
                expected_state_version=1,
                command_type="world.tick",
            ),
            [TimeAdvancedEvent(event_id=unrelated_event_id, to_tick=11)],
        )

        forged_event = GovernanceSuccessionEvent(
            organization_id=organization_id,
            slot="leader",
            predecessor_id=predecessor_id,
            successor_id=successor_id,
            expected_governance_revision=1,
            decision_basis="election",
            decision_request_id=uuid4(),
            decision_option_id=f"leader_holder_{successor_id.hex}",
            linked_leadership_office_ids=["office.leader"],
            source_death_event_id=unrelated_event_id,
            evidence_refs=[predecessor_id, unrelated_event_id],
        )
        with pytest.raises(InvalidEvent, match="latest actual terminal event"):
            repository.commit_command(
                scope,
                created.world.world_id,
                created.branch.branch_id,
                CommandEnvelope(
                    actor_id=successor_id,
                    idempotency_key="forged-succession",
                    expected_state_version=2,
                    command_type="governance.succession",
                    causation_ids=[death_event_id, unrelated_event_id],
                ),
                [forged_event],
            )

        valid_event = forged_event.model_copy(
            update={
                "event_id": uuid4(),
                "source_death_event_id": death_event_id,
                "evidence_refs": [predecessor_id, death_event_id],
            }
        )
        result = repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            CommandEnvelope(
                actor_id=successor_id,
                idempotency_key="valid-succession",
                expected_state_version=2,
                command_type="governance.succession",
                causation_ids=[death_event_id],
            ),
            [valid_event],
        )
        assert result.state_version == 3
        view = repository.read_world(scope, created.world.world_id)
        governance = view.state.entities[organization_id].components["governance"]
        assert governance["leader_id"] == governance["offices"][0]["holder_id"] == str(successor_id)
        assert governance["offices"][0]["revision"] == 2
        replay = repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True)
        assert replay.state_hash == view.state_hash == result.state_hash
    finally:
        database.dispose()
