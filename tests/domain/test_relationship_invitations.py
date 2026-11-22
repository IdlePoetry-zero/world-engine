from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.domain.components import (
    DailyRoutineComponent,
    DailyRoutineWindow,
    LifeComponent,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_attendance import (
    NpcAttendance,
    npc_attendance_is_valid,
    plan_npc_attendance_start,
)
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.relationship_reflections import MeetingReflection
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    InvitationMeetingUnavailable,
    RelationshipInvitation,
    accept_relationship_invitation,
    attend_relationship_meeting,
    decline_relationship_invitation,
    end_relationship_invitation,
    invitation_meeting_available,
    propose_relationship_invitation,
    relationship_invitation_blocks_pair_at_tick,
    withdraw_relationship_invitation,
)
from world_engine.domain.work_shift import WorkShiftTerms


DAY_SECONDS = 86_400
YEAR_DAYS = 360
YEAR_SECONDS = DAY_SECONDS * YEAR_DAYS
PLACE_A = UUID("a1000000-0000-4000-8000-000000000001")
PLACE_B = UUID("a1000000-0000-4000-8000-000000000002")
ALICE = UUID("a2000000-0000-4000-8000-000000000001")
BOB = UUID("a2000000-0000-4000-8000-000000000002")
CASEY = UUID("a2000000-0000-4000-8000-000000000003")
WORK_SHIFT_SECONDS = 3_600
WORK_EMPLOYER = UUID("b1000000-0000-4000-8000-000000000001")
WORK_POSTING = UUID("b2000000-0000-4000-8000-000000000002")
WORK_WINDOW = UUID("b3000000-0000-4000-8000-000000000003")
WORK_SOURCE = OccupationSource(kind="genesis", reference_id="relationship-invitation-work-fixture")
WORK_ATTENDANCE_RESOURCE = "core.silver"


def _character(entity_id: UUID, name: str, *, tick: int, age_years: int = 20,
               life_stage: str = "adult", location_id: UUID = PLACE_A, status: str = "active"):
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=location_id,
        status=status,
        components={
            "life": LifeComponent(
                birth_tick=tick - age_years * YEAR_SECONDS,
                life_stage=life_stage,
            ).model_dump(mode="json"),
        },
    )


def _state(*, tick: int = 0, alice=None, bob=None, casey=None) -> WorldState:
    entities = {
        PLACE_A: WorldEntity(id=PLACE_A, kind="place", name="Station"),
        PLACE_B: WorldEntity(id=PLACE_B, kind="place", name="Clinic"),
        ALICE: alice or _character(ALICE, "Alice", tick=tick),
        BOB: bob or _character(BOB, "Bob", tick=tick),
        CASEY: casey or _character(CASEY, "Casey", tick=tick),
    }
    return WorldState(
        current_tick=tick,
        entities=entities,
        calendar={"day_seconds": DAY_SECONDS, "year_days": YEAR_DAYS},
    )


def _propose(state: WorldState, *, proposer=ALICE, invitee=BOB, records=(), ident=None, meeting=None):
    return propose_relationship_invitation(
        state,
        records,
        invitation_id=ident or UUID("a3000000-0000-4000-8000-000000000001"),
        proposer_id=proposer,
        invitee_id=invitee,
        meeting=meeting,
    )


def _at_tick(state: WorldState, tick: int) -> WorldState:
    raw = state.model_dump(mode="python")
    raw["current_tick"] = tick
    return WorldState.model_validate(raw)


def _move_actor(state: WorldState, actor_id: UUID, location_id: UUID) -> WorldState:
    raw = state.model_dump(mode="python")
    raw["entities"][actor_id]["location_id"] = location_id
    return WorldState.model_validate(raw)


def _state_with_work_attendance(*, worker_id: UUID = ALICE) -> tuple[WorldState, NpcAttendance]:
    """Build a current, valid attendance from registered terms and a work routine."""
    state = _state(tick=WORK_SHIFT_SECONDS)
    payload = state.model_dump(mode="python")
    payload["entities"][WORK_EMPLOYER] = WorldEntity(
        id=WORK_EMPLOYER,
        kind="company",
        name="Employer",
        location_id=PLACE_A,
        status="active",
    ).model_dump(mode="python")
    payload["entities"][worker_id]["components"]["daily_routine"] = DailyRoutineComponent(
        windows=[DailyRoutineWindow(
            window_id=WORK_WINDOW,
            start_second=0,
            duration_seconds=2 * WORK_SHIFT_SECONDS,
            activity="work",
            place_id=PLACE_A,
        )],
    ).model_dump(mode="python")

    posting = WorkPosting(
        posting_id=WORK_POSTING,
        employer_id=WORK_EMPLOYER,
        location_id=PLACE_A,
        title="Clerk",
        slots=1,
        valid_from_tick=0,
        revision=4,
    )
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(WORK_SOURCE, worker_id, WORK_POSTING),
        actor_id=worker_id,
        posting_id=WORK_POSTING,
        employer_id=WORK_EMPLOYER,
        location_id=PLACE_A,
        title="Clerk",
        started_tick=0,
        revision=2,
        source=WORK_SOURCE,
    )
    terms = WorkShiftTerms(
        posting_id=WORK_POSTING,
        duration_seconds=WORK_SHIFT_SECONDS,
        resource_id=WORK_ATTENDANCE_RESOURCE,
        wage="12.50",
    )
    payload["work_postings"] = [posting.model_dump(mode="python")]
    payload["work_assignments"] = [assignment.model_dump(mode="python")]
    payload["metadata"] = {"work_shift_terms": [terms.model_dump(mode="json")]}
    state = WorldState.model_validate(payload)

    attendance = plan_npc_attendance_start(state, assignment.assignment_id)
    assert attendance is not None
    assert npc_attendance_is_valid(state, attendance)
    payload = state.model_dump(mode="python")
    payload["npc_attendance"] = [attendance.model_dump(mode="python")]
    return WorldState.model_validate(payload), attendance


def _state_with_invitation(state: WorldState, invitation: RelationshipInvitation) -> WorldState:
    payload = state.model_dump(mode="python")
    payload["relationship_invitations"] = [invitation.model_dump(mode="python")]
    return WorldState.model_validate(payload)


def _accepted_timed_invitation(
    state: WorldState,
    *,
    interval: tuple[int, int],
    ident: UUID,
) -> RelationshipInvitation:
    proposed = _propose(
        state,
        ident=ident,
        meeting=InvitationMeeting(start_tick=interval[0], end_tick=interval[1]),
    )
    return accept_relationship_invitation(
        state,
        [proposed],
        invitation_id=proposed.invitation_id,
        actor_id=BOB,
        expected_revision=1,
    )


def test_invitee_must_explicitly_accept_and_either_participant_can_end_when_apart():
    state = _state()
    proposed = _propose(state)

    assert proposed.status == "pending"
    assert proposed.revision == 1
    assert proposed.proposed_tick == proposed.updated_tick == 0
    with pytest.raises(InvalidEvent, match="Only the invitee"):
        accept_relationship_invitation(
            state, [proposed], invitation_id=proposed.invitation_id,
            actor_id=ALICE, expected_revision=1,
        )

    accepted = accept_relationship_invitation(
        state, [proposed], invitation_id=proposed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    assert accepted.status == "accepted"
    assert accepted.revision == 2

    apart = _move_actor(_at_tick(state, 30), BOB, PLACE_B)
    ended = end_relationship_invitation(
        apart, [accepted], invitation_id=accepted.invitation_id,
        actor_id=ALICE, expected_revision=2,
    )
    assert ended.status == "ended"
    assert ended.revision == 3
    assert ended.updated_tick == 30


def test_only_invitee_can_decline_and_only_proposer_can_withdraw_pending_invitation():
    state = _state()
    proposed = _propose(state)

    with pytest.raises(InvalidEvent, match="Only the invitee"):
        decline_relationship_invitation(
            state, [proposed], invitation_id=proposed.invitation_id,
            actor_id=ALICE, expected_revision=1,
        )
    declined = decline_relationship_invitation(
        state, [proposed], invitation_id=proposed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    assert declined.status == "declined"
    assert declined.revision == 2

    second = _propose(state, ident=UUID("a3000000-0000-4000-8000-000000000002"))
    with pytest.raises(InvalidEvent, match="Only the proposer"):
        withdraw_relationship_invitation(
            state, [second], invitation_id=second.invitation_id,
            actor_id=BOB, expected_revision=1,
        )
    withdrawn = withdraw_relationship_invitation(
        state, [second], invitation_id=second.invitation_id,
        actor_id=ALICE, expected_revision=1,
    )
    assert withdrawn.status == "withdrawn"
    assert withdrawn.revision == 2


def test_proposal_requires_two_active_adults_at_the_same_active_place():
    too_young = _character(BOB, "Bob", tick=0, age_years=17, life_stage="adult")
    with pytest.raises(InvalidEvent, match="at least 18 world years"):
        _propose(_state(bob=too_young))

    child_stage = _character(BOB, "Bob", tick=0, age_years=20, life_stage="child")
    with pytest.raises(InvalidEvent, match="at least 18 world years"):
        _propose(_state(bob=child_stage))

    remote = _character(BOB, "Bob", tick=0, location_id=PLACE_B)
    with pytest.raises(InvalidEvent, match="co-located"):
        _propose(_state(bob=remote))

    inactive = _character(BOB, "Bob", tick=0, status="retired")
    with pytest.raises(InvalidEvent, match="active characters"):
        _propose(_state(bob=inactive))


def test_response_rechecks_adulthood_and_co_location_at_response_time():
    state = _state()
    proposed = _propose(state)
    apart = _move_actor(_at_tick(state, 20), BOB, PLACE_B)
    with pytest.raises(InvalidEvent, match="co-located"):
        accept_relationship_invitation(
            apart, [proposed], invitation_id=proposed.invitation_id,
            actor_id=BOB, expected_revision=1,
        )


def test_only_one_pending_or_accepted_record_per_unordered_pair_but_other_pairs_are_independent():
    state = _state()
    first = _propose(state)

    with pytest.raises(InvalidEvent, match="pending or accepted"):
        _propose(
            state,
            proposer=BOB,
            invitee=ALICE,
            ident=UUID("a3000000-0000-4000-8000-000000000002"),
            records=[first],
        )

    other_pair = _propose(
        state,
        proposer=ALICE,
        invitee=CASEY,
        ident=UUID("a3000000-0000-4000-8000-000000000003"),
        records=[first],
    )
    accepted = accept_relationship_invitation(
        state, [first], invitation_id=first.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    with pytest.raises(InvalidEvent, match="pending or accepted"):
        _propose(
            state,
            proposer=ALICE,
            invitee=BOB,
            ident=UUID("a3000000-0000-4000-8000-000000000004"),
            records=[accepted, other_pair],
        )

    ended = end_relationship_invitation(
        state, [accepted], invitation_id=accepted.invitation_id,
        actor_id=BOB, expected_revision=2,
    )
    reproposed = _propose(
        state,
        proposer=BOB,
        invitee=ALICE,
        ident=UUID("a3000000-0000-4000-8000-000000000005"),
        records=[ended, other_pair],
    )
    assert reproposed.status == "pending"


def test_timed_pair_availability_expires_at_pending_cutoff_and_meeting_end():
    state = _state()
    pending = _propose(state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    accepted = accept_relationship_invitation(
        state, [pending], invitation_id=pending.invitation_id,
        actor_id=BOB, expected_revision=1,
    )

    assert relationship_invitation_blocks_pair_at_tick(pending, 99)
    assert not relationship_invitation_blocks_pair_at_tick(pending, 100)
    assert relationship_invitation_blocks_pair_at_tick(accepted, 199)
    assert not relationship_invitation_blocks_pair_at_tick(accepted, 200)


def test_expired_pending_meeting_allows_successive_date_but_cannot_be_accepted_late():
    initial = _state()
    first = _propose(initial, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    current = _at_tick(_state_with_invitation(initial, first), 100)

    second = _propose(
        current,
        records=current.relationship_invitations,
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=300, end_tick=400),
    )
    with pytest.raises(InvitationMeetingUnavailable):
        accept_relationship_invitation(
            current, [first, second], invitation_id=first.invitation_id,
            actor_id=BOB, expected_revision=1,
        )

    accepted_second = accept_relationship_invitation(
        current, [first, second], invitation_id=second.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    saved = WorldState.model_validate({
        **current.model_dump(mode="python"),
        "relationship_invitations": [first, accepted_second],
    })
    assert saved.relationship_invitations == [first, accepted_second]


def test_completed_timed_meeting_allows_next_date_and_preserves_both_records():
    initial = _state()
    first = _accepted_timed_invitation(
        initial,
        interval=(100, 200),
        ident=UUID("a3000000-0000-4000-8000-000000000001"),
    )
    at_meeting = _at_tick(initial, 100)
    first = attend_relationship_meeting(
        at_meeting, [first], invitation_id=first.invitation_id,
        actor_id=ALICE, expected_revision=2,
    )
    first = attend_relationship_meeting(
        at_meeting, [first], invitation_id=first.invitation_id,
        actor_id=BOB, expected_revision=3,
    )
    reflection = MeetingReflection(
        invitation_id=first.invitation_id,
        actor_id=ALICE,
        outlook="closer",
        recorded_tick=200,
    )
    current = WorldState.model_validate({
        **initial.model_dump(mode="python"),
        "current_tick": 200,
        "relationship_invitations": [first],
        "relationship_reflections": [reflection],
    })

    second = _propose(
        current,
        records=current.relationship_invitations,
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=300, end_tick=400),
    )
    accepted_second = accept_relationship_invitation(
        current, [first, second], invitation_id=second.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    saved = WorldState.model_validate({
        **current.model_dump(mode="python"),
        "relationship_invitations": [first, accepted_second],
    })

    assert saved.relationship_invitations == [first, accepted_second]
    assert saved.relationship_invitations[0] == first
    assert saved.relationship_invitations[1].meeting == InvitationMeeting(start_tick=300, end_tick=400)
    assert saved.relationship_reflections == [reflection]


def test_world_state_keeps_one_active_pair_but_allows_finished_date_history():
    initial = _state()
    first = _accepted_timed_invitation(
        initial,
        interval=(100, 200),
        ident=UUID("a3000000-0000-4000-8000-000000000001"),
    )
    second_pending = _propose(
        _at_tick(initial, 199),
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=300, end_tick=400),
    )
    snapshot = {
        **initial.model_dump(mode="python"),
        "current_tick": 200,
        "relationship_invitations": [first, second_pending],
    }
    assert len(WorldState.model_validate(snapshot).relationship_invitations) == 2

    with pytest.raises(ValueError, match="One relationship invitation may be open per pair"):
        WorldState.model_validate({**snapshot, "current_tick": 199})


def test_untimed_invitations_still_block_same_pair_after_elapsed_time():
    initial = _state()
    first = _propose(initial)
    current = _at_tick(_state_with_invitation(initial, first), 100)

    with pytest.raises(InvalidEvent, match="pending or accepted"):
        _propose(
            current,
            records=current.relationship_invitations,
            ident=UUID("a3000000-0000-4000-8000-000000000002"),
            meeting=InvitationMeeting(start_tick=300, end_tick=400),
        )


def test_transitions_require_exact_revision_pending_status_and_monotonic_world_time():
    state = _state(tick=100)
    proposed = _propose(state)

    with pytest.raises(InvalidEvent, match="expected_revision"):
        accept_relationship_invitation(
            state, [proposed], invitation_id=proposed.invitation_id,
            actor_id=BOB, expected_revision=0,
        )
    with pytest.raises(InvalidEvent, match="expected_revision"):
        accept_relationship_invitation(
            state, [proposed], invitation_id=proposed.invitation_id,
            actor_id=BOB, expected_revision=2,
        )

    accepted = accept_relationship_invitation(
        state, [proposed], invitation_id=proposed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    with pytest.raises(InvalidEvent, match="pending invitation"):
        decline_relationship_invitation(
            state, [accepted], invitation_id=accepted.invitation_id,
            actor_id=BOB, expected_revision=2,
        )
    with pytest.raises(InvalidEvent, match="Only an accepted invitation"):
        end_relationship_invitation(
            state, [proposed], invitation_id=proposed.invitation_id,
            actor_id=ALICE, expected_revision=1,
        )

    earlier = _at_tick(state, 99)
    with pytest.raises(InvalidEvent, match="from the future"):
        end_relationship_invitation(
            earlier, [accepted], invitation_id=accepted.invitation_id,
            actor_id=ALICE, expected_revision=2,
        )


def test_dialogue_like_text_or_untrusted_record_copy_cannot_skip_explicit_acceptance():
    proposed = _propose(_state())
    forged = proposed.model_copy(update={"status": "accepted", "revision": 2})

    with pytest.raises(InvalidEvent, match="Only an accepted invitation"):
        end_relationship_invitation(
            _state(), [proposed], invitation_id=forged.invitation_id,
            actor_id=ALICE, expected_revision=1,
        )

    assert proposed.status == "pending"
    assert "private_text" not in RelationshipInvitation.model_fields


def test_timed_meeting_has_strict_positive_half_open_ticks_and_future_start():
    with pytest.raises(ValueError, match="positive half-open interval"):
        InvitationMeeting(start_tick=10, end_tick=10)
    with pytest.raises(ValueError, match="positive half-open interval"):
        InvitationMeeting(start_tick=11, end_tick=10)
    with pytest.raises(ValueError):
        InvitationMeeting(start_tick=True, end_tick=20)
    with pytest.raises(ValueError):
        InvitationMeeting(start_tick=10, end_tick="20")

    state = _state(tick=10)
    with pytest.raises(InvitationMeetingUnavailable):
        _propose(state, meeting=InvitationMeeting(start_tick=10, end_tick=20))
    timed = _propose(state, meeting=InvitationMeeting(start_tick=11, end_tick=20))
    assert timed.meeting == InvitationMeeting(start_tick=11, end_tick=20)
    with pytest.raises(ValueError, match="must be accepted before its meeting starts"):
        RelationshipInvitation(
            invitation_id=UUID("a3000000-0000-4000-8000-000000000002"),
            proposer_id=ALICE,
            invitee_id=BOB,
            status="accepted",
            proposed_tick=10,
            updated_tick=11,
            revision=2,
            meeting=InvitationMeeting(start_tick=11, end_tick=20),
        )


@pytest.mark.parametrize(
    ("proposer", "invitee"),
    [(ALICE, CASEY), (CASEY, BOB)],
)
def test_acceptance_rejects_overlap_with_another_accepted_meeting_for_either_participant(proposer, invitee):
    state = _state()
    existing = _propose(state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    existing = accept_relationship_invitation(
        state, [existing], invitation_id=existing.invitation_id,
        actor_id=existing.invitee_id, expected_revision=1,
    )
    candidate = _propose(
        state, proposer=proposer, invitee=invitee, records=[existing],
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=150, end_tick=250),
    )

    with pytest.raises(InvitationMeetingUnavailable, match="meeting is no longer available"):
        accept_relationship_invitation(
            state, [existing, candidate], invitation_id=candidate.invitation_id,
            actor_id=invitee, expected_revision=1,
        )


@pytest.mark.parametrize("candidate_interval", [(50, 100), (200, 300)])
def test_meetings_may_be_adjacent_at_either_half_open_boundary(candidate_interval):
    state = _state()
    existing = _propose(state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    existing = accept_relationship_invitation(
        state, [existing], invitation_id=existing.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    candidate = _propose(
        state, proposer=ALICE, invitee=CASEY, records=[existing],
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=candidate_interval[0], end_tick=candidate_interval[1]),
    )

    assert invitation_meeting_available(state, [existing, candidate], candidate)
    accepted = accept_relationship_invitation(
        state, [existing, candidate], invitation_id=candidate.invitation_id,
        actor_id=CASEY, expected_revision=1,
    )
    assert accepted.meeting == candidate.meeting


def test_acceptance_at_meeting_start_is_too_late_and_lifecycle_keeps_meeting():
    state = _state()
    proposed = _propose(state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    at_start = _at_tick(state, 100)
    assert not invitation_meeting_available(at_start, [proposed], proposed)
    with pytest.raises(InvitationMeetingUnavailable):
        accept_relationship_invitation(
            at_start, [proposed], invitation_id=proposed.invitation_id,
            actor_id=BOB, expected_revision=1,
        )

    accepted = accept_relationship_invitation(
        state, [proposed], invitation_id=proposed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    assert accepted.meeting == proposed.meeting
    ended = end_relationship_invitation(
        state, [accepted], invitation_id=accepted.invitation_id,
        actor_id=ALICE, expected_revision=2,
    )
    assert ended.meeting == proposed.meeting


@pytest.mark.parametrize("historical_status", ["pending", "withdrawn", "ended"])
def test_pending_withdrawn_and_ended_meetings_do_not_reserve_time(historical_status):
    state = _state()
    historical = _propose(
        state, proposer=ALICE, invitee=CASEY,
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=100, end_tick=200),
    )
    if historical_status == "withdrawn":
        historical = withdraw_relationship_invitation(
            state, [historical], invitation_id=historical.invitation_id,
            actor_id=ALICE, expected_revision=1,
        )
    elif historical_status == "ended":
        historical = accept_relationship_invitation(
            state, [historical], invitation_id=historical.invitation_id,
            actor_id=CASEY, expected_revision=1,
        )
        historical = end_relationship_invitation(
            state, [historical], invitation_id=historical.invitation_id,
            actor_id=ALICE, expected_revision=2,
        )

    candidate = _propose(
        state, proposer=BOB, invitee=CASEY, records=[historical],
        ident=UUID("a3000000-0000-4000-8000-000000000003"),
        meeting=InvitationMeeting(start_tick=150, end_tick=250),
    )
    accepted = accept_relationship_invitation(
        state, [historical, candidate], invitation_id=candidate.invitation_id,
        actor_id=CASEY, expected_revision=1,
    )
    assert accepted.meeting == candidate.meeting


def test_inactive_historical_participant_releases_accepted_meeting_reservation():
    state = _state()
    existing = _propose(state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    existing = accept_relationship_invitation(
        state, [existing], invitation_id=existing.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    inactive_state = _state(bob=_character(BOB, "Bob", tick=0, status="retired"))
    candidate = _propose(
        inactive_state, proposer=ALICE, invitee=CASEY, records=[existing],
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=150, end_tick=250),
    )

    assert invitation_meeting_available(inactive_state, [existing, candidate], candidate)
    accepted = accept_relationship_invitation(
        inactive_state, [existing, candidate], invitation_id=candidate.invitation_id,
        actor_id=CASEY, expected_revision=1,
    )
    assert accepted.status == "accepted"


def test_legacy_invitation_serialization_omits_absent_meeting():
    state = _state()
    invitation = _propose(state)
    with_invitation = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "relationship_invitations": [invitation],
    })
    legacy_payload = with_invitation.model_dump(mode="json")
    assert "meeting" not in legacy_payload["relationship_invitations"][0]
    assert "meeting_attendance" not in legacy_payload["relationship_invitations"][0]

    replayed_legacy = WorldState.model_validate(legacy_payload)
    from world_engine.domain.reducers import state_hash

    assert state_hash(with_invitation) == state_hash(replayed_legacy)


def test_meeting_attendance_is_explicit_per_participant_and_keeps_acceptance_tick():
    proposal_state = _state()
    proposed = _propose(proposal_state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    accepted = accept_relationship_invitation(
        proposal_state, [proposed], invitation_id=proposed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    meeting_start = _at_tick(proposal_state, 100)

    attended = attend_relationship_meeting(
        meeting_start, [accepted], invitation_id=accepted.invitation_id,
        actor_id=ALICE, expected_revision=2,
    )
    assert attended.status == "accepted"
    assert attended.revision == 3
    assert attended.updated_tick == accepted.updated_tick == 0
    assert [(row.actor_id, row.attended_tick) for row in attended.meeting_attendance] == [(ALICE, 100)]

    second_attendance = attend_relationship_meeting(
        meeting_start, [attended], invitation_id=attended.invitation_id,
        actor_id=BOB, expected_revision=3,
    )
    assert second_attendance.revision == 4
    assert [(row.actor_id, row.attended_tick) for row in second_attendance.meeting_attendance] == [
        (ALICE, 100), (BOB, 100),
    ]

    after_meeting = _at_tick(meeting_start, 200)
    ended = end_relationship_invitation(
        after_meeting, [second_attendance], invitation_id=second_attendance.invitation_id,
        actor_id=ALICE, expected_revision=4,
    )
    assert ended.status == "ended"
    assert ended.meeting_attendance == second_attendance.meeting_attendance


def test_meeting_attendance_requires_current_interval_active_adults_and_colocation():
    state = _state()
    proposed = _propose(state, meeting=InvitationMeeting(start_tick=100, end_tick=200))
    accepted = accept_relationship_invitation(
        state, [proposed], invitation_id=proposed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )

    for tick in (99, 200):
        with pytest.raises(InvitationMeetingUnavailable):
            attend_relationship_meeting(
                _at_tick(state, tick), [accepted], invitation_id=accepted.invitation_id,
                actor_id=ALICE, expected_revision=2,
            )
    with pytest.raises(InvalidEvent, match="Only an accepted invitation"):
        attend_relationship_meeting(
            state, [proposed], invitation_id=proposed.invitation_id,
            actor_id=ALICE, expected_revision=1,
        )
    untimed = _propose(state)
    untimed = accept_relationship_invitation(
        state, [untimed], invitation_id=untimed.invitation_id,
        actor_id=BOB, expected_revision=1,
    )
    with pytest.raises(InvalidEvent, match="scheduled invitation meeting"):
        attend_relationship_meeting(
            _at_tick(state, 100), [untimed], invitation_id=untimed.invitation_id,
            actor_id=ALICE, expected_revision=2,
        )
    with pytest.raises(InvalidEvent, match="Only a participant"):
        attend_relationship_meeting(
            _at_tick(state, 100), [accepted], invitation_id=accepted.invitation_id,
            actor_id=CASEY, expected_revision=2,
        )
    with pytest.raises(InvalidEvent, match="already been recorded"):
        attend_relationship_meeting(
            _at_tick(state, 100),
            [attend_relationship_meeting(
                _at_tick(state, 100), [accepted], invitation_id=accepted.invitation_id,
                actor_id=ALICE, expected_revision=2,
            )],
            invitation_id=accepted.invitation_id, actor_id=ALICE, expected_revision=3,
        )
    with pytest.raises(InvalidEvent, match="co-located"):
        attend_relationship_meeting(
            _move_actor(_at_tick(state, 100), ALICE, PLACE_B),
            [accepted], invitation_id=accepted.invitation_id,
            actor_id=ALICE, expected_revision=2,
        )
    retired_pair = _state(
        tick=100,
        alice=_character(ALICE, "Alice", tick=100, status="retired"),
    )
    with pytest.raises(InvalidEvent, match="active characters"):
        attend_relationship_meeting(
            retired_pair, [accepted], invitation_id=accepted.invitation_id,
            actor_id=BOB, expected_revision=2,
        )
    underage_pair = _state(
        tick=100,
        alice=_character(ALICE, "Alice", tick=100, age_years=17),
    )
    with pytest.raises(InvalidEvent, match="at least 18 world years old"):
        attend_relationship_meeting(
            underage_pair, [accepted], invitation_id=accepted.invitation_id,
            actor_id=BOB, expected_revision=2,
        )


@pytest.mark.parametrize("worker_id", [ALICE, BOB])
def test_new_meeting_attendance_rejects_either_participant_working_now(worker_id):
    work_state, work_attendance = _state_with_work_attendance(worker_id=worker_id)
    accepted = _accepted_timed_invitation(
        work_state,
        interval=(WORK_SHIFT_SECONDS + 1, WORK_SHIFT_SECONDS + 1_000),
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
    )
    meeting_state = _state_with_invitation(_at_tick(work_state, WORK_SHIFT_SECONDS + 1), accepted)
    assert npc_attendance_is_valid(meeting_state, work_attendance)

    before = meeting_state.model_dump(mode="python")
    with pytest.raises(InvitationMeetingUnavailable):
        attend_relationship_meeting(
            meeting_state,
            [accepted],
            invitation_id=accepted.invitation_id,
            actor_id=ALICE,
            expected_revision=2,
            check_work_calendar=True,
        )
    assert meeting_state.model_dump(mode="python") == before


def test_legacy_meeting_attendance_keeps_old_behavior_during_valid_work():
    work_state, work_attendance = _state_with_work_attendance()
    accepted = _accepted_timed_invitation(
        work_state,
        interval=(WORK_SHIFT_SECONDS + 1, WORK_SHIFT_SECONDS + 1_000),
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
    )
    meeting_state = _state_with_invitation(_at_tick(work_state, WORK_SHIFT_SECONDS + 1), accepted)
    assert npc_attendance_is_valid(meeting_state, work_attendance)

    attended = attend_relationship_meeting(
        meeting_state,
        [accepted],
        invitation_id=accepted.invitation_id,
        actor_id=ALICE,
        expected_revision=2,
        check_work_calendar=False,
    )
    assert attended.revision == 3
    assert attended.meeting_attendance[0].actor_id == ALICE


def test_stale_work_attendance_does_not_block_new_meeting_attendance():
    work_state, work_attendance = _state_with_work_attendance()
    accepted = _accepted_timed_invitation(
        work_state,
        interval=(WORK_SHIFT_SECONDS + 1, WORK_SHIFT_SECONDS + 1_000),
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
    )
    meeting_state = _state_with_invitation(_at_tick(work_state, WORK_SHIFT_SECONDS + 1), accepted)
    payload = meeting_state.model_dump(mode="python")
    payload["work_postings"][0]["revision"] += 1
    stale_state = WorldState.model_validate(payload)
    assert not npc_attendance_is_valid(stale_state, work_attendance)

    attended = attend_relationship_meeting(
        stale_state,
        [accepted],
        invitation_id=accepted.invitation_id,
        actor_id=ALICE,
        expected_revision=2,
        check_work_calendar=True,
    )
    assert attended.revision == 3
    assert attended.meeting_attendance[0].actor_id == ALICE


def test_exact_work_shift_end_is_available_for_new_meeting_attendance():
    work_state, work_attendance = _state_with_work_attendance()
    accepted = _accepted_timed_invitation(
        work_state,
        interval=(2 * WORK_SHIFT_SECONDS, 2 * WORK_SHIFT_SECONDS + 100),
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
    )
    meeting_state = _state_with_invitation(_at_tick(work_state, 2 * WORK_SHIFT_SECONDS), accepted)
    assert npc_attendance_is_valid(meeting_state, work_attendance)

    attended = attend_relationship_meeting(
        meeting_state,
        [accepted],
        invitation_id=accepted.invitation_id,
        actor_id=ALICE,
        expected_revision=2,
        check_work_calendar=True,
    )
    assert attended.revision == 3


def test_work_attendance_does_not_block_free_or_nonoverlapping_future_invitations():
    work_state, work_attendance = _state_with_work_attendance()
    assert npc_attendance_is_valid(work_state, work_attendance)

    free = _propose(work_state)
    accepted_free = accept_relationship_invitation(
        work_state, [free], invitation_id=free.invitation_id, actor_id=BOB, expected_revision=1,
    )
    assert accepted_free.status == "accepted"
    assert accepted_free.meeting is None

    future = _propose(
        work_state,
        ident=UUID("a3000000-0000-4000-8000-000000000002"),
        meeting=InvitationMeeting(start_tick=2 * WORK_SHIFT_SECONDS, end_tick=2 * WORK_SHIFT_SECONDS + 100),
    )
    accepted_future = accept_relationship_invitation(
        work_state, [future], invitation_id=future.invitation_id, actor_id=BOB, expected_revision=1,
    )
    assert accepted_future.status == "accepted"


def test_meeting_attendance_rows_are_validated_as_private_participant_facts():
    invitation = RelationshipInvitation(
        invitation_id=UUID("a3000000-0000-4000-8000-000000000002"),
        proposer_id=ALICE,
        invitee_id=BOB,
        status="accepted",
        proposed_tick=0,
        updated_tick=1,
        revision=2,
        meeting=InvitationMeeting(start_tick=10, end_tick=20),
    )
    base = invitation.model_dump(mode="python")
    for attendance in (
        [{"actor_id": str(CASEY), "attended_tick": 10}],
        [{"actor_id": str(ALICE), "attended_tick": 20}],
        [{"actor_id": str(ALICE), "attended_tick": 10}, {"actor_id": str(ALICE), "attended_tick": 11}],
    ):
        with pytest.raises(ValueError):
            RelationshipInvitation.model_validate({**base, "meeting_attendance": attendance})
