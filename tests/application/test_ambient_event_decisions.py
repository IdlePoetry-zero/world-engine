import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import FinishReason, RoleTask, TraceContext
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application.ambient_event_decisions import propose_ambient_event
from world_engine.domain.ambient_events import AmbientEventPolicy, AmbientEventProposal, AmbientEventRecord
from world_engine.domain.models import AccessScope, WorldEntity, WorldState


def _fixture_state():
    region_id, place_id, npc_id, player_id, excluded_npc_id = [uuid4() for _ in range(5)]
    policy = AmbientEventPolicy(
        scope_kind="region", scope_id=region_id, check_every_days=7,
        chance_basis_points=2_500,
    )
    old_event = AmbientEventRecord(
        event_id=uuid4(), schedule_id=uuid4(), scope_id=region_id,
        start_tick=5, end_tick=20, cooldown_until_tick=30,
        proposal=AmbientEventProposal(
            kind="local_observation", place_id=place_id, initiator_id=npc_id,
            title="Market lanterns", description="Lanterns appeared near the market.",
        ),
    )
    other_event = AmbientEventRecord(
        event_id=uuid4(), schedule_id=uuid4(), scope_id=uuid4(),
        start_tick=8, end_tick=20, cooldown_until_tick=30,
        proposal=AmbientEventProposal(
            kind="rumor", place_id=place_id, initiator_id=excluded_npc_id,
            title="PRIVATE_OUTSIDE_SCOPE", description="PRIVATE_EVENT_DESCRIPTION",
        ),
    )
    state = WorldState(
        current_tick=10,
        player_id=player_id,
        entities={
            region_id: WorldEntity(
                id=region_id, kind="place", name="North Region",
                components={"public_description": "A public region."},
            ),
            place_id: WorldEntity(id=place_id, kind="place", name="Market Square", location_id=region_id),
            npc_id: WorldEntity(
                id=npc_id, kind="character", name="Courier Lin", location_id=place_id,
                components={"public_role": "Courier"},
            ),
            player_id: WorldEntity(id=player_id, kind="character", name="PRIVATE_PLAYER_MARKER", location_id=place_id),
            excluded_npc_id: WorldEntity(
                id=excluded_npc_id, kind="character", name="EXCLUDED_NPC_MARKER", location_id=place_id,
                components={"public_role": "PRIVATE_ROLE_MARKER"},
            ),
        },
        ambient_events=[old_event, other_event],
    )
    return state, policy, npc_id, place_id, player_id, excluded_npc_id


class StubModels:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if self.error is not None:
            raise self.error
        return self.result


def _result(output, *, call_id="actual-ambient-call", finish_reason=FinishReason.STOP):
    return SimpleNamespace(
        normalized_output=output,
        finish_reason=finish_reason,
        call_trace=SimpleNamespace(request_id=call_id, finish_reason=finish_reason),
    )


def _request_context():
    return TraceContext(
        request_id="scheduled-opportunity",
        parent_trace="simulation-job",
        world_id=str(uuid4()),
        branch_id=str(uuid4()),
        job_id=str(uuid4()),
    )


def test_proposal_uses_only_bounded_public_scope_and_eligible_npc_context():
    state, policy, npc_id, place_id, _player_id, _excluded_npc_id = _fixture_state()
    models = StubModels(_result({
        "decision": "propose",
        "proposal": {
            "kind": "request_for_help",
            "place_id": str(place_id),
            "initiator_id": str(npc_id),
            "title": "A loose cart wheel",
            "description": "The courier asks for help securing a cart wheel.",
            "duration_days": 2,
        },
    }))
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())

    result = asyncio.run(propose_ambient_event(
        models, scope, state, policy, {npc_id: place_id}, _request_context(), due_tick=10,
    ))

    assert result.status == "proposed"
    assert result.model_call_id == "actual-ambient-call"
    assert result.proposal is not None and result.proposal.initiator_id == npc_id
    request = models.calls[0][1]
    assert request.role_task is RoleTask.NPC_DECISION
    assert request.trace_context.request_id == "scheduled-opportunity"
    assert request.output_schema["properties"]["decision"]["enum"] == ["propose", "abstain"]
    assert "没有世界写入权" in request.messages[0].content
    prompt = json.loads(request.messages[1].content)
    assert prompt["scope"]["name"] == "North Region"
    assert prompt["eligible_npcs"] == [{
        "id": str(npc_id),
        "name": "Courier Lin",
        "public_role": "Courier",
        "place_id": str(place_id),
        "place_name": "Market Square",
    }]
    assert prompt["recent_public_event_mentions"] == [{
        "tick": 5,
        "kind": "local_observation",
        "title": "Market lanterns",
        "description": "Lanterns appeared near the market.",
        "place_name": "Market Square",
    }]
    serialized = request.messages[1].content
    for marker in (
        "PRIVATE_PLAYER_MARKER", "EXCLUDED_NPC_MARKER", "PRIVATE_ROLE_MARKER",
        "PRIVATE_OUTSIDE_SCOPE", "PRIVATE_EVENT_DESCRIPTION",
    ):
        assert marker not in serialized


def test_abstention_is_deferred_and_keeps_the_actual_call_id():
    state, policy, npc_id, place_id, *_ = _fixture_state()
    models = StubModels(_result({"decision": "abstain"}, call_id="call-abstained"))

    result = asyncio.run(propose_ambient_event(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
        {npc_id: place_id}, _request_context(), due_tick=10,
    ))

    assert result.status == "deferred"
    assert result.proposal is None
    assert result.model_call_id == "call-abstained"


def test_output_outside_the_projected_eligible_set_fails_closed():
    state, policy, _npc_id, place_id, *_ = _fixture_state()
    foreign_id = uuid4()
    models = StubModels(_result({
        "decision": "propose",
        "proposal": {
            "kind": "rumor", "place_id": str(place_id), "initiator_id": str(foreign_id),
            "title": "A rumor", "description": "A passerby repeats a rumor.",
        },
    }))

    result = asyncio.run(propose_ambient_event(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
        {_npc_id: place_id}, _request_context(), due_tick=10,
    ))

    assert result.status == "unavailable"
    assert result.proposal is None
    assert result.model_call_id == "actual-ambient-call"


def test_truncated_success_response_is_not_accepted_even_if_it_looks_structured():
    state, policy, npc_id, place_id, *_ = _fixture_state()
    models = StubModels(_result({
        "decision": "propose",
        "proposal": {
            "kind": "rumor", "place_id": str(place_id), "initiator_id": str(npc_id),
            "title": "A rumor", "description": "A passerby repeats a rumor.",
        },
    }, finish_reason=FinishReason.LENGTH))

    result = asyncio.run(propose_ambient_event(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
        {npc_id: place_id}, _request_context(), due_tick=10,
    ))

    assert result.status == "truncated"
    assert result.proposal is None
    assert result.model_call_id == "actual-ambient-call"


def test_truncated_provider_error_preserves_only_its_trace_id():
    state, policy, npc_id, place_id, *_ = _fixture_state()
    error = ProviderError(
        ProviderErrorCode.TRUNCATED,
        "Output was truncated.",
        call_trace=SimpleNamespace(request_id="provider-truncated-call", finish_reason=FinishReason.LENGTH),
    )
    models = StubModels(error=error)

    result = asyncio.run(propose_ambient_event(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
        {npc_id: place_id}, _request_context(), due_tick=10,
    ))

    assert result.status == "truncated"
    assert result.model_call_id == "provider-truncated-call"


def test_provider_unavailable_without_trace_does_not_invent_a_call_id():
    state, policy, npc_id, place_id, *_ = _fixture_state()
    models = StubModels(error=ProviderError(ProviderErrorCode.UNAVAILABLE, "No provider response."))

    result = asyncio.run(propose_ambient_event(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
        {npc_id: place_id}, _request_context(), due_tick=10,
    ))

    assert result.status == "unavailable"
    assert result.proposal is None
    assert result.model_call_id is None


def test_player_cannot_be_added_to_the_eligible_actor_projection():
    state, policy, npc_id, place_id, player_id, _excluded_npc_id = _fixture_state()
    models = StubModels(_result({"decision": "abstain"}))

    with pytest.raises(ValueError, match="eligible"):
        asyncio.run(propose_ambient_event(
            models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
            {npc_id: place_id, player_id: place_id}, _request_context(), due_tick=10,
        ))
    assert models.calls == []


def test_timeout_returns_unavailable_without_an_invented_call_id():
    state, policy, npc_id, place_id, *_ = _fixture_state()

    class SlowModels:
        async def generate(self, scope, request):
            await asyncio.sleep(0.05)
            return _result({"decision": "abstain"}, call_id="must-not-be-used")

    result = asyncio.run(propose_ambient_event(
        SlowModels(), AccessScope(tenant_id=uuid4(), user_id=uuid4()), state, policy,
        {npc_id: place_id}, _request_context(), due_tick=10, timeout_seconds=0.01,
    ))

    assert result.status == "unavailable"
    assert result.model_call_id is None
