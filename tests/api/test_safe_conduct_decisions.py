from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import DepthProfile, RoleTask, SpeedProfile
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.safe_conduct_decisions import (
    SAFE_CONDUCT_RESPONSE_SCHEMA,
    choose_safe_conduct_response,
)
from world_engine.domain.components import (
    GovernanceComponent,
    KnowledgeRecord,
    OfficeRecord,
    PersonalityComponent,
    PlaceComponent,
    PlaceRoute,
    RelationRecord,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG, SafeConductTreaty, propose_safe_conduct
from world_engine.domain.reducers import state_json


WORLD_ID = UUID("f1000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("f2000000-0000-4000-8000-000000000001")
PLAYER_ID = UUID("f3000000-0000-4000-8000-000000000001")
SOURCE_REGION_ID = UUID("f4000000-0000-4000-8000-000000000001")
RECIPIENT_REGION_ID = UUID("f4000000-0000-4000-8000-000000000002")
ORIGIN_ID = UUID("f4000000-0000-4000-8000-000000000003")
DESTINATION_ID = UUID("f4000000-0000-4000-8000-000000000004")
PROPOSER_ID = UUID("f5000000-0000-4000-8000-000000000001")
RECIPIENT_ID = UUID("f5000000-0000-4000-8000-000000000002")
SOURCE_MEMBER_ID = UUID("f5000000-0000-4000-8000-000000000003")
SOURCE_ORGANIZATION_ID = UUID("f6000000-0000-4000-8000-000000000001")
RECIPIENT_ORGANIZATION_ID = UUID("f6000000-0000-4000-8000-000000000002")
CURRENT_TICK = 50_000


class FakeModels:
    def __init__(self, normalized_output=None, error=None):
        self.normalized_output = normalized_output
        self.error = error
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(normalized_output=self.normalized_output)


def _fixture():
    route = PlaceRoute(
        route_id=uuid4(),
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=3600,
        access_tags=[SAFE_CONDUCT_TAG, "route.passport"],
        travel_modes=["travel.walk"],
    )
    source_governance = GovernanceComponent(
        revision=4,
        members=[PROPOSER_ID, SOURCE_MEMBER_ID],
        leader_id=PROPOSER_ID,
        charter="SOURCE_PRIVATE_CHARTER_MUST_NOT_ENTER_PROMPT",
        offices=[OfficeRecord(
            office_id="office.leader", title="Source Steward", holder_id=PROPOSER_ID,
            authority=["authority.represent"],
        )],
    )
    recipient_governance = GovernanceComponent(
        revision=8,
        members=[RECIPIENT_ID],
        leader_id=RECIPIENT_ID,
        charter="RECIPIENT_PRIVATE_CHARTER_MUST_NOT_ENTER_PROMPT",
        offices=[OfficeRecord(
            office_id="office.leader", title="Recipient Steward", holder_id=RECIPIENT_ID,
            authority=["authority.represent"],
        )],
    )
    entities = {
        PLAYER_ID: WorldEntity(
            id=PLAYER_ID, kind="character", name="Player",
            components={"personality": PersonalityComponent(traits=["PLAYER_PRIVATE_TRAIT"]).model_dump(mode="json")},
        ),
        SOURCE_REGION_ID: WorldEntity(
            id=SOURCE_REGION_ID, kind="place", name="North Region",
            components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
        ),
        RECIPIENT_REGION_ID: WorldEntity(
            id=RECIPIENT_REGION_ID, kind="place", name="South Region",
            components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
        ),
        ORIGIN_ID: WorldEntity(
            id=ORIGIN_ID, kind="place", name="North Gate",
            components={"place": PlaceComponent(
                place_type="settlement", region_id=SOURCE_REGION_ID, routes=[route],
            ).model_dump(mode="json")},
        ),
        DESTINATION_ID: WorldEntity(
            id=DESTINATION_ID, kind="place", name="South Gate",
            components={"place": PlaceComponent(
                place_type="settlement", region_id=RECIPIENT_REGION_ID,
            ).model_dump(mode="json")},
        ),
        PROPOSER_ID: WorldEntity(
            id=PROPOSER_ID, kind="character", name="Alden",
            components={
                "personality": PersonalityComponent(traits=["SOURCE_PRIVATE_PERSONALITY"]).model_dump(mode="json"),
                "public_role": "Northern envoy",
                "public_description": "Represents the northern league in formal talks.",
            },
        ),
        RECIPIENT_ID: WorldEntity(
            id=RECIPIENT_ID, kind="character", name="Rui",
            components={
                "personality": PersonalityComponent(
                    openness=Decimal("0.8"), conscientiousness=Decimal("0.7"),
                    extraversion=Decimal("0.4"), agreeableness=Decimal("0.6"),
                    neuroticism=Decimal("0.2"), traits=["measured"], values=["reciprocity"],
                ).model_dump(mode="json"),
                "public_role": "Southern steward",
                "public_description": "Coordinates travel and trade for the southern league.",
            },
        ),
        SOURCE_MEMBER_ID: WorldEntity(
            id=SOURCE_MEMBER_ID, kind="character", name="North Member",
        ),
        SOURCE_ORGANIZATION_ID: WorldEntity(
            id=SOURCE_ORGANIZATION_ID,
            kind="organization",
            name="Northern League",
            location_id=SOURCE_REGION_ID,
            components={
                "governance": source_governance.model_dump(mode="json"),
                "public_role": "Regional association",
                "public_description": "Coordinates the northern settlements.",
            },
        ),
        RECIPIENT_ORGANIZATION_ID: WorldEntity(
            id=RECIPIENT_ORGANIZATION_ID,
            kind="organization",
            name="Southern League",
            location_id=RECIPIENT_REGION_ID,
            components={
                "governance": recipient_governance.model_dump(mode="json"),
                "public_role": "Regional association",
                "public_description": "Coordinates the southern settlements.",
            },
        ),
    }
    base = WorldState(
        current_tick=CURRENT_TICK,
        player_id=PLAYER_ID,
        entities=entities,
        relations=[RelationRecord(
            id=uuid4(), from_id=RECIPIENT_ID, to_id=PROPOSER_ID,
            dimensions={"trust": Decimal("88")}, label="PRIVATE_RELATION_LABEL", updated_tick=CURRENT_TICK,
        )],
        knowledge=[KnowledgeRecord(
            id=uuid4(), holder_id=RECIPIENT_ID, subject_id=PROPOSER_ID,
            kind="memory", claim="PRIVATE_MEMORY_MARKER: an unrelated secret.", received_tick=CURRENT_TICK,
        )],
    )
    treaty = propose_safe_conduct(
        base,
        treaty_id=uuid4(),
        proposer_organization_id=SOURCE_ORGANIZATION_ID,
        recipient_organization_id=RECIPIENT_ORGANIZATION_ID,
        proposer_actor_id=PROPOSER_ID,
        route=route,
        expires_at_tick=CURRENT_TICK + 720_000,
    )
    state = WorldState.model_validate({
        **base.model_dump(mode="python"),
        "safe_conduct_treaties": [treaty],
    })
    return state, treaty


def _request():
    return MessageRequest(
        request_id=uuid4(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        session_id=uuid4(),
        actor_id=PLAYER_ID,
        expected_state_version="5",
        text="PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT",
        profile=QualityProfile(speed="patient", depth="exhaustive"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["ratify", "decline", "defer", "counter_half", "counter_day"])
async def test_decision_uses_closed_schema_and_only_public_terms_plus_recipient_self(choice):
    state, treaty = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()
    models = FakeModels({"choice": choice})
    before = state_json(state)

    result = await choose_safe_conduct_response(models, scope, request, state, treaty)

    assert result == choice
    assert len(models.calls) == 1
    observed_scope, model_request = models.calls[0]
    assert observed_scope == scope
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.output_schema == {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": [
            "ratify", "decline", "defer", "counter_half", "counter_day",
        ]}},
        "required": ["choice"],
        "additionalProperties": False,
    }
    assert model_request.profile.speed is SpeedProfile.PATIENT
    assert model_request.profile.depth is DepthProfile.EXHAUSTIVE
    assert model_request.trace_context.parent_trace == str(request.request_id)
    assert model_request.trace_context.world_id == str(WORLD_ID)
    assert model_request.trace_context.branch_id == str(BRANCH_ID)

    assert len(model_request.messages) == 2
    context = json.loads(model_request.messages[1].content)
    assert set(context) == {"recipient", "parties", "treaty_terms", "current_tick"}
    assert context["recipient"]["name"] == "Rui"
    assert context["recipient"]["personality"]["traits"] == ["measured"]
    assert context["recipient"]["personality"]["values"] == ["reciprocity"]
    assert context["parties"] == {
        "proposer": {
            "organization": {
                "name": "Northern League",
                "public_role": "Regional association",
                "public_description": "Coordinates the northern settlements.",
            },
            "representative": {
                "name": "Alden",
                "public_role": "Northern envoy",
                "public_description": "Represents the northern league in formal talks.",
            },
        },
        "recipient": {
            "organization": {
                "name": "Southern League",
                "public_role": "Regional association",
                "public_description": "Coordinates the southern settlements.",
            },
        },
    }
    terms = context["treaty_terms"]
    assert terms["treaty_id"] == str(treaty.treaty_id)
    assert terms["status"] == "pending"
    assert terms["expires_at_tick"] == treaty.expires_at_tick
    assert terms["counter_options"] == {
        "counter_half": {"expires_at_tick": CURRENT_TICK + (treaty.expires_at_tick - CURRENT_TICK) // 2},
        "counter_day": {"expires_at_tick": CURRENT_TICK + state.calendar.day_seconds},
    }
    assert terms["route"]["origin_name"] == "North Gate"
    assert terms["route"]["destination_name"] == "South Gate"
    assert terms["route"]["travel_ticks"] == 3600
    assert "governance_revision" not in terms

    prompt = "\n".join(message.content for message in model_request.messages)
    for forbidden in (
        "PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT",
        "PRIVATE_MEMORY_MARKER",
        "PRIVATE_RELATION_LABEL",
        "SOURCE_PRIVATE_PERSONALITY",
        "SOURCE_PRIVATE_CHARTER_MUST_NOT_ENTER_PROMPT",
        "RECIPIENT_PRIVATE_CHARTER_MUST_NOT_ENTER_PROMPT",
        "PLAYER_PRIVATE_TRAIT",
        str(SOURCE_MEMBER_ID),
    ):
        assert forbidden not in prompt
    assert state_json(state) == before


@pytest.mark.asyncio
async def test_counter_choice_is_removed_when_domain_has_no_valid_counter_deadline():
    state, treaty = _fixture()
    short_treaty = treaty.model_copy(update={"expires_at_tick": CURRENT_TICK + 1})
    short_state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "safe_conduct_treaties": [short_treaty.model_dump(mode="json")],
    })
    models = FakeModels({"choice": "counter_half"})

    result = await choose_safe_conduct_response(
        models,
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        _request(),
        short_state,
        short_treaty,
    )

    assert result == "defer"
    assert len(models.calls) == 1
    model_request = models.calls[0][1]
    assert model_request.output_schema == SAFE_CONDUCT_RESPONSE_SCHEMA
    assert model_request.output_schema["properties"]["choice"]["enum"] == ["ratify", "decline", "defer"]
    context = json.loads(model_request.messages[1].content)
    assert context["treaty_terms"]["counter_options"] == {}


@pytest.mark.asyncio
async def test_stale_expired_changed_or_player_treaties_defer_without_provider_call():
    state, treaty = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()
    models = FakeModels({"choice": "ratify"})

    stale = treaty.model_copy(update={"revision": treaty.revision + 1})
    assert await choose_safe_conduct_response(models, scope, request, state, stale) == "defer"
    assert not models.calls

    missing = WorldState.model_validate({
        **state.model_dump(mode="python"), "safe_conduct_treaties": [],
    })
    assert await choose_safe_conduct_response(models, scope, request, missing, treaty) == "defer"
    assert not models.calls

    expired = WorldState.model_validate({
        **state.model_dump(mode="python"), "current_tick": treaty.expires_at_tick,
    })
    assert await choose_safe_conduct_response(models, scope, request, expired, treaty) == "defer"
    assert not models.calls

    changed_org = state.entities[RECIPIENT_ORGANIZATION_ID]
    governance = GovernanceComponent.model_validate(changed_org.components["governance"])
    changed_org = changed_org.model_copy(update={
        "components": {**changed_org.components, "governance": governance.model_copy(
            update={"revision": governance.revision + 1},
        ).model_dump(mode="json")},
    })
    changed_governance = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, RECIPIENT_ORGANIZATION_ID: changed_org},
    })
    assert await choose_safe_conduct_response(models, scope, request, changed_governance, treaty) == "defer"
    assert not models.calls

    player_recipient = WorldState.model_validate({
        **state.model_dump(mode="python"), "player_id": RECIPIENT_ID,
    })
    assert await choose_safe_conduct_response(models, scope, request, player_recipient, treaty) == "defer"
    assert not models.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, "ratify", {}, {"choice": "unknown"}, {"choice": "ratify", "reason": "extra"}])
async def test_malformed_model_output_defers(output):
    state, treaty = _fixture()
    models = FakeModels(output)

    result = await choose_safe_conduct_response(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), _request(), state, treaty,
    )

    assert result == "defer"


@pytest.mark.asyncio
async def test_provider_failure_propagates():
    state, treaty = _fixture()
    failure = RuntimeError("provider unavailable")
    models = FakeModels(error=failure)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await choose_safe_conduct_response(
            models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), _request(), state, treaty,
        )
