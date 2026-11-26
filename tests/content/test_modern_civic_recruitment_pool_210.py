from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS
from world_engine.domain.market_pricing import PRICE_REVIEW_CONFIG_KEY, PRICE_REVIEW_HANDLER_ID
from world_engine.domain.reducers import state_hash


_VERSION = "2.10.0"
_MODERN_CITY = "modern_city"


def _waypoint_count(state) -> int:
    return sum(
        len(entity.components.get("place", {}).get("waypoint_opportunities", []))
        for entity in state.entities.values()
    )


def _seat_policies(state) -> list[dict]:
    return [
        state.entities[UUID(raw_id)].components["population"]["seat_policy"]
        for raw_id in state.metadata["content_index"]["population_group_ids"]
    ]


def _assert_existing_modern_feature_set(state) -> None:
    assert 3 <= _waypoint_count(state) <= 4
    assert state.work_postings and state.kinship_links
    assert PRICE_REVIEW_CONFIG_KEY in state.metadata
    assert any(event.handler_id == PRICE_REVIEW_HANDLER_ID for event in state.scheduled_events)
    assert state.goals
    assert any("investigation_case" in entity.components for entity in state.entities.values())


def test_modern_2_10_is_listed_only_for_sustained_modern_city_and_rejects_other_modes() -> None:
    catalog = ContentCatalog()
    listed = {pack["pack_id"]: pack for pack in catalog.list_packs()}

    assert listed[_MODERN_CITY]["version"] == "2.0.0"
    assert listed[_MODERN_CITY]["available_versions"] == ["2.0.0", "2.2.0", "2.3.0", "2.8.0", "2.12.0", "2.13.0"]
    assert listed[_MODERN_CITY]["sustained_available_versions"] == [
        "2.4.0", "2.5.0", "2.6.0", "2.7.0", _VERSION,
    ]
    assert all(
        _VERSION not in pack["sustained_available_versions"]
        for pack_id, pack in listed.items() if pack_id != _MODERN_CITY
    )

    with pytest.raises(ValueError, match="only with sustained population mode"):
        catalog.build_world(
            _MODERN_CITY, "Natural 2.10", "Rin", seed=20261121,
            population_mode="natural", pack_version=_VERSION,
        )
    for pack in BACKGROUNDS:
        if pack.pack_id == _MODERN_CITY:
            continue
        with pytest.raises(ValueError, match="only for the modern_city background"):
            catalog.build_world(
                pack.pack_id, "Wrong 2.10 Background", "Rin", seed=20261121,
                population_mode="sustained", pack_version=_VERSION,
            )

    natural_default = catalog.build_world(
        _MODERN_CITY, "Natural default", "Rin", seed=20261121,
        population_mode="natural",
    )
    assert all(
        policy is None or "maintain_civic_recruitment_pool" not in policy
        for policy in _seat_policies(natural_default.state)
    )


def test_modern_2_10_adds_only_the_recruitment_pool_flag_and_preserves_genesis_features() -> None:
    catalog = ContentCatalog()
    title = "Modern civic recruitment pool"
    seed = 20261121
    first = catalog.build_world(
        _MODERN_CITY, title, "Rin", seed=seed,
        population_mode="sustained", pack_version=_VERSION,
    )
    repeated = catalog.build_world(
        _MODERN_CITY, title, "Rin", seed=seed,
        population_mode="sustained", pack_version=_VERSION,
    )
    legacy = catalog.build_world(
        _MODERN_CITY, title, "Rin", seed=seed,
        population_mode="sustained", pack_version="2.8.0",
    )
    state = first.state
    counts = state.metadata["seed_counts"]

    assert first.pack_version == state.metadata["pack_version"] == _VERSION
    assert state.metadata["population_mode"] == "sustained"
    assert state.metadata["generation_seed"] == seed
    assert counts == {
        "countries": 24,
        "organizations": 24,
        "key_npcs": 32,
        "other_named_living_characters": 264,
        "places": 120,
    }
    assert len(state.metadata["content_index"]["other_resident_ids"]) == 264
    assert len(state.metadata["content_index"]["country_ids"]) == 24
    assert len(state.metadata["content_index"]["organization_ids"]) == 24
    assert _waypoint_count(state) == _waypoint_count(legacy.state)
    assert len(state.work_postings) == len(legacy.state.work_postings) == 12
    assert len(state.kinship_links) == len(legacy.state.kinship_links) == 12
    assert PRICE_REVIEW_CONFIG_KEY in state.metadata and PRICE_REVIEW_CONFIG_KEY in legacy.state.metadata
    assert state.goals and legacy.state.goals
    assert any("investigation_case" in entity.components for entity in state.entities.values())
    assert any("investigation_case" in entity.components for entity in legacy.state.entities.values())
    assert all(
        policy["maintain_civic_occupations"] is True
        and policy["maintain_civic_recruitment_pool"] is True
        and policy["occupation_profile"]["role_weights"]
        for policy in _seat_policies(state)
    )
    assert all(
        policy["maintain_civic_occupations"] is True
        and "maintain_civic_recruitment_pool" not in policy
        and policy["occupation_profile"]["role_weights"]
        for policy in _seat_policies(legacy.state)
    )
    assert state_hash(state) == state_hash(repeated.state)
    assert first.model_dump(mode="json") == repeated.model_dump(mode="json")
    assert state.metadata["content_index"]["region_ids"] != legacy.state.metadata["content_index"]["region_ids"]
    _assert_existing_modern_feature_set(state)
