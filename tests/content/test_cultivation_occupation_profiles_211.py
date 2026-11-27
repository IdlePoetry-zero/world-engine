from __future__ import annotations

from collections import Counter
from uuid import UUID, uuid4

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.cultivation_occupation_profiles_211 import (
    CULTIVATION_OCCUPATION_PROFILES_211_VERSION,
    build_cultivation_occupation_profiles_211,
)
from world_engine.content.definitions import BACKGROUNDS, CULTIVATION
from world_engine.content.recruitment_roles import RECRUITMENT_ROLES
from world_engine.domain.market_pricing import PRICE_REVIEW_CONFIG_KEY, PRICE_REVIEW_HANDLER_ID
from world_engine.domain.gameplay_events import RenewableResourceHarvestedEvent
from world_engine.domain.population_seats import plan_population_seat_promotions
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules import RuleContext, RuleEngine


_PACK_VERSION = "2.11.0"
_BASELINE_28_HASHES = {
    0: "3ad778df279ea5cedf15c80a7e38639eb0c94dce7cff2713b8e125500cf85ccd",
    1: "69dd1ae8b1905598aaf92b460c0b9b5e4150917deb3fcbbc31672b9dec687ea4",
    2: "62b519e8d8ca6086a3fe569ff24ab705aec00fbec6877fc7c76f50b1b40a5ff7",
}


def _build(catalog: ContentCatalog, *, seed: int, version: str):
    return catalog.build_world(
        "long_life_cultivation",
        f"R11 Cultivation Draft Validation seed {seed}",
        "Draft Player",
        seed=seed,
        population_mode="sustained",
        pack_version=version,
    )


def _regional_residents(state):
    groups = {
        UUID(raw_id): state.entities[UUID(raw_id)]
        for raw_id in state.metadata["content_index"]["population_group_ids"]
    }
    residents_by_region = {state.entities[group.location_id].name: [] for group in groups.values()}
    for raw_id in state.metadata["content_index"]["other_resident_ids"]:
        resident = state.entities[UUID(raw_id)]
        group_id = UUID(resident.components["life"]["population_group_id"])
        region = state.entities[groups[group_id].location_id]
        residents_by_region[region.name].append(resident)
    return residents_by_region


def _governance_rosters(state):
    entities = [
        entity for entity in state.entities.values()
        if entity.kind in {"polity", "organization"}
    ]
    return {
        entity.id: tuple(entity.components["governance"].get("members", []))
        for entity in entities
    }


def test_2_11_is_sustained_cultivation_only_and_preserves_pack_defaults() -> None:
    catalog = ContentCatalog()
    listed = {pack["pack_id"]: pack for pack in catalog.list_packs()}

    assert CULTIVATION_OCCUPATION_PROFILES_211_VERSION == _PACK_VERSION
    assert listed["long_life_cultivation"]["version"] == "2.0.0"
    assert listed["long_life_cultivation"]["available_versions"] == [
        "2.0.0", "2.2.0", "2.3.0", "2.8.0", "2.12.0", "2.13.0", "2.14.0", "2.15.0",
    ]
    assert listed["long_life_cultivation"]["sustained_available_versions"] == ["2.4.0", _PACK_VERSION]
    assert listed["modern_city"]["sustained_available_versions"] == [
        "2.4.0", "2.5.0", "2.6.0", "2.7.0", "2.10.0",
    ]
    assert listed["low_magic_feudal"]["sustained_available_versions"] == ["2.4.0", "2.9.0"]
    assert all("2.11.0" not in pack["available_versions"] for pack in listed.values())

    with pytest.raises(ValueError, match="only with sustained population mode"):
        catalog.build_world("long_life_cultivation", "Natural 2.11", "Rin", pack_version=_PACK_VERSION)
    for pack in BACKGROUNDS:
        if pack.pack_id == "long_life_cultivation":
            continue
        with pytest.raises(ValueError, match="only for the long_life_cultivation background"):
            catalog.build_world(
                pack.pack_id,
                "Wrong background 2.11",
                "Rin",
                population_mode="sustained",
                pack_version=_PACK_VERSION,
            )

    default = catalog.build_world(
        "long_life_cultivation", "Unchanged cultivation default", "Rin", seed=20261121,
    )
    assert default.pack_version == default.state.metadata["pack_version"] == "2.0.0"
    assert default.state.metadata["population_mode"] == "natural"
    for raw_id in default.state.metadata["content_index"]["population_group_ids"]:
        policy = default.state.entities[UUID(raw_id)].components["population"]["seat_policy"]
        assert policy is None


@pytest.mark.parametrize("seed", (0, 1, 2))
def test_2_11_snapshots_are_stable_and_2_8_opening_roles_remain_compatible(seed: int) -> None:
    catalog = ContentCatalog()
    legacy = _build(catalog, seed=seed, version="2.8.0")
    first = _build(catalog, seed=seed, version=_PACK_VERSION)
    repeated = _build(catalog, seed=seed, version=_PACK_VERSION)

    assert state_hash(legacy.state) == _BASELINE_28_HASHES[seed]
    assert state_hash(first.state) == state_hash(repeated.state)
    assert first.model_dump(mode="json") == repeated.model_dump(mode="json")

    legacy_ids = legacy.state.metadata["content_index"]["other_resident_ids"]
    new_ids = first.state.metadata["content_index"]["other_resident_ids"]
    assert len(legacy_ids) == len(new_ids) == 264
    for legacy_id, new_id in zip(legacy_ids, new_ids, strict=True):
        legacy_resident = legacy.state.entities[UUID(legacy_id)]
        new_resident = first.state.entities[UUID(new_id)]
        assert new_resident.components["public_role"] == legacy_resident.components["public_role"]
        assert new_resident.components["skills"] == legacy_resident.components["skills"]

    legacy_rosters = _governance_rosters(legacy.state)
    new_rosters_by_name = {
        (entity.kind, entity.name): tuple(
            first.state.entities[UUID(member_id)].name
            for member_id in entity.components["governance"].get("members", [])
        )
        for entity in first.state.entities.values()
        if entity.kind in {"polity", "organization"}
    }
    legacy_rosters_by_name = {
        (entity.kind, entity.name): tuple(
            legacy.state.entities[UUID(member_id)].name
            for member_id in entity.components["governance"].get("members", [])
        )
        for entity in legacy.state.entities.values()
        if entity.kind in {"polity", "organization"}
    }
    assert new_rosters_by_name == legacy_rosters_by_name
    assert len(legacy_rosters) == 48


def test_2_11_inherits_full_dense_cultivation_content_and_scoped_seat_profiles() -> None:
    catalog = ContentCatalog()
    draft = _build(catalog, seed=0, version=_PACK_VERSION)
    state = draft.state
    index = state.metadata["content_index"]
    profiles = build_cultivation_occupation_profiles_211()

    assert draft.pack_version == state.metadata["pack_version"] == _PACK_VERSION
    assert state.metadata["population_mode"] == "sustained"
    assert len(index["country_ids"]) == state.metadata["seed_counts"]["countries"] == 24
    assert len(index["organization_ids"]) == state.metadata["seed_counts"]["organizations"] == 24
    assert len(index["key_npc_ids"]) == state.metadata["seed_counts"]["key_npcs"] == 32
    assert len(index["other_resident_ids"]) == state.metadata["seed_counts"]["other_named_living_characters"] == 264
    assert state.metadata["seed_counts"]["places"] == 120
    assert sum(entity.kind == "polity" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "organization" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "place" for entity in state.entities.values()) == 120
    assert all(state.entities[UUID(raw_id)].status == "active" for raw_id in index["key_npc_ids"])
    assert all(
        state.entities[UUID(raw_id)].status == "active" and state.entities[UUID(raw_id)].name
        for raw_id in index["other_resident_ids"]
    )
    assert len(state.work_postings) == 12
    assert len(state.kinship_links) == 12
    assert 3 <= sum(
        len(entity.components.get("place", {}).get("waypoint_opportunities", []))
        for entity in state.entities.values()
    ) <= 4
    assert state.metadata[PRICE_REVIEW_CONFIG_KEY]
    assert any(event.handler_id == PRICE_REVIEW_HANDLER_ID for event in state.scheduled_events)
    assert sum("investigation_case" in entity.components for entity in state.entities.values()) == 1
    assert state.goals
    assert "promise_trust_policy_id" in state.metadata

    region_ids = {UUID(raw_id) for raw_id in index["region_ids"]}
    region_names = {state.entities[region_id].name for region_id in region_ids}
    assert set(profiles) == region_names
    assert len({tuple(profile.role_weights.items()) for profile in profiles.values()}) == 12
    assert set().union(*(set(profile.role_weights) for profile in profiles.values())) == set(CULTIVATION.extra_roles)

    resident_counts = Counter()
    for region_name, residents in _regional_residents(state).items():
        resident_counts[region_name] = len(residents)
        assert len(profiles[region_name].role_weights) == 12
        assert set(profiles[region_name].role_skill_levels) == set(profiles[region_name].role_weights)
        for role, levels in profiles[region_name].role_skill_levels.items():
            assert levels
            assert all(skill_id.startswith("skill.") and level.isdecimal() for skill_id, level in levels.items())

    assert sum(resident_counts.values()) == 264
    assert len(resident_counts) == 12

    for raw_id in index["population_group_ids"]:
        group = state.entities[UUID(raw_id)]
        region = state.entities[group.location_id]
        profile = profiles[region.name]
        policy = group.components["population"]["seat_policy"]
        assert policy["maintain_civic_occupations"] is True
        assert policy["maintain_civic_recruitment_pool"] is True
        assert policy["extra_roles"] == [role for role in CULTIVATION.extra_roles if role in profile.role_weights]
        assert policy["occupation_profile"]["role_weights"] == profile.role_weights
        assert policy["occupation_profile"]["role_skill_levels"] == profile.role_skill_levels

    organizations_by_name = {org.name: org for org in CULTIVATION.organizations}
    for raw_id in index["organization_ids"]:
        organization = state.entities[UUID(raw_id)]
        governance = organization.components["governance"]
        policy, = governance["membership_recruitment"]
        region = state.entities[UUID(policy["region_id"])]
        profile = profiles[region.name]
        authored = organizations_by_name[organization.name]
        expected_roles = RECRUITMENT_ROLES[CULTIVATION.pack_id][authored.organization_type]

        assert policy["eligible_roles"] == list(expected_roles)
        assert set(policy["eligible_roles"]) <= set(profile.role_weights)
        assert policy["authority_tag"] in governance["authority_tags"]
        assert policy["charter_clause_id"] in governance["charter_clause_ids"]
        assert policy["region_id"] == str(organization.location_id)
        assert policy["active_member_capacity"] > len(governance["members"])

    for raw_id in index["country_ids"]:
        polity = state.entities[UUID(raw_id)]
        governance = polity.components["governance"]
        policy, = governance["membership_recruitment"]
        assert policy["eligible_roles"] == list(CULTIVATION.extra_roles)
        assert policy["authority_tag"] in governance["authority_tags"]
        assert policy["charter_clause_id"] in governance["charter_clause_ids"]
        assert policy["region_id"] == str(polity.location_id)

    key_ids = {UUID(raw_id) for raw_id in index["key_npc_ids"]}
    assert all(
        UUID(member_id) in key_ids
        for raw_id in index["country_ids"] + index["organization_ids"]
        for member_id in state.entities[UUID(raw_id)].components["governance"]["members"]
    )


def test_2_11_vacancy_plan_uses_regional_role_profile_without_granting_membership() -> None:
    catalog = ContentCatalog()
    state = _build(catalog, seed=0, version=_PACK_VERSION).state
    index = state.metadata["content_index"]
    group_id = UUID(index["population_group_ids"][0])
    resident = next(
        state.entities[UUID(raw_id)]
        for raw_id in index["other_resident_ids"]
        if UUID(state.entities[UUID(raw_id)].components["life"]["population_group_id"]) == group_id
    )
    assert resident.status == "active"

    harvest_schedule, = [
        event for event in state.scheduled_events
        if event.handler_id == "environmental.harvest"
    ]
    resident_components = dict(resident.components)
    resident_components["health"] = {**resident_components["health"], "current": 0}
    changed_entities = dict(state.entities)
    changed_entities[resident.id] = resident.model_copy(update={"status": "dead", "components": resident_components})
    group = state.entities[group_id]
    group_population = dict(group.components["population"])
    group_population["unmodeled_headcount"] += 1
    age_years = (
        (harvest_schedule.due_tick - resident.components["life"]["birth_tick"])
        // (state.calendar.day_seconds * state.calendar.year_days)
    )
    age_cohorts = [dict(cohort) for cohort in group_population["age_cohorts"]]
    cohort = next(item for item in age_cohorts if item["age_years"] == age_years)
    cohort["headcount"] += 1
    group_population["age_cohorts"] = age_cohorts
    group_components = {**group.components, "population": group_population}
    changed_entities[group_id] = group.model_copy(update={"components": group_components})
    vacancy_state = state.model_copy(
        update={"entities": changed_entities, "current_tick": harvest_schedule.due_tick}
    )
    vacancy_state = type(state).model_validate(vacancy_state.model_dump(mode="json"))
    rosters_before = _governance_rosters(vacancy_state)
    plan = plan_population_seat_promotions(vacancy_state, period_index=1)
    context = RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(),
        actor_id=state.player_id, ruleset_version="1.0.0",
    )
    resolution = RuleEngine().resolve_scheduled(vacancy_state, harvest_schedule, context)

    assert len(plan.promotions) == 1
    assert resolution.outcome.status == "succeeded"
    harvest_event, = [event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent)]
    assert harvest_event.promotions == list(plan.promotions)
    promotion = plan.promotions[0]
    group = plan.state.entities[group_id]
    region = plan.state.entities[group.location_id]
    profile = build_cultivation_occupation_profiles_211()[region.name]
    promoted = promotion.character
    committed_state = vacancy_state
    for event in resolution.events:
        committed_state = apply_event(committed_state, event)
    committed_promoted = committed_state.entities[promoted.id]

    assert promoted.id in plan.state.entities
    assert promoted.status == "active"
    assert promoted.components["public_role"] in profile.role_weights
    assert promoted.components["public_role"] in group.components["population"]["seat_policy"]["extra_roles"]
    assert promotion.vacant_member_id == resident.id
    assert plan.state.current_tick == vacancy_state.current_tick
    assert committed_state.current_tick == vacancy_state.current_tick
    assert committed_promoted.status == "active"
    assert committed_promoted.components["life"]["population_origin"]["source_event_id"] == str(harvest_event.event_id)
    assert _governance_rosters(committed_state) == rosters_before
    assert all(
        promoted.id not in {
            UUID(member_id) for member_id in committed_state.entities[entity_id].components["governance"]["members"]
        }
        for entity_id in rosters_before
    )
