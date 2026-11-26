from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS, LOW_MAGIC
from world_engine.content.feudal_occupation_profiles_290 import (
    FEUDAL_ORGANIZATION_HOME_REGIONS_290,
    build_feudal_occupation_profiles_290,
    validate_feudal_occupation_contract_290,
)
from world_engine.content.recruitment_roles import RECRUITMENT_ROLES
from world_engine.domain.reducers import state_hash


_YEAR_SECONDS = 360 * 86_400
_PACK_VERSION = "2.9.0"


def _ordinary_residents_by_region(state):
    result = defaultdict(list)
    for raw_id in state.metadata["content_index"]["other_resident_ids"]:
        resident = state.entities[UUID(raw_id)]
        life = resident.components["life"]
        group = state.entities[UUID(life["population_group_id"])]
        region = state.entities[group.location_id].name
        age_years = (state.current_tick - life["birth_tick"]) // _YEAR_SECONDS
        result[region].append((resident, age_years))
    return result


def _recruitment_candidates(state, regional_residents):
    result = {}
    for raw_id in state.metadata["content_index"]["organization_ids"]:
        organization = state.entities[UUID(raw_id)]
        policy = organization.components["governance"]["membership_recruitment"][0]
        region_name = state.entities[organization.location_id].name
        result[organization.name] = [
            resident
            for resident, age in regional_residents[region_name]
            if resident.components["life"]["life_stage"] == "adult"
            and policy["minimum_age_years"] <= age <= policy["maximum_age_years"]
            and resident.components["public_role"] in policy["eligible_roles"]
        ]
    return result


def test_feudal_2_9_is_only_listed_for_sustained_low_magic() -> None:
    catalog = ContentCatalog()
    listed = {pack["pack_id"]: pack for pack in catalog.list_packs()}

    assert listed["low_magic_feudal"]["available_versions"] == ["2.0.0", "2.2.0", "2.3.0", "2.8.0", "2.12.0", "2.13.0", "2.14.0"]
    assert listed["low_magic_feudal"]["sustained_available_versions"] == ["2.4.0", _PACK_VERSION]
    assert listed["long_life_cultivation"]["sustained_available_versions"] == ["2.4.0", "2.11.0"]
    assert listed["modern_city"]["sustained_available_versions"] == [
        "2.4.0", "2.5.0", "2.6.0", "2.7.0", "2.10.0",
    ]

    with pytest.raises(ValueError, match="only with sustained population mode"):
        catalog.build_world(
            "low_magic_feudal", "Natural Feudal 2.9", "Rin", pack_version=_PACK_VERSION,
        )
    for pack in BACKGROUNDS:
        if pack.pack_id == "low_magic_feudal":
            continue
        with pytest.raises(ValueError, match="only for the low_magic_feudal background"):
            catalog.build_world(
                pack.pack_id,
                "Wrong Feudal Background",
                "Rin",
                population_mode="sustained",
                pack_version=_PACK_VERSION,
            )


def test_feudal_2_9_build_uses_authored_profiles_homes_and_2_8_systems() -> None:
    catalog = ContentCatalog()
    title = "Feudal 2.9 Regional Occupations"
    first = catalog.build_world(
        "low_magic_feudal", title, "Rin", seed=20261121,
        population_mode="sustained", pack_version=_PACK_VERSION,
    )
    repeated = catalog.build_world(
        "low_magic_feudal", title, "Rin", seed=20261121,
        population_mode="sustained", pack_version=_PACK_VERSION,
    )
    state = first.state
    index = state.metadata["content_index"]
    profiles = build_feudal_occupation_profiles_290()
    report = validate_feudal_occupation_contract_290(
        regions=LOW_MAGIC.regions,
        organizations=LOW_MAGIC.organizations,
        profiles=profiles,
        roles=LOW_MAGIC.extra_roles,
        recruitment_roles=RECRUITMENT_ROLES[LOW_MAGIC.pack_id],
        organization_home_regions=FEUDAL_ORGANIZATION_HOME_REGIONS_290,
    )

    assert first.pack_version == state.metadata["pack_version"] == _PACK_VERSION
    assert state.metadata["population_mode"] == "sustained"
    assert state.metadata["generation_seed"] == 20261121
    assert len(index["other_resident_ids"]) == state.metadata["seed_counts"]["other_named_living_characters"] == 264
    assert len(index["country_ids"]) == state.metadata["seed_counts"]["countries"] == 24
    assert len(index["organization_ids"]) == state.metadata["seed_counts"]["organizations"] == 24
    assert state.metadata["seed_counts"]["places"] == 120
    assert sum(entity.kind == "polity" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "organization" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "place" for entity in state.entities.values()) == 120
    assert len(state.work_postings) == 12
    assert len(state.kinship_links) == 12
    assert 3 <= sum(
        len(entity.components.get("place", {}).get("waypoint_opportunities", []))
        for entity in state.entities.values()
    ) <= 4
    assert report.version == _PACK_VERSION
    assert report.region_count == 12
    assert report.organization_count == 24

    region_names = {region.name for region in LOW_MAGIC.regions}
    organizations = [state.entities[UUID(raw_id)] for raw_id in index["organization_ids"]]
    assert {organization.name: state.entities[organization.location_id].name for organization in organizations} == (
        FEUDAL_ORGANIZATION_HOME_REGIONS_290
    )
    assert all(state.entities[organization.location_id].name in region_names for organization in organizations)

    for raw_id in index["population_group_ids"]:
        group = state.entities[UUID(raw_id)]
        region_name = state.entities[group.location_id].name
        policy = group.components["population"]["seat_policy"]
        assert policy["maintain_civic_occupations"] is True
        assert policy["extra_roles"] == [
            role for role in LOW_MAGIC.extra_roles if role in profiles[region_name].role_weights
        ]
        assert policy["occupation_profile"]["role_weights"] == profiles[region_name].role_weights
        assert policy["occupation_profile"]["role_skill_levels"] == profiles[region_name].role_skill_levels

    regional_residents = _ordinary_residents_by_region(state)
    assert set(regional_residents) == region_names
    assert sum(map(len, regional_residents.values())) == 264
    for region_name, residents in regional_residents.items():
        profile = profiles[region_name]
        for resident, _age in residents:
            role = resident.components["public_role"]
            assert role in profile.role_weights
            skills = resident.components["skills"]["skills"]
            for skill_id, level in profile.role_skill_levels[role].items():
                assert Decimal(skills[skill_id]["level"]) == Decimal(level)

    assert state_hash(state) == state_hash(repeated.state)
    assert first.model_dump(mode="json") == repeated.model_dump(mode="json")


def test_feudal_2_9_local_recruitment_has_seeded_adult_roster_coverage() -> None:
    catalog = ContentCatalog()
    candidates_seen = Counter()
    expected_names = {organization.name for organization in LOW_MAGIC.organizations}

    for seed in range(8):
        state = catalog.build_world(
            "low_magic_feudal",
            "Feudal 2.9 Recruitment Coverage",
            "Rin",
            seed=seed,
            population_mode="sustained",
            pack_version=_PACK_VERSION,
        ).state
        regional_residents = _ordinary_residents_by_region(state)
        candidates = _recruitment_candidates(state, regional_residents)

        assert set(candidates) == expected_names
        candidates_seen.update(name for name, residents in candidates.items() if residents)

    assert set(candidates_seen) == expected_names


def test_feudal_2_8_and_pre_2_9_builds_remain_deterministic() -> None:
    catalog = ContentCatalog()
    for version, population_mode in (("2.8.0", "sustained"), ("2.4.0", "sustained"), ("2.3.0", "natural")):
        first = catalog.build_world(
            "low_magic_feudal",
            f"Feudal deterministic {version}",
            "Rin",
            seed=20261120,
            population_mode=population_mode,
            pack_version=version,
        )
        repeated = catalog.build_world(
            "low_magic_feudal",
            f"Feudal deterministic {version}",
            "Rin",
            seed=20261120,
            population_mode=population_mode,
            pack_version=version,
        )
        assert state_hash(first.state) == state_hash(repeated.state)
        assert first.model_dump(mode="json") == repeated.model_dump(mode="json")
        if version == "2.8.0":
            assert all(
                "maintain_civic_occupations"
                not in first.state.entities[UUID(raw_id)].components["population"]["seat_policy"]
                for raw_id in first.state.metadata["content_index"]["population_group_ids"]
            )

