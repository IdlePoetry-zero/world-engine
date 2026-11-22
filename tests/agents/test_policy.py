from __future__ import annotations

from uuid import UUID
from uuid import uuid4

import pytest

from world_engine.agents import AgentPolicy, build_agent_knowledge
from world_engine.application.actions import action_scene
from world_engine.content import ContentCatalog
from world_engine.domain import InventoryComponent
from world_engine.domain.components import KnowledgeRecord, VisibilityScope
from world_engine.domain.models import WorldEntity, WorldState


@pytest.fixture(scope="module")
def full_state():
    return ContentCatalog().build_world("modern_city", "Agent memory", "Mina Sol").state


def _with_food_need(state: WorldState, actor_id: UUID, value: int) -> WorldState:
    raw = state.model_dump(mode="json")
    entity = raw["entities"][str(actor_id)]
    entity["components"]["needs"]["food"]["value"] = value
    return WorldState.model_validate(raw)


def test_agent_memory_excludes_other_people_private_knowledge(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    actor_records = [
        item for item in full_state.knowledge
        if actor_id in {item.owner_id, item.holder_id}
    ]
    other_private = [
        item for item in full_state.knowledge
        if item.visibility.policy == "private" and actor_id not in {item.owner_id, item.holder_id}
    ]
    assert actor_records and other_private

    view = build_agent_knowledge(full_state, actor_id)

    assert {item.id for item in view.known_records}.issubset({item.id for item in actor_records})
    assert not ({item.id for item in view.known_records} & {item.id for item in other_private})
    assert all(goal.owner_id == actor_id for goal in view.active_goals)
    assert all(not hasattr(entity, "components") for entity in view.visible_entities)
    assert all(not hasattr(entity, "components") for entity in view.visible_entities)


def test_agent_and_action_context_exclude_author_only_and_future_owned_claims(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    actor = full_state.entities[actor_id]
    assert actor.location_id is not None
    current_tick = full_state.current_tick
    hidden_id = uuid4()
    hidden_entity = WorldEntity(
        id=hidden_id, kind="character", name="hidden knowledge sentinel",
        location_id=actor.location_id, status="active", components={"hidden": True},
    )
    records = [
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, subject_id=hidden_id, kind="observation",
            claim="R05 author-only observation sentinel", received_tick=current_tick,
            valid_from_tick=current_tick, visibility=VisibilityScope(policy="author_only"),
        ),
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="R05 author-only owner claim sentinel",
            received_tick=current_tick, visibility=VisibilityScope(policy="author_only"),
        ),
        KnowledgeRecord(
            id=uuid4(), holder_id=actor_id, claim="R05 author-only holder claim sentinel",
            visibility=VisibilityScope(policy="author_only"),
        ),
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="R05 future receipt sentinel",
            received_tick=current_tick + 1,
        ),
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="R05 future validity sentinel",
            received_tick=current_tick, valid_from_tick=current_tick + 1,
        ),
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, claim="R05 exact tick sentinel",
            received_tick=current_tick, valid_from_tick=current_tick,
        ),
    ]
    state = full_state.model_copy(update={
        "entities": {**full_state.entities, hidden_id: hidden_entity},
        "knowledge": [*full_state.knowledge, *records],
    })

    knowledge = build_agent_knowledge(state, actor_id)
    knowledge_by_claim = {record.claim: record for record in knowledge.known_records}
    assert "R05 exact tick sentinel" in knowledge_by_claim
    assert not any("R05 " in claim and claim != "R05 exact tick sentinel" for claim in knowledge_by_claim)
    assert hidden_id not in {entity.id for entity in knowledge.visible_entities}

    scene = action_scene(state.model_dump(mode="json"), str(actor_id), authoritative=state)
    scene_json = str(scene)
    assert "R05 exact tick sentinel" in {record["claim"] for record in scene["knowledge"]}
    assert all(
        marker not in scene_json
        for marker in (
            "R05 author-only observation sentinel", "R05 author-only owner claim sentinel",
            "R05 author-only holder claim sentinel", "R05 future receipt sentinel", "R05 future validity sentinel",
        )
    )
    assert str(hidden_id) not in scene["entities"]

    authorized_state = state.model_copy(update={"knowledge": [
        *state.knowledge,
        KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, subject_id=hidden_id, kind="observation",
            claim="R05 current private observation sentinel", received_tick=current_tick,
            valid_from_tick=current_tick, visibility=VisibilityScope(policy="private"),
        ),
    ]})
    authorized_knowledge = build_agent_knowledge(authorized_state, actor_id)
    assert hidden_id in {entity.id for entity in authorized_knowledge.visible_entities}
    assert any(record.claim == "R05 current private observation sentinel" for record in authorized_knowledge.known_records)


def test_ordinary_policy_prioritizes_owned_food_over_requested_work(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    actor = full_state.entities[actor_id]
    has_food = any(
        account.owner_id == actor_id
        and account.resource_id == full_state.metadata["resource_roles"]["food_resource_id"]
        and account.balance > 0
        for account in full_state.resources.values()
    )
    assert has_food
    state = _with_food_need(full_state, actor_id, 10)

    decision = AgentPolicy().choose(state, actor_id, "work", food_available=True)

    assert decision.activity == "eat"
    assert decision.reason_code == "critical_food_need"
    assert decision.knowledge.actor_id == actor_id


def test_policy_does_not_invent_a_food_source(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    state = _with_food_need(full_state, actor_id, 10)

    decision = AgentPolicy().choose(state, actor_id, "wait", food_available=False)

    assert decision.activity == "wait"
    assert all(candidate.activity != "eat" for candidate in decision.candidates)


def test_background_self_care_uses_only_real_stock_and_sorted_active_agents(full_state: WorldState) -> None:
    resident_ids = [UUID(value) for value in full_state.metadata["content_index"]["other_resident_ids"]]
    eater_id, sleeper_id, drinker_id, unaffected_id = resident_ids[:4]
    raw = full_state.model_dump(mode="json")
    raw["entities"][str(eater_id)]["components"]["needs"]["food"]["value"] = 10
    raw["entities"][str(eater_id)]["components"]["needs"]["rest"]["value"] = 60
    raw["entities"][str(sleeper_id)]["components"]["needs"]["food"]["value"] = 70
    raw["entities"][str(sleeper_id)]["components"]["needs"]["rest"]["value"] = 10
    raw["entities"][str(drinker_id)]["components"]["needs"]["food"]["value"] = 70
    raw["entities"][str(drinker_id)]["components"]["needs"]["water"]["value"] = 10
    raw["entities"][str(drinker_id)]["components"]["needs"]["rest"]["value"] = 60
    raw["entities"][str(unaffected_id)]["components"]["needs"]["food"]["value"] = 70
    raw["entities"][str(unaffected_id)]["components"]["needs"]["rest"]["value"] = 60
    state = WorldState.model_validate(raw)

    decisions = AgentPolicy().choose_background(
        state,
        exclude_actor_ids=frozenset({state.player_id}) if state.player_id else frozenset(),
    )

    assert [(item.actor_id, item.activity) for item in decisions] == sorted(
        [(eater_id, "eat"), (sleeper_id, "rest"), (drinker_id, "drink")], key=lambda value: str(value[0])
    )
    food_resource_id = state.metadata["resource_roles"]["food_resource_id"]
    eater = state.entities[eater_id]
    eater_choice = next(item for item in decisions if item.actor_id == eater_id)
    assert eater_choice.resource_id == food_resource_id
    assert eater_choice.account_id is not None
    assert eater_choice.quantity is not None and eater_choice.quantity > 0
    assert state.resources[eater_choice.account_id].owner_id == eater_id
    assert any(
        lot.account_id == eater_choice.account_id
        and lot.resource_id == food_resource_id
        and (lot.expiry_tick is None or lot.expiry_tick > state.current_tick)
        for lot in InventoryComponent.model_validate(eater.components["inventory"]).lots
    )
    drinker_choice = next(item for item in decisions if item.actor_id == drinker_id)
    assert drinker_choice.activity == "drink"
    assert drinker_choice.resource_id == state.metadata["resource_roles"]["water_resource_id"]
    assert drinker_choice.account_id is not None and state.resources[drinker_choice.account_id].owner_id == drinker_id
    assert drinker_choice.quantity is not None and drinker_choice.quantity > 0


def test_ordinary_policy_selects_drinking_from_owned_water(full_state: WorldState) -> None:
    actor_id = full_state.player_id
    assert actor_id is not None
    actor = full_state.entities[actor_id]
    water_resource = full_state.metadata["resource_roles"]["water_resource_id"]
    water_stock = next(
        lot for lot in InventoryComponent.model_validate(actor.components["inventory"]).lots
        if lot.resource_id == water_resource
    )
    raw = full_state.model_dump(mode="json")
    raw["entities"][str(actor_id)]["components"]["needs"]["water"]["value"] = 10
    state = WorldState.model_validate(raw)

    decision = AgentPolicy().choose(
        state,
        actor_id,
        "work",
        food_available=True,
        water_available=True,
    )

    assert decision.activity == "drink"
    assert decision.reason_code == "critical_water_need"
    assert state.resources[water_stock.account_id].owner_id == actor_id


def test_background_self_care_boundary_is_exact_and_requires_food_stock(full_state: WorldState) -> None:
    resident_ids = [UUID(value) for value in full_state.metadata["content_index"]["other_resident_ids"]]
    cared_id, unstocked_id = resident_ids[:2]
    food_resource_id = full_state.metadata["resource_roles"]["food_resource_id"]
    raw = full_state.model_dump(mode="json")
    raw["entities"][str(cared_id)]["components"]["needs"]["food"]["value"] = 38
    raw["entities"][str(cared_id)]["components"]["needs"]["rest"]["value"] = 60
    unstocked = raw["entities"][str(unstocked_id)]
    unstocked["components"]["needs"]["food"]["value"] = 10
    unstocked["components"]["needs"]["rest"]["value"] = 60
    removed_food_lots = [
        lot for lot in unstocked["components"]["inventory"]["lots"]
        if lot["resource_id"] == food_resource_id
    ]
    unstocked["components"]["inventory"]["lots"] = [
        lot for lot in unstocked["components"]["inventory"]["lots"]
        if lot["resource_id"] != food_resource_id
    ]
    # Keep the test's ledger consistent: an empty inventory lot cannot leave
    # spendable balance on the same owned holder account.
    for lot in removed_food_lots:
        raw["resources"][lot["account_id"]]["balance"] = "0"
    state = WorldState.model_validate(raw)
    excluded = frozenset(
        entity.id for entity in state.entities.values()
        if entity.kind in {"character", "person"} and entity.id != cared_id
    )

    boundary = AgentPolicy().next_background_self_care_boundary(state, exclude_actor_ids=excluded)

    assert boundary == state.current_tick + state.calendar.day_seconds
    assert AgentPolicy().choose_background(
        state,
        exclude_actor_ids=frozenset({state.player_id, cared_id}) if state.player_id else frozenset({cared_id}),
    ) == ()
