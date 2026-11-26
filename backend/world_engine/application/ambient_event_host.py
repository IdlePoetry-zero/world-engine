"""Load the authorized decision checkpoint and obtain an ambient proposal."""

import asyncio

from world_engine.adapters.models.contracts import TraceContext
from world_engine.domain.ambient_events import AMBIENT_EVENT_HANDLER, AmbientEventPolicy, ambient_participants
from world_engine.domain.reducers import state_hash
from world_engine.simulation.contracts import DecisionResponse
from world_engine.simulation.engine import StaleDecisionResponse


async def invoke_ambient_event(operations, repository, scope, lease, job, decision) -> DecisionResponse:
    from .ambient_event_decisions import propose_ambient_event

    view, controlled = await asyncio.gather(
        asyncio.to_thread(repository.read_world, scope, lease.world_id, lease.branch_id),
        asyncio.to_thread(repository.world_actor_control_ids, scope, lease.world_id),
    )
    if (
        view.world.owner_id != scope.user_id or job.job_id != lease.job_id
        or decision.kind != AMBIENT_EVENT_HANDLER
        or (view.branch.state_version, view.state.current_tick) != (decision.state_version, decision.at_tick)
    ):
        raise StaleDecisionResponse("The ambient decision no longer matches its authorized checkpoint.")
    schedule = next((row for row in view.state.scheduled_events if row.id == decision.causal_subject_id), None)
    if schedule is None or schedule.handler_id != AMBIENT_EVENT_HANDLER or schedule.due_tick != decision.at_tick:
        raise StaleDecisionResponse("The ambient decision has no current schedule.")
    policy = AmbientEventPolicy.model_validate(schedule.payload)
    eligible = ambient_participants(view.state, policy, controlled=controlled)
    result = await propose_ambient_event(
        operations, scope, view.state, policy, eligible,
        TraceContext(request_id=str(decision.request_id), world_id=str(lease.world_id),
                     branch_id=str(lease.branch_id), job_id=str(lease.job_id)),
        decision.at_tick,
    )
    outcome = "accepted" if result.status == "proposed" and result.model_call_id is not None else (
        "unavailable" if result.status in {"unavailable", "truncated"} else "deferred"
    )
    return DecisionResponse(
        request_id=decision.request_id, state_version=decision.state_version, at_tick=decision.at_tick,
        source="model" if result.model_call_id is not None else "policy", option_id="ambient",
        ambient_outcome=outcome, ambient_model_call_id=result.model_call_id,
        ambient_proposal=result.proposal if outcome == "accepted" else None,
        ambient_state_hash=await asyncio.to_thread(state_hash, view.state),
    )
