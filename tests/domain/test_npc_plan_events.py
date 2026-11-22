from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.npc_plan_events import (
    NPC_PLAN_CREATED_EVENT_TYPE,
    NPC_PLAN_REVISED_EVENT_TYPE,
    NpcPlanCreatedEvent,
    NpcPlanRevisedEvent,
    npc_plan_event_id,
    parse_npc_plan_event,
)
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep


def _plan() -> NpcPlan:
    return NpcPlan(
        plan_id=uuid4(),
        actor_id=uuid4(),
        goal_ids=[uuid4()],
        revision=1,
        status="ready",
        created_tick=0,
        updated_tick=0,
        next_review_tick=30,
        steps=[NpcPlanStep(capability_id="core.travel.route", arguments={"route": "west"})],
        current_step=0,
        attempts=0,
        last_failure=None,
        reason="Reach the next known location.",
        evidence_refs=[],
    )


def _created(plan: NpcPlan, *, visibility=None, event_id=None) -> NpcPlanCreatedEvent:
    command_id = uuid4()
    return NpcPlanCreatedEvent.model_validate({
        "type": NPC_PLAN_CREATED_EVENT_TYPE,
        "event_id": event_id or npc_plan_event_id(
            command_id, "ruleset.test", "created", plan.plan_id, plan.revision,
        ),
        "world_id": uuid4(),
        "branch_id": uuid4(),
        "command_id": command_id,
        "actor_id": plan.actor_id,
        "ruleset_version": "ruleset.test",
        "plan": plan,
        **({"visibility": visibility} if visibility is not None else {}),
    })


def _revised(plan: NpcPlan, *, visibility=None, expected_revision=1, event_id=None) -> NpcPlanRevisedEvent:
    command_id = uuid4()
    return NpcPlanRevisedEvent.model_validate({
        "type": NPC_PLAN_REVISED_EVENT_TYPE,
        "event_id": event_id or npc_plan_event_id(
            command_id, "ruleset.test", "revised", plan.plan_id, plan.revision,
        ),
        "world_id": uuid4(),
        "branch_id": uuid4(),
        "command_id": command_id,
        "actor_id": plan.actor_id,
        "ruleset_version": "ruleset.test",
        "plan": plan,
        "expected_revision": expected_revision,
        **({"visibility": visibility} if visibility is not None else {}),
    })


def test_plan_event_ids_are_deterministic_and_revision_scoped() -> None:
    command_id = uuid4()
    plan_id = uuid4()
    created = npc_plan_event_id(command_id, "ruleset.test", "created", plan_id, 1)
    assert created == npc_plan_event_id(command_id, "ruleset.test", "created", plan_id, 1)
    assert created != npc_plan_event_id(command_id, "ruleset.test", "revised", plan_id, 1)
    assert created != npc_plan_event_id(command_id, "ruleset.test", "created", plan_id, 2)
    assert created != npc_plan_event_id(uuid4(), "ruleset.test", "created", plan_id, 1)
    with pytest.raises(ValueError, match="positive signed 64-bit"):
        npc_plan_event_id(command_id, "ruleset.test", "created", plan_id, 0)


def test_created_and_revised_events_parse_through_the_closed_discriminated_union() -> None:
    plan = _plan()
    created = _created(plan)
    assert created.visibility == {"policy": "author_only"}
    parsed_created = parse_npc_plan_event(created.model_dump(mode="python"))
    assert isinstance(parsed_created, NpcPlanCreatedEvent)
    assert parsed_created.plan == plan

    revised_plan = NpcPlan.model_validate({
        **plan.model_dump(mode="python"),
        "revision": 2,
        "status": "running",
        "next_review_tick": 60,
    })
    revised = _revised(revised_plan)
    assert revised.visibility == {"policy": "author_only"}
    parsed_revised = parse_npc_plan_event(revised.model_dump(mode="python"))
    assert isinstance(parsed_revised, NpcPlanRevisedEvent)
    assert parsed_revised.expected_revision == 1
    assert parsed_revised.plan.revision == 2

    with pytest.raises(ValidationError):
        parse_npc_plan_event({"type": "core.npc_plan_unknown.v1"})


@pytest.mark.parametrize("event_builder", (_created, _revised))
def test_plan_event_visibility_is_fixed_to_author_only(event_builder) -> None:
    plan = _plan()
    if event_builder is _revised:
        plan = NpcPlan.model_validate({
            **plan.model_dump(mode="python"),
            "revision": 2,
            "status": "running",
            "next_review_tick": 60,
        })
    with pytest.raises(ValidationError, match="fixed author_only visibility"):
        event_builder(plan, visibility={"policy": "public"})


def test_plan_event_ids_and_revision_numbers_are_validated() -> None:
    plan = _plan()
    with pytest.raises(ValidationError, match="bound to its command, plan, and revision"):
        _created(plan, event_id=uuid4())

    revised_plan = NpcPlan.model_validate({
        **plan.model_dump(mode="python"),
        "revision": 2,
        "status": "running",
        "next_review_tick": 60,
    })
    with pytest.raises(ValidationError, match="increment expected_revision exactly once"):
        _revised(revised_plan, expected_revision=2)
