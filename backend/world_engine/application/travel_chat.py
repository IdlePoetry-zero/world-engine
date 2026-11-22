"""Durable chat replies and queueing for boundary-limited travel intents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from world_engine.api.contracts import MessageRequest
from world_engine.domain.components import PlaceComponent, PlaceRoute
from world_engine.domain.models import AccessScope, JobRecord, JobStatus, WorldState, WorldView
from world_engine.rules.contracts import MoveIntent, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.simulation import LongActionPlan

from .simulation_jobs import SimulationJobs


MAX_TICK = 2**63 - 1
_UNSTARTED_TRAVEL_REASONS = {
    "travel_not_started_before_target": "window_elapsed",
    "travel_exceeds_requested_window": "route_no_longer_fits_window",
}


def travel_message_key(scope: AccessScope, request_id: UUID) -> str:
    """Return the stable account-scoped job key for a travel chat request."""

    return f"travel-message:{scope.user_id}:{request_id}"


def select_move_route(state: WorldState, actor_id: UUID, intent: MoveIntent) -> PlaceRoute | None:
    """Select the same shortest registered route that ordinary move rules use."""

    actor = state.entities.get(actor_id)
    if actor is None or actor.location_id is None:
        return None
    origin = state.entities.get(actor.location_id)
    if origin is None or origin.kind not in {"place", "location"}:
        return None
    try:
        place = PlaceComponent.model_validate(origin.components.get("place"))
    except Exception:
        return None
    candidates = [
        route for route in place.routes
        if route.to_place_id == intent.destination_id
        and (intent.route_id is None or route.route_id == intent.route_id)
    ]
    return min(candidates, key=lambda route: (route.travel_ticks, str(route.route_id)), default=None)


def travel_queue_reply(request_id: UUID, job: JobRecord) -> dict[str, Any]:
    """Rebuild the original immutable reply from the persisted travel plan."""

    if job.job_type != "simulation.long_action":
        raise ValueError("The referenced job is not a simulation long action.")
    plan = LongActionPlan.model_validate(job.payload.get("plan"))
    if plan.activity != "travel" or plan.route_id is None or plan.started_tick is None:
        raise ValueError("The referenced long action is not a persisted travel plan.")
    duration = plan.target_tick - plan.started_tick
    if not 0 < duration <= MAX_TICK:
        raise ValueError("The persisted travel plan has an invalid duration.")
    timing = (f"登记行程 {duration} 秒，预计抵达世界时刻 {plan.target_tick}，途中仍可能暂停。"
              if plan.depart_immediately else f"计划持续 {duration} 秒，目标世界时刻为 {plan.target_tick}。")
    return {
        "message_id": str(request_id),
        "kind": "action",
        "content": (
            f"旅行作业已排队，{timing}"
            "作业开始后按模拟窗口推进；本次排队没有推进世界时间。"
        ),
        "world_effect": False,
        "action_status": "succeeded",
        "job_id": str(job.job_id),
        "elapsed_ticks": "0",
        "references": [str(plan.route_id)],
    }


def travel_job_readback(
    journey: dict[str, Any], jobs: Sequence[JobRecord], current_tick: int,
) -> dict[str, Any] | None:
    """Match the actor's current journey to its job without exposing job payloads."""
    cursor = {"actor_id": journey["actor_id"], "route_id": journey["route_id"],
              "started_tick": journey["departed_tick"]}
    for job in jobs:
        if job.job_type != "simulation.long_action" or job.committed_through_tick is None:
            continue
        if not journey["departed_tick"] <= job.committed_through_tick <= current_tick:
            continue
        plan = job.payload.get("plan")
        if not isinstance(plan, dict) or plan.get("activity") != "travel":
            continue
        if any(plan.get(field) != value for field, value in cursor.items()):
            continue
        interruption = (job.result or {}).get("interruption")
        stopped_for_survival = (
            isinstance(interruption, dict)
            and interruption.get("subject_id") == journey["actor_id"]
            and interruption.get("tick") == job.committed_through_tick
            and interruption.get("reason_code") == "survival_pressure"
        )
        return {
            "status": job.status.value,
            "committed_through_tick": job.committed_through_tick,
            "survival_pressure": stopped_for_survival,
        }
    return None


def recent_resolved_travel(
    state: WorldState, actor_id: UUID, visible_entities: dict[str, Any],
) -> list[dict[str, Any]]:
    journeys = sorted(
        (journey for journey in state.travel_journeys if journey.actor_id == actor_id
         and journey.resolved_tick is not None and journey.resolved_tick <= state.current_tick),
        key=lambda journey: (journey.resolved_tick, str(journey.journey_id)),
        reverse=True,
    )
    return [
        {
            "journey_id": str(journey.journey_id),
            "status": journey.status,
            "departed_tick": journey.departed_tick,
            "resolved_tick": journey.resolved_tick,
            "stationary_ticks": journey.stationary_ticks,
            "from_place": visible_entities.get(str(journey.from_place_id), {}).get("name"),
            "to_place": visible_entities.get(str(journey.to_place_id), {}).get("name"),
        }
        for journey in journeys[:4]
    ]


def recent_unstarted_travel(
    actor_id: UUID, jobs: Sequence[JobRecord], current_tick: int,
) -> list[dict[str, Any]]:
    """Report pauses that explicitly recorded that the actor never departed."""
    rows = []
    for job in jobs:
        if job.job_type != "simulation.long_action" or job.status != JobStatus.PAUSED:
            continue
        plan = job.payload.get("plan")
        interruption = (job.result or {}).get("interruption")
        if not isinstance(plan, dict) or not isinstance(interruption, dict):
            continue
        started_tick = plan.get("started_tick")
        stopped_tick = job.committed_through_tick
        reason = interruption.get("reason_code")
        if (
            plan.get("actor_id") != str(actor_id) or plan.get("activity") != "travel"
            or interruption.get("subject_id") != str(actor_id)
            or not isinstance(reason, str) or reason not in _UNSTARTED_TRAVEL_REASONS
            or type(started_tick) is not int or stopped_tick is None
            or interruption.get("tick") != stopped_tick
            or not started_tick <= stopped_tick <= current_tick
        ):
            continue
        rows.append({
            "job_id": str(job.job_id), "status": "paused_before_departure",
            "planned_start_tick": started_tick, "paused_tick": stopped_tick,
            "reason": _UNSTARTED_TRAVEL_REASONS[reason],
        })
        if len(rows) == 4:
            break
    return rows


def _rejected_reply(request_id: UUID, message: str) -> dict[str, Any]:
    return {
        "message_id": str(request_id),
        "kind": "action",
        "content": message,
        "world_effect": False,
        "action_status": "rejected",
        "elapsed_ticks": "0",
        "references": [],
    }


def enqueue_travel_for_move(
    scope: AccessScope,
    view: WorldView,
    request: MessageRequest,
    intent: MoveIntent,
    simulation_jobs: SimulationJobs,
    rule_engine: RuleEngine,
) -> dict[str, Any]:
    """Queue a durable route plan after rechecking departure eligibility.

    The caller decides whether the ordinary move hit a duration boundary; this
    fallback only handles an explicit ``MoveIntent``.  It never handles retreat
    or commits the departure event itself.
    """

    if view.world.world_id != request.world_id or view.branch.branch_id != request.branch_id:
        raise ValueError("The authorized view does not match the travel request scope.")
    route = select_move_route(view.state, request.actor_id, intent)
    if route is None:
        return _rejected_reply(request.request_id, "当前地点没有通往所选目的地的登记路线。")

    start_tick = view.state.current_tick
    target_tick = start_tick + route.travel_ticks
    if target_tick > MAX_TICK:
        return _rejected_reply(request.request_id, "这段路线的目标时间超出世界时钟范围，未安排旅行。")

    eligibility = rule_engine.evaluate_travel_departure(
        view.state,
        route.route_id,
        RuleContext(
            world_id=view.world.world_id,
            branch_id=view.branch.branch_id,
            command_id=request.request_id,
            actor_id=request.actor_id,
            ruleset_version=view.world.ruleset_version,
        ),
    )
    if eligibility.outcome.status != "succeeded":
        return _rejected_reply(
            request.request_id,
            "当前条件下无法开始这段旅程，请检查角色状态与路线通行条件。",
        )

    plan = LongActionPlan(
        actor_id=request.actor_id,
        started_tick=start_tick,
        target_tick=target_tick,
        activity="travel",
        route_id=route.route_id,
        depart_immediately=True,
    )
    job = simulation_jobs.enqueue(
        scope,
        view.world.world_id,
        view.branch.branch_id,
        plan,
        idempotency_key=travel_message_key(scope, request.request_id),
        speed=request.profile.speed,
        depth=request.profile.depth,
        expected_state_version=int(request.expected_state_version),
    )
    return travel_queue_reply(request.request_id, job)
