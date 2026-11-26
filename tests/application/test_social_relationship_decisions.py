from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import FinishReason, RoleTask, TraceContext
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application.social_relationship_decisions import (
    KEY_RELATIONSHIP_INTERPRETATION_SCHEMA,
    ValidatedRelationshipFact,
    interpret_key_relationship_evidence,
)
from world_engine.domain.components import RelationRecord
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.reducers import state_hash


WORLD_ID = UUID("10000000-0000-0000-0000-000000000001")
BRANCH_ID = UUID("20000000-0000-0000-0000-000000000002")


def _fixture():
    observer_id, counterpart_id, other_id, place_id = [uuid4() for _ in range(4)]
    state = WorldState(
        entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Meeting room"),
            observer_id: WorldEntity(
                id=observer_id,
                kind="character",
                name="Rae",
                location_id=place_id,
            ),
            counterpart_id: WorldEntity(
                id=counterpart_id,
                kind="character",
                name="Morgan",
                location_id=place_id,
            ),
            other_id: WorldEntity(id=other_id, kind="character", name="Other"),
        },
        relations=[RelationRecord(
            id=uuid4(),
            from_id=observer_id,
            to_id=counterpart_id,
            dimensions={"trust": "74"},
            label="PRIVATE_RELATION_LABEL",
        )],
        knowledge=[],
    )
    facts = (
        ValidatedRelationshipFact(
            event_id=uuid4(),
            from_id=observer_id,
            to_id=counterpart_id,
            dimension="trust",
            event_type="core.promise_breached.v1",
            summary="The counterpart missed the agreed return deadline after confirming the item was available.",
        ),
        ValidatedRelationshipFact(
            event_id=uuid4(),
            from_id=observer_id,
            to_id=counterpart_id,
            dimension="trust",
            event_type="core.promise_item_returned_late.v1",
            summary="The counterpart later returned the same item, after the deadline.",
        ),
    )
    return state, observer_id, counterpart_id, other_id, facts


def _trace() -> TraceContext:
    return TraceContext(
        request_id="request-social-relationship",
        parent_trace="conversation-123",
        world_id=str(WORLD_ID),
        branch_id=str(BRANCH_ID),
    )


class StubModels:
    def __init__(self, output=None, *, error=None, finish_reason=FinishReason.STOP, call_id="key-reason-call"):
        self.output = output
        self.error = error
        self.finish_reason = finish_reason
        self.call_id = call_id
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            normalized_output=self.output,
            finish_reason=self.finish_reason,
            call_trace=SimpleNamespace(request_id=self.call_id, finish_reason=self.finish_reason),
        )


def _call(models, state, observer_id, counterpart_id, facts, **kwargs):
    return asyncio.run(interpret_key_relationship_evidence(
        models,
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        state,
        _trace(),
        observer_id=observer_id,
        counterpart_id=counterpart_id,
        dimension="trust",
        evidence=facts,
        **kwargs,
    ))


def test_interpretation_uses_only_the_exact_validated_directed_evidence_and_returns_a_closed_class():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels({"decision": "classify", "reason_class": "relationship_repair"})
    before = state_hash(state)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())

    result = asyncio.run(interpret_key_relationship_evidence(
        models,
        scope,
        state,
        _trace(),
        observer_id=observer_id,
        counterpart_id=counterpart_id,
        dimension="trust",
        evidence=facts,
    ))

    assert result.status == "interpreted"
    assert result.reason_class == "relationship_repair"
    assert result.observer_id == observer_id
    assert result.counterpart_id == counterpart_id
    assert result.dimension == "trust"
    assert result.evidence_event_ids == tuple(fact.event_id for fact in facts)
    assert result.model_call_id == "key-reason-call"
    observed_scope, request = models.calls[0]
    assert observed_scope == scope
    assert request.role_task is RoleTask.NPC_DECISION
    assert request.schema_ref == "key_relationship_interpretation_v1"
    assert request.output_schema == KEY_RELATIONSHIP_INTERPRETATION_SCHEMA
    assert request.trace_context == _trace()
    assert request.profile.max_output_tokens == 256

    for field_name in ("score", "value", "delta", "event_id", "evidence_event_ids", "explanation"):
        assert field_name not in request.output_schema["properties"]
    assert "绝不能输出分数" in request.messages[0].content
    context = json.loads(request.messages[1].content)
    assert context["observer"] == {"id": str(observer_id), "name": "Rae"}
    assert context["counterpart"] == {"id": str(counterpart_id), "name": "Morgan"}
    assert [row["event_id"] for row in context["validated_evidence"]] == [
        str(fact.event_id) for fact in facts
    ]
    prompt = "\n".join(message.content for message in request.messages)
    assert "PRIVATE_RELATION_LABEL" not in prompt
    assert "Other" not in prompt
    assert state_hash(state) == before


def test_decline_returns_same_actor_dimension_and_event_bindings_without_a_reason():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels({"decision": "decline"}, call_id="declined-call")

    result = _call(models, state, observer_id, counterpart_id, facts)

    assert result.status == "declined"
    assert result.reason_class is None
    assert result.evidence_event_ids == tuple(fact.event_id for fact in facts)
    assert result.model_call_id == "declined-call"


@pytest.mark.parametrize(
    "output",
    [
        None,
        "classify",
        {},
        {"decision": "classify"},
        {"decision": "classify", "reason_class": "invented"},
        {"decision": "classify", "reason_class": "betrayal", "score": 100},
        {"decision": "decline", "reason_class": "betrayal"},
        {"decision": "classify", "reason_class": "betrayal", "event_ids": [str(uuid4())]},
    ],
)
def test_malformed_or_out_of_contract_output_fails_closed(output):
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels(output)

    result = _call(models, state, observer_id, counterpart_id, facts)

    assert result.status == "unavailable"
    assert result.reason_class is None
    assert result.evidence_event_ids == tuple(fact.event_id for fact in facts)


def test_missing_model_is_an_optional_unavailable_result_with_input_bindings():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()

    result = _call(None, state, observer_id, counterpart_id, facts)

    assert result.status == "unavailable"
    assert result.evidence_event_ids == tuple(fact.event_id for fact in facts)
    assert result.model_call_id is None


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_id"),
    [
        (ProviderError(ProviderErrorCode.UNAVAILABLE, "offline"), "unavailable", None),
        (
            ProviderError(
                ProviderErrorCode.TRUNCATED,
                "cut off",
                call_trace=SimpleNamespace(request_id="truncated-call"),
            ),
            "truncated",
            "truncated-call",
        ),
    ],
)
def test_provider_failure_is_graceful_and_preserves_only_a_real_call_id(error, expected_status, expected_id):
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels(error=error)

    result = _call(models, state, observer_id, counterpart_id, facts)

    assert result.status == expected_status
    assert result.model_call_id == expected_id
    assert result.evidence_event_ids == tuple(fact.event_id for fact in facts)


def test_truncated_success_is_not_accepted_even_with_structured_output():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels(
        {"decision": "classify", "reason_class": "betrayal"},
        finish_reason=FinishReason.LENGTH,
    )

    result = _call(models, state, observer_id, counterpart_id, facts)

    assert result.status == "truncated"
    assert result.reason_class is None
    assert result.model_call_id == "key-reason-call"


def test_timeout_is_unavailable_and_does_not_invent_a_call_id():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()

    class SlowModels:
        async def generate(self, scope, request):
            await asyncio.sleep(0.05)
            return SimpleNamespace(
                normalized_output={"decision": "decline"},
                finish_reason=FinishReason.STOP,
                call_trace=SimpleNamespace(request_id="not-returned"),
            )

    result = _call(
        SlowModels(), state, observer_id, counterpart_id, facts, timeout_seconds=0.01,
    )

    assert result.status == "unavailable"
    assert result.model_call_id is None


def test_provider_cancellation_propagates_to_the_caller():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels(error=ProviderError(ProviderErrorCode.CANCELLED, "cancelled"))

    with pytest.raises(ProviderError):
        _call(models, state, observer_id, counterpart_id, facts)


@pytest.mark.parametrize("invalid", ["inactive", "same_actor", "missing_relation", "reverse_fact", "wrong_dimension"])
def test_invalid_actor_pair_or_event_binding_is_rejected_before_model_call(invalid):
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    if invalid == "inactive":
        entities = dict(state.entities)
        entities[counterpart_id] = entities[counterpart_id].model_copy(update={"status": "retired"})
        state = state.model_copy(update={"entities": entities})
    elif invalid == "same_actor":
        counterpart_id = observer_id
    elif invalid == "missing_relation":
        state = state.model_copy(update={"relations": []})
    elif invalid == "reverse_fact":
        facts = (facts[0].__class__(
            event_id=facts[0].event_id,
            from_id=counterpart_id,
            to_id=observer_id,
            dimension="trust",
            event_type=facts[0].event_type,
            summary=facts[0].summary,
        ),)
    elif invalid == "wrong_dimension":
        facts = (facts[0].__class__(
            event_id=facts[0].event_id,
            from_id=observer_id,
            to_id=counterpart_id,
            dimension="respect",
            event_type=facts[0].event_type,
            summary=facts[0].summary,
        ),)
    models = StubModels({"decision": "decline"})

    with pytest.raises(ValueError):
        _call(models, state, observer_id, counterpart_id, facts)
    assert models.calls == []


def test_duplicate_or_excessive_evidence_is_rejected_before_model_call():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    duplicate = (facts[0], facts[0])
    too_many = tuple(
        ValidatedRelationshipFact(
            event_id=uuid4(),
            from_id=observer_id,
            to_id=counterpart_id,
            dimension="trust",
            event_type="core.promise_breached.v1",
            summary="A validated bounded fact.",
        )
        for _ in range(9)
    )
    models = StubModels({"decision": "decline"})

    with pytest.raises(ValueError, match="unique"):
        _call(models, state, observer_id, counterpart_id, duplicate)
    with pytest.raises(ValueError, match="at most 8"):
        _call(models, state, observer_id, counterpart_id, too_many)
    assert models.calls == []


def test_non_uuid_event_ids_and_oversized_summaries_are_rejected():
    state, observer_id, counterpart_id, _other_id, facts = _fixture()
    models = StubModels({"decision": "decline"})
    malformed_id = ValidatedRelationshipFact(
        event_id="not-a-uuid",  # type: ignore[arg-type]
        from_id=observer_id,
        to_id=counterpart_id,
        dimension="trust",
        event_type="core.promise_breached.v1",
        summary="A validated bounded fact.",
    )
    oversized = ValidatedRelationshipFact(
        event_id=uuid4(),
        from_id=observer_id,
        to_id=counterpart_id,
        dimension="trust",
        event_type="core.promise_breached.v1",
        summary="x" * 361,
    )

    with pytest.raises(ValueError, match="UUID"):
        _call(models, state, observer_id, counterpart_id, (malformed_id,))
    with pytest.raises(ValueError, match="summary"):
        _call(models, state, observer_id, counterpart_id, (oversized,))
    assert models.calls == []
