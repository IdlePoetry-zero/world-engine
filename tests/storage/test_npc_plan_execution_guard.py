from uuid import uuid4

import pytest

from world_engine.content import ContentCatalog
from world_engine.application.visibility import project_event, project_player_state
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, CommandEnvelope
from world_engine.domain.npc_plan_events import NpcPlanCreatedEvent, npc_plan_event_id
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _proposal(draft, command_id):
    goal = next(goal for goal in draft.state.goals
                if goal.owner_id != draft.state.player_id
                and draft.state.entities[goal.owner_id].kind == "character")
    plan = NpcPlan(
        plan_id=uuid4(), actor_id=goal.owner_id, goal_ids=[goal.id], revision=1,
        status="ready", created_tick=0, updated_tick=0, next_review_tick=60,
        steps=[NpcPlanStep(capability_id="core.travel.route", arguments={})],
        current_step=0, reason="A proposal awaiting validated capability execution.",
    )
    return NpcPlanCreatedEvent(
        event_id=npc_plan_event_id(command_id, draft.ruleset_version, "created", plan.plan_id, 1),
        world_id=draft.world_id, branch_id=draft.branch_id, command_id=command_id,
        actor_id=plan.actor_id, ruleset_version=draft.ruleset_version, plan=plan,
    )


@pytest.mark.parametrize("command_type", ["player.action", "author.create_npc_plan", "simulation.window"])
def test_direct_plan_commits_cannot_bypass_pending_execution_validation(tmp_path, command_type):
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        draft = ContentCatalog().build_world("modern_city", "Plan guard", "Player", seed=9)
        repository.create_world(scope, draft)
        before = repository.read_world(scope, draft.world_id, draft.branch_id)
        command = CommandEnvelope(
            actor_id=scope.user_id, command_type=command_type, idempotency_key=str(uuid4()),
            expected_state_version=before.branch.state_version,
        )
        event = _proposal(draft, command.command_id)
        # The typed reducer supports replay, but a caller cannot submit an
        # unregistered capability through a command name or fabricated receipt.
        proposed = apply_event(before.state, event)
        assert proposed.npc_plans[0] == event.plan
        player_view = project_player_state(proposed.model_dump(mode="json"), str(proposed.player_id))
        assert "npc_plans" not in player_view
        assert project_event({
            "event_id": str(event.event_id), "type": event.type,
            "visibility": event.visibility, "payload": event.model_dump(mode="json"),
        }, str(proposed.player_id)) is None
        with pytest.raises(InvalidEvent, match="registered simulation execution path"):
            repository.commit_command(scope, draft.world_id, draft.branch_id, command, [event])
        after = repository.read_world(scope, draft.world_id, draft.branch_id)
        assert (after.last_seq, after.state_hash, after.branch.state_version) == (
            before.last_seq, before.state_hash, before.branch.state_version,
        )
        assert not after.state.npc_plans
    finally:
        database.dispose()


def test_genesis_cannot_seed_an_unexecuted_plan_and_empty_field_preserves_hash(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        draft = ContentCatalog().build_world("modern_city", "Plan genesis guard", "Player", seed=9)
        assert "npc_plans" not in draft.state.model_dump(mode="json")
        original_hash = state_hash(draft.state)
        assert state_hash(draft.state.model_copy(update={"npc_plans": []})) == original_hash
        seeded = draft.model_copy(update={"state": apply_event(draft.state, _proposal(draft, uuid4()))})
        with pytest.raises(InvalidEvent, match="cannot seed NPC plans"):
            repository.create_world(scope, seeded)
    finally:
        database.dispose()
