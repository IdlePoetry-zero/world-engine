"""Strict chat contracts for explicitly ordered travel itineraries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool

from world_engine.domain.combat import active_combat_opponent
from world_engine.domain.components import PlaceComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, ContractModel, JobRecord, WorldState
from world_engine.domain.npc_plans import occupied_npc_ids
from world_engine.domain.travel_itinerary import plan_travel_itinerary
from world_engine.rules.engine import route_accessible
from world_engine.simulation import LongActionPlan

from .study_chat import MAX_SAFE_INTEGER, study_duration_seconds


MAX_TICK = 2**63 - 1
MAX_ITINERARY_ROUTE_IDS = 256
_VISIBLE_ROUTE_FIELDS = frozenset({
    "route_id",
    "from_place_id",
    "from_place_name",
    "to_place_id",
    "to_place_name",
    "travel_ticks",
})


class QueueItineraryIntent(ContractModel):
    """A player-selected, explicitly ordered route sequence and time limit."""

    kind: Literal["queue_itinerary"]
    route_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_ITINERARY_ROUTE_IDS)
    amount: Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)]
    unit: Literal["second", "hour", "day", "month", "year"]
    itinerary_auto_care: StrictBool = False


def itinerary_message_key(scope: AccessScope, request_id: UUID) -> str:
    """Return the stable account-scoped job key for one itinerary request."""

    if not isinstance(scope, AccessScope) or not isinstance(request_id, UUID):
        raise ValueError("Itinerary message keys require a trusted scope and request UUID.")
    return f"itinerary-message:{scope.user_id}:{request_id}"


def itinerary_queue_reply(request_id: UUID, job: JobRecord) -> dict[str, object]:
    """Build a stable acknowledgment from the persisted immutable plan."""

    if not isinstance(request_id, UUID):
        raise ValueError("An itinerary queue receipt requires a request UUID.")
    if job.job_type != "simulation.long_action":
        raise ValueError("The referenced job is not a simulation long action.")
    plan = LongActionPlan.model_validate(job.payload.get("plan"))
    itinerary = plan.travel_itinerary
    if plan.activity != "travel" or itinerary is None or plan.started_tick is None:
        raise ValueError("The referenced long action is not a persisted itinerary plan.")
    duration = plan.target_tick - plan.started_tick
    if not 0 < duration <= MAX_SAFE_INTEGER:
        raise ValueError("The persisted itinerary plan has an invalid duration.")
    care_text = "途中已授权使用本人现有食物和饮水并休息。" if plan.itinerary_auto_care else "途中未授权自动休息或使用补给。"
    return {
        "message_id": str(request_id),
        "kind": "action",
        "content": (
            f"已排队 {len(itinerary.legs)} 段登记路线，时限 {duration} 秒，目标世界时刻为 {plan.target_tick}。"
            f"{care_text}作业开始后按模拟窗口推进；本次排队没有推进世界时间。"
        ),
        "world_effect": False,
        "action_status": "succeeded",
        "job_id": str(job.job_id),
        "elapsed_ticks": "0",
        "references": [str(leg.route_id) for leg in itinerary.legs],
    }


def build_itinerary_chat_plan(
    state: WorldState,
    actor_id: UUID,
    action: QueueItineraryIntent,
    *,
    itinerary_id: UUID,
    visible_routes: Sequence[Mapping[str, object]],
) -> LongActionPlan:
    """Build a closed long-action plan from only the player's visible route rows.

    The route list remains exactly in player-supplied order. This function does
    not search for a path, grant future permissions, or infer care preferences.
    """

    if not isinstance(state, WorldState) or not isinstance(actor_id, UUID) or not isinstance(itinerary_id, UUID):
        raise ValueError("Itinerary planning requires a trusted state and UUID identities.")
    if not isinstance(action, QueueItineraryIntent):
        raise ValueError("Itinerary planning requires a validated queue intent.")
    try:
        intent = QueueItineraryIntent.model_validate(action.model_dump(mode="python"))
    except (TypeError, ValueError) as error:
        raise ValueError("The itinerary queue intent is invalid.") from error
    if actor_id not in state.entities or state.entities[actor_id].kind not in {"character", "person"}:
        raise ValueError("The selected itinerary actor is not present in this world.")

    approved_rows = _parse_visible_routes(visible_routes)
    route_rows_by_id: dict[UUID, list[dict[str, object]]] = {}
    for row in approved_rows:
        route_rows_by_id.setdefault(row["route_id"], []).append(row)
    for route_id in intent.route_ids:
        if route_id not in route_rows_by_id:
            raise ValueError("Every itinerary route must come from the player's visible route options.")

    duration = _itinerary_duration(intent, state)
    deadline_tick = state.current_tick + duration
    if deadline_tick > MAX_TICK:
        raise ValueError("The itinerary deadline exceeds the supported world clock.")

    itinerary = plan_travel_itinerary(
        state,
        itinerary_id=itinerary_id,
        actor_id=actor_id,
        route_ids=intent.route_ids,
        deadline_tick=deadline_tick,
    )
    for leg in itinerary.legs:
        matching_rows = [
            row for row in route_rows_by_id[leg.route_id]
            if row["from_place_id"] == leg.from_place_id
            and row["to_place_id"] == leg.to_place_id
            and row["travel_ticks"] == str(leg.travel_ticks)
            and row["from_place_name"] == _place_name(state, leg.from_place_id)
            and row["to_place_name"] == _place_name(state, leg.to_place_id)
        ]
        if len(matching_rows) != 1:
            raise ValueError("A selected itinerary edge does not match its approved visible route row.")

    actor = state.entities[actor_id]
    if actor_id in occupied_npc_ids(state, state.current_tick):
        raise InvalidEvent("The actor has another action in progress and cannot depart now.")
    if active_combat_opponent(state, actor) is not None:
        raise InvalidEvent("An active encounter prevents ordinary travel departure.")
    first_leg = itinerary.legs[0]
    origin = state.entities[first_leg.from_place_id]
    destination = state.entities[first_leg.to_place_id]
    place = PlaceComponent.model_validate(origin.components.get("place"))
    first_route = next(route for route in place.routes if route.route_id == first_leg.route_id)
    if not route_accessible(state, actor, first_route, destination):
        raise InvalidEvent("The first itinerary leg is not currently accessible to this actor.")

    return LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=deadline_tick,
        activity="travel",
        constraints=(),
        stop_conditions=(),
        travel_itinerary=itinerary,
        itinerary_auto_care=intent.itinerary_auto_care,
        depart_immediately=True,
    )


def _itinerary_duration(intent: QueueItineraryIntent, state: WorldState) -> int:
    try:
        if intent.unit == "month":
            # A month is fixed at 30 world-calendar days, independent of year length.
            return study_duration_seconds(intent.amount * 30, "day", state.calendar)
        return study_duration_seconds(intent.amount, intent.unit, state.calendar)
    except ValueError as error:
        raise ValueError("The itinerary time limit is outside the safe duration range.") from error


def _parse_visible_routes(
    visible_routes: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(visible_routes, Sequence) or isinstance(visible_routes, (str, bytes, bytearray)):
        raise ValueError("Itinerary routes must be supplied as the visible ordered graph projection.")
    rows: list[dict[str, object]] = []
    for raw in visible_routes:
        if not isinstance(raw, Mapping) or set(raw) != _VISIBLE_ROUTE_FIELDS:
            raise ValueError("A visible itinerary route row has an unexpected shape.")
        if any(not isinstance(raw[field], str) for field in _VISIBLE_ROUTE_FIELDS):
            raise ValueError("Visible itinerary route fields must be strings.")
        try:
            route_id = _canonical_uuid(raw["route_id"])
            from_place_id = _canonical_uuid(raw["from_place_id"])
            to_place_id = _canonical_uuid(raw["to_place_id"])
        except ValueError as error:
            raise ValueError("Visible itinerary route IDs must be canonical UUID strings.") from error
        ticks_text = raw["travel_ticks"]
        if (
            not ticks_text.isascii()
            or not ticks_text.isdigit()
            or len(ticks_text) > 19
            or str(int(ticks_text)) != ticks_text
            or not 0 < int(ticks_text) <= MAX_TICK
        ):
            raise ValueError("Visible itinerary route duration must be a positive canonical integer string.")
        if not raw["from_place_name"] or not raw["to_place_name"]:
            raise ValueError("Visible itinerary route place names cannot be empty.")
        rows.append({
            "route_id": route_id,
            "from_place_id": from_place_id,
            "from_place_name": raw["from_place_name"],
            "to_place_id": to_place_id,
            "to_place_name": raw["to_place_name"],
            "travel_ticks": ticks_text,
        })
    return rows


def _canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("UUID values must be strings.")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("UUID string is not canonical.")
    return parsed


def _place_name(state: WorldState, place_id: UUID) -> str:
    place = state.entities.get(place_id)
    if place is None:
        raise ValueError("A selected itinerary place is missing from trusted state.")
    return place.name or str(place.id)
