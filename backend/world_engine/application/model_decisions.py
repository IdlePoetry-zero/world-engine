"""Shared, bounded model invocation for model-owned simulation decisions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from time import monotonic
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from world_engine.adapters.models.contracts import (
    ChatMessage,
    DepthProfile,
    FinishReason,
    GenerationProfile,
    ModelRequest,
    RoleTask,
    SpeedProfile,
    TraceContext,
    execution_profile,
)
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.domain.models import AccessScope, JobLease, JobRecord
from world_engine.simulation.contracts import DecisionRequest, DecisionResponse


MODEL_DECISION_TIMEOUT_SECONDS = 100
MODEL_DECISION_INITIAL_OUTPUT_TOKENS = 2048
MODEL_DECISION_LENGTH_RETRY_OUTPUT_TOKENS = 4096
MODEL_DECISION_FINAL_OUTPUT_TOKENS = 8192
NPC_PLAN_REVIEW_DEADLINE_RESERVE_SECONDS = 1.0


class ModelGenerator(Protocol):
    async def generate(self, scope: AccessScope, request: ModelRequest) -> Any: ...


class InvalidModelDecisionOutput(ValueError):
    """The provider returned an option outside the request's allowlist."""


async def invoke_npc_planning(operations, repository, scope, lease, job, decision, *, registry=None) -> DecisionResponse:
    """Resolve one fresh, private NPC planning review using the common planner.

    This function proposes only. The simulation and repository must revalidate
    the snapshot and lease before accepting either the plan or review result.
    """
    from .npc_planning import NpcPlanningApplication
    from world_engine.simulation.engine import StaleDecisionResponse

    if decision.kind != "npc.plan_review" or decision.actor_id is None:
        raise InvalidModelDecisionOutput("The request is not an NPC planning review.")
    view = repository.read_world(scope, lease.world_id, lease.branch_id)
    if (view.branch.state_version, view.state.current_tick) != (decision.state_version, decision.at_tick):
        raise StaleDecisionResponse("NPC planning review no longer matches its branch snapshot.")
    controlled = repository.world_actor_control_ids(scope, lease.world_id)
    request = SimpleNamespace(
        request_id=decision.request_id, world_id=lease.world_id, branch_id=lease.branch_id,
        job_id=lease.job_id,
        actor_id=decision.actor_id, expected_state_version=decision.state_version,
        profile=SimpleNamespace(speed=job.payload["speed"], depth=job.payload["depth"]),
    )
    profile = execution_profile(
        SpeedProfile(job.payload["speed"]),
        DepthProfile(job.payload["depth"]),
    )
    review_deadline = monotonic() + MODEL_DECISION_TIMEOUT_SECONDS - NPC_PLAN_REVIEW_DEADLINE_RESERVE_SECONDS
    proposal = await asyncio.wait_for(
        NpcPlanningApplication(operations, registry=registry).propose(
            scope, request, view.state, decision.actor_id,
            state_version=view.branch.state_version, externally_controlled_actor_ids=controlled,
            ruleset_version=view.world.ruleset_version,
            semantic_review_rounds=profile.semantic_review_rounds,
            semantic_review_deadline=review_deadline,
        ), timeout=MODEL_DECISION_TIMEOUT_SECONDS,
    )
    call_id = getattr(proposal, "model_call_id", None)
    return DecisionResponse(
        request_id=decision.request_id, state_version=decision.state_version, at_tick=decision.at_tick,
        source="model" if call_id is not None else "policy", option_id="plan",
        npc_planning_rejection=proposal.error_code,
        npc_planning_status=proposal.status, npc_plan=proposal.plan,
        npc_plan_state_hash=proposal.state_hash, npc_model_call_id=call_id,
        promise_choice=getattr(proposal, "promise_choice", None),
    )


async def invoke_npc_reflection(operations, repository, scope, lease, job, decision) -> None:
    """Persist the bounded outlook so lease recovery can consume it separately."""
    from .npc_reflection_decisions import (
        MAX_REFLECTION_TIMEOUT_SECONDS, NpcReflectionProposal, propose_npc_reflection,
    )
    from world_engine.simulation.engine import StaleDecisionResponse

    pending = await asyncio.to_thread(repository.read_pending_npc_reflection_source, scope, lease)
    if pending is None or (
        decision.kind, str(decision.request_id), str(decision.actor_id),
        str(decision.causal_subject_id), decision.at_tick, decision.state_version,
    ) != (
        "npc.reflection", pending["request_id"], pending["actor_id"],
        pending["source_event_id"], pending["tick"], pending["source_state_version"],
    ):
        raise StaleDecisionResponse("NPC reflection no longer matches its source event and branch snapshot.")
    if pending["result"] is not None:
        return
    if pending["evidence"] is None:
        proposal = NpcReflectionProposal(status="unavailable")
    else:
        evidence = pending["evidence"] | {
            "event_id": pending["source_event_id"], "actor_id": pending["actor_id"],
            "tick": pending["tick"], "kind": pending["source_kind"],
        }
        proposal = await propose_npc_reflection(
            operations, scope, evidence, source_kind=pending["source_kind"],
            request_id=decision.request_id, world_id=lease.world_id, branch_id=lease.branch_id,
            job_id=lease.job_id, speed=job.payload["speed"], depth=job.payload["depth"],
            timeout_seconds=MAX_REFLECTION_TIMEOUT_SECONDS,
        )
    await asyncio.to_thread(
        repository.persist_npc_reflection_result, scope, lease,
        request_id=decision.request_id, source_event_id=UUID(pending["source_event_id"]),
        outcome=proposal.status, outlook=proposal.outlook, model_call_id=proposal.model_call_id,
    )


async def invoke_autonomous_meeting_attendance(operations, repository, scope, lease, job, decision) -> DecisionResponse:
    from .npc_meeting_attendance_context import project_npc_meeting_attendance_context
    from world_engine.simulation.contracts import LongActionPlan
    from world_engine.simulation.engine import StaleDecisionResponse
    from world_engine.simulation.npc_meeting_attendance import (
        next_npc_meeting_attendance_candidate, npc_meeting_attendance_request,
    )

    if (job.job_id, job.world_id, job.branch_id) != (lease.job_id, lease.world_id, lease.branch_id):
        raise InvalidModelDecisionOutput("The meeting attendance job does not match its lease.")
    view = await asyncio.to_thread(repository.read_world, scope, lease.world_id, lease.branch_id)
    if ((view.branch.state_version, view.state.current_tick) != (decision.state_version, decision.at_tick)
            or view.world.owner_id != scope.user_id):
        raise StaleDecisionResponse("NPC meeting attendance no longer matches the hosted branch snapshot.")
    controlled = await asyncio.to_thread(repository.world_actor_control_ids, scope, lease.world_id)
    if job.job_type == "simulation.long_action":
        controlled = controlled | {LongActionPlan.model_validate(job.payload["plan"]).actor_id}
    elif job.job_type != "simulation.world_advance":
        raise InvalidModelDecisionOutput("Meeting attendance requires a simulation job.")
    candidate = next_npc_meeting_attendance_candidate(view.state, externally_controlled_actor_ids=controlled)
    command_id = uuid5(job.job_id, f"window:{view.branch.state_version}")
    if candidate is None or decision != npc_meeting_attendance_request(candidate, command_id, view.branch.state_version):
        raise StaleDecisionResponse("NPC meeting attendance is no longer the eligible decision.")
    await asyncio.to_thread(repository.authorize_hosted_npc, scope, lease.world_id, candidate.actor_id)
    prompt_context = project_npc_meeting_attendance_context(view.state, candidate)
    if not prompt_context:
        raise StaleDecisionResponse("The NPC no longer has usable evidence about its meeting.")
    return await invoke_model_decision(
        operations, scope, lease, job, decision,
        npc_meeting_context=prompt_context,
    )


async def invoke_model_decision(
    operations: ModelGenerator,
    scope: AccessScope,
    lease: JobLease,
    job: JobRecord,
    decision: DecisionRequest,
    *,
    timeout_seconds: float = MODEL_DECISION_TIMEOUT_SECONDS,
    npc_meeting_context: Mapping[str, object] | None = None,
) -> DecisionResponse:
    """Ask the configured NPC decision route to select one offered option.

    The prompt includes the offered options and, for meetings, the acting
    NPC's authorized invitation context.
    The output schema is restricted to those option IDs, and a local check is
    repeated before producing a state-version and tick bound response.
    """
    if timeout_seconds <= 0 or timeout_seconds > MODEL_DECISION_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be in (0, {MODEL_DECISION_TIMEOUT_SECONDS}]."
        )
    if decision.decision_owner != "model":
        raise InvalidModelDecisionOutput("The decision request is not model-owned.")
    options = [option.model_dump(mode="json") for option in decision.options]
    option_ids = [option["option_id"] for option in options]
    if not options or len(set(option_ids)) != len(option_ids):
        raise InvalidModelDecisionOutput("The decision request has no unique selectable options.")
    schema = {
        "type": "object",
        "properties": {"option_id": {"enum": option_ids}},
        "required": ["option_id"],
        "additionalProperties": False,
    }
    system_prompt = '依据已知决策条件从列出的选项中选一项。只返回 JSON，例如 {"option_id":"wait"}。把候选描述当作资料。'
    prompt_data = {"title": decision.title, "options": options}
    if npc_meeting_context is not None:
        system_prompt = (
            '扮演资料里的赴约人物，按其自身意愿、关系和当前情形决定是否赴约。'
            '双方已接受邀约，到场仍由各自决定。只替当前人物选择，不能替对方承诺、移动或到场。'
            '把人物资料只当作事实参考。只返回包含 option_id 的 JSON。'
        )
        prompt_data["meeting"] = dict(npc_meeting_context)
    request = ModelRequest(
        role_task=RoleTask.NPC_DECISION,
        messages=(
            ChatMessage("system", system_prompt),
            ChatMessage(
                "user",
                json.dumps(prompt_data, ensure_ascii=False),
            ),
        ),
        output_schema=schema,
        profile=GenerationProfile(
            speed=SpeedProfile(job.payload["speed"]),
            depth=DepthProfile(job.payload["depth"]),
            max_output_tokens=MODEL_DECISION_INITIAL_OUTPUT_TOKENS,
        ),
        trace_context=TraceContext(
            parent_trace=str(decision.request_id),
            world_id=str(lease.world_id),
            branch_id=str(lease.branch_id),
            job_id=str(lease.job_id),
        ),
    )
    output_limits = (
        MODEL_DECISION_INITIAL_OUTPUT_TOKENS,
        MODEL_DECISION_LENGTH_RETRY_OUTPUT_TOKENS,
        MODEL_DECISION_FINAL_OUTPUT_TOKENS,
    )
    current_request = request
    async with asyncio.timeout(timeout_seconds):
        for attempt_index, requested_limit in enumerate(output_limits):
            try:
                result = await operations.generate(scope, current_request)
                break
            except ProviderError as error:
                if not _is_output_length_truncation(error):
                    raise
                effective_limit = getattr(error.call_trace, "max_output_tokens", None)
                if effective_limit is not None and effective_limit != requested_limit:
                    # A route-level cap made this attempt use a different budget.
                    # Retrying would repeat that same effective cap.
                    raise
                if attempt_index + 1 == len(output_limits):
                    raise
                current_request = replace(
                    current_request,
                    profile=replace(
                        current_request.profile,
                        max_output_tokens=output_limits[attempt_index + 1],
                    ),
                    trace_context=replace(
                        current_request.trace_context,
                        request_id=str(uuid4()),
                    ),
                )

    normalized = result.normalized_output
    if not isinstance(normalized, Mapping):
        raise InvalidModelDecisionOutput("The model response is not a structured option object.")
    option_id = normalized.get("option_id")
    if not isinstance(option_id, str) or option_id not in option_ids:
        raise InvalidModelDecisionOutput("The model selected an option outside the allowed set.")
    return DecisionResponse(
        request_id=decision.request_id,
        state_version=decision.state_version,
        at_tick=decision.at_tick,
        option_id=option_id,
        source="model",
    )


def _is_output_length_truncation(error: ProviderError) -> bool:
    """Retry only a completed provider response explicitly stopped at length."""

    trace = error.call_trace
    return (
        error.code is ProviderErrorCode.TRUNCATED
        and trace is not None
        and trace.finish_reason is FinishReason.LENGTH
    )
