"""Privacy and fail-closed checks for the hosted combat truce decision."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.application.combat_truce_decisions import (
    COMBAT_TRUCE_RESPONSE_SCHEMA,
    choose_combat_truce_response,
)
from world_engine.domain.combat_truce import CombatTruceOffer
from world_engine.domain.components import (
    CombatEncounterComponent,
    PersonalityComponent,
    RelationRecord,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState


def _fixture(*, stale=False):
    player_id, npc_id, place_id = uuid4(), uuid4(), uuid4()
    encounter_id, last_event_id = uuid4(), uuid4()
    recorded_count = 2 if stale else 3

    def encounter(opponent_id):
        return CombatEncounterComponent(
            encounter_id=encounter_id,
            opponent_id=opponent_id,
            location_id=place_id,
            started_tick=40,
            exchange_count=3,
            last_event_id=last_event_id,
            last_outcome="hit",
        ).model_dump(mode="json")

    npc_personality = PersonalityComponent(
        traits=[f"trait-{index}" for index in range(10)],
        values=[f"value-{index}" for index in range(10)],
    )
    offer = CombatTruceOffer(
        offer_id=uuid4(),
        encounter_id=encounter_id,
        proposer_id=player_id,
        recipient_id=npc_id,
        location_id=place_id,
        exchange_count=recorded_count,
        last_event_id=last_event_id if not stale else uuid4(),
        offered_tick=50,
    )
    state = WorldState(
        current_tick=50,
        player_id=player_id,
        entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Old Bridge"),
            player_id: WorldEntity(
                id=player_id,
                kind="character",
                name="Player",
                location_id=place_id,
                components={
                    "combat_encounter": encounter(npc_id),
                    "health": {"current": 100, "maximum": 100},
                    "personality": {"traits": ["PLAYER_PRIVATE_MARKER"]},
                },
            ),
            npc_id: WorldEntity(
                id=npc_id,
                kind="character",
                name="Rae",
                location_id=place_id,
                components={
                    "combat_encounter": encounter(player_id),
                    "health": {
                        "current": 8,
                        "maximum": 100,
                        "injuries": [{
                            "injury_id": str(uuid4()),
                            "injury_type": "wound",
                            "severity": 8,
                            "started_tick": 49,
                            "description": "NPC_PRIVATE_INJURY_MARKER",
                        }],
                    },
                    "personality": npc_personality.model_dump(mode="json"),
                },
            ),
        },
        combat_truce_offers=[offer],
        relations=[RelationRecord(
            id=uuid4(),
            from_id=npc_id,
            to_id=player_id,
            dimensions={"trust": -35},
            updated_tick=49,
        )],
    )
    request = SimpleNamespace(
        request_id=uuid4(),
        world_id=uuid4(),
        branch_id=uuid4(),
        actor_id=player_id,
        profile=SimpleNamespace(speed="fast", depth="standard"),
        text="PLAYER_INSTRUCTION_PRIVATE_MARKER",
    )
    return state, offer, request


class _Models:
    def __init__(self, output):
        self.output = output
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        return SimpleNamespace(normalized_output=self.output)


@pytest.mark.asyncio
async def test_current_truce_decision_uses_only_bounded_recipient_context():
    state, offer, request = _fixture()
    models = _Models({"choice": "accept"})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())

    choice = await choose_combat_truce_response(models, scope, request, state, offer)

    assert choice == "accept"
    assert len(models.calls) == 1
    _scope, model_request = models.calls[0]
    assert model_request.output_schema == COMBAT_TRUCE_RESPONSE_SCHEMA
    assert model_request.output_schema["additionalProperties"] is False
    context = json.loads(model_request.messages[-1].content)
    encoded = json.dumps(context, ensure_ascii=False)
    assert context["recipient"]["name"] == "Rae"
    assert context["recipient"]["personality"]["traits"] == [f"trait-{index}" for index in range(8)]
    assert context["recipient"]["personality"]["values"] == [f"value-{index}" for index in range(8)]
    assert context["recipient"]["own_active_encounter"]["last_outcome"] == "hit"
    assert context["recent_pair_impression"] == "对提议者缺乏信任"
    assert context["offer"]["offered_tick"] == 50
    assert context["offer"]["exchange_count"] == 3
    assert "PLAYER_INSTRUCTION_PRIVATE_MARKER" not in encoded
    assert "PLAYER_PRIVATE_MARKER" not in encoded
    assert "NPC_PRIVATE_INJURY_MARKER" not in encoded
    assert '"current": 8' not in encoded
    assert "last_event_id" not in encoded


@pytest.mark.asyncio
async def test_stale_offer_fails_closed_without_calling_model():
    state, offer, request = _fixture(stale=True)
    models = _Models({"choice": "accept"})

    choice = await choose_combat_truce_response(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), request, state, offer,
    )

    assert choice == "defer"
    assert models.calls == []


@pytest.mark.asyncio
async def test_mismatched_requester_and_malformed_model_choice_fail_closed():
    state, offer, request = _fixture()
    request.actor_id = uuid4()
    models = _Models({"choice": "surrender"})

    choice = await choose_combat_truce_response(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), request, state, offer,
    )

    assert choice == "defer"
    assert models.calls == []

    request.actor_id = offer.proposer_id
    choice = await choose_combat_truce_response(
        models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), request, state, offer,
    )
    assert choice == "defer"
    assert len(models.calls) == 1
