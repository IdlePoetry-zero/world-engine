"""Private, snapshot-bounded readbacks for a player's long actions."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from world_engine.domain.models import JobRecord, JobStatus, WorldState
from world_engine.domain.travel_itinerary import (
    TravelItineraryArrivalProof,
    travel_itinerary_journey_id,
)
from world_engine.domain.travel_journeys import TravelJourney
from world_engine.simulation import LongActionPlan
from world_engine.simulation.contracts import WorkPurchaseAffordabilityReceipt


MAX_OWN_LONG_ACTION_READBACKS = 8
_INTEGER = re.compile(r"^-?(?:0|[1-9]\d*)$")
_REASON_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def own_long_action_readbacks(
    jobs: Iterable[JobRecord],
    *,
    actor_id: UUID,
    user_id: UUID,
    current_tick: int,
    state_version: int,
    state: WorldState | None = None,
    visible_entities: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return only this user's bounded work, study, and itinerary readbacks at a snapshot.

    The function performs no I/O and treats the durable job payload as the only
    source of paid-work receipts and itinerary cursor progress. A job is omitted
    if its plan, commit boundary, or cursor cannot be validated against the
    supplied world snapshot.
    """

    actor = _coerce_uuid(actor_id)
    user = _coerce_uuid(user_id)
    snapshot_tick = _integer_value(current_tick)
    snapshot_version = _integer_value(state_version, nonnegative=True)
    if actor is None or user is None or snapshot_tick is None or snapshot_version is None:
        return {"work": [], "studies": [], "itineraries": []}

    work_rows: list[tuple[str, dict[str, Any]]] = []
    study_rows: list[tuple[str, dict[str, Any]]] = []
    itinerary_rows: list[tuple[str, dict[str, Any]]] = []
    journey_index = None
    if state is not None:
        if not isinstance(state, WorldState) or state.current_tick != snapshot_tick:
            journey_index = None
        else:
            journey_index = _index_travel_journeys(state)
    for job in jobs:
        payload = getattr(job, "payload", None)
        if (
            getattr(job, "job_type", None) != "simulation.long_action"
            or not isinstance(payload, dict)
            or _coerce_uuid(payload.get("submitted_by")) != user
        ):
            continue
        try:
            plan = LongActionPlan.model_validate(payload.get("plan"))
        except (TypeError, ValueError):
            continue
        if plan.actor_id != actor or plan.started_tick is None:
            continue
        if not _visible_at_snapshot(job, payload, plan, snapshot_tick, snapshot_version):
            continue

        common = _common_job_readback(job, plan)
        sort_key = _recent_sort_key(job)
        if plan.activity == "work":
            progress = _work_progress(payload.get("work_progress"), getattr(job, "committed_through_tick", None))
            if progress is None:
                continue
            work_row = common | {"work_progress": progress}
            purchase_target = plan.work_schedule.purchase_target if plan.work_schedule is not None else None
            if purchase_target is not None:
                work_row["purchase_target"] = purchase_target.model_dump(
                    mode="json", include={"listing_id", "resource_id", "currency_resource_id", "quantity"},
                )
                affordability = work_purchase_affordability(job, plan)
                if affordability is not None:
                    work_row["work_purchase_affordability"] = affordability
            resume = _work_resume(
                payload.get("work_resume"),
                plan,
                committed_tick_raw=getattr(job, "committed_through_tick", None),
                committed_version_raw=payload.get("committed_state_version"),
            )
            if resume is not None:
                work_row["work_resume"] = resume
            work_rows.append((sort_key, work_row))
        elif plan.activity == "study":
            study_row = common | {"skill_id": plan.skill_id}
            threshold = _study_stop_safety_threshold(plan, payload.get("plan"))
            if threshold is not None:
                study_row["study_stop_safety_at_or_below"] = threshold
            study_rows.append((sort_key, study_row))
        elif plan.activity == "travel" and plan.travel_itinerary is not None:
            itinerary_progress = _itinerary_progress(
                plan,
                payload.get("itinerary_cursor"),
                getattr(job, "committed_through_tick", None),
            )
            if itinerary_progress is None:
                continue
            progress, cursor = itinerary_progress
            row = common | {"itinerary_progress": progress}
            if state is not None:
                if journey_index is None:
                    continue
                arrived_legs = _arrived_itinerary_legs(
                    plan,
                    cursor,
                    getattr(job, "committed_through_tick", None),
                    snapshot_tick=snapshot_tick,
                    journey_index=journey_index,
                    visible_entities=visible_entities,
                )
                if arrived_legs is None:
                    continue
                row["arrived_legs"] = arrived_legs
            itinerary_rows.append((sort_key, row))

    work_rows.sort(key=lambda item: (item[0], item[1]["job_id"]), reverse=True)
    study_rows.sort(key=lambda item: (item[0], item[1]["job_id"]), reverse=True)
    itinerary_rows.sort(key=lambda item: (item[0], item[1]["job_id"]), reverse=True)
    return {
        "work": [row for _, row in work_rows[:MAX_OWN_LONG_ACTION_READBACKS]],
        "studies": [row for _, row in study_rows[:MAX_OWN_LONG_ACTION_READBACKS]],
        "itineraries": [row for _, row in itinerary_rows[:MAX_OWN_LONG_ACTION_READBACKS]],
    }


def work_purchase_affordability(job: JobRecord, plan: LongActionPlan) -> dict[str, Any] | None:
    """Read a qualified historical quote from an already-authorized work job."""
    if job.status != JobStatus.SUCCEEDED or plan.activity != "work" or plan.work_schedule is None:
        return None
    target = plan.work_schedule.purchase_target
    if target is None or plan.started_tick is None or not isinstance(job.result, dict):
        return None
    metrics = job.result.get("metrics")
    if not isinstance(metrics, dict) or metrics.get("integration_boundary") != "work_purchase_target_affordable":
        return None
    try:
        receipt = WorkPurchaseAffordabilityReceipt.model_validate(metrics.get("work_purchase_affordability"))
    except (TypeError, ValueError):
        return None
    if (
        receipt.listing_id != target.listing_id
        or receipt.quantity != target.quantity
        or receipt.currency_resource_id != target.currency_resource_id
        or receipt.confirmed_tick != job.committed_through_tick
        or not plan.started_tick <= receipt.confirmed_tick <= plan.target_tick
    ):
        return None
    return receipt.model_dump(mode="json") | {"confirmed_tick": str(receipt.confirmed_tick)}


def _study_stop_safety_threshold(plan: LongActionPlan, raw_plan: Any) -> int | None:
    """Return one explicitly stored own safety stop, never a coerced or third-party one."""
    if plan.started_tick is None or plan.activity != "study" or plan.skill_id is None:
        return None
    if not isinstance(raw_plan, dict):
        return None
    raw_conditions = raw_plan.get("stop_conditions")
    if not isinstance(raw_conditions, (list, tuple)) or len(raw_conditions) != len(plan.stop_conditions):
        return None

    own_safety_indices = [
        index for index, condition in enumerate(plan.stop_conditions)
        if condition.kind == "need_at_or_below"
        and condition.need == "safety"
        and condition.subject_id == plan.actor_id
    ]
    if len(own_safety_indices) != 1:
        return None
    index = own_safety_indices[0]
    condition = plan.stop_conditions[index]
    raw_condition = raw_conditions[index]
    if (
        not isinstance(raw_condition, dict)
        or raw_condition.get("kind") != "need_at_or_below"
        or raw_condition.get("need") != "safety"
        or _coerce_uuid(raw_condition.get("subject_id")) != plan.actor_id
        or type(raw_condition.get("threshold")) is not int
        or raw_condition["threshold"] != condition.threshold
        or not 0 <= raw_condition["threshold"] <= 100
        or raw_condition.get("status") is not None
        or raw_condition.get("goal_id") is not None
    ):
        return None
    return raw_condition["threshold"]


def _visible_at_snapshot(
    job: JobRecord,
    payload: dict[str, Any],
    plan: LongActionPlan,
    snapshot_tick: int,
    snapshot_version: int,
) -> bool:
    initial_version = _integer_value(payload.get("initial_state_version"), nonnegative=True)
    if initial_version is None:
        return False

    committed_tick = _integer_value(getattr(job, "committed_through_tick", None))
    committed_version = _integer_value(payload.get("committed_state_version"), nonnegative=True)
    has_committed_tick = getattr(job, "committed_through_tick", None) is not None
    has_committed_version = payload.get("committed_state_version") is not None

    if has_committed_tick and not has_committed_version:
        # enqueue_job initializes this column to the reviewed branch tick. It
        # is not a simulation commit unless the worker also wrote a version.
        # Accept only that exact pre-window boundary, with no receipt/cursor.
        if (
            committed_tick != plan.started_tick
            or payload.get("work_progress") is not None
            or payload.get("itinerary_cursor") is not None
        ):
            return False
        effective_tick = committed_tick
        effective_version = initial_version
    elif has_committed_version and not has_committed_tick:
        return False
    elif has_committed_tick:
        if committed_tick is None or committed_version is None:
            return False
        if committed_version < initial_version:
            return False
        effective_tick = committed_tick
        effective_version = committed_version
    else:
        # A queued or completed-with-no-events job belongs to the version at
        # which it was authorized and has made no durable world-time progress.
        effective_tick = plan.started_tick
        effective_version = initial_version

    return effective_tick is not None and effective_tick <= snapshot_tick and effective_version <= snapshot_version


def _common_job_readback(job: JobRecord, plan: LongActionPlan) -> dict[str, Any]:
    committed_tick = _integer_value(getattr(job, "committed_through_tick", None))
    result = getattr(job, "result", None)
    reason = current_job_reason_code(job, _reason_code(result))
    status = getattr(job, "status", None)
    status_value = getattr(status, "value", status)
    if reason is None and status_value == "succeeded" and plan.activity == "work" and isinstance(result, dict):
        metrics = result.get("metrics")
        boundary = metrics.get("integration_boundary") if isinstance(metrics, dict) else None
        if boundary in ("work_balance_target_reached", "work_purchase_target_affordable"):
            reason = boundary
    return {
        "job_id": str(job.job_id),
        "status": str(status_value),
        "started_tick": str(plan.started_tick),
        "target_tick": str(plan.target_tick),
        "last_committed_tick": str(committed_tick) if committed_tick is not None else None,
        "reason_code": reason,
    }


def current_job_reason_code(job: Any, reason_code: str | None) -> str | None:
    """Hide a prior simulation result reason while the job is active again."""
    job_type = getattr(job, "job_type", None)
    status = getattr(job, "status", None)
    status_value = getattr(status, "value", status)
    if job_type in {"simulation.long_action", "simulation.world_advance"} and status_value in {"queued", "running"}:
        return None
    return reason_code


def _reason_code(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    interruption = result.get("interruption")
    value = interruption.get("reason_code") if isinstance(interruption, dict) else None
    if value is None:
        value = result.get("reason_code") or result.get("error")
    return value if isinstance(value, str) and _REASON_CODE.fullmatch(value) else None


def _work_progress(raw: Any, committed_tick_raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return {
            "shifts_paid": 0,
            "wages_by_resource": {},
            "last_paid_tick": None,
            "balance_resource_id": None,
            "balance_after_last_pay": None,
        }
    if not isinstance(raw, dict):
        return None

    shifts_paid = raw.get("shifts_paid")
    wages = raw.get("wages_by_resource")
    if type(shifts_paid) is not int or shifts_paid < 0 or not isinstance(wages, dict):
        return None
    if any(
        not isinstance(resource_id, str) or not resource_id.strip()
        or not isinstance(amount, str) or not _DECIMAL.fullmatch(amount)
        for resource_id, amount in wages.items()
    ):
        return None

    if shifts_paid == 0:
        if wages:
            return None
        return {
            "shifts_paid": 0,
            "wages_by_resource": {},
            "last_paid_tick": None,
            "balance_resource_id": None,
            "balance_after_last_pay": None,
        }

    last_paid_tick = raw.get("last_paid_tick")
    balance_resource_id = raw.get("balance_resource_id")
    balance_after_last_pay = raw.get("balance_after_last_pay")
    paid_tick = _integer_value(last_paid_tick)
    committed_tick = _integer_value(committed_tick_raw)
    if (
        paid_tick is None or committed_tick is None or paid_tick > committed_tick
        or not isinstance(balance_resource_id, str) or not balance_resource_id.strip()
        or not isinstance(balance_after_last_pay, str) or not _DECIMAL.fullmatch(balance_after_last_pay)
        or not wages
    ):
        return None
    return {
        "shifts_paid": shifts_paid,
        "wages_by_resource": dict(sorted(wages.items())),
        "last_paid_tick": str(paid_tick),
        "balance_resource_id": balance_resource_id,
        "balance_after_last_pay": balance_after_last_pay,
    }


def _work_resume(
    raw: Any,
    plan: LongActionPlan,
    *,
    committed_tick_raw: Any,
    committed_version_raw: Any,
) -> dict[str, Any] | None:
    """Project only a valid persisted resume receipt for a committed checkpoint."""

    if (
        not isinstance(raw, dict)
        or set(raw) != {"skipped_shifts", "next_shift_tick"}
        or type(raw.get("skipped_shifts")) is not int
        or raw["skipped_shifts"] < 0
        or plan.started_tick is None
        or plan.work_schedule is None
    ):
        return None

    committed_tick = _integer_value(committed_tick_raw)
    committed_version = _integer_value(committed_version_raw, nonnegative=True)
    if (
        committed_tick is None
        or committed_tick < plan.started_tick
        or committed_version is None
    ):
        return None

    next_shift_tick = raw["next_shift_tick"]
    if next_shift_tick is None:
        normalized_next_tick = None
    elif isinstance(next_shift_tick, str):
        parsed_tick = _integer_value(next_shift_tick)
        first_allowed_tick = max(plan.started_tick, plan.work_schedule.first_shift_tick)
        if (
            parsed_tick is None
            or str(parsed_tick) != next_shift_tick
            or not first_allowed_tick <= parsed_tick < plan.target_tick
        ):
            return None
        normalized_next_tick = str(parsed_tick)
    else:
        return None

    return {
        "skipped_shifts": raw["skipped_shifts"],
        "next_shift_tick": normalized_next_tick,
    }


def _itinerary_progress(
    plan: LongActionPlan,
    raw_cursor: Any,
    committed_tick_raw: Any,
) -> tuple[dict[str, int], TravelItineraryArrivalProof | None] | None:
    itinerary = plan.travel_itinerary
    if itinerary is None:
        return None
    arrived = 0
    cursor = None
    if raw_cursor is not None:
        try:
            cursor = TravelItineraryArrivalProof.model_validate(raw_cursor)
        except (TypeError, ValueError):
            return None
        if not 0 <= cursor.leg_index < len(itinerary.legs):
            return None
        leg = itinerary.legs[cursor.leg_index]
        committed_tick = _integer_value(committed_tick_raw)
        if (
            cursor.itinerary_id != itinerary.itinerary_id
            or cursor.actor_id != plan.actor_id
            or cursor.leg_id != leg.leg_id
            or cursor.journey_id != travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id)
            or cursor.route_id != leg.route_id
            or cursor.from_place_id != leg.from_place_id
            or cursor.to_place_id != leg.to_place_id
            or cursor.route_fingerprint != leg.route_fingerprint
            or committed_tick is None
            or cursor.arrived_tick > committed_tick
        ):
            return None
        arrived = cursor.leg_index + 1
    return {"legs_total": len(itinerary.legs), "legs_arrived": arrived}, cursor


def _arrived_itinerary_legs(
    plan: LongActionPlan,
    cursor: TravelItineraryArrivalProof | None,
    committed_tick_raw: Any,
    *,
    snapshot_tick: int,
    journey_index: Mapping[UUID, list[TravelJourney]],
    visible_entities: Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Project only the exact committed arrival prefix backed by this snapshot."""
    itinerary = plan.travel_itinerary
    if (
        itinerary is None or plan.activity != "travel" or plan.started_tick is None
        or itinerary.actor_id != plan.actor_id or itinerary.created_tick != plan.started_tick
        or itinerary.deadline_tick != plan.target_tick
    ):
        return None

    committed_tick = _integer_value(committed_tick_raw)
    if committed_tick is None or committed_tick > snapshot_tick:
        return None
    if cursor is None:
        last_arrived_index = -1
    else:
        if not 0 <= cursor.leg_index < len(itinerary.legs):
            return None
        last_arrived_index = cursor.leg_index

    next_leg_index = last_arrived_index + 1
    # A journey for the current leg may be in transit or interrupted while the
    # cursor still records only the last arrival. A later committed arrival
    # cannot exist because the worker advances the cursor atomically at arrival.
    for leg in itinerary.legs[next_leg_index + 1:]:
        if journey_index.get(travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id)):
            return None
    if next_leg_index < len(itinerary.legs):
        current_id = travel_itinerary_journey_id(
            itinerary.itinerary_id, itinerary.legs[next_leg_index].leg_id,
        )
        if any(journey.status == "arrived" for journey in journey_index.get(current_id, ())):
            # The arrival transition and cursor update share one commit.
            return None

    rows: list[dict[str, Any]] = []
    previous_arrived_tick: int | None = None
    for leg in itinerary.legs[:last_arrived_index + 1]:
        journey_id = travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id)
        matches = journey_index.get(journey_id, [])
        if len(matches) != 1:
            return None
        journey = matches[0]
        if (
            journey.journey_id != journey_id
            or journey.actor_id != itinerary.actor_id
            or journey.route_id != leg.route_id
            or journey.from_place_id != leg.from_place_id
            or journey.to_place_id != leg.to_place_id
            or journey.status != "arrived"
            or journey.hazard_tick is not None
            or journey.resolved_tick != journey.arrival_tick
            or journey.arrival_tick - journey.departed_tick != leg.travel_ticks + journey.stationary_ticks
            or journey.departed_tick < plan.started_tick
            or journey.arrival_tick > itinerary.deadline_tick
            or journey.arrival_tick > committed_tick
            or journey.arrival_tick > snapshot_tick
            or (previous_arrived_tick is not None and journey.departed_tick < previous_arrived_tick)
        ):
            return None

        row: dict[str, Any] = {
            "leg_index": leg.leg_index,
            "leg_id": str(leg.leg_id),
            "journey_id": str(journey.journey_id),
            "route_id": str(leg.route_id),
            "from_place_id": str(leg.from_place_id),
            "to_place_id": str(leg.to_place_id),
            "departed_tick": str(journey.departed_tick),
            "arrived_tick": str(journey.arrival_tick),
        }
        if visible_entities is not None:
            from_name = _visible_place_name(visible_entities, leg.from_place_id)
            to_name = _visible_place_name(visible_entities, leg.to_place_id)
            if from_name is not None:
                row["from_place_name"] = from_name
            if to_name is not None:
                row["to_place_name"] = to_name
        rows.append(row)
        previous_arrived_tick = journey.arrival_tick

    if cursor is not None:
        last_leg = itinerary.legs[last_arrived_index]
        last_journey = journey_index[travel_itinerary_journey_id(itinerary.itinerary_id, last_leg.leg_id)][0]
        if (
            cursor.itinerary_id != itinerary.itinerary_id
            or cursor.leg_id != last_leg.leg_id
            or cursor.leg_index != last_leg.leg_index
            or cursor.journey_id != last_journey.journey_id
            or cursor.actor_id != itinerary.actor_id
            or cursor.route_id != last_leg.route_id
            or cursor.from_place_id != last_leg.from_place_id
            or cursor.to_place_id != last_leg.to_place_id
            or cursor.route_fingerprint != last_leg.route_fingerprint
            or cursor.departed_tick != last_journey.departed_tick
            or cursor.arrived_tick != last_journey.arrival_tick
        ):
            return None
    return rows


def _index_travel_journeys(state: WorldState) -> dict[UUID, list[TravelJourney]] | None:
    index: dict[UUID, list[TravelJourney]] = {}
    for journey in state.travel_journeys:
        if not isinstance(journey, TravelJourney):
            return None
        index.setdefault(journey.journey_id, []).append(journey)
    return index


def _visible_place_name(
    visible_entities: Mapping[str, Mapping[str, Any]], place_id: UUID,
) -> str | None:
    record = visible_entities.get(str(place_id))
    if not isinstance(record, Mapping):
        return None
    name = record.get("name")
    return name if isinstance(name, str) and name.strip() and len(name) <= 240 else None


def _integer_value(value: Any, *, nonnegative: bool = False) -> int | None:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and _INTEGER.fullmatch(value):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    if nonnegative and parsed < 0:
        return None
    return parsed


def _coerce_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _recent_sort_key(job: JobRecord) -> str:
    value = getattr(job, "updated_at", None) or getattr(job, "created_at", None)
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return normalized.isoformat(timespec="microseconds")
    return value if isinstance(value, str) else ""
