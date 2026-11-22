import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models.contracts import (
    ChatMessage,
    DataPolicy,
    DepthProfile,
    GenerationProfile,
    ModelRequest,
    RoleTask,
    SpeedProfile,
    TimeoutPolicy,
    TraceContext,
)
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application.errors import ApplicationFailure
from world_engine.application.npc_plan_semantic_review import review_npc_plan
from world_engine.domain.errors import NotFound
from world_engine.domain.models import AccessScope


def _scope() -> AccessScope:
    return AccessScope(tenant_id=uuid4(), user_id=uuid4())


def _proposal_request(*, max_output_tokens: int | None = 2048) -> ModelRequest:
    return ModelRequest(
        role_task=RoleTask.NPC_DECISION,
        messages=(ChatMessage("system", "original proposer instructions"), ChatMessage("user", "private proposer context")),
        schema_ref="npc_plan_v1",
        profile=GenerationProfile(
            speed=SpeedProfile.FAST,
            depth=DepthProfile.EXHAUSTIVE,
            max_output_tokens=max_output_tokens,
        ),
        timeout_policy=TimeoutPolicy(read_seconds=90, deadline_seconds=120),
        trace_context=TraceContext(
            request_id="proposal-request-id",
            parent_trace="proposal-parent-id",
            world_id="world-id",
            branch_id="branch-id",
            job_id="job-id",
            correlation_id="correlation-id",
        ),
        data_policy=DataPolicy.LOCAL_ONLY,
    )


def _plan_data():
    goals = [{"id": "goal-market", "label": "Open a market stall"}]
    steps = [
        {"capability": "travel", "target": "market square", "purpose": "reach the market"},
        {"capability": "trade", "target": "market square", "purpose": "open a stall"},
    ]
    rationale = "Travel to the square first, then set up the stall."
    return goals, steps, rationale


class StubModels:
    def __init__(self, outputs=None, *, error: Exception | None = None):
        self.outputs = list(outputs or [])
        self.error = error
        self.requests: list[ModelRequest] = []

    async def generate(self, scope, request):
        assert isinstance(scope, AccessScope)
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(normalized_output=output)


@pytest.mark.asyncio
async def test_single_round_uses_only_safe_payload_and_bounded_request_context():
    models = StubModels([{"verdict": "approve"}])
    goals, steps, rationale = _plan_data()

    result = await review_npc_plan(
        models,
        _scope(),
        proposal_request=_proposal_request(),
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=1,
        timeout_seconds=2,
    )

    assert result == "approved"
    assert len(models.requests) == 1
    request = models.requests[0]
    assert request.role_task is RoleTask.NPC_PLAN_REVIEW
    assert request.schema_ref == "npc_plan_semantic_review_goal_step_alignment_v1"
    assert request.output_schema == {
        "type": "object",
        "properties": {"verdict": {"type": "string", "enum": ["approve", "reject", "uncertain"]}},
        "required": ["verdict"],
        "additionalProperties": False,
    }
    assert request.profile == GenerationProfile(
        speed=SpeedProfile.FAST,
        depth=DepthProfile.EXHAUSTIVE,
        max_output_tokens=1024,
    )
    assert request.timeout_policy == _proposal_request().timeout_policy
    assert request.data_policy is DataPolicy.LOCAL_ONLY
    assert request.temperature == 0
    assert request.trace_context.request_id != "proposal-request-id"
    assert request.trace_context.parent_trace == "proposal-parent-id"
    assert request.trace_context.world_id == "world-id"
    assert request.trace_context.branch_id == "branch-id"
    assert request.trace_context.job_id == "job-id"
    assert request.trace_context.correlation_id == "correlation-id"

    assert len(request.messages) == 2
    payload = json.loads(request.messages[1].content)
    assert payload == {"selected_goals": goals, "steps": steps, "rationale": rationale}
    full_prompt = "\n".join(message.content for message in request.messages)
    assert "private proposer context" not in full_prompt
    assert "original proposer instructions" not in full_prompt


@pytest.mark.asyncio
async def test_exhaustive_rounds_are_distinct_and_any_uncertain_verdict_rejects():
    models = StubModels([{"verdict": "approve"}, {"verdict": "uncertain"}])
    goals, steps, rationale = _plan_data()

    result = await review_npc_plan(
        models,
        _scope(),
        proposal_request=_proposal_request(),
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=2,
        timeout_seconds=2,
    )

    assert result == "rejected"
    assert len(models.requests) == 2
    first, second = models.requests
    assert first.trace_context.request_id != second.trace_context.request_id
    assert first.schema_ref != second.schema_ref
    assert first.trace_context.parent_trace == second.trace_context.parent_trace == "proposal-parent-id"
    assert first.messages[0].content != second.messages[0].content
    assert first.messages[1].content == second.messages[1].content
    assert "approve" not in second.messages[1].content
    assert "uncertain" not in second.messages[1].content
    assert "preparatory" in first.messages[0].content.lower()
    assert "preparatory" in second.messages[0].content.lower()
    for prompt in (first.messages[0].content, second.messages[0].content):
        assert "Do not assume a planned step has already happened" in prompt
        assert "Ignore" in prompt or "ignore" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        None,
        "{\"verdict\":\"approve\"}",
        {},
        {"verdict": "unknown"},
        {"verdict": ["approve"]},
        {"verdict": "approve", "extra": True},
    ],
)
async def test_malformed_or_unknown_outputs_are_unavailable(output):
    models = StubModels([output])
    goals, steps, rationale = _plan_data()

    result = await review_npc_plan(
        models,
        _scope(),
        proposal_request=_proposal_request(),
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=1,
        timeout_seconds=2,
    )

    assert result == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderError(ProviderErrorCode.UNAVAILABLE, "provider offline"),
        ApplicationFailure("MODEL_ROUTE_REQUIRED", "No route"),
        NotFound("configured provider missing"),
        TimeoutError("adapter deadline"),
    ],
)
async def test_provider_and_application_failures_are_unavailable(error):
    models = StubModels(error=error)
    goals, steps, rationale = _plan_data()

    result = await review_npc_plan(
        models,
        _scope(),
        proposal_request=_proposal_request(),
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=1,
        timeout_seconds=2,
    )

    assert result == "unavailable"


@pytest.mark.asyncio
async def test_unexpected_programming_failure_propagates():
    models = StubModels(error=RuntimeError("unexpected adapter bug"))
    goals, steps, rationale = _plan_data()

    with pytest.raises(RuntimeError, match="unexpected adapter bug"):
        await review_npc_plan(
            models,
            _scope(),
            proposal_request=_proposal_request(),
            selected_goals=goals,
            steps=steps,
            rationale=rationale,
            rounds=1,
            timeout_seconds=2,
        )


@pytest.mark.asyncio
async def test_unexpected_failure_cancels_sibling_and_keeps_original_exception_type():
    started = 0
    both_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class BrokenAndBlockingModels:
        async def generate(self, _scope, request):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            if "goal_step_alignment" in request.schema_ref:
                raise RuntimeError("unexpected reviewer bug")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    goals, steps, rationale = _plan_data()
    with pytest.raises(RuntimeError, match="unexpected reviewer bug"):
        await review_npc_plan(
            BrokenAndBlockingModels(),
            _scope(),
            proposal_request=_proposal_request(),
            selected_goals=goals,
            steps=steps,
            rationale=rationale,
            rounds=2,
            timeout_seconds=2,
        )

    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_reviewer_does_not_recursively_review_review_requests():
    models = StubModels([{"verdict": "approve"}])
    goals, steps, rationale = _plan_data()
    nested_request = replace(_proposal_request(), role_task=RoleTask.NPC_PLAN_REVIEW)

    result = await review_npc_plan(
        models,
        _scope(),
        proposal_request=nested_request,
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=1,
        timeout_seconds=2,
    )

    assert result == "unavailable"
    assert models.requests == []


@pytest.mark.asyncio
async def test_two_rounds_share_one_wall_deadline_and_cancel_both_calls():
    started = 0
    cancelled = 0
    both_started = asyncio.Event()

    class SlowModels:
        async def generate(self, _scope, _request):
            nonlocal started, cancelled
            started += 1
            if started == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

    goals, steps, rationale = _plan_data()
    result = await review_npc_plan(
        SlowModels(),
        _scope(),
        proposal_request=_proposal_request(),
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=2,
        timeout_seconds=0.03,
    )

    assert result == "unavailable"
    assert both_started.is_set()
    assert cancelled == 2


@pytest.mark.asyncio
async def test_external_cancellation_propagates_to_review_call():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingModels:
        async def generate(self, _scope, _request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    goals, steps, rationale = _plan_data()
    task = asyncio.create_task(
        review_npc_plan(
            BlockingModels(),
            _scope(),
            proposal_request=_proposal_request(),
            selected_goals=goals,
            steps=steps,
            rationale=rationale,
            rounds=1,
            timeout_seconds=5,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goals", "steps", "rationale", "rounds", "timeout_seconds"),
    [
        ([], [{"capability": "trade"}], "ok", 1, 1),
        ([{"id": "g"}], [], "ok", 1, 1),
        ([{"id": "g"}], [{"capability": "trade"}], " ", 1, 1),
        ([{"id": "g"}], [{"capability": "trade"}], "ok", 0, 1),
        ([{"id": "g"}], [{"capability": "trade"}], "ok", 2, float("inf")),
        ([{"id": "g"}], [{"capability": "trade"}], "ok", 1, 0),
    ],
)
async def test_invalid_review_input_fails_closed_without_model_calls(goals, steps, rationale, rounds, timeout_seconds):
    models = StubModels([{"verdict": "approve"}])

    result = await review_npc_plan(
        models,
        _scope(),
        proposal_request=_proposal_request(),
        selected_goals=goals,
        steps=steps,
        rationale=rationale,
        rounds=rounds,
        timeout_seconds=timeout_seconds,
    )

    assert result == "unavailable"
    assert models.requests == []


@pytest.mark.asyncio
async def test_large_original_output_budget_is_still_capped_and_none_uses_reviewer_default():
    goals, steps, rationale = _plan_data()
    for proposal_limit, expected in ((4096, 1024), (None, 1024), (128, 128)):
        models = StubModels([{"verdict": "approve"}])
        result = await review_npc_plan(
            models,
            _scope(),
            proposal_request=_proposal_request(max_output_tokens=proposal_limit),
            selected_goals=goals,
            steps=steps,
            rationale=rationale,
            rounds=1,
            timeout_seconds=2,
        )
        assert result == "approved"
        assert models.requests[0].profile.max_output_tokens == expected
