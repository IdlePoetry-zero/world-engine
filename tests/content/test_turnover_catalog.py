from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS
from world_engine.domain.market_pricing import PRICE_REVIEW_CONFIG_KEY
from world_engine.domain.promise_trust_settlement import PROMISE_TRUST_POLICY_KEY


@pytest.fixture(scope="module")
def catalog() -> ContentCatalog:
    return ContentCatalog()


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_sustained_240_roster_and_pack_picker_defaults_stay_unchanged(catalog, pack) -> None:
    listed_pack = next(item for item in catalog.list_packs() if item["pack_id"] == pack.pack_id)
    assert listed_pack["version"] == "2.0.0"
    assert listed_pack["available_versions"] == ["2.0.0", "2.2.0", "2.3.0", "2.8.0", "2.12.0", "2.13.0", "2.14.0", "2.15.0"]
    expected_sustained_versions = ["2.4.0"]
    if pack.pack_id == "modern_city":
        expected_sustained_versions.extend(["2.5.0", "2.6.0", "2.7.0", "2.10.0"])
    elif pack.pack_id == "low_magic_feudal":
        expected_sustained_versions.append("2.9.0")
    elif pack.pack_id == "long_life_cultivation":
        expected_sustained_versions.append("2.11.0")
    assert listed_pack["sustained_available_versions"] == expected_sustained_versions
    assert listed_pack["capacity"]["other_named_living_characters"] == 240

    default = catalog.build_world(
        pack.pack_id, "Original Roster", "Rin", seed=2026, population_mode="sustained",
    )
    legacy_family = catalog.build_world(
        pack.pack_id, "Original Roster", "Rin", seed=2026,
        population_mode="sustained", pack_version="2.3.0",
    )
    assert default.pack_version == "2.0.0"
    assert len(default.state.metadata["content_index"]["other_resident_ids"]) == 240
    assert legacy_family.pack_version == "2.3.0"
    assert len(legacy_family.state.metadata["content_index"]["other_resident_ids"]) == 240
    assert len(legacy_family.state.work_postings) == 12
    assert legacy_family.state.kinship_links

    with pytest.raises(ValueError, match="only with sustained population mode"):
        catalog.build_world(
            pack.pack_id, "Invalid Dense Roster", "Rin", seed=2026,
            population_mode="natural", pack_version="2.4.0",
        )


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_sustained_240_plus_24_seed_full_regional_turnover_roster(catalog, pack) -> None:
    draft = catalog.build_world(
        pack.pack_id, "Dense Sustained Roster", "Rin", seed=20261117,
        population_mode="sustained", pack_version="2.4.0",
    )
    state = draft.state
    index = state.metadata["content_index"]
    resident_ids = [UUID(value) for value in index["other_resident_ids"]]
    added_ids = resident_ids[240:]
    key_ids = [UUID(value) for value in index["key_npc_ids"]]

    assert draft.pack_version == state.metadata["pack_version"] == "2.4.0"
    assert state.metadata["population_mode"] == "sustained"
    assert state.metadata["generation_seed"] == 20261117
    assert len(resident_ids) == state.metadata["seed_counts"]["other_named_living_characters"] == 264
    assert len(key_ids) == state.metadata["seed_counts"]["key_npcs"] == 32
    assert state.metadata["seed_counts"]["countries"] == 24
    assert state.metadata["seed_counts"]["organizations"] == 24
    assert state.metadata["seed_counts"]["places"] == 120
    assert sum(entity.kind == "place" for entity in state.entities.values()) == 120
    assert sum(entity.kind == "polity" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "organization" for entity in state.entities.values()) == 24
    assert len([entity for entity in state.entities.values() if entity.kind == "character"]) == 297

    resident_groups: dict[UUID, int] = {}
    for resident_id in resident_ids:
        resident = state.entities[resident_id]
        group_id = UUID(resident.components["life"]["population_group_id"])
        resident_groups[group_id] = resident_groups.get(group_id, 0) + 1
    groups = [
        state.entities[UUID(group_id)] for group_id in index["population_group_ids"]
    ]
    legacy_resident_counts = [27, 27, 24, *([18] * 9)]
    assert [resident_groups[group.id] for group in groups] == [count + 2 for count in legacy_resident_counts]
    assert len(added_ids) == 24
    added_by_group: dict[UUID, int] = {}
    for resident_id in added_ids:
        person = state.entities[resident_id]
        group_id = UUID(person.components["life"]["population_group_id"])
        added_by_group[group_id] = added_by_group.get(group_id, 0) + 1
    assert [added_by_group[group.id] for group in groups] == [2] * 12

    active_people = [
        entity for entity in state.entities.values()
        if entity.kind == "character" and entity.status not in {"dead", "retired"}
    ]
    people_by_group: dict[UUID, list] = {}
    for person in active_people:
        group_id = UUID(person.components["life"]["population_group_id"])
        people_by_group.setdefault(group_id, []).append(person)

    year_ticks = state.calendar.year_days * state.calendar.day_seconds
    expected_civilian_lifespan = 100 if pack.pack_id == "long_life_cultivation" else 82
    for group in groups:
        population = group.components["population"]
        regional_people = people_by_group[group.id]
        assert population["headcount"] - population["unmodeled_headcount"] == len(regional_people)
        policy = population["seat_policy"]
        expected_capacity = len(regional_people) - (1 if state.player_id in {p.id for p in regional_people} else 0)
        assert policy["non_player_seat_capacity"] == expected_capacity
        assert policy["promotion_serial"] == 0
        assert policy["eligible_min_age_years"] == 18
        assert policy["eligible_max_age_years"] == 64
        assert policy["lifespan_min_years"] == policy["lifespan_max_years"] == expected_civilian_lifespan
        assert policy["extra_given_names"] == list(pack.extra_given_names)
        assert policy["extra_family_names"] == list(pack.extra_family_names)
        assert policy["extra_roles"] == list(pack.extra_roles)

        region = state.entities[group.location_id]
        harvest_policies = region.components["place"]["renewable_harvests"]
        expected_recipient_ids = {str(person.id) for person in regional_people}
        assert len(harvest_policies) == 2
        for harvest in harvest_policies:
            recipients = harvest["recipients"]
            assert {item["entity_id"] for item in recipients} == expected_recipient_ids
            ration_per_person = Decimal(recipients[0]["per_period"])
            assert all(Decimal(item["per_period"]) == ration_per_person for item in recipients)
            assert Decimal(harvest["per_period"]) == ration_per_person * len(recipients)
            period_count = state.calendar.year_days * 600 // 30
            assert Decimal(harvest["maximum_total"]) == Decimal(harvest["per_period"]) * period_count
            source = state.resources[UUID(harvest["source_account_id"])]
            assert source.account_type == "environmental_source"
            assert source.credit_limit == 0

    food_id = state.metadata["resource_roles"]["food_resource_id"]
    water_id = state.metadata["resource_roles"]["water_resource_id"]
    for resident_id in added_ids:
        resident = state.entities[resident_id]
        components = resident.components
        assert resident.status == "active"
        assert components["public_role"] in pack.extra_roles
        group_id = UUID(components["life"]["population_group_id"])
        group = state.entities[group_id]
        assert resident.location_id in {
            UUID(place_id) for place_id in group.components["population"]["seat_policy"]["home_place_ids"]
        }
        assert {
            "life", "health", "needs", "skills", "personality", "progression",
            "capabilities", "inventory", "appearance",
        } <= set(components)
        assert components["life"]["is_immortal"] is False
        age_years = -components["life"]["birth_tick"] // year_ticks
        assert 18 <= age_years <= 64
        expected_lifespan = (
            1_200
            if pack.pack_id == "long_life_cultivation" and any(
                marker in components["public_role"].casefold()
                for marker in ("cultivat", "disciple", "master", "sect")
            )
            else expected_civilian_lifespan
        )
        assert components["life"]["lifespan_ticks"] == expected_lifespan * year_ticks
        for resource_id, minimum in ((food_id, Decimal("54")), (water_id, Decimal("90"))):
            lots = [
                lot for lot in components["inventory"]["lots"]
                if lot["resource_id"] == resource_id
            ]
            assert len(lots) == 1
            lot = lots[0]
            assert Decimal(lot["quantity"]) >= minimum
            account = state.resources[UUID(lot["account_id"])]
            assert account.owner_id == resident_id
            assert account.resource_id == resource_id
            assert account.balance >= Decimal(lot["quantity"]) > 0

    endowments = state.metadata["resource_roles"]["environmental_endowments"]
    for endowment in endowments:
        allocated = sum((Decimal(row["quantity"]) for row in endowment["allocations"]), Decimal("0"))
        assert Decimal(endowment["quantity"]) == Decimal(endowment["remaining_quantity"]) + allocated
        assert Decimal(endowment["remaining_quantity"]) >= 0

    for group in groups:
        region_id = str(group.location_id)
        regional_endowments = [item for item in endowments if item["region_id"] == region_id]
        for resident_id in added_ids:
            if UUID(state.entities[resident_id].components["life"]["population_group_id"]) != group.id:
                continue
            for resource_id, minimum in ((food_id, Decimal("54")), (water_id, Decimal("90"))):
                endowment, = [item for item in regional_endowments if item["resource_id"] == resource_id]
                quantity = sum(
                    (Decimal(row["quantity"]) for row in endowment["allocations"] if row["holder_id"] == str(resident_id)),
                    Decimal("0"),
                )
                assert quantity >= minimum

    assert state.metadata[PRICE_REVIEW_CONFIG_KEY]
    assert state.metadata[PROMISE_TRUST_POLICY_KEY]
    assert len(state.work_postings) == 12
    assert state.kinship_links
    assert any(entity.components.get("investigation_case") for entity in state.entities.values())


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_sustained_264_genesis_is_deterministic_and_key_profiles_are_preserved(catalog, pack) -> None:
    first = catalog.build_world(
        pack.pack_id, "Repeat Dense Roster", "Rin", seed=-923,
        population_mode="sustained", pack_version="2.4.0",
    )
    second = catalog.build_world(
        pack.pack_id, "Repeat Dense Roster", "Rin", seed=-923,
        population_mode="sustained", pack_version="2.4.0",
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")

    expected_key_roles = {
        f"{given} {family}": profile.role
        for (given, family), profile in zip(pack.key_names, pack.key_people, strict=True)
    }
    key_ids = [UUID(value) for value in first.state.metadata["content_index"]["key_npc_ids"]]
    assert {
        first.state.entities[person_id].name: first.state.entities[person_id].components["public_role"]
        for person_id in key_ids
    } == expected_key_roles

    resident_ids = [UUID(value) for value in first.state.metadata["content_index"]["other_resident_ids"]]
    all_names = [first.state.entities[person_id].name for person_id in [
        *key_ids, *resident_ids, first.state.player_id,
    ]]
    assert len(set(name.casefold() for name in all_names)) == 1 + 32 + 264
