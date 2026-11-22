from __future__ import annotations

from collections import Counter
from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS, MODERN_CITY
from world_engine.content.recruitment_roles import RECRUITMENT_ROLES
from world_engine.domain.population_seats import select_population_occupation_role
from world_engine.domain.reducers import state_hash


def _resident_entities(state):
    resident_ids = [UUID(value) for value in state.metadata["content_index"]["other_resident_ids"]]
    return [state.entities[entity_id] for entity_id in resident_ids]


def _group_for_region(state, region_name: str):
    region = next(entity for entity in state.entities.values() if entity.kind == "place" and entity.name == region_name)
    return next(
        entity for entity in state.entities.values()
        if entity.kind == "group" and entity.location_id == region.id
    )


def _organization_signature(state):
    rows = []
    for raw_id in state.metadata["content_index"]["organization_ids"]:
        organization = state.entities[UUID(raw_id)]
        governance = organization.components["governance"]
        recruitment = governance["membership_recruitment"][0]
        rows.append((
            organization.name,
            state.entities[organization.location_id].name,
            tuple(state.entities[UUID(member_id)].name for member_id in governance["members"]),
            tuple(recruitment["eligible_roles"]),
            recruitment["minimum_age_years"],
            recruitment["maximum_age_years"],
            recruitment["active_member_capacity"],
            recruitment["max_recruits_per_period"],
        ))
    return rows


def _population_signature(state):
    rows = []
    for raw_id in state.metadata["content_index"]["population_group_ids"]:
        group = state.entities[UUID(raw_id)]
        population = group.components["population"]
        rows.append((
            state.entities[group.location_id].name,
            population["headcount"],
            population["unmodeled_headcount"],
            population["carrying_capacity"],
            population["annual_birth_rate"],
            population["annual_death_rate"],
            population["annual_migration_rate"],
        ))
    return rows


def _population_schedule_signature(state):
    schedule = next(row for row in state.scheduled_events if row.handler_id == "population.year_advance")
    return (
        schedule.due_tick,
        schedule.phase,
        schedule.priority,
        schedule.dependencies,
        schedule.handler_id,
        schedule.handler_version,
        schedule.status,
        schedule.repeat_ticks,
        schedule.max_runs,
        schedule.completed_runs,
        schedule.payload,
    )


def test_2_5_is_explicit_modern_sustained_opt_in_and_2_4_hash_is_stable() -> None:
    catalog = ContentCatalog()
    listed = {pack["pack_id"]: pack for pack in catalog.list_packs()}
    assert listed["modern_city"]["sustained_available_versions"] == ["2.4.0", "2.5.0", "2.6.0", "2.7.0", "2.10.0"]
    assert all(
        pack["sustained_available_versions"] == ["2.4.0"]
        for pack_id, pack in listed.items()
        if pack_id not in {"modern_city", "low_magic_feudal", "long_life_cultivation"}
    )
    assert listed["long_life_cultivation"]["sustained_available_versions"] == ["2.4.0", "2.11.0"]
    assert listed["low_magic_feudal"]["sustained_available_versions"] == ["2.4.0", "2.9.0"]

    default = catalog.build_world(
        "modern_city", "Modern Profile Default", "Rin", seed=20261117,
        population_mode="sustained",
    )
    assert default.pack_version == "2.0.0"

    with pytest.raises(ValueError, match="only with sustained population mode"):
        catalog.build_world("modern_city", "Natural 2.5", "Rin", pack_version="2.5.0")
    with pytest.raises(ValueError, match="only for the modern_city background"):
        catalog.build_world(
            "low_magic_feudal", "Wrong Pack 2.5", "Rin",
            population_mode="sustained", pack_version="2.5.0",
        )

    baseline = catalog.build_world(
        "modern_city", "Profile Baseline", "Rin", seed=-923,
        population_mode="sustained", pack_version="2.4.0",
    )
    assert state_hash(baseline.state) == "dc33433b468c74ffb27beb7ac2b5d223fac8027024b53ac7938bc7c97927921e"


def test_2_5_profiles_repeat_and_keep_the_full_broad_resident_mix() -> None:
    catalog = ContentCatalog()
    first = catalog.build_world(
        "modern_city", "Regional Occupations", "Rin", seed=20261117,
        population_mode="sustained", pack_version="2.5.0",
    )
    second = catalog.build_world(
        "modern_city", "Regional Occupations", "Rin", seed=20261117,
        population_mode="sustained", pack_version="2.5.0",
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")

    state = first.state
    assert state.metadata["population_mode"] == "sustained"
    assert state.metadata["pack_version"] == "2.5.0"
    assert len(_resident_entities(state)) == 264
    assert state.metadata["seed_counts"]["other_named_living_characters"] == 264

    all_residents = _resident_entities(state)
    all_roles = Counter(entity.components["public_role"] for entity in all_residents)
    assert set(all_roles) == set(MODERN_CITY.extra_roles)

    university_group = _group_for_region(state, "University Quarter")
    university_residents = [
        entity for entity in all_residents
        if entity.components["life"]["population_group_id"] == str(university_group.id)
    ]
    university_roles = Counter(entity.components["public_role"] for entity in university_residents)
    clinic_roles = set(RECRUITMENT_ROLES["modern_city"]["health_network"])
    assert clinic_roles == {"nurse", "care worker", "clinic receptionist", "data operator"}
    assert clinic_roles <= set(MODERN_CITY.regional_occupation_profiles["University Quarter"].role_weights)
    assert set(university_roles) & clinic_roles == clinic_roles
    assert sum(university_roles[role] for role in clinic_roles) >= 8
    assert sum(count for role, count in university_roles.items() if role not in clinic_roles) >= 5

    allowed_skills = {
        "skill.social", "skill.local_knowledge", "skill.repair", "skill.research",
        "skill.navigation", "skill.care", "skill.mediation",
    }
    profiles = MODERN_CITY.regional_occupation_profiles
    for group_id in state.metadata["content_index"]["population_group_ids"]:
        group = state.entities[UUID(group_id)]
        region = state.entities[group.location_id]
        policy_profile = group.components["population"]["seat_policy"]["occupation_profile"]
        authored_profile = profiles[region.name]
        assert policy_profile["role_weights"] == authored_profile.role_weights
        assert policy_profile["role_skill_levels"] == authored_profile.role_skill_levels
        assert set(policy_profile["role_weights"]) <= set(MODERN_CITY.extra_roles)
        assert all(weight > 1 for weight in policy_profile["role_weights"].values())
        assert all(
            skill_id in allowed_skills
            for role_levels in policy_profile["role_skill_levels"].values()
            for skill_id in role_levels
        )
        assert set(policy_profile["role_skill_levels"]) <= set(MODERN_CITY.extra_roles)

    serials_by_group: Counter[str] = Counter()
    for resident in all_residents:
        role = resident.components["public_role"]
        group_id = resident.components["life"]["population_group_id"]
        group = state.entities[UUID(group_id)]
        region = state.entities[group.location_id]
        authored_profile = profiles[region.name]
        policy = group.components["population"]["seat_policy"]
        expected_role = select_population_occupation_role(
            MODERN_CITY.extra_roles,
            authored_profile.role_weights,
            seed_id=UUID(policy["seed_id"]),
            group_id=group.id,
            period_index=0,
            serial=serials_by_group[group_id],
        )
        serials_by_group[group_id] += 1
        assert role == expected_role

        expected_levels = authored_profile.role_skill_levels.get(role, {})
        skills = resident.components["skills"]["skills"]
        for skill_id, level in expected_levels.items():
            assert Decimal(skills[skill_id]["level"]) == Decimal(level)


def test_2_5_keeps_sustained_totals_membership_jurisdiction_and_population_phase() -> None:
    catalog = ContentCatalog()
    baseline = catalog.build_world(
        "modern_city", "Regional Comparison", "Rin", seed=20261117,
        population_mode="sustained", pack_version="2.4.0",
    ).state
    profiled = catalog.build_world(
        "modern_city", "Regional Comparison", "Rin", seed=20261117,
        population_mode="sustained", pack_version="2.5.0",
    ).state

    assert _population_signature(profiled) == _population_signature(baseline)
    assert _organization_signature(profiled) == _organization_signature(baseline)
    assert len(profiled.work_postings) == len(baseline.work_postings) == 12
    assert len(profiled.kinship_links) == len(baseline.kinship_links)
    assert _population_schedule_signature(profiled) == _population_schedule_signature(baseline)
    assert _population_schedule_signature(profiled)[1] == "environment"

    for state in (profiled, baseline):
        for raw_group_id in state.metadata["content_index"]["population_group_ids"]:
            policy = state.entities[UUID(raw_group_id)].components["population"]["seat_policy"]
            assert policy["eligible_min_age_years"] == 18
            assert policy["eligible_max_age_years"] == 64
            assert policy["promotion_serial"] == 0
