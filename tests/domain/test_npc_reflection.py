from __future__ import annotations

from uuid import uuid4, uuid5

import pytest

from world_engine.domain.components import (
    GoalRecord,
    KnowledgeRecord,
    ProvenanceReference,
    VisibilityScope,
)
from world_engine.domain.gameplay_events import InvestigationCaseClosedEvent, KnowledgeAcquiredEvent
from world_engine.domain.investigation import EvidenceAssessment, WEIGH_EVIDENCE_POLICY, assessment_text
from world_engine.domain.investigation_cases import (
    CaseEvidenceRequirement,
    CaseSourceReference,
    InvestigationCaseDefinition,
    InvestigationCaseFile,
    InvestigationCaseRoute,
    case_definition_digest,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_plan_events import NpcPlanRevisedEvent, npc_plan_event_id
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plan_review_events import (
    NpcPlanReviewResolvedEvent,
    _npc_plan_review_request_id_for_occurrence,
    apply_npc_plan_review_resolved,
    npc_plan_review_event_id,
)
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.npc_reflection import (
    NpcReflectionTrigger,
    project_npc_reflection_source,
    select_npc_reflection_trigger,
)


def _world_with_actor(*, tick: int = 120, actor_id=None, player_id=None) -> tuple[WorldState, object]:
    actor_id = actor_id or uuid4()
    entities = {actor_id: WorldEntity(id=actor_id, kind="person", name="Mara")}
    if player_id is not None and player_id != actor_id:
        entities[player_id] = WorldEntity(id=player_id, kind="person", name="Player")
    state = WorldState(current_tick=tick, entities=entities, player_id=player_id)
    return state, actor_id


def _goal_completion(
    actor_id,
    *,
    tick: int = 120,
    title: str = "Open the north road",
    goal_id=None,
    plan_id=None,
    evidence_id=None,
) -> tuple[NpcPlanRevisedEvent, GoalRecord, NpcPlan]:
    goal_id = goal_id or uuid4()
    plan_id = plan_id or uuid4()
    evidence_id = evidence_id or uuid4()
    goal = GoalRecord(
        id=goal_id,
        owner_id=actor_id,
        title=title,
        status="succeeded",
        progress=100,
        evidence_refs=[evidence_id],
        created_tick=0,
        updated_tick=tick,
    )
    plan = NpcPlan(
        plan_id=plan_id,
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=2,
        status="completed",
        created_tick=0,
        updated_tick=tick,
        steps=[NpcPlanStep(capability_id="world.action.travel.v1")],
        current_step=1,
        reason="The route was opened.",
        evidence_refs=[evidence_id],
    )
    command_id = uuid4()
    event = NpcPlanRevisedEvent(
        event_id=npc_plan_event_id(command_id, "test.rules", "revised", plan_id, 2),
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version="test.rules",
        plan=plan,
        expected_revision=1,
        fulfilled_goal_ids=(goal_id,),
    )
    return event, goal, plan


def _plan_review_defer(state: WorldState, actor_id) -> tuple[NpcPlanReviewResolvedEvent, WorldState]:
    world_id = uuid4()
    schedule = build_npc_plan_review_schedule(state, world_id, actor_id, state.current_tick)
    state_with_schedule = WorldState.model_validate(
        state.model_dump(mode="python") | {"scheduled_events": [schedule]},
    )
    command_id = uuid4()
    ruleset_version = "test.rules"
    request_id = _npc_plan_review_request_id_for_occurrence(
        command_id, ruleset_version, schedule.id, schedule.revision,
    )
    event = NpcPlanReviewResolvedEvent(
        event_id=npc_plan_review_event_id(
            command_id, ruleset_version, schedule.id, schedule.revision,
        ),
        world_id=world_id,
        branch_id=uuid4(),
        command_id=command_id,
        ruleset_version=ruleset_version,
        scheduled_event_id=schedule.id,
        expected_schedule_revision=schedule.revision,
        actor_id=actor_id,
        at_tick=state.current_tick,
        decision_request_id=request_id,
        based_on_state_version=0,
        source="model",
        outcome="defer",
        model_call_id="trace-123",
        reason="proposal_deferred",
    )
    return event, apply_npc_plan_review_resolved(state_with_schedule, event)


def _plan_review_ready(state: WorldState, actor_id) -> tuple[NpcPlanReviewResolvedEvent, WorldState]:
    world_id = uuid4()
    schedule = build_npc_plan_review_schedule(state, world_id, actor_id, state.current_tick)
    command_id = uuid4()
    ruleset_version = "test.rules"
    request_id = _npc_plan_review_request_id_for_occurrence(
        command_id, ruleset_version, schedule.id, schedule.revision,
    )
    plan_id = uuid5(request_id, f"npc.plan.v1:{actor_id}:0")
    goal_id = next(goal.id for goal in state.goals if goal.owner_id == actor_id and goal.status == "active")
    plan = NpcPlan(
        plan_id=plan_id,
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=1,
        status="ready",
        created_tick=state.current_tick,
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick + 30,
        steps=[NpcPlanStep(capability_id="world.action.travel.v1")],
        current_step=0,
        reason="Prepare the next journey.",
    )
    state_with_inputs = WorldState.model_validate(
        state.model_dump(mode="python") | {"scheduled_events": [schedule], "npc_plans": [plan]},
    )
    event = NpcPlanReviewResolvedEvent(
        event_id=npc_plan_review_event_id(
            command_id, ruleset_version, schedule.id, schedule.revision,
        ),
        world_id=world_id,
        branch_id=uuid4(),
        command_id=command_id,
        ruleset_version=ruleset_version,
        scheduled_event_id=schedule.id,
        expected_schedule_revision=schedule.revision,
        actor_id=actor_id,
        at_tick=state.current_tick,
        decision_request_id=request_id,
        based_on_state_version=0,
        source="model",
        outcome="ready",
        plan_id=plan_id,
        model_call_id="trace-ready",
        reason="plan_proposed",
    )
    return event, apply_npc_plan_review_resolved(state_with_inputs, event)


def _conflict_source(state: WorldState, actor_id) -> tuple[KnowledgeAcquiredEvent, WorldState]:
    subject_id = next(entity_id for entity_id in state.entities if entity_id != actor_id)
    observed = KnowledgeRecord(
        id=uuid4(),
        owner_id=actor_id,
        subject_id=subject_id,
        claim="The gate is open.",
        kind="observation",
        fact={"predicate": "entity_status", "value": "active", "effective_tick": state.current_tick},
        source_event_id=uuid4(),
    )
    claimed = KnowledgeRecord(
        id=uuid4(),
        owner_id=actor_id,
        subject_id=subject_id,
        claim="The gate is closed.",
        kind="belief",
        fact={"predicate": "entity_status", "value": "inactive", "effective_tick": state.current_tick},
        source_event_id=uuid4(),
    )
    assessment = EvidenceAssessment(
        finding="conflict",
        observation_id=observed.id,
        claim_id=claimed.id,
        predicate="entity_status",
        effective_tick=state.current_tick,
    )
    claim_text, summary = assessment_text(observed.fact, "conflict")
    record = KnowledgeRecord(
        id=uuid4(),
        owner_id=actor_id,
        subject_id=subject_id,
        claim=claim_text,
        summary=summary,
        source_refs=[observed.id, claimed.id],
        provenance=[ProvenanceReference(kind="rule", reference_id=WEIGH_EVIDENCE_POLICY)],
        received_tick=state.current_tick,
        valid_from_tick=state.current_tick,
        kind="belief",
        assessment=assessment,
        visibility=VisibilityScope(policy="private"),
    )
    event = KnowledgeAcquiredEvent(record=record)
    next_state = WorldState.model_validate(
        state.model_dump(mode="python") | {"knowledge": [observed, claimed, record]},
    )
    return event, next_state


def _contested_case(state: WorldState, actor_id) -> tuple[InvestigationCaseClosedEvent, WorldState]:
    suspect_a = uuid4()
    suspect_b = uuid4()
    anchor_id = uuid4()
    case_id = uuid4()
    entities = {
        **state.entities,
        suspect_a: WorldEntity(id=suspect_a, kind="person", name="Suspect A"),
        suspect_b: WorldEntity(id=suspect_b, kind="person", name="Suspect B"),
        anchor_id: WorldEntity(id=anchor_id, kind="item", components={}),
    }
    requirement_a = CaseEvidenceRequirement(
        subject_id=suspect_a,
        predicate="entity_status",
        effective_tick=0,
        finding="conflict",
        observation_provenance=[CaseSourceReference(kind="event", reference_id="witness-a")],
        claim_provenance=[CaseSourceReference(kind="event", reference_id="record-a")],
    )
    requirement_b = CaseEvidenceRequirement(
        subject_id=suspect_b,
        predicate="entity_status",
        effective_tick=0,
        finding="conflict",
        observation_provenance=[CaseSourceReference(kind="event", reference_id="witness-b")],
        claim_provenance=[CaseSourceReference(kind="event", reference_id="record-b")],
    )
    definition = InvestigationCaseDefinition(
        case_id=case_id,
        title="The missing seal",
        routes=[
            InvestigationCaseRoute(
                route_id="route_a", suspect_id=suspect_a, finding="supports", requirements=[requirement_a],
            ),
            InvestigationCaseRoute(
                route_id="route_b", suspect_id=suspect_b, finding="supports", requirements=[requirement_b],
            ),
        ],
    )
    entities[anchor_id] = WorldEntity(
        id=anchor_id,
        kind="item",
        components={"investigation_case": definition.model_dump(mode="json")},
    )
    case_file = InvestigationCaseFile(
        id=uuid4(),
        case_id=case_id,
        anchor_id=anchor_id,
        owner_id=actor_id,
        definition_hash=case_definition_digest(definition),
        title="Two contradictory accounts",
        opened_tick=0,
        updated_tick=state.current_tick,
        closed_tick=state.current_tick,
        status="closed",
        revision=2,
        outcome="contested",
    )
    next_state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {"entities": entities, "investigation_cases": [case_file]},
    )
    command_id = uuid4()
    ruleset_version = "test.rules"
    event = InvestigationCaseClosedEvent(
        event_id=uuid5(command_id, f"{ruleset_version}:investigation_case.closed"),
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        case_file_id=case_file.id,
        expected_revision=1,
        outcome="contested",
        closed_tick=state.current_tick,
    )
    return event, next_state


def test_reflection_modes_gate_goal_review_and_conflict_sources() -> None:
    state, actor_id = _world_with_actor()
    target = uuid4()
    state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {"entities": {**state.entities, target: WorldEntity(id=target, kind="person")},
           "goals": [GoalRecord(id=uuid4(), owner_id=actor_id, title="Current duty", created_tick=0)]},
    )
    review, state_with_review = _plan_review_defer(state, actor_id)
    conflict, state_with_conflict = _conflict_source(state, actor_id)

    assert select_npc_reflection_trigger(
        [review], state_with_review, mode="salient_events", externally_controlled_actor_ids=set(),
    ) is None
    deep = select_npc_reflection_trigger(
        [review], state_with_review, mode="salient_events_and_plans", externally_controlled_actor_ids=set(),
    )
    assert deep == NpcReflectionTrigger(
        event_id=review.event_id, actor_id=actor_id, tick=state.current_tick, kind="npc_plan_review",
    )
    assert select_npc_reflection_trigger(
        [conflict], state_with_conflict, mode="salient_events", externally_controlled_actor_ids=set(),
    ) is None
    exhaustive = select_npc_reflection_trigger(
        [conflict], state_with_conflict,
        mode="salient_events_plans_and_conflicts", externally_controlled_actor_ids=set(),
    )
    assert exhaustive is not None and exhaustive.kind == "evidence_conflict"
    assert select_npc_reflection_trigger(
        [review], state_with_review, mode="unknown", externally_controlled_actor_ids=set(),
    ) is None


def test_goal_completion_requires_actor_owned_succeeded_goal_in_exact_state() -> None:
    state, actor_id = _world_with_actor()
    event, goal, plan = _goal_completion(actor_id, tick=state.current_tick)
    completed_state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {"goals": [goal], "npc_plans": [plan]},
    )
    projected = project_npc_reflection_source(
        event, completed_state, externally_controlled_actor_ids=set(),
    )
    assert projected is not None
    assert projected.model_dump(mode="json") == {
        "event_id": str(event.event_id),
        "actor_id": str(actor_id),
        "tick": state.current_tick,
        "kind": "goal_completion",
        "status": "succeeded",
        "title": goal.title,
        "predicate": None,
        "effective_tick": None,
    }
    foreign_id = uuid4()
    foreign_state = completed_state.model_copy(
        update={"goals": [goal.model_copy(update={"owner_id": foreign_id})]},
    )
    assert project_npc_reflection_source(
        event, foreign_state, externally_controlled_actor_ids=set(),
    ) is None
    active_goal = goal.model_copy(update={"status": "active", "progress": 1})
    assert project_npc_reflection_source(
        event,
        WorldState.model_validate(state.model_dump(mode="python") | {"goals": [active_goal], "npc_plans": [plan]}),
        externally_controlled_actor_ids=set(),
    ) is None


def test_actor_authority_excludes_player_external_and_inactive_person() -> None:
    player_id = uuid4()
    state, actor_id = _world_with_actor(player_id=player_id)
    event, goal, plan = _goal_completion(actor_id, tick=state.current_tick)
    completed_state = WorldState.model_validate(
        state.model_dump(mode="python") | {"goals": [goal], "npc_plans": [plan]},
    )
    assert select_npc_reflection_trigger(
        [event], completed_state, mode="salient_events", externally_controlled_actor_ids={actor_id},
    ) is None

    player_event, player_goal, player_plan = _goal_completion(player_id, tick=state.current_tick)
    player_state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {"goals": [player_goal], "npc_plans": [player_plan]},
    )
    assert select_npc_reflection_trigger(
        [player_event], player_state, mode="salient_events", externally_controlled_actor_ids=set(),
    ) is None

    inactive_entities = dict(completed_state.entities)
    inactive_entities[actor_id] = WorldEntity(id=actor_id, kind="person", status="dead")
    inactive_state = WorldState.model_validate(
        completed_state.model_dump(mode="python") | {"entities": inactive_entities},
    )
    assert select_npc_reflection_trigger(
        [event], inactive_state, mode="salient_events", externally_controlled_actor_ids=set(),
    ) is None


def test_precedence_and_input_order_are_stable_and_duplicates_fail_closed() -> None:
    state, first_actor = _world_with_actor()
    second_actor = uuid4()
    target = uuid4()
    entities = {
        **state.entities,
        second_actor: WorldEntity(id=second_actor, kind="person"),
        target: WorldEntity(id=target, kind="person"),
    }
    first_event, first_goal, first_plan = _goal_completion(first_actor, tick=state.current_tick, title="First")
    second_event, second_goal, second_plan = _goal_completion(second_actor, tick=state.current_tick, title="Second")
    active_goal = GoalRecord(
        id=uuid4(), owner_id=first_actor, title="Unfinished duty", created_tick=0,
    )
    state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {
            "entities": entities,
            "goals": [first_goal, second_goal, active_goal],
            "npc_plans": sorted([first_plan, second_plan], key=lambda item: str(item.plan_id)),
        },
    )
    review, review_state = _plan_review_defer(state, first_actor)
    conflict, conflict_state = _conflict_source(state, first_actor)
    combined_state = WorldState.model_validate(
        review_state.model_dump(mode="python") | {"knowledge": conflict_state.knowledge},
    )
    selected = select_npc_reflection_trigger(
        [review, conflict, second_event, first_event], combined_state,
        mode="salient_events_plans_and_conflicts", externally_controlled_actor_ids=set(),
    )
    assert selected is not None and selected.event_id == second_event.event_id
    selected_conflict = select_npc_reflection_trigger(
        [review, conflict], combined_state,
        mode="salient_events_plans_and_conflicts", externally_controlled_actor_ids=set(),
    )
    assert selected_conflict is not None and selected_conflict.event_id == conflict.event_id
    selected_goal = select_npc_reflection_trigger(
        [review, first_event], review_state,
        mode="salient_events_and_plans", externally_controlled_actor_ids=set(),
    )
    assert selected_goal is not None and selected_goal.event_id == first_event.event_id
    duplicate = select_npc_reflection_trigger(
        [first_event, first_event], state, mode="salient_events", externally_controlled_actor_ids=set(),
    )
    assert duplicate is None


def test_typed_conflict_projection_omits_private_claims_and_source_references() -> None:
    state, actor_id = _world_with_actor()
    target = uuid4()
    state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {"entities": {**state.entities, target: WorldEntity(id=target, kind="person")}},
    )
    event, conflict_state = _conflict_source(state, actor_id)
    evidence = project_npc_reflection_source(
        event, conflict_state, externally_controlled_actor_ids=set(),
    )
    assert evidence is not None
    dumped = evidence.model_dump(mode="json")
    assert dumped["kind"] == "evidence_conflict"
    assert dumped["status"] == "conflict"
    assert dumped["predicate"] == "entity_status"
    assert dumped["effective_tick"] == state.current_tick
    assert "The gate is open." not in str(dumped)
    assert str(event.record.source_refs[0]) not in str(dumped)


def test_plan_review_requires_model_ready_or_defer_and_committed_schedule() -> None:
    state, actor_id = _world_with_actor()
    state = WorldState.model_validate(
        state.model_dump(mode="python")
        | {"goals": [GoalRecord(id=uuid4(), owner_id=actor_id, title="Current duty", created_tick=0)]},
    )
    event, committed_state = _plan_review_defer(state, actor_id)
    assert project_npc_reflection_source(
        event, committed_state, externally_controlled_actor_ids=set(),
    ) is not None
    ready_event, ready_state = _plan_review_ready(state, actor_id)
    ready_evidence = project_npc_reflection_source(
        ready_event, ready_state, externally_controlled_actor_ids=set(),
    )
    assert ready_evidence is not None and ready_evidence.status == "ready"
    policy_event = NpcPlanReviewResolvedEvent.model_validate(
        event.model_dump(mode="python") | {"source": "policy", "model_call_id": None},
    )
    assert project_npc_reflection_source(
        policy_event, committed_state, externally_controlled_actor_ids=set(),
    ) is None
    stale_state = WorldState.model_validate(
        committed_state.model_dump(mode="python") | {"scheduled_events": []},
    )
    assert project_npc_reflection_source(
        event, stale_state, externally_controlled_actor_ids=set(),
    ) is None


def test_private_actor_owned_contested_case_can_trigger_exhaustive_reflection() -> None:
    state, actor_id = _world_with_actor()
    event, contested_state = _contested_case(state, actor_id)
    evidence = project_npc_reflection_source(
        event, contested_state, externally_controlled_actor_ids=set(),
    )
    assert evidence is not None
    assert evidence.kind == "investigation_case_contested"
    assert evidence.status == "contested"
    assert evidence.title == "Two contradictory accounts"
    assert "brief" not in evidence.model_dump(mode="json")
    assert select_npc_reflection_trigger(
        [event], contested_state, mode="salient_events_and_plans", externally_controlled_actor_ids=set(),
    ) is None
    selected = select_npc_reflection_trigger(
        [event], contested_state,
        mode="salient_events_plans_and_conflicts", externally_controlled_actor_ids=set(),
    )
    assert selected is not None and selected.kind == "investigation_case_contested"

    public_event = InvestigationCaseClosedEvent.model_validate(
        event.model_dump(mode="python") | {"public_accusation": True, "suspect_id": uuid4()},
    )
    assert project_npc_reflection_source(
        public_event, contested_state, externally_controlled_actor_ids=set(),
    ) is None


def test_forged_or_untyped_source_events_are_ignored() -> None:
    state, actor_id = _world_with_actor()
    event, goal, plan = _goal_completion(actor_id, tick=state.current_tick)
    completed_state = WorldState.model_validate(
        state.model_dump(mode="python") | {"goals": [goal], "npc_plans": [plan]},
    )
    forged = event.model_copy(update={"visibility": {"policy": "public"}})
    assert project_npc_reflection_source(
        forged, completed_state, externally_controlled_actor_ids=set(),
    ) is None
    assert select_npc_reflection_trigger(
        [{"type": event.type, "event_id": str(event.event_id)}],
        completed_state,
        mode="salient_events",
        externally_controlled_actor_ids=set(),
    ) is None

