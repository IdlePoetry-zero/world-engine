from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import (
    GoalCondition,
    GoalRecord,
    KnowledgeRecord,
    PersonalityComponent,
    PersonalityLockViolation,
    PopulationComponent,
    RelationRecord,
)
from world_engine.domain.errors import InsufficientResources, InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import (
    AuthorComponentRevisedEvent,
    ComponentReplacedEvent,
    DamageAppliedEvent,
    EntityDiscoveredEvent,
    GoalCreatedEvent,
    GoalUpdatedEvent,
    HealthRestoredEvent,
    KnowledgeAcquiredEvent,
    MovementCompletedEvent,
    ProvenanceReference,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
    ResourceConsumedEvent,
    ResourceProducedEvent,
    ConsumedResourceFlow,
    ProducedResourceFlow,
    ResourceConversion,
)
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.npc_goal_completion import npc_goal_fulfillment_closure
from world_engine.domain.reducers import (
    apply_event,
    author_value_hash,
    ledger_postings,
    validate_ledger_postings,
)


def test_registered_component_replacement_requires_revision_and_author_edit_hash() -> None:
    character_id = uuid4()
    entity = WorldEntity(
        id=character_id,
        kind="character",
        name="Mira",
        components={
            "personality": {"openness": "0.4", "revision": 1},
            "public_description": "A careful navigator.",
        },
    )
    state = WorldState(entities={character_id: entity})

    replaced = apply_event(
        state,
        ComponentReplacedEvent(
            entity_id=character_id,
            component_name="personality",
            expected_revision=1,
            replacement={"revision": 2, "openness": "0.8"},
        ),
    )
    assert replaced.entities[character_id].components["personality"]["openness"] == "0.8"
    with pytest.raises(InvalidEvent, match="expected_revision"):
        apply_event(
            replaced,
            ComponentReplacedEvent(
                entity_id=character_id,
                component_name="personality",
                expected_revision=1,
                replacement={"revision": 3, "openness": "0.7"},
            ),
        )
    with pytest.raises(InvalidEvent):
        apply_event(
            replaced,
            {
                "type": "core.component_replaced.v1",
                "entity_id": str(character_id),
                "component_name": "personality",
                "expected_revision": 2,
                "replacement": {"revision": 3, "secret_override": True},
            },
        )
    with pytest.raises(InvalidEvent, match="Unregistered event type"):
        parse_event({"type": "core.entity_json_patch.v1", "patch": {"anything": True}})

    revised = apply_event(
        replaced,
        AuthorComponentRevisedEvent(
            entity_id=character_id,
            author_id=uuid4(),
            component_name="public_description",
            expected_previous_hash=author_value_hash("A careful navigator."),
            replacement="A patient navigator with a talent for charts.",
            revision_note="Clarify the character profile.",
            provenance=[ProvenanceReference(kind="pack", reference_id="harbor-stories" )],
        ),
    )
    assert revised.entities[character_id].components["public_description"] == (
        "A patient navigator with a talent for charts."
    )
    existing_personality = revised.entities[character_id].components["personality"]
    revised = apply_event(
        revised,
        AuthorComponentRevisedEvent(
            entity_id=character_id,
            author_id=uuid4(),
            component_name="personality",
            expected_previous_hash=author_value_hash(existing_personality),
            replacement={"revision": 3, "openness": "0.9", "values": ["curiosity"]},
            revision_note="Tune the custom character profile.",
            provenance=[ProvenanceReference(kind="pack", reference_id="harbor-stories")],
        ),
    )
    assert revised.entities[character_id].components["personality"]["revision"] == 3
    assert revised.entities[character_id].components["personality"]["openness"] == "0.9"
    with pytest.raises(InvalidEvent, match="expected_previous_hash"):
        apply_event(
            revised,
            AuthorComponentRevisedEvent(
                entity_id=character_id,
                author_id=uuid4(),
                component_name="public_description",
                expected_previous_hash=author_value_hash("stale value"),
                replacement="Overwritten profile.",
                revision_note="Stale edit.",
                provenance=[ProvenanceReference(kind="pack", reference_id="harbor-stories")],
            ),
        )


@pytest.mark.parametrize("locked_fields", [["revision"], ["traits", "traits"], None])
def test_personality_lock_fields_must_be_recognized_and_unique(locked_fields) -> None:
    with pytest.raises(ValidationError):
        PersonalityComponent.model_validate({"locked_fields": locked_fields})


def test_personality_locks_require_separate_author_unlock_and_preserve_locked_values() -> None:
    character_id, author_id = uuid4(), uuid4()
    state = WorldState(entities={character_id: WorldEntity(id=character_id, kind="character")})

    def personality(world: WorldState):
        return world.entities[character_id].components.get("personality")

    def author_revision(world: WorldState, replacement) -> AuthorComponentRevisedEvent:
        return AuthorComponentRevisedEvent(
            entity_id=character_id,
            author_id=author_id,
            component_name="personality",
            expected_previous_hash=author_value_hash(personality(world), component_name="personality"),
            replacement=replacement,
            revision_note="Revise the character profile.",
            provenance=[ProvenanceReference(kind="pack", reference_id="harbor-stories")],
        )

    def generic_replacement(replacement, expected_revision: int | None = None) -> ComponentReplacedEvent:
        return ComponentReplacedEvent(
            entity_id=character_id,
            component_name="personality",
            expected_revision=expected_revision,
            replacement=replacement,
        )

    initial = {"revision": 1, "openness": "0.4", "traits": ["patient"], "locked_fields": ["traits", "openness"]}
    with pytest.raises(InvalidEvent, match="only be changed through an author revision"):
        apply_event(state, generic_replacement(initial))
    state = apply_event(state, author_revision(state, initial))
    assert personality(state)["locked_fields"] == ["openness", "traits"]

    next_values = {"revision": 2, "openness": "0.40", "traits": ["patient"], "values": ["curiosity"]}
    for locks in ([], ["traits"], ["openness", "traits", "values"]):
        with pytest.raises(InvalidEvent, match="only be changed through an author revision"):
            apply_event(state, generic_replacement({**next_values, "locked_fields": locks}, expected_revision=1))
    blocked = {**next_values, "openness": "0.8", "traits": ["impulsive"]}
    for event in (
        generic_replacement(blocked, expected_revision=1),
        author_revision(state, {**blocked, "locked_fields": []}),
    ):
        with pytest.raises(InvalidEvent) as error:
            apply_event(state, event)
        assert isinstance(error.value.__cause__, PersonalityLockViolation)
        assert error.value.__cause__.fields == ("openness", "traits")

    state = apply_event(state, generic_replacement(next_values, expected_revision=1))
    assert personality(state)["openness"] == "0.4"
    assert personality(state)["locked_fields"] == ["openness", "traits"]
    assert personality(state)["values"] == ["curiosity"]

    stale_unlock = author_revision(state, {**next_values, "revision": 4, "locked_fields": []})
    state = apply_event(state, author_revision(state, {**next_values, "revision": 3, "values": ["honesty"]}))
    assert personality(state)["locked_fields"] == ["openness", "traits"]
    with pytest.raises(InvalidEvent, match="expected_previous_hash"):
        apply_event(state, stale_unlock)

    unlock = author_revision(state, personality(state) | {"revision": 4, "locked_fields": []})
    state = apply_event(state, parse_event(unlock.model_dump(mode="json")))
    assert "locked_fields" not in personality(state)
    state = apply_event(state, author_revision(state, {**blocked, "revision": 5}))
    assert personality(state)["openness"] == "0.8"
    assert personality(state)["traits"] == ["impulsive"]


def test_relations_knowledge_and_goals_are_typed_revisioned_records() -> None:
    character_id, other_id, place_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        current_tick=5,
        entities={
            character_id: WorldEntity(id=character_id, kind="character", name="Mira"),
            other_id: WorldEntity(id=other_id, kind="character", name="Tomas"),
            place_id: WorldEntity(id=place_id, kind="place", name="North Pier"),
        },
    )
    knowledge = KnowledgeRecord(
        id=uuid4(),
        owner_id=character_id,
        claim="The north road is open.",
        received_tick=5,
        source_refs=[uuid4()],
    )
    state = apply_event(state, KnowledgeAcquiredEvent(record=knowledge))
    assert state.knowledge[0].claim == "The north road is open."

    relation = RelationRecord(id=uuid4(), from_id=character_id, to_id=other_id)
    state = apply_event(state, RelationEstablishedEvent(relation=relation))
    state = apply_event(
        state,
        RelationDimensionChangedEvent(
            relation_id=relation.id,
            dimension="trust",
            value="35.5",
            evidence_refs=[uuid4()],
        ),
    )
    assert state.relations[0].dimensions["trust"] == Decimal("35.5")
    assert state.relations[0].revision == 2
    with pytest.raises(InvalidEvent, match="expected_previous"):
        apply_event(
            state,
            RelationDimensionChangedEvent(
                relation_id=relation.id,
                dimension="trust",
                expected_previous="0",
                value="40",
            ),
        )

    goal = GoalRecord(
        id=uuid4(),
        owner_id=character_id,
        title="Reach the north pier",
        conditions=[{
            "kind": "reach_location",
            "subject_id": str(character_id),
            "target_id": str(place_id),
        }],
        created_tick=5,
        updated_tick=5,
    )
    state = apply_event(state, GoalCreatedEvent(goal=goal))
    evidence = uuid4()
    state = apply_event(
        state,
        GoalUpdatedEvent(
            goal_id=goal.id,
            expected_revision=1,
            progress=100,
            status="succeeded",
            evidence_refs=[evidence],
        ),
    )
    assert state.goals[0].status == "succeeded"
    assert state.goals[0].revision == 2
    assert state.goals[0].evidence_refs == [evidence]


def test_goal_dependencies_accept_forward_references_without_self_justifying_a_cycle() -> None:
    actor, first, second = uuid4(), uuid4(), uuid4()
    goals = [
        GoalRecord(id=first, owner_id=actor, title="First", conditions=[
            GoalCondition(kind="goal_completed", target_id=second),
        ]),
        GoalRecord(id=second, owner_id=actor, title="Second", conditions=[
            GoalCondition(kind="goal_completed", target_id=first),
        ]),
    ]
    state = WorldState(entities={actor: WorldEntity(id=actor, kind="character")}, goals=goals)
    assert state.goals == goals
    assert npc_goal_fulfillment_closure(state, state.goals) == frozenset()
    with pytest.raises(ValidationError, match="condition references a missing target"):
        WorldState(entities=state.entities, goals=goals[:1])


def test_resource_consumption_and_production_are_audited_and_balanced() -> None:
    owner_id = uuid4()
    grain_id, grain_sink_id = uuid4(), uuid4()
    meal_source_id, meal_id, meal_sink_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        entities={
            owner_id: WorldEntity(
                id=owner_id,
                kind="character",
                name="Mira",
                components={
                    "production": {
                        "recipes": [
                            {
                                "recipe_id": "core.recipe.simple_meal",
                                "inputs": [{"resource_id": "core.grain", "quantity": "2.25"}],
                                "outputs": [{"resource_id": "core.meal", "quantity": "1"}],
                                "duration_ticks": 1,
                            }
                        ]
                    }
                },
            )
        },
        resources={
            grain_id: ResourceAccount(resource_id="core.grain", balance="5", owner_id=owner_id),
            grain_sink_id: ResourceAccount(
                resource_id="core.grain", balance="0", account_type="consumption_sink"
            ),
            meal_source_id: ResourceAccount(
                resource_id="core.meal", balance="0", owner_id=owner_id,
                account_type="production_source", credit_limit="10"
            ),
            meal_id: ResourceAccount(resource_id="core.meal", balance="0", owner_id=owner_id),
            meal_sink_id: ResourceAccount(
                resource_id="core.meal", balance="0", account_type="consumption_sink"
            ),
        },
    )
    event = ResourceProducedEvent(
        producer_id=owner_id,
        recipe_id="core.recipe.simple_meal",
        consumed_inputs=[
            ConsumedResourceFlow(
                holder_account_id=grain_id,
                sink_account_id=grain_sink_id,
                resource_id="core.grain",
                quantity="2.25",
            )
        ],
        produced_outputs=[
            ProducedResourceFlow(
                source_account_id=meal_source_id,
                holder_account_id=meal_id,
                resource_id="core.meal",
                quantity="1",
            )
        ],
        conversions=[
            ResourceConversion(
                step_id="core.process.cook",
                input_resource_id="core.grain",
                input_quantity="2.25",
                output_resource_id="core.meal",
                output_quantity="1",
            )
        ],
        provenance=[ProvenanceReference(kind="rule", reference_id="core.recipe.simple_meal")],
    )
    postings = ledger_postings(event)
    validate_ledger_postings(postings)
    assert [posting.delta for posting in postings] == [
        Decimal("-2.25"), Decimal("2.25"), Decimal("-1"), Decimal("1")
    ]
    changed = apply_event(state, event)
    assert changed.resources[grain_id].balance == Decimal("2.75")
    assert changed.resources[grain_sink_id].balance == Decimal("2.25")
    assert changed.resources[meal_source_id].balance == Decimal("-1")
    assert changed.resources[meal_id].balance == Decimal("1")

    consume = ResourceConsumedEvent(
        consumer_id=owner_id,
        purpose="eat a meal",
        flows=[
            ConsumedResourceFlow(
                holder_account_id=meal_id,
                sink_account_id=meal_sink_id,
                resource_id="core.meal",
                quantity="2",
            )
        ],
    )
    with pytest.raises(InsufficientResources):
        apply_event(changed, consume)

    quota_exhausted_resources = dict(changed.resources)
    quota_exhausted_resources[meal_source_id] = changed.resources[meal_source_id].model_copy(
        update={"credit_limit": Decimal("1")}
    )
    quota_exhausted = WorldState(
        current_tick=changed.current_tick,
        entities=changed.entities,
        resources=quota_exhausted_resources,
        player_id=changed.player_id,
        calendar=changed.calendar,
        metadata=changed.metadata,
    )
    with pytest.raises(InsufficientResources, match="finite .* quota"):
        apply_event(quota_exhausted, event)


def test_population_can_exceed_capacity_but_inventory_is_aggregated() -> None:
    place_id = uuid4()
    place = WorldEntity(
        id=place_id,
        kind="group",
        components={
            "population": PopulationComponent(
                species_id="core.human",
                headcount=250,
                carrying_capacity=100,
            )
        },
    )
    assert place.components["population"]["headcount"] == 250

    character_id, account_id = uuid4(), uuid4()
    with pytest.raises(ValueError, match="inventory lots exceed account balance"):
        WorldState(
            entities={
                character_id: WorldEntity(
                    id=character_id,
                    kind="character",
                    components={
                        "inventory": {
                            "lots": [
                                {
                                    "lot_id": str(uuid4()),
                                    "account_id": str(account_id),
                                    "resource_id": "core.grain",
                                    "quantity": "0.7",
                                },
                                {
                                    "lot_id": str(uuid4()),
                                    "account_id": str(account_id),
                                    "resource_id": "core.grain",
                                    "quantity": "0.7",
                                },
                            ]
                        }
                    },
                )
            },
            resources={
                account_id: ResourceAccount(
                    resource_id="core.grain", balance="1", owner_id=character_id
                )
            },
        )


def test_legacy_ownerless_production_event_still_replays_from_its_v1_record() -> None:
    input_owner, output_owner = uuid4(), uuid4()
    input_account, input_sink = uuid4(), uuid4()
    output_source, output_account = uuid4(), uuid4()
    state = WorldState(
        entities={
            input_owner: WorldEntity(id=input_owner, kind="character"),
            output_owner: WorldEntity(id=output_owner, kind="character"),
        },
        resources={
            input_account: ResourceAccount(resource_id="core.input", balance="2", owner_id=input_owner),
            input_sink: ResourceAccount(resource_id="core.input", balance="0", account_type="consumption_sink"),
            output_source: ResourceAccount(
                resource_id="core.output", balance="0", account_type="production_source"
            ),
            output_account: ResourceAccount(resource_id="core.output", balance="0", owner_id=output_owner),
        },
    )
    event = ResourceProducedEvent(
        recipe_id="legacy.recipe",
        consumed_inputs=[
            ConsumedResourceFlow(
                holder_account_id=input_account,
                sink_account_id=input_sink,
                resource_id="core.input",
                quantity="1",
            )
        ],
        produced_outputs=[
            ProducedResourceFlow(
                source_account_id=output_source,
                holder_account_id=output_account,
                resource_id="core.output",
                quantity="1",
            )
        ],
        conversions=[
            ResourceConversion(
                step_id="legacy.process",
                input_resource_id="core.input",
                input_quantity="1",
                output_resource_id="core.output",
                output_quantity="1",
            )
        ],
        source_event_ids=[uuid4()],
    )

    replayed = apply_event(state, event)
    assert replayed.resources[output_source].balance == Decimal("-1")
    assert replayed.resources[output_account].balance == Decimal("1")


def test_movement_event_requires_a_matching_registered_route_and_seconds() -> None:
    origin_id, destination_id, actor_id = uuid4(), uuid4(), uuid4()
    route_id = uuid4()
    origin = WorldEntity(
        id=origin_id,
        kind="place",
        components={
            "place": {
                "routes": [
                    {
                        "route_id": str(route_id),
                        "from_place_id": str(origin_id),
                        "to_place_id": str(destination_id),
                        "travel_ticks": 3_600,
                    }
                ]
            }
        },
    )
    destination = WorldEntity(id=destination_id, kind="place")
    actor = WorldEntity(id=actor_id, kind="character", location_id=origin_id)
    state = WorldState(current_tick=10, entities={origin_id: origin, destination_id: destination, actor_id: actor})
    advanced = apply_event(state, TimeAdvancedEvent(to_tick=3_610))
    arrived = apply_event(
        advanced,
        MovementCompletedEvent(
            actor_id=actor_id,
            route_id=route_id,
            from_place_id=origin_id,
            to_place_id=destination_id,
            started_tick=10,
            arrived_tick=3_610,
        ),
    )
    assert arrived.entities[actor_id].location_id == destination_id

    with pytest.raises(InvalidEvent, match="duration must match"):
        apply_event(
            advanced,
            MovementCompletedEvent(
                actor_id=actor_id,
                route_id=route_id,
                from_place_id=origin_id,
                to_place_id=destination_id,
                started_tick=11,
                arrived_tick=3_610,
            ),
        )


def test_discovery_reveals_only_a_hidden_local_target_and_damage_death_is_atomic() -> None:
    place_id, discoverer_id, target_id, attacker_id, victim_id = (uuid4() for _ in range(5))
    place = WorldEntity(id=place_id, kind="place")
    discoverer = WorldEntity(id=discoverer_id, kind="character", location_id=place_id)
    hidden = WorldEntity(
        id=target_id,
        kind="item",
        location_id=place_id,
        components={"hidden": True, "public_description": "A sealed satchel."},
    )
    attacker = WorldEntity(id=attacker_id, kind="character")
    victim = WorldEntity(
        id=victim_id,
        kind="character",
        components={"health": {"current": 8, "maximum": 10}},
    )
    state = WorldState(
        current_tick=30,
        entities={
            place_id: place,
            discoverer_id: discoverer,
            target_id: hidden,
            attacker_id: attacker,
            victim_id: victim,
        },
    )
    found = apply_event(
        state,
        EntityDiscoveredEvent(
            discoverer_id=discoverer_id, entity_id=target_id, place_id=place_id, roll=1, chance_percent=100
        ),
    )
    assert found.entities[target_id].components["hidden"] is False
    assert str(place_id) in found.entities[discoverer_id].components["known_places"]
    with pytest.raises(InvalidEvent, match="not hidden"):
        apply_event(
            found,
            EntityDiscoveredEvent(
                discoverer_id=discoverer_id, entity_id=target_id, place_id=place_id, roll=1, chance_percent=100
            ),
        )

    dead = apply_event(
        state,
        DamageAppliedEvent(
            target_id=victim_id,
            source_id=attacker_id,
            damage=9,
            expected_health_revision=1,
            injury_type="combat.slash",
            description="A severe strike.",
        ),
    )
    assert dead.entities[victim_id].status == "dead"
    assert dead.entities[victim_id].components["health"]["current"] == 0
    assert dead.entities[victim_id].components["health"]["injuries"][0]["severity"] == 90
    with pytest.raises(InvalidEvent, match="terminal character cannot be healed"):
        apply_event(
            dead,
            HealthRestoredEvent(target_id=victim_id, amount=2, expected_health_revision=2),
        )
