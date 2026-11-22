from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from world_engine.application.long_action_readback import (
    MAX_OWN_LONG_ACTION_READBACKS,
    current_job_reason_code,
    own_long_action_readbacks,
)
from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.models import JobRecord, JobStatus, WorldEntity, WorldState
from world_engine.domain.travel_itinerary import (
    TravelItinerary,
    TravelItineraryArrivalProof,
    TravelItineraryLeg,
    travel_itinerary_journey_id,
    travel_itinerary_leg_id,
)
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)
from world_engine.simulation import LongActionPlan, StopCondition


USER_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_USER_ID = UUID("10000000-0000-4000-8000-000000000002")
ACTOR_ID = UUID("20000000-0000-4000-8000-000000000001")
OTHER_ACTOR_ID = UUID("20000000-0000-4000-8000-000000000002")
TENANT_ID = UUID("30000000-0000-4000-8000-000000000001")
WORLD_ID = UUID("40000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("50000000-0000-4000-8000-000000000001")
ASSIGNMENT_ID = UUID("60000000-0000-4000-8000-000000000001")
ITINERARY_ID = UUID("70000000-0000-4000-8000-000000000001")
ORIGIN_ID = UUID("80000000-0000-4000-8000-000000000001")
MIDDLE_ID = UUID("80000000-0000-4000-8000-000000000002")
DESTINATION_ID = UUID("80000000-0000-4000-8000-000000000003")
ROUTE_1_ID = UUID("90000000-0000-4000-8000-000000000001")
ROUTE_2_ID = UUID("90000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _work_plan(actor_id: UUID = ACTOR_ID) -> dict:
    plan = LongActionPlan(
        actor_id=actor_id,
        started_tick=100,
        target_tick=1_000,
        activity="work",
        work_schedule={
            "assignment_id": ASSIGNMENT_ID,
            "first_shift_tick": 100,
            "repeat_every_days": 1,
        },
    )
    return plan.model_dump(mode="json")


def _study_plan(
    actor_id: UUID = ACTOR_ID,
    *,
    safety_threshold: int | None = None,
    safety_subject_id: UUID | None = None,
) -> dict:
    conditions = () if safety_threshold is None else (
        StopCondition(
            kind="need_at_or_below",
            subject_id=safety_subject_id or actor_id,
            need="safety",
            threshold=safety_threshold,
        ),
    )
    return LongActionPlan(
        actor_id=actor_id,
        started_tick=100,
        target_tick=1_000,
        activity="study",
        skill_id="skill.research",
        stop_conditions=conditions,
    ).model_dump(mode="json")


def _itinerary_plan() -> tuple[dict, TravelItinerary]:
    legs = (
        TravelItineraryLeg(
            leg_id=travel_itinerary_leg_id(ITINERARY_ID, 0, ROUTE_1_ID),
            leg_index=0,
            route_id=ROUTE_1_ID,
            from_place_id=ORIGIN_ID,
            to_place_id=MIDDLE_ID,
            travel_ticks=20,
            route_fingerprint="a" * 64,
        ),
        TravelItineraryLeg(
            leg_id=travel_itinerary_leg_id(ITINERARY_ID, 1, ROUTE_2_ID),
            leg_index=1,
            route_id=ROUTE_2_ID,
            from_place_id=MIDDLE_ID,
            to_place_id=DESTINATION_ID,
            travel_ticks=30,
            route_fingerprint="b" * 64,
        ),
    )
    itinerary = TravelItinerary(
        itinerary_id=ITINERARY_ID,
        actor_id=ACTOR_ID,
        origin_place_id=ORIGIN_ID,
        destination_place_id=DESTINATION_ID,
        created_tick=100,
        deadline_tick=500,
        legs=legs,
    )
    plan = LongActionPlan(
        actor_id=ACTOR_ID,
        started_tick=100,
        target_tick=500,
        activity="travel",
        travel_itinerary=itinerary,
    )
    return plan.model_dump(mode="json"), itinerary


def _job(
    *,
    plan: dict | None = None,
    submitted_by: UUID = USER_ID,
    job_type: str = "simulation.long_action",
    actor_id: UUID = ACTOR_ID,
    initial_version: int = 5,
    committed_version: int | None = 8,
    committed_tick: int | None = 180,
    progress: dict | None = None,
    cursor: dict | None = None,
    resume: dict | None = None,
    result: dict | None = None,
    job_id: UUID | None = None,
    status: JobStatus = JobStatus.SUCCEEDED,
    updated_at: datetime = NOW,
) -> JobRecord:
    payload = {
        "submitted_by": str(submitted_by),
        "initial_state_version": initial_version,
        "plan": plan if plan is not None else _work_plan(actor_id),
        "pending_decision": {"text": "private model text should never appear"},
    }
    if committed_version is not None:
        payload["committed_state_version"] = committed_version
    if progress is not None:
        payload["work_progress"] = progress
    if cursor is not None:
        payload["itinerary_cursor"] = cursor
    if resume is not None:
        payload["work_resume"] = resume
    return JobRecord(
        job_id=job_id or uuid4(),
        tenant_id=TENANT_ID,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        job_type=job_type,
        status=status,
        payload=payload,
        attempt_count=1,
        fencing_token=1,
        committed_through_tick=committed_tick,
        result=result,
        created_at=updated_at - timedelta(minutes=1),
        updated_at=updated_at,
    )


def _arrival_cursor(itinerary: TravelItinerary, leg_index: int, departed_tick: int, arrived_tick: int):
    leg = itinerary.legs[leg_index]
    return TravelItineraryArrivalProof(
        itinerary_id=itinerary.itinerary_id,
        leg_id=leg.leg_id,
        leg_index=leg_index,
        journey_id=travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id),
        actor_id=itinerary.actor_id,
        route_id=leg.route_id,
        from_place_id=leg.from_place_id,
        to_place_id=leg.to_place_id,
        route_fingerprint=leg.route_fingerprint,
        departed_tick=departed_tick,
        arrived_tick=arrived_tick,
    )


def _itinerary_state(
    itinerary: TravelItinerary,
    *,
    current_tick: int = 180,
    arrived_count: int = 2,
    current_leg_in_transit: bool = False,
    journey_overrides: dict[int, dict[str, Any]] | None = None,
) -> WorldState:
    journey_overrides = journey_overrides or {}
    journeys = []
    schedules = []
    for index, leg in enumerate(itinerary.legs[:arrived_count]):
        departed_tick = 100 if index == 0 else 130
        arrival_tick = departed_tick + leg.travel_ticks
        values = {
            "journey_id": travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id),
            "actor_id": itinerary.actor_id,
            "route_id": leg.route_id,
            "from_place_id": leg.from_place_id,
            "to_place_id": leg.to_place_id,
            "departed_tick": departed_tick,
            "arrival_tick": arrival_tick,
            "status": "arrived",
            "resolved_tick": arrival_tick,
        } | journey_overrides.get(index, {})
        journey = TravelJourney.model_validate(values)
        journeys.append(journey)
        schedules.append(ScheduledEventRecord(
            id=travel_resolution_schedule_id(journey.journey_id),
            due_tick=journey.arrival_tick,
            phase="resolution",
            handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
            handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
            status="completed",
            completed_runs=1,
            payload={"journey_id": str(journey.journey_id)},
        ))
    if current_leg_in_transit and arrived_count < len(itinerary.legs):
        leg = itinerary.legs[arrived_count]
        departed_tick = 100 if arrived_count == 0 else 130
        journey = TravelJourney(
            journey_id=travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id),
            actor_id=itinerary.actor_id,
            route_id=leg.route_id,
            from_place_id=leg.from_place_id,
            to_place_id=leg.to_place_id,
            departed_tick=departed_tick,
            arrival_tick=departed_tick + leg.travel_ticks,
            status="in_transit",
        )
        journeys.append(journey)
        schedules.append(ScheduledEventRecord(
            id=travel_resolution_schedule_id(journey.journey_id),
            due_tick=journey.arrival_tick,
            phase="resolution",
            handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
            handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
            status="queued",
            payload={"journey_id": str(journey.journey_id)},
        ))

    places = (ORIGIN_ID, MIDDLE_ID, DESTINATION_ID)
    entities = {
        place_id: WorldEntity(id=place_id, kind="place")
        for place_id in places
    }
    actor_location = None if current_leg_in_transit else (
        itinerary.legs[arrived_count - 1].to_place_id if arrived_count else itinerary.origin_place_id
    )
    entities[ACTOR_ID] = WorldEntity(
        id=ACTOR_ID, kind="character", location_id=actor_location,
    )
    return WorldState(
        current_tick=current_tick,
        entities=entities,
        travel_journeys=journeys,
        scheduled_events=schedules,
    )


def test_readbacks_are_scoped_to_submitter_actor_and_supported_plan_kind():
    itinerary_plan, _ = _itinerary_plan()
    work = _job(progress=None, result={"reason_code": "work_balance_target_reached"})
    itinerary = _job(plan=itinerary_plan, progress=None, cursor=None)
    other_actor = _job(plan=_work_plan(OTHER_ACTOR_ID), progress=None)
    other_user = _job(submitted_by=OTHER_USER_ID, progress=None)
    non_simulation = _job(job_type="conversation.message", progress=None)
    legacy_travel = LongActionPlan(
        actor_id=ACTOR_ID,
        started_tick=100,
        target_tick=500,
        activity="travel",
        route_id=ROUTE_1_ID,
    ).model_dump(mode="json")
    legacy = _job(plan=legacy_travel, progress=None)

    result = own_long_action_readbacks(
        [work, itinerary, other_actor, other_user, non_simulation, legacy],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert [row["job_id"] for row in result["work"]] == [str(work.job_id)]
    assert [row["job_id"] for row in result["itineraries"]] == [str(itinerary.job_id)]
    work_readback = result["work"][0]
    assert work_readback["reason_code"] == "work_balance_target_reached"
    assert work_readback["work_progress"]["shifts_paid"] == 0
    assert "pending_decision" not in work_readback
    assert "plan" not in work_readback
    assert "route_ids" not in result["itineraries"][0]


def test_active_simulation_readback_drops_only_stale_current_reason():
    stale_result = {
        "reason_code": "waypoint_opportunity",
        "interruption": {"reason_code": "waypoint_opportunity"},
    }
    queued = _job(status=JobStatus.QUEUED, result=stale_result)
    running = _job(status=JobStatus.RUNNING, result=stale_result)
    media = _job(job_type="media.generate", status=JobStatus.QUEUED, result=stale_result)

    readback = own_long_action_readbacks(
        [queued, running],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )
    work_rows = {row["job_id"]: row for row in readback["work"]}
    assert work_rows[str(queued.job_id)]["reason_code"] is None
    assert work_rows[str(running.job_id)]["reason_code"] is None
    assert current_job_reason_code(media, "MEDIA_PROVIDER_BUSY") == "MEDIA_PROVIDER_BUSY"


@pytest.mark.parametrize(
    "status",
    [JobStatus.WAITING_INPUT, JobStatus.PAUSED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SUCCEEDED],
)
def test_terminal_and_waiting_simulation_reasons_are_preserved(status):
    job = _job(status=status, result={"reason_code": "waypoint_opportunity"})

    readback = own_long_action_readbacks(
        [job],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert readback["work"][0]["reason_code"] == "waypoint_opportunity"


def test_study_readback_projects_common_fields_skill_and_zero_safety_threshold():
    job = _job(
        plan=_study_plan(safety_threshold=0),
        result={"reason_code": "study_safety_threshold_reached"},
    )

    result = own_long_action_readbacks(
        [job],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert result["studies"] == [{
        "job_id": str(job.job_id),
        "status": "succeeded",
        "started_tick": "100",
        "target_tick": "1000",
        "last_committed_tick": "180",
        "reason_code": "study_safety_threshold_reached",
        "skill_id": "skill.research",
        "study_stop_safety_at_or_below": 0,
    }]
    assert "plan" not in result["studies"][0]
    assert "stop_conditions" not in result["studies"][0]


def test_study_threshold_is_omitted_without_one_valid_own_standard_condition():
    no_threshold = _job(plan=_study_plan())
    other_actor_condition = _job(plan=_study_plan(safety_threshold=20, safety_subject_id=OTHER_ACTOR_ID))
    malformed_raw_plan = _study_plan(safety_threshold=35)
    malformed_raw_plan["stop_conditions"][0]["threshold"] = True
    boolean_threshold = _job(plan=malformed_raw_plan)

    result = own_long_action_readbacks(
        [no_threshold, other_actor_condition, boolean_threshold],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    rows = {row["job_id"]: row for row in result["studies"]}
    for job in (no_threshold, other_actor_condition, boolean_threshold):
        assert str(job.job_id) in rows
        assert "study_stop_safety_at_or_below" not in rows[str(job.job_id)]


def test_study_readbacks_exclude_other_actor_submitter_and_future_or_unstamped_jobs():
    own = _job(plan=_study_plan(safety_threshold=60))
    other_actor = _job(plan=_study_plan(OTHER_ACTOR_ID, safety_threshold=61))
    other_account = _job(plan=_study_plan(safety_threshold=62), submitted_by=OTHER_USER_ID)
    future_tick = _job(plan=_study_plan(safety_threshold=63), committed_tick=181)
    future_version = _job(plan=_study_plan(safety_threshold=64), committed_version=9)
    unstamped_initial = _job(plan=_study_plan(safety_threshold=65))
    unstamped_initial.payload.pop("initial_state_version")
    unstamped_submitter = _job(plan=_study_plan(safety_threshold=66))
    unstamped_submitter.payload.pop("submitted_by")

    result = own_long_action_readbacks(
        [own, other_actor, other_account, future_tick, future_version, unstamped_initial, unstamped_submitter],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert result["studies"] == [{
        "job_id": str(own.job_id),
        "status": "succeeded",
        "started_tick": "100",
        "target_tick": "1000",
        "last_committed_tick": "180",
        "reason_code": None,
        "skill_id": "skill.research",
        "study_stop_safety_at_or_below": 60,
    }]


def test_study_readback_list_uses_shared_limit_and_recent_first_order():
    jobs = [
        _job(
            plan=_study_plan(safety_threshold=index),
            job_id=UUID(f"b0000000-0000-4000-8000-{index:012d}"),
            updated_at=NOW + timedelta(minutes=index),
        )
        for index in range(MAX_OWN_LONG_ACTION_READBACKS + 2)
    ]

    result = own_long_action_readbacks(
        jobs,
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert len(result["studies"]) == MAX_OWN_LONG_ACTION_READBACKS
    assert result["studies"][0]["job_id"] == str(jobs[-1].job_id)


def test_snapshot_filters_committed_tick_version_and_uncommitted_initial_version():
    visible = _job(progress=None, committed_version=8, committed_tick=180)
    future_version = _job(progress=None, committed_version=9, committed_tick=180)
    future_tick = _job(progress=None, committed_version=8, committed_tick=181)
    uncommitted_visible = _job(progress=None, committed_version=None, committed_tick=None, initial_version=8)
    uncommitted_future = _job(progress=None, committed_version=None, committed_tick=None, initial_version=9)

    result = own_long_action_readbacks(
        [visible, future_version, future_tick, uncommitted_visible, uncommitted_future],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert {row["job_id"] for row in result["work"]} == {str(visible.job_id), str(uncommitted_visible.job_id)}
    zero_receipt = next(row for row in result["work"] if row["job_id"] == str(uncommitted_visible.job_id))
    assert zero_receipt["last_committed_tick"] is None
    assert zero_receipt["work_progress"] == {
        "shifts_paid": 0,
        "wages_by_resource": {},
        "last_paid_tick": None,
        "balance_resource_id": None,
        "balance_after_last_pay": None,
    }


def test_queued_job_start_tick_uses_initial_version_before_its_first_commit():
    queued = _job(
        progress=None,
        status=JobStatus.QUEUED,
        committed_version=None,
        committed_tick=100,
        initial_version=5,
    )

    result = own_long_action_readbacks(
        [queued],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=100,
        state_version=5,
    )

    assert len(result["work"]) == 1
    assert result["work"][0]["status"] == "queued"
    assert result["work"][0]["started_tick"] == "100"
    assert result["work"][0]["last_committed_tick"] == "100"
    assert result["work"][0]["work_progress"]["shifts_paid"] == 0


def test_readback_includes_exact_committed_work_receipt_and_cursor_only_itinerary_count():
    itinerary_plan, itinerary = _itinerary_plan()
    second_leg = itinerary.legs[1]
    cursor = TravelItineraryArrivalProof(
        itinerary_id=itinerary.itinerary_id,
        leg_id=second_leg.leg_id,
        leg_index=1,
        journey_id=travel_itinerary_journey_id(itinerary.itinerary_id, second_leg.leg_id),
        actor_id=ACTOR_ID,
        route_id=second_leg.route_id,
        from_place_id=second_leg.from_place_id,
        to_place_id=second_leg.to_place_id,
        route_fingerprint=second_leg.route_fingerprint,
        departed_tick=130,
        arrived_tick=160,
    )
    receipt = {
        "shifts_paid": 2,
        "wages_by_resource": {"currency.silver": "15.50", "currency.gold": "2"},
        "last_paid_tick": "175",
        "balance_resource_id": "currency.silver",
        "balance_after_last_pay": "24.1250",
    }
    work = _job(progress=receipt, committed_version=8, committed_tick=180)
    itinerary_job = _job(
        plan=itinerary_plan,
        cursor=cursor.model_dump(mode="json"),
        committed_version=8,
        committed_tick=180,
    )

    result = own_long_action_readbacks(
        [work, itinerary_job],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    work_row = result["work"][0]
    assert work_row["started_tick"] == "100"
    assert work_row["target_tick"] == "1000"
    assert work_row["last_committed_tick"] == "180"
    assert work_row["work_progress"] == receipt
    itinerary_row = result["itineraries"][0]
    assert itinerary_row["itinerary_progress"] == {"legs_total": 2, "legs_arrived": 2}
    assert itinerary_row["last_committed_tick"] == "180"
    assert "plan" not in itinerary_row
    assert "itinerary_cursor" not in itinerary_row


def test_purchase_affordability_readback_requires_matching_owner_plan_and_completion_checkpoint():
    plan = _work_plan()
    plan["work_schedule"]["purchase_target"] = {
        "market_id": str(ORIGIN_ID), "listing_id": str(ROUTE_1_ID), "resource_id": "food.grain",
        "seller_id": str(OTHER_ACTOR_ID), "currency_resource_id": "currency.silver", "quantity": "4",
    }
    quote = {
        "listing_id": str(ROUTE_1_ID), "quantity": "4", "currency_resource_id": "currency.silver",
        "total_price_minor": "36", "wage_balance": "38", "confirmed_tick": 180,
    }

    def result(receipt):
        return {"metrics": {"integration_boundary": "work_purchase_target_affordable",
                            "work_purchase_affordability": receipt}}

    qualified = _job(plan=plan, result=result(quote))
    earlier_job = _job(plan=plan, result={"metrics": {"integration_boundary": "work_purchase_target_affordable"}})
    wrong_listing = _job(plan=plan, result=result(quote | {"listing_id": str(ROUTE_2_ID)}))
    wrong_tick = _job(plan=plan, result=result(quote | {"confirmed_tick": 179}))
    too_expensive = _job(plan=plan, result=result(quote | {"total_price_minor": "39"}))
    interrupted = _job(plan=plan, result=result(quote), status=JobStatus.PAUSED)
    other_user = _job(plan=plan, result=result(quote), submitted_by=OTHER_USER_ID)
    readback = own_long_action_readbacks(
        [qualified, earlier_job, wrong_listing, wrong_tick, too_expensive, interrupted, other_user],
        actor_id=ACTOR_ID, user_id=USER_ID, current_tick=180, state_version=8,
    )
    rows = {row["job_id"]: row for row in readback["work"]}
    assert str(other_user.job_id) not in rows
    assert rows[str(qualified.job_id)]["work_purchase_affordability"] == quote | {"confirmed_tick": "180"}
    assert rows[str(qualified.job_id)]["reason_code"] == "work_purchase_target_affordable"
    assert set(row_id for row_id, row in rows.items() if "work_purchase_affordability" in row) == {
        str(qualified.job_id),
    }


def test_work_resume_receipt_is_projected_only_for_own_valid_checkpoint():
    valid = _job(
        resume={"skipped_shifts": 3, "next_shift_tick": "200"},
        committed_version=8,
        committed_tick=180,
    )
    malformed_shape = _job(
        resume={"skipped_shifts": True, "next_shift_tick": "200"},
        committed_version=8,
        committed_tick=180,
    )
    before_original_first_shift = _job(
        resume={"skipped_shifts": 1, "next_shift_tick": "99"},
        committed_version=8,
        committed_tick=180,
    )
    at_original_deadline = _job(
        resume={"skipped_shifts": 1, "next_shift_tick": "1000"},
        committed_version=8,
        committed_tick=180,
    )
    teammate = _job(
        submitted_by=OTHER_USER_ID,
        resume={"skipped_shifts": 4, "next_shift_tick": "200"},
        committed_version=8,
        committed_tick=180,
    )

    result = own_long_action_readbacks(
        [valid, malformed_shape, before_original_first_shift, at_original_deadline, teammate],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    by_id = {row["job_id"]: row for row in result["work"]}
    assert by_id[str(valid.job_id)]["work_resume"] == {
        "skipped_shifts": 3,
        "next_shift_tick": "200",
    }
    assert str(teammate.job_id) not in by_id
    for job in (malformed_shape, before_original_first_shift, at_original_deadline):
        assert "work_resume" not in by_id[str(job.job_id)]


def test_invalid_or_future_itinerary_cursor_is_not_reported_as_progress():
    itinerary_plan, itinerary = _itinerary_plan()
    last_leg = itinerary.legs[-1]
    cursor = TravelItineraryArrivalProof(
        itinerary_id=itinerary.itinerary_id,
        leg_id=last_leg.leg_id,
        leg_index=1,
        journey_id=travel_itinerary_journey_id(itinerary.itinerary_id, last_leg.leg_id),
        actor_id=ACTOR_ID,
        route_id=last_leg.route_id,
        from_place_id=last_leg.from_place_id,
        to_place_id=last_leg.to_place_id,
        route_fingerprint=last_leg.route_fingerprint,
        departed_tick=190,
        arrived_tick=210,
    )
    job = _job(plan=itinerary_plan, cursor=cursor.model_dump(mode="json"), committed_version=8, committed_tick=200)

    result = own_long_action_readbacks(
        [job], actor_id=ACTOR_ID, user_id=USER_ID, current_tick=200, state_version=8,
    )

    assert result == {"work": [], "studies": [], "itineraries": []}


def test_snapshot_readback_adds_only_committed_arrival_receipts_and_safe_names():
    itinerary_plan, itinerary = _itinerary_plan()
    cursor = _arrival_cursor(itinerary, 1, departed_tick=130, arrived_tick=160)
    job = _job(
        plan=itinerary_plan,
        cursor=cursor.model_dump(mode="json"),
        committed_version=8,
        committed_tick=180,
    )
    state = _itinerary_state(
        itinerary,
        journey_overrides={0: {"arrival_tick": 125, "resolved_tick": 125, "stationary_ticks": 5}},
    )
    visible_entities = {
        str(ORIGIN_ID): {"id": str(ORIGIN_ID), "name": "Old Gate"},
        str(MIDDLE_ID): {"id": str(MIDDLE_ID), "name": "River Station"},
    }

    result = own_long_action_readbacks(
        [job],
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
        state=state,
        visible_entities=visible_entities,
    )

    row = result["itineraries"][0]
    assert row["itinerary_progress"] == {"legs_total": 2, "legs_arrived": 2}
    assert row["arrived_legs"] == [
        {
            "leg_index": 0,
            "leg_id": str(itinerary.legs[0].leg_id),
            "journey_id": str(travel_itinerary_journey_id(itinerary.itinerary_id, itinerary.legs[0].leg_id)),
            "route_id": str(ROUTE_1_ID),
            "from_place_id": str(ORIGIN_ID),
            "to_place_id": str(MIDDLE_ID),
            "departed_tick": "100",
            "arrived_tick": "125",
            "from_place_name": "Old Gate",
            "to_place_name": "River Station",
        },
        {
            "leg_index": 1,
            "leg_id": str(itinerary.legs[1].leg_id),
            "journey_id": str(travel_itinerary_journey_id(itinerary.itinerary_id, itinerary.legs[1].leg_id)),
            "route_id": str(ROUTE_2_ID),
            "from_place_id": str(MIDDLE_ID),
            "to_place_id": str(DESTINATION_ID),
            "departed_tick": "130",
            "arrived_tick": "160",
            "from_place_name": "River Station",
        },
    ]

    legacy = own_long_action_readbacks(
        [job], actor_id=ACTOR_ID, user_id=USER_ID, current_tick=180, state_version=8,
    )
    assert "arrived_legs" not in legacy["itineraries"][0]


def test_state_backed_readback_accepts_empty_cursor_and_an_in_transit_current_leg():
    itinerary_plan, itinerary = _itinerary_plan()
    job = _job(
        plan=itinerary_plan,
        cursor=None,
        committed_version=8,
        committed_tick=110,
        status=JobStatus.RUNNING,
    )
    state = _itinerary_state(
        itinerary, current_tick=110, arrived_count=0, current_leg_in_transit=True,
    )

    result = own_long_action_readbacks(
        [job], actor_id=ACTOR_ID, user_id=USER_ID, current_tick=110, state_version=8, state=state,
    )

    assert result["itineraries"][0]["itinerary_progress"] == {"legs_total": 2, "legs_arrived": 0}
    assert result["itineraries"][0]["arrived_legs"] == []


@pytest.mark.parametrize(
    "override",
    [
        {"route_id": ROUTE_2_ID},
        {"to_place_id": DESTINATION_ID},
        {"journey_id": UUID("a0000000-0000-4000-8000-000000000001")},
    ],
)
def test_state_backed_readback_rejects_missing_or_mismatched_arrival_receipt(override):
    itinerary_plan, itinerary = _itinerary_plan()
    cursor = _arrival_cursor(itinerary, 0, departed_tick=100, arrived_tick=120)
    job = _job(plan=itinerary_plan, cursor=cursor.model_dump(mode="json"), committed_tick=180)
    state = _itinerary_state(
        itinerary, arrived_count=1, journey_overrides={0: override},
    )

    result = own_long_action_readbacks(
        [job], actor_id=ACTOR_ID, user_id=USER_ID, current_tick=180, state_version=8, state=state,
    )

    assert result["itineraries"] == []


def test_state_backed_readback_rejects_duplicate_out_of_order_and_cursor_mismatched_receipts():
    itinerary_plan, itinerary = _itinerary_plan()
    cursor = _arrival_cursor(itinerary, 1, departed_tick=130, arrived_tick=160)
    job = _job(plan=itinerary_plan, cursor=cursor.model_dump(mode="json"), committed_tick=180)
    state = _itinerary_state(itinerary)

    duplicate_state = WorldState.model_construct(
        current_tick=state.current_tick,
        entities=state.entities,
        travel_journeys=[state.travel_journeys[0], state.travel_journeys[0], state.travel_journeys[1]],
        scheduled_events=state.scheduled_events,
    )
    out_of_order_state = _itinerary_state(
        itinerary,
        journey_overrides={1: {"departed_tick": 110, "arrival_tick": 140, "resolved_tick": 140}},
    )
    mismatched_cursor = cursor.model_copy(update={"arrived_tick": 159})

    for candidate_state, candidate_cursor in (
        (duplicate_state, cursor),
        (out_of_order_state, _arrival_cursor(itinerary, 1, departed_tick=110, arrived_tick=140)),
        (state, mismatched_cursor),
    ):
        candidate = _job(
            plan=itinerary_plan,
            cursor=candidate_cursor.model_dump(mode="json"),
            committed_tick=180,
        )
        result = own_long_action_readbacks(
            [candidate],
            actor_id=ACTOR_ID,
            user_id=USER_ID,
            current_tick=180,
            state_version=8,
            state=candidate_state,
        )
        assert result["itineraries"] == []


def test_state_backed_readback_rejects_arrival_without_matching_cursor_and_snapshot_mismatch():
    itinerary_plan, itinerary = _itinerary_plan()
    first_cursor = _arrival_cursor(itinerary, 0, departed_tick=100, arrived_tick=120)
    job = _job(plan=itinerary_plan, cursor=first_cursor.model_dump(mode="json"), committed_tick=180)
    both_arrived = _itinerary_state(itinerary)

    out_of_order = own_long_action_readbacks(
        [job], actor_id=ACTOR_ID, user_id=USER_ID, current_tick=180, state_version=8, state=both_arrived,
    )
    wrong_snapshot = own_long_action_readbacks(
        [job], actor_id=ACTOR_ID, user_id=USER_ID, current_tick=181, state_version=8, state=both_arrived,
    )

    assert out_of_order["itineraries"] == []
    assert wrong_snapshot["itineraries"] == []


def test_readback_lists_are_bounded_and_recent_first():
    jobs = [
        _job(
            progress=None,
            job_id=UUID(f"a0000000-0000-4000-8000-{index:012d}"),
            updated_at=NOW + timedelta(minutes=index),
        )
        for index in range(MAX_OWN_LONG_ACTION_READBACKS + 2)
    ]

    result = own_long_action_readbacks(
        jobs,
        actor_id=ACTOR_ID,
        user_id=USER_ID,
        current_tick=180,
        state_version=8,
    )

    assert len(result["work"]) == MAX_OWN_LONG_ACTION_READBACKS
    assert result["work"][0]["job_id"] == str(jobs[-1].job_id)
