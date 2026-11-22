from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from world_engine.domain.components import (
    DailyRoutineComponent,
    DailyRoutineWindow,
    GoalRecord,
    LifeComponent,
    ScheduledEventRecord,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_attendance import plan_npc_attendance_start
from world_engine.domain.npc_plans import ActiveNpcTraining, NpcPlan, NpcPlanStep
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    InvitationMeetingAttendance,
    RelationshipInvitation,
)
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.simulation import LongActionPlan, SimulationContext, SimulationEngine, WorldAdvancePlan
from world_engine.simulation.npc_meeting_attendance import (
    NpcMeetingAttendanceCandidate,
    next_npc_meeting_attendance_boundary,
    next_npc_meeting_attendance_candidate,
)


DAY_SECONDS = 86_400
YEAR_DAYS = 360
YEAR_SECONDS = DAY_SECONDS * YEAR_DAYS
WORLD_ID = UUID("01000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("02000000-0000-4000-8000-000000000002")
PLACE_ID = UUID("03000000-0000-4000-8000-000000000003")
OTHER_PLACE_ID = UUID("03000000-0000-4000-8000-000000000004")
PLAYER_ID = UUID("10000000-0000-4000-8000-000000000001")
NPC_A_ID = UUID("10000000-0000-4000-8000-000000000002")
NPC_B_ID = UUID("10000000-0000-4000-8000-000000000003")
NPC_C_ID = UUID("10000000-0000-4000-8000-000000000004")
INVITATION_A_ID = UUID("20000000-0000-4000-8000-000000000001")
INVITATION_B_ID = UUID("20000000-0000-4000-8000-000000000002")
INVITATION_C_ID = UUID("20000000-0000-4000-8000-000000000003")
WORK_EMPLOYER_ID = UUID("30000000-0000-4000-8000-000000000005")
WORK_POSTING_ID = UUID("40000000-0000-4000-8000-000000000001")
WORK_SOURCE_ID = UUID("50000000-0000-4000-8000-000000000001")
WORK_WINDOW_ID = UUID("60000000-0000-4000-8000-000000000001")
WORK_RESOURCE_ID = "core.silver"
JOURNEY_ID = UUID("70000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("80000000-0000-4000-8000-000000000001")
GOAL_ID = UUID("90000000-0000-4000-8000-000000000001")
PLAN_COMMAND_ID = UUID("a0000000-0000-4000-8000-000000000001")
RULESET_VERSION = "rules.test.v1"


def _character(actor_id: UUID, tick: int, *, location_id=PLACE_ID, status="active"):
    return WorldEntity(
        id=actor_id,
        kind="character",
        name=f"Person {actor_id.hex[-4:]}",
        location_id=location_id,
        status=status,
        components={"life": LifeComponent(
            birth_tick=tick - 25 * YEAR_SECONDS,
            life_stage="adult",
        ).model_dump(mode="python")},
    )


def _invitation(
    invitation_id: UUID,
    proposer_id: UUID,
    invitee_id: UUID,
    *,
    start_tick=100,
    end_tick=200,
    proposed_tick=0,
    updated_tick=0,
    status="accepted",
    revision=2,
):
    return RelationshipInvitation(
        invitation_id=invitation_id,
        proposer_id=proposer_id,
        invitee_id=invitee_id,
        status=status,
        proposed_tick=proposed_tick,
        updated_tick=updated_tick,
        revision=revision,
        meeting=InvitationMeeting(start_tick=start_tick, end_tick=end_tick),
    )


def _state(
    *,
    tick=100,
    player_id=PLAYER_ID,
    invitations=None,
    characters=None,
    **updates,
):
    entities = {
        PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Meeting place"),
        OTHER_PLACE_ID: WorldEntity(id=OTHER_PLACE_ID, kind="place", name="Other place"),
        PLAYER_ID: _character(PLAYER_ID, tick),
        NPC_A_ID: _character(NPC_A_ID, tick),
        NPC_B_ID: _character(NPC_B_ID, tick),
        NPC_C_ID: _character(NPC_C_ID, tick),
    }
    if characters:
        entities.update(characters)
    fields = {
        "current_tick": tick,
        "entities": entities,
        "player_id": player_id,
        "calendar": {"day_seconds": DAY_SECONDS, "year_days": YEAR_DAYS},
        "relationship_invitations": list(invitations or []),
    }
    fields.update(updates)
    return WorldState(**fields)


def _accepted_player_meeting(*, start_tick=100, end_tick=200, tick=100):
    return _state(
        tick=tick,
        invitations=[_invitation(
            INVITATION_A_ID, PLAYER_ID, NPC_A_ID,
            start_tick=start_tick, end_tick=end_tick,
        )],
    )


@pytest.mark.parametrize("target_tick", [100, 250])
def test_world_advance_keeps_meeting_start_at_or_before_its_target(target_tick):
    state = _accepted_player_meeting(tick=10)
    context = SimulationContext(
        world_id=WORLD_ID, branch_id=BRANCH_ID, command_id=PLAN_COMMAND_ID,
        command_actor_id=PLAYER_ID, state_version=0, world_seed="meeting",
        allow_autonomous_meeting_attendance=True,
    )
    plan = WorldAdvancePlan(target_tick=target_tick, started_tick=10)

    resolution = SimulationEngine().plan_window(state, plan, context)

    assert resolution.outcome == "advanced"
    assert resolution.next_tick == 100
    assert resolution.metrics.integration_boundary == "npc_meeting_start"
    assert not resolution.decision_requests
    assert resolution.continuation == plan


def test_wait_reaches_meeting_start_without_preempting_unauthorized_jobs():
    state = _accepted_player_meeting(tick=10)
    context = SimulationContext(
        world_id=WORLD_ID, branch_id=BRANCH_ID, command_id=PLAN_COMMAND_ID,
        command_actor_id=PLAYER_ID, state_version=0, world_seed="meeting",
    )
    plan = LongActionPlan(actor_id=PLAYER_ID, target_tick=250, started_tick=10, activity="wait")
    engine = SimulationEngine()

    unhosted = engine.plan_window(state, plan, context)
    hosted = engine.plan_window(state, plan, context.model_copy(update={
        "allow_autonomous_meeting_attendance": True,
    }))

    assert unhosted.next_tick == 250
    assert hosted.outcome == "advanced"
    assert hosted.next_tick == 100
    assert hosted.metrics.integration_boundary == "npc_meeting_start"


def _with_travel(state: WorldState, *, actor_id=NPC_A_ID, arrival_tick=150):
    journey = TravelJourney(
        journey_id=JOURNEY_ID,
        actor_id=actor_id,
        route_id=UUID("71000000-0000-4000-8000-000000000001"),
        from_place_id=OTHER_PLACE_ID,
        to_place_id=PLACE_ID,
        departed_tick=state.current_tick - 50,
        arrival_tick=arrival_tick,
        status="in_transit",
    )
    schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(journey.journey_id),
        due_tick=arrival_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        payload={"journey_id": str(journey.journey_id)},
    )
    characters = {actor_id: _character(actor_id, state.current_tick, location_id=None)}
    return WorldState.model_validate(state.model_dump(mode="python") | {
        "entities": {**state.entities, **characters},
        "travel_journeys": [journey],
        "scheduled_events": [schedule],
    })


def _arrived_from_travel(state: WorldState, *, tick=150, actor_id=NPC_A_ID, arrival_tick=150):
    journey = TravelJourney(
        journey_id=JOURNEY_ID,
        actor_id=actor_id,
        route_id=UUID("71000000-0000-4000-8000-000000000001"),
        from_place_id=OTHER_PLACE_ID,
        to_place_id=PLACE_ID,
        departed_tick=arrival_tick - 100,
        arrival_tick=arrival_tick,
        status="arrived",
        resolved_tick=arrival_tick,
    )
    schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(journey.journey_id),
        due_tick=arrival_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        status="completed",
        completed_runs=1,
        payload={"journey_id": str(journey.journey_id)},
    )
    characters = {actor_id: _character(actor_id, tick, location_id=PLACE_ID)}
    return WorldState.model_validate(state.model_dump(mode="python") | {
        "current_tick": tick,
        "entities": {**state.entities, **characters},
        "travel_journeys": [journey],
        "scheduled_events": [schedule],
    })


def _state_with_active_work(*, tick=3_600):
    source = OccupationSource(kind="event", reference_id=str(WORK_SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, NPC_A_ID, WORK_POSTING_ID),
        actor_id=NPC_A_ID,
        posting_id=WORK_POSTING_ID,
        employer_id=WORK_EMPLOYER_ID,
        location_id=PLACE_ID,
        title="Clerk",
        started_tick=0,
        source=source,
    )
    terms = WorkShiftTerms(
        posting_id=WORK_POSTING_ID,
        duration_seconds=3_600,
        resource_id=WORK_RESOURCE_ID,
        wage="12.50",
    )
    attendance_state = _state(
        tick=tick,
        invitations=[_invitation(
            INVITATION_A_ID, PLAYER_ID, NPC_A_ID,
            start_tick=3_600, end_tick=10_800,
        )],
        characters={
            NPC_A_ID: WorldEntity(
                id=NPC_A_ID,
                kind="character",
                name="Worker NPC",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=tick - 25 * YEAR_SECONDS,
                        life_stage="adult",
                    ).model_dump(mode="python"),
                    "daily_routine": DailyRoutineComponent(windows=[DailyRoutineWindow(
                        window_id=WORK_WINDOW_ID,
                        start_second=0,
                        duration_seconds=7_200,
                        activity="work",
                        place_id=PLACE_ID,
                    )]).model_dump(mode="python"),
                },
            ),
            WORK_EMPLOYER_ID: WorldEntity(
                id=WORK_EMPLOYER_ID,
                kind="company",
                name="Employer",
                location_id=PLACE_ID,
            ),
        },
        work_postings=[WorkPosting(
            posting_id=WORK_POSTING_ID,
            employer_id=WORK_EMPLOYER_ID,
            location_id=PLACE_ID,
            title="Clerk",
            slots=1,
            valid_from_tick=0,
        )],
        work_assignments=[assignment],
        metadata={"work_shift_terms": [terms.model_dump(mode="json")]},
    )
    attendance = plan_npc_attendance_start(attendance_state, assignment.assignment_id)
    assert attendance is not None
    payload = attendance_state.model_dump(mode="python")
    payload["npc_attendance"] = [attendance.model_dump(mode="python")]
    return WorldState.model_validate(payload)


def _state_with_active_timed_plan(*, tick=150):
    state = _accepted_player_meeting(start_tick=100, end_tick=250, tick=tick)
    goal = GoalRecord(
        id=GOAL_ID,
        owner_id=NPC_A_ID,
        title="Finish the current training",
        created_tick=0,
        updated_tick=0,
    )
    execution = ActiveNpcTraining(
        kind="training",
        place_id=PLACE_ID,
        skill_id="core.medicine",
        start_event_id=uuid5(
            PLAN_COMMAND_ID,
            f"{RULESET_VERSION}:npc_plan.revised:{PLAN_ID}:2",
        ),
        start_command_id=PLAN_COMMAND_ID,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        ruleset_version=RULESET_VERSION,
        started_tick=0,
        due_tick=200,
    )
    plan = NpcPlan(
        plan_id=PLAN_ID,
        actor_id=NPC_A_ID,
        goal_ids=[GOAL_ID],
        revision=2,
        status="running",
        created_tick=0,
        updated_tick=0,
        next_review_tick=200,
        steps=[NpcPlanStep(
            capability_id="world.action.train.v1",
            arguments={"skill_id": "core.medicine"},
        )],
        current_step=0,
        reason="Training is underway.",
        active_execution=execution,
    )
    return WorldState.model_validate(state.model_dump(mode="python") | {
        "goals": [goal],
        "npc_plans": [plan],
    })


def test_boundary_wakes_at_start_within_horizon_without_assuming_current_presence():
    state = _accepted_player_meeting(start_tick=100, end_tick=200, tick=0)

    assert next_npc_meeting_attendance_boundary(state, 99) is None
    assert next_npc_meeting_attendance_boundary(state, 100) == 100
    assert next_npc_meeting_attendance_boundary(state, 101) == 100

    # The player is never a possible autonomous actor. An appointment with no
    # hosted NPC participant does not wake the world advance.
    player_only = _state(
        tick=0,
        invitations=[_invitation(
            INVITATION_A_ID, PLAYER_ID, NPC_A_ID,
            start_tick=100, end_tick=200,
        )],
    )
    assert next_npc_meeting_attendance_boundary(
        player_only, 100, externally_controlled_actor_ids={NPC_A_ID},
    ) is None


def test_candidate_is_one_deterministic_hosted_participant_per_call():
    state = _state(
        invitations=[
            _invitation(INVITATION_A_ID, NPC_A_ID, NPC_B_ID),
            _invitation(INVITATION_B_ID, PLAYER_ID, NPC_C_ID),
        ],
    )

    candidate = next_npc_meeting_attendance_candidate(state)
    assert candidate == NpcMeetingAttendanceCandidate(
        invitation_id=INVITATION_A_ID,
        actor_id=NPC_A_ID,
        counterpart_id=NPC_B_ID,
        expected_invitation_revision=2,
        at_tick=100,
    )
    assert next_npc_meeting_attendance_candidate(
        state, attempted={(INVITATION_A_ID, NPC_A_ID)},
    ) == NpcMeetingAttendanceCandidate(
        invitation_id=INVITATION_A_ID,
        actor_id=NPC_B_ID,
        counterpart_id=NPC_A_ID,
        expected_invitation_revision=2,
        at_tick=100,
    )
    assert next_npc_meeting_attendance_candidate(
        state,
        externally_controlled_actor_ids={NPC_A_ID, NPC_B_ID, NPC_C_ID},
    ) is None
    assert next_npc_meeting_attendance_boundary(
        _state(invitations=[_invitation(INVITATION_A_ID, NPC_A_ID, NPC_B_ID)], tick=0),
        100,
        attempted={(INVITATION_A_ID, NPC_A_ID), (INVITATION_A_ID, NPC_B_ID)},
    ) is None


def test_candidate_uses_current_half_open_interval_and_attempt_keys():
    before_start = _accepted_player_meeting(start_tick=100, end_tick=200, tick=99)
    assert next_npc_meeting_attendance_candidate(before_start) is None

    at_start = _accepted_player_meeting(start_tick=100, end_tick=200, tick=100)
    assert next_npc_meeting_attendance_candidate(at_start) is not None
    inside = _accepted_player_meeting(start_tick=100, end_tick=200, tick=150)
    assert next_npc_meeting_attendance_candidate(inside).at_tick == 150
    at_end = _accepted_player_meeting(start_tick=100, end_tick=200, tick=200)
    assert next_npc_meeting_attendance_candidate(at_end) is None

    assert next_npc_meeting_attendance_candidate(
        inside, attempted={(INVITATION_A_ID, NPC_A_ID)},
    ) is None

    invitation = _invitation(INVITATION_A_ID, PLAYER_ID, NPC_A_ID)
    already_registered = invitation.model_copy(update={
        "meeting_attendance": [InvitationMeetingAttendance(actor_id=NPC_A_ID, attended_tick=100)],
    })
    recorded = _state(tick=100, invitations=[already_registered])
    assert next_npc_meeting_attendance_candidate(recorded) is None


def test_separation_or_in_transit_blocks_candidate_until_actual_arrival():
    state = _accepted_player_meeting(start_tick=100, end_tick=200, tick=100)
    separated = WorldState.model_validate(state.model_dump(mode="python") | {
        "entities": {
            **state.entities,
            NPC_A_ID: _character(NPC_A_ID, 100, location_id=OTHER_PLACE_ID),
        },
    })
    assert next_npc_meeting_attendance_candidate(separated) is None

    in_transit = _with_travel(state, actor_id=NPC_A_ID, arrival_tick=150)
    assert next_npc_meeting_attendance_candidate(in_transit) is None

    arrived = _arrived_from_travel(in_transit, tick=150, actor_id=NPC_A_ID, arrival_tick=150)
    candidate = next_npc_meeting_attendance_candidate(arrived)
    assert candidate is not None
    assert candidate.actor_id == NPC_A_ID
    assert candidate.at_tick == 150


def test_canonical_work_conflict_blocks_until_shift_end_with_half_open_end():
    working = _state_with_active_work(tick=3_600)
    assert next_npc_meeting_attendance_candidate(working) is None

    after_shift = WorldState.model_validate(working.model_dump(mode="python") | {
        "current_tick": 7_200,
    })
    candidate = next_npc_meeting_attendance_candidate(after_shift)
    assert candidate is not None
    assert candidate.actor_id == NPC_A_ID
    assert candidate.at_tick == 7_200


def test_active_timed_npc_plan_blocks_candidate_at_current_tick():
    state = _state_with_active_timed_plan()

    assert next_npc_meeting_attendance_candidate(state) is None


def test_unaccepted_or_ended_invitation_does_not_create_candidate_or_future_boundary():
    pending = _state(
        tick=50,
        invitations=[_invitation(
            INVITATION_A_ID, PLAYER_ID, NPC_A_ID,
            start_tick=100, end_tick=200, status="pending", revision=1, updated_tick=0,
        )],
    )
    assert next_npc_meeting_attendance_boundary(pending, 100) is None

    ended = _state(
        tick=100,
        invitations=[_invitation(
            INVITATION_A_ID, PLAYER_ID, NPC_A_ID,
            start_tick=100, end_tick=200, status="ended", revision=3,
        )],
    )
    assert next_npc_meeting_attendance_candidate(ended) is None
