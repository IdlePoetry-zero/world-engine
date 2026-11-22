from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from world_engine.domain.components import HealthComponent, LifeComponent
from world_engine.domain.errors import AccessDenied, InvalidEvent
from world_engine.domain.gameplay_events import LifeEndedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.player_continuation import (
    PLAYER_CONTINUE_AFTER_DEATH_COMMAND,
    plan_player_character_continuation,
)
from world_engine.storage.database import Database
from world_engine.storage.models import ActorControlRow
from world_engine.storage.repository import WorldRepository


def _continuation(repository, scope, created, branch, previous_id, target_id, source_id, *, causes):
    head = repository.read_world(scope, created.world.world_id, branch.branch_id)
    command_id = uuid4()
    event = plan_player_character_continuation(
        head.state,
        world_id=created.world.world_id,
        branch_id=branch.branch_id,
        command_id=command_id,
        ruleset_version=created.world.ruleset_version,
        actor_id=previous_id,
        previous_player_id=previous_id,
        new_player_id=target_id,
        source_death_event_id=source_id,
    )
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=previous_id,
        idempotency_key=f"player-continuation:{command_id}",
        expected_state_version=head.branch.state_version,
        command_type=PLAYER_CONTINUE_AFTER_DEATH_COMMAND,
        payload={
            "previous_player_id": str(previous_id),
            "new_player_id": str(target_id),
            "source_death_event_id": str(source_id),
        },
        causation_ids=causes,
    )
    return command, event


def _actor_controls(repository, world_id):
    with repository.database.session_factory() as session:
        return set(session.execute(
            select(ActorControlRow.user_id, ActorControlRow.actor_id).where(
                ActorControlRow.world_id == world_id,
            )
        ).all())


def test_player_continuation_is_owner_only_branch_local_and_death_causally_bound(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'player-continuation.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        previous_id, controlled_target_id, new_player_id, next_player_id = (uuid4() for _ in range(4))
        previous = WorldEntity(
            id=previous_id,
            kind="character",
            name="Former player",
            components={
                "life": LifeComponent(birth_tick=0, lifespan_ticks=10),
                "health": HealthComponent(current=100, maximum=100),
            },
        )
        controlled_target = WorldEntity(id=controlled_target_id, kind="character", name="Held by another user")
        new_player = WorldEntity(id=new_player_id, kind="character", name="New player")
        next_player = WorldEntity(id=next_player_id, kind="character", name="Second available player")
        created = repository.create_world(scope, NewWorld(
            title="Player continuation storage",
            state=WorldState(
                player_id=previous_id,
                entities={item.id: item for item in (previous, controlled_target, new_player, next_player)},
            ),
        ))
        world_id = created.world.world_id

        other_user_id = uuid4()
        repository.grant_membership(
            scope, world_id, other_user_id, MembershipRole.PLAYER,
            controlled_actor_ids=[controlled_target_id],
        )
        editor_id = uuid4()
        repository.grant_membership(scope, world_id, editor_id, MembershipRole.EDITOR)
        assert repository.unavailable_continuation_actor_ids(scope, world_id) == {controlled_target_id}
        with pytest.raises(AccessDenied, match="world owner"):
            repository.unavailable_continuation_actor_ids(
                AccessScope(tenant_id=scope.tenant_id, user_id=editor_id), world_id,
            )

        before_death = repository.fork(scope, world_id, created.branch.branch_id, 0, "before death")
        time_event = TimeAdvancedEvent(to_tick=10)
        death_event = LifeEndedEvent(
            entity_id=previous_id,
            expected_life_revision=1,
        )
        death_command = CommandEnvelope(
            actor_id=previous_id,
            idempotency_key="record-player-death",
            expected_state_version=0,
            command_type="simulation.death",
        )
        death_result = repository.commit_command(
            scope, world_id, created.branch.branch_id, death_command, [time_event, death_event],
        )
        after_death = repository.fork(
            scope, world_id, created.branch.branch_id, death_result.last_seq, "after death",
        )
        assert repository.find_death_events(
            scope, world_id, before_death.branch_id, {previous_id},
        ) == {}
        assert repository.find_death_events(
            scope, world_id, after_death.branch_id, {previous_id},
        ) == {previous_id: death_event.event_id}

        # A second, already-present cause is rejected: the death event is the only allowed cause.
        extra_cause_command, extra_cause_event = _continuation(
            repository, scope, created, after_death, previous_id, new_player_id,
            death_event.event_id, causes=[death_event.event_id, time_event.event_id],
        )
        with pytest.raises(InvalidEvent, match="exactly its source death event"):
            repository.commit_command(
                scope, world_id, after_death.branch_id, extra_cause_command, [extra_cause_event],
            )

        before_failed_write = repository.read_world(scope, world_id, after_death.branch_id)
        controls_before = _actor_controls(repository, world_id)
        blocked_command, blocked_event = _continuation(
            repository, scope, created, after_death, previous_id, controlled_target_id,
            death_event.event_id, causes=[death_event.event_id],
        )
        with pytest.raises(AccessDenied, match="controlled by another user"):
            repository.commit_command(
                scope, world_id, after_death.branch_id, blocked_command, [blocked_event],
            )
        assert repository.read_world(scope, world_id, after_death.branch_id).state_hash == before_failed_write.state_hash
        assert _actor_controls(repository, world_id) == controls_before

        command, event = _continuation(
            repository, scope, created, after_death, previous_id, new_player_id,
            death_event.event_id, causes=[death_event.event_id],
        )
        committed = repository.commit_command(
            scope, world_id, after_death.branch_id, command, [event],
        )
        current = repository.read_world(scope, world_id, after_death.branch_id)
        assert current.state.player_id == new_player_id
        assert current.state.entities[previous_id].status == "dead"
        assert repository.read_world(scope, world_id, created.branch.branch_id).state.player_id == previous_id
        assert repository.read_world(scope, world_id, before_death.branch_id).state.player_id == previous_id
        assert _actor_controls(repository, world_id) == controls_before

        repeated = repository.commit_command(
            scope, world_id, after_death.branch_id, command, [event],
        )
        assert repeated.duplicate is True
        assert repeated.state_version == committed.state_version
        assert repository.read_world(scope, world_id, after_death.branch_id).last_seq == committed.last_seq

        with pytest.raises(AccessDenied, match="world owner"):
            repository.commit_command(
                AccessScope(tenant_id=scope.tenant_id, user_id=editor_id),
                world_id,
                after_death.branch_id,
                command,
                [event],
            )
        assert repository.read_world(scope, world_id, after_death.branch_id).state_hash == committed.state_hash

        replayed = repository.replay(
            scope, world_id, after_death.branch_id, from_genesis=True,
        )
        assert replayed.state_hash == committed.state_hash
        assert replayed.state.player_id == new_player_id
    finally:
        database.dispose()
