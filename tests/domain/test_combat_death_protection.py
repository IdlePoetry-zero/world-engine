from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.combat import combat_roll_digest, combat_roll_value
from world_engine.domain.components import CapabilitiesComponent, HealthComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    AuthorComponentRevisedEvent,
    BackgroundDeprivationResolvedEvent,
    CombatExchangeResolvedEvent,
    ComponentReplacedEvent,
    DamageAppliedEvent,
    HealthRestoredEvent,
    LifeEndedEvent,
)
from world_engine.domain.models import EntityFieldChangedEvent, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, author_value_hash


def _state() -> tuple[WorldState, UUID, UUID, UUID]:
    actor_id, target_id, place_id, cohort_id = (uuid4() for _ in range(4))
    return WorldState(current_tick=10, player_id=actor_id, entities={
        actor_id: WorldEntity(id=actor_id, kind="character", location_id=place_id),
        target_id: WorldEntity(id=target_id, kind="character", location_id=place_id, components={
            "capabilities": {"grants": ["world.travel"]},
            "health": {"current": 5, "maximum": 10},
            "life": {"birth_tick": 0, "lifespan_ticks": 10, "population_group_id": str(cohort_id)},
            "needs": {"food": {"value": 10, "critical_below": 20, "last_critical_damage_tick": 0}},
        }),
        place_id: WorldEntity(id=place_id, kind="place"),
        cohort_id: WorldEntity(id=cohort_id, kind="cohort", components={
            "population": {
                "species_id": "core.human", "headcount": 2, "unmodeled_headcount": 1, "carrying_capacity": 2,
            },
        }),
    }), actor_id, target_id, cohort_id


def _author_capabilities(state: WorldState, target_id: UUID, **changes) -> WorldState:
    capabilities = state.entities[target_id].components.get("capabilities")
    revision = capabilities["revision"] + 1 if capabilities else 1
    return apply_event(state, AuthorComponentRevisedEvent(
        entity_id=target_id,
        author_id=uuid4(),
        component_name="capabilities",
        expected_previous_hash=author_value_hash(capabilities, component_name="capabilities"),
        replacement={"revision": revision, **changes},
        revision_note="Update character capabilities.",
        provenance=[{"kind": "rule", "reference_id": "core.author_revision.v1"}],
    ))


def _lethal_hit(state: WorldState, actor_id: UUID, target_id: UUID) -> tuple[WorldState, DamageAppliedEvent]:
    world_id, branch_id, command_id = UUID(int=1), UUID(int=2), UUID(int=400)
    ruleset_version = "1.0.0"
    digest = combat_roll_digest(
        world_id=world_id, branch_id=branch_id, command_id=command_id, ruleset_version=ruleset_version,
        label="combat.attack",
    )
    damage_id = uuid5(command_id, f"{ruleset_version}:combat.damage")
    exchange = CombatExchangeResolvedEvent(
        event_id=uuid5(command_id, f"{ruleset_version}:combat.exchange"),
        world_id=world_id, branch_id=branch_id, command_id=command_id, ruleset_version=ruleset_version,
        encounter_id=uuid5(command_id, f"{ruleset_version}:combat.encounter"), exchange_number=1,
        actor_id=actor_id, target_id=target_id, location_id=state.entities[actor_id].location_id,
        started_tick=0, resolved_tick=10, chance_percent=55, roll=combat_roll_value(digest, 100),
        roll_digest=digest, outcome="hit", encounter_status="resolved", damage_event_id=damage_id,
        damage=5, expected_target_health_revision=1,
        visibility={"policy": "subject_ids", "subject_ids": sorted((str(actor_id), str(target_id)))},
    )
    damage = DamageAppliedEvent(
        event_id=damage_id, target_id=target_id, source_id=actor_id, damage=5,
        expected_health_revision=1, source_event_ids=[exchange.event_id],
    )
    return apply_event(state, exchange), damage


def test_combat_death_protection_is_a_strict_author_opt_in():
    state, actor_id, target_id, _ = _state()
    with pytest.raises(ValidationError):
        CapabilitiesComponent(combat_death_protection="true")
    assert "combat_death_protection" not in CapabilitiesComponent(combat_death_protection=False).model_dump()
    assert "incapacitating_injury_id" not in HealthComponent(current=1, maximum=1).model_dump()
    with pytest.raises(InvalidEvent, match="non-player character"):
        _author_capabilities(state, actor_id, combat_death_protection=True)

    def replaced(state, revision, **changes):
        return apply_event(state, ComponentReplacedEvent(
            entity_id=target_id, component_name="capabilities", expected_revision=revision,
            replacement={"revision": revision + 1, "grants": ["world.combat"], **changes},
        ))

    with pytest.raises(InvalidEvent, match="author revision"):
        replaced(state, 1, combat_death_protection=True)
    protected = _author_capabilities(state, target_id, combat_death_protection=True)
    with pytest.raises(InvalidEvent, match="author revision"):
        replaced(protected, 2, combat_death_protection=False)
    updated = replaced(protected, 2)
    assert updated.entities[target_id].components["capabilities"]["combat_death_protection"] is True
    revised = _author_capabilities(updated, target_id, grants=["world.travel"])
    assert revised.entities[target_id].components["capabilities"]["combat_death_protection"] is True
    disabled = _author_capabilities(revised, target_id, combat_death_protection=False)
    assert "combat_death_protection" not in disabled.entities[target_id].components["capabilities"]


def test_protected_cohort_member_keeps_the_combat_injury_and_can_still_die_of_age():
    state, actor_id, target_id, cohort_id = _state()
    state = _author_capabilities(state, target_id, combat_death_protection=True)
    after_exchange, damage = _lethal_hit(state, actor_id, target_id)
    with pytest.raises(InvalidEvent, match="registered exchange"):
        apply_event(state, damage)
    incapacitated = apply_event(after_exchange, damage)
    target = incapacitated.entities[target_id]
    health = target.components["health"]
    assert target.status == "incapacitated"
    assert health["current"] == 1
    assert health["incapacitating_injury_id"] == str(damage.event_id)
    assert health["injuries"][0]["treated"] is False
    assert incapacitated.entities[cohort_id] == state.entities[cohort_id]
    assert WorldState.model_validate_json(incapacitated.model_dump_json()) == incapacitated

    dead = apply_event(incapacitated, LifeEndedEvent(
        entity_id=target_id, expected_life_revision=1, expected_population_revision=1,
    ))
    assert dead.entities[target_id].status == "dead"
    assert dead.entities[target_id].components["health"]["current"] == 0
    assert "incapacitating_injury_id" not in dead.entities[target_id].components["health"]
    assert dead.entities[cohort_id].components["population"]["headcount"] == 1

    unrelated = damage.model_copy(update={"source_event_ids": [], "expected_population_revision": 1})
    assert apply_event(state, unrelated).entities[target_id].status == "dead"


def test_generic_events_cannot_remove_an_incapacitating_injury_or_bypass_recovery():
    state, actor_id, target_id, _ = _state()
    state = _author_capabilities(state, target_id, combat_death_protection=True)
    after_exchange, damage = _lethal_hit(state, actor_id, target_id)
    incapacitated = apply_event(after_exchange, damage)
    health = incapacitated.entities[target_id].components["health"]
    replacement = {**health, "revision": 3}
    replacement.pop("incapacitating_injury_id")
    event = ComponentReplacedEvent(
        entity_id=target_id, component_name="health", expected_revision=2, replacement=replacement,
    )
    updated = apply_event(incapacitated, event)
    assert updated.entities[target_id].components["health"]["incapacitating_injury_id"] == str(damage.event_id)
    with pytest.raises(InvalidEvent, match="cannot be changed"):
        apply_event(incapacitated, event.model_copy(update={
            "replacement": {**replacement, "incapacitating_injury_id": None},
        }))
    with pytest.raises(InvalidEvent, match="existing untreated injury"):
        apply_event(incapacitated, event.model_copy(update={"replacement": {**replacement, "injuries": []}}))
    with pytest.raises(InvalidEvent, match="must be incapacitated"):
        apply_event(incapacitated, EntityFieldChangedEvent(entity_id=target_id, field_name="status", value="active"))


@pytest.mark.parametrize("lethal", [False, True])
def test_background_deprivation_does_not_restore_or_protect_an_incapacitated_character(lethal: bool):
    state, actor_id, target_id, cohort_id = _state()
    state = _author_capabilities(state, target_id, combat_death_protection=True)
    after_exchange, damage = _lethal_hit(state, actor_id, target_id)
    state = apply_event(after_exchange, damage)
    if not lethal:
        state = apply_event(state, HealthRestoredEvent(target_id=target_id, amount=1, expected_health_revision=2))
    state = apply_event(state, TimeAdvancedEvent(to_tick=state.calendar.day_seconds))
    after = apply_event(state, BackgroundDeprivationResolvedEvent(
        actor_id=actor_id,
        resolved_tick=state.current_tick,
        subjects=[{
            "entity_id": target_id,
            "boundary_tick": state.current_tick,
            "expected_needs_revision": 1,
            "expected_health_revision": 2 if lethal else 3,
            "expected_population_revision": 1 if lethal else None,
            "expected_damage": 1,
            "terminal_status": "dead" if lethal else "active",
        }],
        provenance=[{"kind": "rule", "reference_id": "core.background_deprivation.v1"}],
    ))
    target = after.entities[target_id]
    health = target.components["health"]
    assert target.status == ("dead" if lethal else "incapacitated")
    assert health.get("incapacitating_injury_id") == (None if lethal else str(damage.event_id))
    assert after.entities[cohort_id].components["population"]["headcount"] == (1 if lethal else 2)
