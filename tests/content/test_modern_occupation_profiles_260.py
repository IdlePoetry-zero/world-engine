from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS, MODERN_CITY, RegionalOccupationProfile
from world_engine.content.modern_occupation_profiles_260 import (
    MODERN_CITY_OCCUPATION_PROFILE_OVERRIDES_260,
    build_modern_city_occupation_profiles_260,
    validate_modern_city_occupation_contract_260,
)
from world_engine.content.recruitment_roles import RECRUITMENT_ROLES
from world_engine.domain.reducers import state_hash


_PROFILE_260_REGIONS = {
    "Harbor Ward",
    "North Station",
    "Old Town",
    "East Works",
    "South Gardens",
    "Airport Loop",
}
_YEAR_SECONDS = 365 * 86_400


def _generated_residents(state):
    resident_ids = state.metadata["content_index"]["other_resident_ids"]
    return [state.entities[UUID(raw_id)] for raw_id in resident_ids]


def _regional_residents(state):
    result = defaultdict(list)
    for resident in _generated_residents(state):
        life = resident.components["life"]
        group = state.entities[UUID(life["population_group_id"])]
        region = state.entities[group.location_id].name
        age_years = (state.current_tick - life["birth_tick"]) // _YEAR_SECONDS
        result[region].append((resident, age_years))
    return result


def _organization_candidates(state, regional_residents):
    organizations = [
        state.entities[UUID(raw_id)]
        for raw_id in state.metadata["content_index"]["organization_ids"]
    ]
    candidates = {}
    for organization in organizations:
        policy = organization.components["governance"]["membership_recruitment"][0]
        region_name = state.entities[organization.location_id].name
        candidates[organization.name] = [
            resident
            for resident, age in regional_residents[region_name]
            if resident.components["life"]["life_stage"] == "adult"
            and 18 <= age <= 64
            and resident.components["public_role"] in policy["eligible_roles"]
        ]
    return candidates


def _validate_260(pack, profiles) -> None:
    validate_modern_city_occupation_contract_260(
        regions=pack.regions,
        organizations=pack.organizations,
        profiles=profiles,
        roles=pack.extra_roles,
        recruitment_roles=RECRUITMENT_ROLES[pack.pack_id],
    )


def test_2_6_is_modern_sustained_opt_in_and_preserves_the_2_5_genesis_hash() -> None:
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

    with pytest.raises(ValueError, match="only with sustained population mode"):
        catalog.build_world("modern_city", "Natural 2.6", "Rin", pack_version="2.6.0")
    for pack in BACKGROUNDS:
        if pack.pack_id == "modern_city":
            continue
        with pytest.raises(ValueError, match="only for the modern_city background"):
            catalog.build_world(
                pack.pack_id,
                "Wrong background 2.6",
                "Rin",
                population_mode="sustained",
                pack_version="2.6.0",
            )

    legacy = catalog.build_world(
        "modern_city",
        "Regional Occupations",
        "Rin",
        seed=20261117,
        population_mode="sustained",
        pack_version="2.5.0",
    )
    repeated = catalog.build_world(
        "modern_city",
        "Regional Occupations",
        "Rin",
        seed=20261117,
        population_mode="sustained",
        pack_version="2.5.0",
    )
    assert state_hash(legacy.state) == "0dc2b9cdb25b845fac3e94e9190f1e9257631d37e0cf7f2aa922b9a0a7104e22"
    assert legacy.model_dump(mode="json") == repeated.model_dump(mode="json")


def test_2_6_overrides_only_the_authored_regions_and_preserves_their_main_roles() -> None:
    base_profiles = MODERN_CITY.regional_occupation_profiles
    assert base_profiles is not None
    profiles = build_modern_city_occupation_profiles_260()

    assert set(profiles) == {region.name for region in MODERN_CITY.regions}
    assert set(MODERN_CITY_OCCUPATION_PROFILE_OVERRIDES_260) == _PROFILE_260_REGIONS
    for region_name, base_profile in base_profiles.items():
        versioned = profiles[region_name]
        if region_name not in _PROFILE_260_REGIONS:
            assert versioned == base_profile
            continue

        for role, weight in base_profile.role_weights.items():
            assert versioned.role_weights[role] == weight
            assert versioned.role_skill_levels[role] == base_profile.role_skill_levels[role]
        assert set(versioned.role_skill_levels) == set(versioned.role_weights)
        assert all(weight > 0 for weight in versioned.role_weights.values())

    _validate_260(MODERN_CITY, profiles)


def test_2_6_contract_covers_all_24_organizations_and_reports_missing_local_support() -> None:
    profiles = build_modern_city_occupation_profiles_260()
    for index, organization in enumerate(MODERN_CITY.organizations):
        region = MODERN_CITY.regions[index % len(MODERN_CITY.regions)]
        eligible = RECRUITMENT_ROLES[MODERN_CITY.pack_id][organization.organization_type]
        assert set(eligible) & set(profiles[region.name].role_weights)

    broken = dict(profiles)
    base_south_gardens = MODERN_CITY.regional_occupation_profiles["South Gardens"]
    broken["South Gardens"] = base_south_gardens
    with pytest.raises(
        ValueError,
        match="organization 'Port Union Freight Cooperative' in region 'South Gardens' has no eligible occupation role",
    ):
        _validate_260(MODERN_CITY, broken)

    incomplete = dict(profiles)
    profile = profiles["North Station"]
    incomplete["North Station"] = replace(
        profile,
        role_skill_levels={
            role: skills for role, skills in profile.role_skill_levels.items() if role != "librarian"
        },
    )
    with pytest.raises(ValueError, match="North Station.*role-to-skill mapping is incomplete"):
        _validate_260(MODERN_CITY, incomplete)


def test_2_6_generated_population_has_adult_resident_candidates_over_seeded_distribution() -> None:
    catalog = ContentCatalog()
    pack = next(pack for pack in BACKGROUNDS if pack.pack_id == "modern_city")
    profiles = build_modern_city_occupation_profiles_260()
    _validate_260(pack, profiles)

    catalog_coverage = Counter()
    for seed in range(8):
        draft = catalog.build_world(
            "modern_city",
            "Regional Eligibility Sweep",
            "Rin",
            seed=seed,
            population_mode="sustained",
            pack_version="2.6.0",
        )
        state = draft.state
        assert draft.pack_version == state.metadata["pack_version"] == "2.6.0"
        assert len(_generated_residents(state)) == 264
        assert len(state.metadata["content_index"]["population_group_ids"]) == 12
        assert len(state.work_postings) == 12
        assert len(state.kinship_links) == len(pack.regions)

        regional_residents = _regional_residents(state)
        assert set(regional_residents) == {region.name for region in pack.regions}
        for region_name, population in regional_residents.items():
            group = next(
                state.entities[UUID(raw_id)]
                for raw_id in state.metadata["content_index"]["population_group_ids"]
                if state.entities[UUID(raw_id)].name == f"Residents of {region_name}"
            )
            occupation_profile = group.components["population"]["seat_policy"]["occupation_profile"]
            assert occupation_profile["role_weights"] == profiles[region_name].role_weights
            assert occupation_profile["role_skill_levels"] == profiles[region_name].role_skill_levels
            assert set(occupation_profile["role_skill_levels"]) == set(occupation_profile["role_weights"])
            assert all(len(profiles[region_name].role_skill_levels[role]) > 0 for role in occupation_profile["role_weights"])
            assert population

        candidates = _organization_candidates(state, regional_residents)
        assert len(candidates) == 24
        catalog_coverage.update(name for name, people in candidates.items() if people)

    assert set(catalog_coverage) == {organization.name for organization in pack.organizations}


def test_2_6_generated_roles_receive_the_authored_registered_skill_levels() -> None:
    draft = ContentCatalog().build_world(
        "modern_city",
        "Regional Skill Profiles",
        "Rin",
        seed=20261117,
        population_mode="sustained",
        pack_version="2.6.0",
    )
    profiles = build_modern_city_occupation_profiles_260()
    for resident in _generated_residents(draft.state):
        group_id = UUID(resident.components["life"]["population_group_id"])
        group = draft.state.entities[group_id]
        region = draft.state.entities[group.location_id].name
        role = resident.components["public_role"]
        skills = resident.components["skills"]["skills"]
        authored_skill_levels = profiles[region].role_skill_levels.get(role, {})
        for skill_id, level in authored_skill_levels.items():
            assert Decimal(skills[skill_id]["level"]) == Decimal(level)
