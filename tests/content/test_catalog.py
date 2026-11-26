from __future__ import annotations

from collections import deque
import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.application.visibility import project_player_state
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS
from world_engine.domain.models import AccessScope, NewWorld, WorldState
from world_engine.domain.components import GovernanceComponent, PlaceRoute
from world_engine.domain.investigation_cases import case_definition_from_anchor
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine, route_accessible
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.fixture(scope="module")
def catalog() -> ContentCatalog:
    return ContentCatalog()


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
@pytest.mark.parametrize("mode", ("natural", "sustained"))
def test_opening_investigation_pairs_private_independent_historical_sources(catalog, pack, mode):
    draft = catalog.build_world(pack.pack_id, "Opening investigation", "Player", seed=0,
                                population_mode=mode, pack_version="1.9.0")
    state = draft.state
    item, = (entity for entity in state.entities.values() if "historical_evidence" in entity.components)
    source = item.components["historical_evidence"]
    rumor, = (record for record in state.knowledge if record.disclosure_policy == "if_asked")
    assert state.entities[rumor.holder_id].location_id == state.entities[state.player_id].location_id
    assert state.entities[item.location_id].components["hidden"] is True
    assert (source["subject_id"], source["fact"]["effective_tick"]) == (str(rumor.subject_id), rumor.fact.effective_tick)
    assert source["fact"]["value"] != rumor.fact.value
    assert source["provenance"][0]["reference_id"] != rumor.provenance[0].reference_id
    projected = project_player_state(state.model_dump(mode="json"), str(state.player_id))
    assert str(item.id) not in projected["entities"]
    assert all(record["id"] != str(rumor.id) for record in projected["knowledge"])


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
@pytest.mark.parametrize("mode", ("natural", "sustained"))
def test_opening_case_routes_have_accessible_authored_sources(catalog, pack, mode):
    draft = catalog.build_world(pack.pack_id, "Case opening", "Player", seed=0,
                                population_mode=mode, pack_version="2.0.0")
    state = draft.state
    opening = state.entities[state.player_id].location_id
    anchor, = (entity for entity in state.entities.values() if "investigation_case" in entity.components)
    definition = case_definition_from_anchor(anchor)
    assert anchor.location_id == opening
    assert draft.pack_version == state.metadata["pack_version"] == "2.0.0"

    artifact_sources = {
        root["reference_id"]: (entity, artifact)
        for entity in state.entities.values()
        if (artifact := entity.components.get("historical_evidence")) is not None
        for root in artifact["provenance"]
    }
    witness_sources = {
        root.reference_id: record
        for record in state.knowledge if record.disclosure_policy == "if_asked"
        for root in record.provenance
    }
    assert [route.finding for route in definition.routes].count("supports") >= 2
    assert any(route.finding == "rules_out" for route in definition.routes)
    used_roots = set()
    for route in definition.routes:
        assert state.entities[route.suspect_id].location_id == opening
        for requirement in route.requirements:
            for root in requirement.observation_provenance:
                entity, artifact = artifact_sources[root.reference_id]
                assert entity.status == "active"
                assert (artifact["subject_id"], artifact["fact"]["predicate"], artifact["fact"]["effective_tick"]) == (
                    str(requirement.subject_id), requirement.predicate, requirement.effective_tick,
                )
                assert root.reference_id not in used_roots
                used_roots.add(root.reference_id)
            for root in requirement.claim_provenance:
                witness = witness_sources[root.reference_id]
                assert state.entities[witness.holder_id].location_id == opening
                assert (witness.subject_id, witness.fact.predicate, witness.fact.effective_tick) == (
                    requirement.subject_id, requirement.predicate, requirement.effective_tick,
                )
                assert root.reference_id not in used_roots
                used_roots.add(root.reference_id)

    view = project_player_state(state.model_dump(mode="json"), str(state.player_id))
    assert view["available_case_anchors"] == [
        {"anchor_id": str(anchor.id), "case_id": str(definition.case_id), "title": definition.title, "brief": definition.brief},
    ]
    assert view["case_files"] == []
    assert "investigation_case" not in view["entities"][str(anchor.id)]["components"]
    assert not ({str(record.id) for record in witness_sources.values()} & {record["id"] for record in view["knowledge"]})


def test_catalog_lists_three_distinct_full_packs(catalog: ContentCatalog) -> None:
    packs = catalog.list_packs()

    assert {pack["pack_id"] for pack in packs} == {
        "low_magic_feudal",
        "modern_city",
        "long_life_cultivation",
    }
    assert len({pack["summary"] for pack in packs}) == 3
    assert {pack["version"] for pack in packs} == {"2.0.0"}
    assert all(pack["available_versions"] == ["2.0.0", "2.2.0", "2.3.0", "2.8.0", "2.12.0", "2.13.0"] for pack in packs)
    assert all(pack["capacity"] == {
        "countries": 24,
        "organizations": 24,
        "key_npcs": 32,
        "other_named_living_characters": 240,
        "places": 120,
    } for pack in packs)


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_full_world_seed_is_valid_and_meets_capacity(catalog: ContentCatalog, pack) -> None:
    draft = catalog.build_world(pack.pack_id, "A New World", "Mara Player")

    assert isinstance(draft, NewWorld)
    assert isinstance(draft.state, WorldState)
    assert draft.state.metadata["pack_version"] == draft.pack_version == "2.0.0"
    index = draft.state.metadata["content_index"]
    assert len(index["country_ids"]) == 24
    assert len(index["organization_ids"]) == 24
    assert len(index["key_npc_ids"]) == 32
    assert len(index["other_resident_ids"]) == 240
    assert len(index["settlement_ids"]) + len(index["region_ids"]) + 1 == 120

    entities = draft.state.entities
    assert sum(entity.kind == "polity" for entity in entities.values()) == 24
    assert sum(entity.kind == "organization" for entity in entities.values()) == 24
    assert sum(entity.kind == "place" for entity in entities.values()) == 120
    discovery_sites = [
        entity for entity in entities.values()
        if entity.kind == "place" and entity.components.get("place", {}).get("place_type") == "site"
    ]
    assert len(discovery_sites) == 1
    discovery_site = discovery_sites[0]
    assert discovery_site.components["hidden"] is True
    assert discovery_site.name == pack.discovery_site.name
    assert discovery_site.components["public_description"] == pack.discovery_site.description
    assert discovery_site.id not in {UUID(value) for value in index["settlement_ids"]}
    assert discovery_site.location_id == UUID(index["settlement_ids"][0])
    start_routes = entities[UUID(index["settlement_ids"][0])].components["place"]["routes"]
    route_to_site = next(route for route in start_routes if UUID(route["to_place_id"]) == discovery_site.id)
    assert route_to_site["travel_ticks"] == 3_600
    assert all(entity.location_id != discovery_site.id or entity.kind in {"character", "person"}
               or "historical_evidence" in entity.components for entity in entities.values())
    assert sum(entity.kind == "character" for entity in entities.values()) == 273
    living_people = [
        entity for entity in entities.values()
        if entity.kind == "character" and entity.status not in {"dead", "retired"}
    ]
    assert len(living_people) == 273
    assert all(entity.components["life"]["population_group_id"] for entity in living_people)
    linked_counts: dict[UUID, int] = {}
    for entity in living_people:
        cohort_id = UUID(entity.components["life"]["population_group_id"])
        linked_counts[cohort_id] = linked_counts.get(cohort_id, 0) + 1
    population_groups = [entity for entity in entities.values() if entity.kind == "group"]
    assert len(population_groups) == 12
    for group in population_groups:
        population = group.components["population"]
        assert population["headcount"] == population["unmodeled_headcount"] + linked_counts[group.id]
    for person_id in index["key_npc_ids"] + index["other_resident_ids"]:
        person = entities[UUID(person_id)]
        assert person.kind == "character"
        assert person.status == "active"
        assert person.name
        assert "life" in person.components

    resident_names = [entities[UUID(person_id)].name for person_id in index["other_resident_ids"]]
    key_names = [entities[UUID(person_id)].name for person_id in index["key_npc_ids"]]
    assert len(set(resident_names)) == 240
    assert len(set(key_names)) == 32
    personality_profiles = {
        tuple(entities[UUID(person_id)].components["personality"][key] for key in (
            "openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"
        ))
        for person_id in index["other_resident_ids"]
    }
    assert len(personality_profiles) >= 200

    assert len(draft.state.relations) >= 240
    assert len(draft.state.knowledge) == 278
    assert len(draft.state.goals) == 321
    assert draft.state.current_tick == 0
    assert len(draft.state.scheduled_events) == 2
    population_schedule = next(
        event for event in draft.state.scheduled_events if event.handler_id == "population.year_advance"
    )
    assert population_schedule.handler_id == "population.year_advance"
    assert population_schedule.handler_version == "1.0.0"
    assert population_schedule.phase == "environment"
    assert population_schedule.due_tick == draft.state.calendar.year_days * draft.state.calendar.day_seconds
    assert population_schedule.repeat_ticks == population_schedule.due_tick
    assert population_schedule.max_runs == 600
    assert population_schedule.payload == {}

    harvest_schedule = next(
        event for event in draft.state.scheduled_events if event.handler_id == "environmental.harvest"
    )
    season_seconds = 30 * draft.state.calendar.day_seconds
    seasons = pack.year_days * 600 // 30
    assert harvest_schedule.phase == "environment"
    assert harvest_schedule.due_tick == season_seconds
    assert harvest_schedule.repeat_ticks == season_seconds
    assert harvest_schedule.max_runs == seasons
    assert harvest_schedule.payload == {}
    harvest_policies = draft.state.metadata["resource_roles"]["environmental_harvest_policies"]
    assert len(harvest_policies) == 24
    assert sum(item["recipient_count"] for item in harvest_policies if item["resource_id"] == draft.state.metadata["resource_roles"]["food_resource_id"]) == 273
    assert sum(item["recipient_count"] for item in harvest_policies if item["resource_id"] == draft.state.metadata["resource_roles"]["water_resource_id"]) == 273
    for policy_index in harvest_policies:
        region_id = UUID(policy_index["region_id"])
        region = draft.state.entities[region_id]
        policy = next(
            item for item in region.components["place"]["renewable_harvests"]
            if item["policy_id"] == policy_index["policy_id"]
        )
        assert policy["period_ticks"] == season_seconds
        assert policy["last_issued_period"] == 0
        assert Decimal(policy["maximum_total"]) == Decimal(policy["per_period"]) * seasons
        assert len(policy["recipients"]) == policy_index["recipient_count"]
        assert sum(Decimal(item["per_period"]) for item in policy["recipients"]) == Decimal(policy["per_period"])
        source = draft.state.resources[UUID(policy["source_account_id"])]
        assert source.account_type == "environmental_source"
        assert source.owner_id == region_id and source.resource_id == policy["resource_id"]
        assert source.balance == 0 and source.credit_limit == 0
        assert all(
            draft.state.resources[UUID(item["account_id"])].owner_id == UUID(item["entity_id"])
            and draft.state.resources[UUID(item["account_id"])].resource_id == policy["resource_id"]
            for item in policy["recipients"]
        )

    for group in population_groups:
        population = group.components["population"]
        assert len(population["age_rates"]) == len(population["age_cohorts"]) == 201
        assert [item["age_years"] for item in population["age_rates"]] == list(range(201))
        assert [item["age_years"] for item in population["age_cohorts"]] == list(range(201))
        assert sum(item["headcount"] for item in population["age_cohorts"]) == population["unmodeled_headcount"]
        assert population["last_advanced_year"] == 0
        assert population["age_rates"][200]["annual_death_rate"] == "1"
        assert all(isinstance(item["annual_birth_rate"], str) for item in population["age_rates"])
        assert all(isinstance(item["annual_death_rate"], str) for item in population["age_rates"])


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_seeded_lifetimes_routes_and_recipes_use_calendar_seconds(catalog: ContentCatalog, pack) -> None:
    state = catalog.build_world(pack.pack_id, "Time Units", "Sol").state
    year_ticks = state.calendar.year_days * state.calendar.day_seconds
    player_life = state.entities[state.player_id].components["life"]

    assert player_life["birth_tick"] == -24 * year_ticks
    if pack.pack_id == "long_life_cultivation":
        assert player_life["lifespan_ticks"] == 100 * year_ticks
    else:
        assert player_life["lifespan_ticks"] == 82 * year_ticks
    assert player_life["maturity_ticks"] == 18 * year_ticks

    route_durations = [
        route["travel_ticks"]
        for entity in state.entities.values()
        for route in entity.components.get("place", {}).get("routes", [])
    ]
    production_durations = [
        recipe["duration_ticks"]
        for entity in state.entities.values()
        for recipe in entity.components.get("production", {}).get("recipes", [])
    ]
    site_id = next(
        entity.id for entity in state.entities.values()
        if entity.kind == "place" and entity.components.get("place", {}).get("place_type") == "site"
    )
    for entity in state.entities.values():
        for route in entity.components.get("place", {}).get("routes", []):
            duration = route["travel_ticks"]
            if pack.pack_id == "modern_city":
                assert duration > 0 and duration % 3_600 == 0
            elif site_id in {UUID(route["from_place_id"]), UUID(route["to_place_id"])}:
                assert duration == 3_600
            else:
                assert duration > 0 and duration % state.calendar.day_seconds == 0
    assert route_durations and 3_600 in route_durations
    production_unit = 3_600 if pack.pack_id == "modern_city" else state.calendar.day_seconds
    assert production_durations and all(duration > 0 and duration % production_unit == 0 for duration in production_durations)
    assert all(duration >= (6 * 3_600 if pack.pack_id == "modern_city" else 2 * state.calendar.day_seconds)
               for duration in production_durations)


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_resource_roles_have_finite_sources_and_owned_stocks(catalog: ContentCatalog, pack) -> None:
    state = catalog.build_world(pack.pack_id, "Resource Ledger", "Ira").state
    resource_roles = state.metadata["resource_roles"]
    resources = state.resources
    resource_ids = set(pack.resources)

    assert set(resource_roles["consumption_sink_account_ids"]) == resource_ids
    assert set(resource_roles["production_source_accounts"]) == resource_ids
    assert set(resource_roles["issuance_source_account_ids"]) == resource_ids
    for resource_id in resource_ids:
        sink_id = UUID(resource_roles["consumption_sink_account_ids"][resource_id])
        sink = resources[sink_id]
        assert sink.account_type == "consumption_sink"
        assert sink.owner_id is None and sink.balance == 0 and sink.credit_limit is None

        source_info = resource_roles["production_source_accounts"][resource_id]
        source_quota_total = Decimal("0")
        for producer_source in source_info["producer_accounts"]:
            source = resources[UUID(producer_source["account_id"])]
            assert source.account_type == "production_source"
            assert source.owner_id == UUID(producer_source["producer_id"])
            assert source.balance == 0
            assert source.credit_limit is not None
            assert str(source.credit_limit) == producer_source["credit_limit"]
            source_quota_total += source.credit_limit
        assert source_quota_total == Decimal(source_info["aggregate_credit_limit"])

        issuance = resources[UUID(resource_roles["issuance_source_account_ids"][resource_id])]
        assert issuance.account_type == "issuance_source"
        assert issuance.owner_id is None
        assert issuance.credit_limit is not None
        assert issuance.balance == -issuance.credit_limit
        assert sum(
            account.balance for account in resources.values() if account.resource_id == resource_id
        ) == 0

    for endowment in resource_roles["environmental_endowments"]:
        account = resources[UUID(endowment["account_id"])]
        assert account.owner_id == UUID(endowment["region_id"])
        assert account.resource_id == endowment["resource_id"]
        allocated = sum(Decimal(item["quantity"]) for item in endowment["allocations"])
        assert Decimal(endowment["quantity"]) == Decimal(endowment["remaining_quantity"]) + allocated
        assert account.balance == Decimal(endowment["remaining_quantity"])

    food_resource = resource_roles["food_resource_id"]
    water_resource = resource_roles["water_resource_id"]
    assert water_resource in resource_ids
    player = state.entities[state.player_id]
    food_lot = next(lot for lot in player.components["inventory"]["lots"] if lot["resource_id"] == food_resource)
    assert resources[UUID(food_lot["account_id"])].owner_id == player.id
    water_lot = next(lot for lot in player.components["inventory"]["lots"] if lot["resource_id"] == water_resource)
    water_account = resources[UUID(water_lot["account_id"])]
    assert water_account.owner_id == player.id
    assert water_account.resource_id == water_resource
    assert water_account.balance >= Decimal(water_lot["quantity"]) > 0
    assert water_resource in resource_roles["consumption_sink_account_ids"]
    assert resources[UUID(resource_roles["consumption_sink_account_ids"][water_resource])].account_type == "consumption_sink"
    water_endowments = [item for item in resource_roles["environmental_endowments"] if item["resource_id"] == water_resource]
    assert len(water_endowments) == 12
    assert all(Decimal(item["quantity"]) > 0 for item in water_endowments)

    market_count = 0
    for entity in state.entities.values():
        market = entity.components.get("market")
        if market:
            for offer in market["offers"]:
                seller = state.entities[UUID(offer["seller_id"])]
                stock_id = UUID(offer["stock_account_id"])
                stock_lot = next(
                    lot for lot in seller.components["inventory"]["lots"]
                    if UUID(lot["account_id"]) == stock_id
                )
                assert Decimal(stock_lot["quantity"]) >= Decimal(offer["minimum_quantity"])
                assert resources[stock_id].balance >= Decimal(stock_lot["quantity"])
        production = entity.components.get("production")
        if production:
            market_count += 1
            recipe = production["recipes"][0]
            input_resource = recipe["inputs"][0]["resource_id"]
            input_account = next(
                account for account in resources.values()
                if account.owner_id == entity.id and account.resource_id == input_resource
            )
            assert input_account.balance > 0
            for output in recipe["outputs"]:
                assert any(
                    account.account_type == "production_source"
                    and account.owner_id == entity.id
                    and account.resource_id == output["resource_id"]
                    and account.credit_limit is not None
                    and account.credit_limit > 0
                    for account in resources.values()
                )
    assert market_count == 108


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_all_seeded_places_are_reachable_and_references_resolve(catalog: ContentCatalog, pack) -> None:
    state = catalog.build_world(pack.pack_id, "Network Check", "Rin").state
    entities = state.entities
    place_ids = {entity_id for entity_id, entity in entities.items() if entity.kind == "place"}
    graph: dict[UUID, set[UUID]] = {place_id: set() for place_id in place_ids}

    for place_id in place_ids:
        for route in entities[place_id].components["place"]["routes"]:
            origin = UUID(route["from_place_id"])
            destination = UUID(route["to_place_id"])
            assert origin == place_id
            assert destination in place_ids
            graph[origin].add(destination)

    start = UUID(entities[state.player_id].location_id.__str__())
    reached = {start}
    pending = deque([start])
    while pending:
        for destination in graph[pending.popleft()]:
            if destination not in reached:
                reached.add(destination)
                pending.append(destination)
    assert reached == place_ids

    resource_map = state.resources
    for entity in entities.values():
        governance = entity.components.get("governance")
        if governance:
            member_ids = {UUID(value) for value in governance["members"]}
            assert member_ids <= set(entities)
            assert UUID(governance["leader_id"]) in member_ids
        treasury = entity.components.get("treasury")
        if treasury:
            for resource_id, account_id in treasury["accounts"].items():
                account = resource_map[UUID(account_id)]
                assert account.resource_id == resource_id
                assert account.owner_id == entity.id
        market = entity.components.get("market")
        if market:
            for offer in market["offers"]:
                account = resource_map[UUID(offer["stock_account_id"])]
                assert account.resource_id == offer["resource_id"]
                assert account.owner_id == UUID(offer["seller_id"])
                assert entities[UUID(offer["seller_id"])].kind == "character"
        for lot in entity.components.get("inventory", {}).get("lots", []):
            account = resource_map[UUID(lot["account_id"])]
            assert account.resource_id == lot["resource_id"]
            assert account.owner_id == entity.id

    for relation in state.relations:
        assert relation.from_id in entities
        assert relation.to_id in entities
    for record in state.knowledge:
        assert record.owner_id in entities or record.holder_id in entities
        assert record.subject_id is None or record.subject_id in entities
    for goal in state.goals:
        assert goal.owner_id in entities
        for condition in goal.conditions:
            assert condition.subject_id is None or condition.subject_id in entities
            assert condition.target_id is None or condition.target_id in entities


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_starting_scene_reveals_authored_site_and_unlocks_private_route(catalog: ContentCatalog, pack) -> None:
    draft = catalog.build_world(pack.pack_id, "Discovery Check", "Rin")
    state = draft.state
    player_id = state.player_id
    assert player_id is not None
    player = state.entities[player_id]
    start_id = player.location_id
    assert start_id is not None
    site = next(
        entity for entity in state.entities.values()
        if entity.kind == "place" and entity.components.get("place", {}).get("place_type") == "site"
    )
    route = next(
        route for route in state.entities[start_id].components["place"]["routes"]
        if UUID(route["to_place_id"]) == site.id
    )
    assert site.components["hidden"] is True
    assert site.location_id == start_id
    assert route["travel_ticks"] == 3_600
    typed_route = PlaceRoute.model_validate(route)
    assert not route_accessible(state, player, typed_route)
    assert str(site.id) not in project_player_state(state.model_dump(mode="json"), str(player_id))["entities"]

    witness_id = next(
        entity.id for entity in state.entities.values()
        if entity.kind == "character" and entity.id != player_id and entity.location_id == start_id
    )
    engine = RuleEngine()
    found = None
    for command_number in range(1, 300):
        context = RuleContext(
            world_id=draft.world_id,
            branch_id=draft.branch_id,
            command_id=UUID(int=command_number),
            actor_id=player_id,
            ruleset_version=draft.ruleset_version,
        )
        candidate = engine.evaluate(
            state,
            {"kind": "explore", "place_id": str(start_id), "focus": "history"},
            context,
        )
        if candidate.outcome.code == "discovered":
            found = (candidate, context)
            break
    assert found is not None
    exploration, context = found
    projected = state
    for event in exploration.events:
        projected = apply_event(projected, event)
    assert any(event.type == "core.entity_discovered.v1" for event in exploration.events)
    assert any(event.type == "core.knowledge_acquired.v1" for event in exploration.events)
    assert pack.discovery_site.description in exploration.outcome.details["claim"]
    assert route_accessible(projected, projected.entities[player_id], typed_route)
    assert not route_accessible(projected, projected.entities[witness_id], typed_route)

    for command_number in range(10_000, 10_300):
        move_context = context.model_copy(update={"command_id": UUID(int=command_number)})
        move = engine.evaluate(
            projected,
            {"kind": "move", "destination_id": str(site.id), "route_id": route["route_id"]},
            move_context,
        )
        if move.outcome.code == "arrived":
            break
    else:
        pytest.fail("The discovered route could not be traversed with a successful hazard check.")
    moved = projected
    for event in move.events:
        moved = apply_event(moved, event)
    assert moved.entities[player_id].location_id == site.id


def test_world_seed_is_deterministic_but_pack_specific(catalog: ContentCatalog) -> None:
    first = catalog.build_world("modern_city", "Repeatable", "Avery")
    second = catalog.build_world("modern_city", "Repeatable", "Avery")
    other = catalog.build_world("long_life_cultivation", "Repeatable", "Avery")

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.world_id == second.world_id
    assert first.world_id != other.world_id
    assert first.state.metadata["content_rules"] != other.state.metadata["content_rules"]


def test_explicit_generation_seed_changes_people_state_and_repeats_exactly(catalog: ContentCatalog) -> None:
    pack = BACKGROUNDS[1]
    legacy = catalog.build_world(pack.pack_id, "Seeded Start", "Avery")
    legacy_none = catalog.build_world(pack.pack_id, "Seeded Start", "Avery", seed=None)
    first = catalog.build_world(pack.pack_id, "Seeded Start", "Avery", seed=123_456)
    repeat = catalog.build_world(pack.pack_id, "Seeded Start", "Avery", seed=123_456)
    other_seed = catalog.build_world(pack.pack_id, "Seeded Start", "Avery", seed=789_012)

    assert legacy.model_dump(mode="json") == legacy_none.model_dump(mode="json")
    assert "generation_seed" not in legacy.state.metadata
    assert first.state.metadata["generation_seed"] == 123_456
    assert first.model_dump(mode="json") == repeat.model_dump(mode="json")
    assert first.world_id != other_seed.world_id
    assert other_seed.state.metadata["generation_seed"] == 789_012

    def human_profiles(state: WorldState) -> tuple[tuple[object, ...], ...]:
        year_seconds = state.calendar.year_days * state.calendar.day_seconds
        return tuple(sorted(
            (
                entity.name,
                -entity.components["life"]["birth_tick"] // year_seconds,
                tuple(
                    entity.components["needs"][need]["value"]
                    for need in ("food", "water", "rest", "shelter", "safety", "health", "social", "belonging")
                ),
                entity.components["public_role"],
                state.entities[entity.location_id].name if entity.location_id else None,
            )
            for entity in state.entities.values()
            if entity.kind == "character"
        ))

    assert human_profiles(first.state) != human_profiles(other_seed.state)

    def regional_named_counts(state: WorldState) -> dict[str, int]:
        counts: dict[UUID, int] = {}
        for entity in state.entities.values():
            if entity.kind == "character" and entity.status not in {"dead", "retired"}:
                group_id = UUID(entity.components["life"]["population_group_id"])
                counts[group_id] = counts.get(group_id, 0) + 1
        return {
            group.name or str(group.id): counts.get(group.id, 0)
            for group in state.entities.values() if group.kind == "group"
        }

    assert regional_named_counts(first.state) == regional_named_counts(other_seed.state)

    expected_key_roles = {
        f"{given} {family}": profile.role
        for (given, family), profile in zip(pack.key_names, pack.key_people, strict=True)
    }
    key_npc_ids = [UUID(value) for value in first.state.metadata["content_index"]["key_npc_ids"]]
    assert {
        first.state.entities[person_id].name: first.state.entities[person_id].components["public_role"]
        for person_id in key_npc_ids
    } == expected_key_roles


@pytest.mark.parametrize("seed", [True, 1.5, 2**63, -(2**63) - 1])
def test_catalog_rejects_invalid_explicit_generation_seed(catalog: ContentCatalog, seed) -> None:
    with pytest.raises(ValueError, match="signed 64-bit integer"):
        catalog.build_world("modern_city", "Seed Validation", "Avery", seed=seed)


def test_catalog_rejects_unknown_population_mode(catalog: ContentCatalog) -> None:
    with pytest.raises(ValueError, match="population_mode"):
        catalog.build_world("modern_city", "Mode Validation", "Avery", population_mode="immortal")


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda pack: pack.pack_id)
def test_population_mode_seeds_only_finite_sustained_npc_seats(catalog: ContentCatalog, pack) -> None:
    natural = catalog.build_world(
        pack.pack_id, "Demographic Regimes", "Avery", seed=42, population_mode="natural",
    )
    sustained = catalog.build_world(
        pack.pack_id, "Demographic Regimes", "Avery", seed=42, population_mode="sustained",
    )
    assert natural.world_id != sustained.world_id
    assert natural.state.metadata["population_mode"] == "natural"
    assert sustained.state.metadata["population_mode"] == "sustained"
    assert natural.pack_version == sustained.pack_version == "2.0.0"
    content_index = sustained.state.metadata["content_index"]
    assert content_index["other_resident_population_groups"] == {
        person_id: sustained.state.entities[UUID(person_id)].components["life"]["population_group_id"]
        for person_id in content_index["other_resident_ids"]
    }
    assert "other_resident_population_groups" not in natural.state.metadata["content_index"]
    governed = [entity for entity in sustained.state.entities.values() if "governance" in entity.components]
    assert len(governed) == 48
    for entity in governed:
        governance = GovernanceComponent.model_validate(entity.components["governance"])
        policy, = governance.membership_recruitment
        assert policy.region_id == entity.location_id
        assert policy.authority_tag in governance.authority_tags
        assert policy.charter_clause_id in governance.charter_clause_ids
        assert policy.active_member_capacity == len(governance.members) + 2
        assert policy.max_recruits_per_period == 2
        assert set(policy.eligible_roles) <= set(pack.extra_roles)
    assert all(
        not GovernanceComponent.model_validate(entity.components["governance"]).membership_recruitment
        for entity in natural.state.entities.values() if "governance" in entity.components
    )
    assert natural.state.metadata["generation_seed"] == sustained.state.metadata["generation_seed"] == 42

    def human_profiles(state: WorldState) -> dict[str, tuple[object, ...]]:
        year_seconds = state.calendar.year_days * state.calendar.day_seconds
        return {
            entity.name: (
                -entity.components["life"]["birth_tick"] // year_seconds,
                tuple(
                    entity.components["needs"][need]["value"]
                    for need in ("food", "water", "rest", "shelter", "safety", "health", "social", "belonging")
                ),
                entity.components["personality"],
                entity.components["public_role"],
                state.entities[entity.location_id].name if entity.location_id else None,
            )
            for entity in state.entities.values() if entity.kind == "character"
        }

    assert human_profiles(natural.state) == human_profiles(sustained.state)
    hidden_sites = {
        entity.id for entity in sustained.state.entities.values()
        if entity.kind == "place" and entity.components.get("hidden") is True
    }
    natural_groups = {
        entity.id: entity.components["population"]
        for entity in natural.state.entities.values() if entity.kind == "group"
    }
    sustained_groups = {
        entity.id: entity.components["population"]
        for entity in sustained.state.entities.values() if entity.kind == "group"
    }
    assert len(natural_groups) == len(sustained_groups) == 12
    for group in sustained.state.entities.values():
        if group.kind != "group":
            continue
        population = group.components["population"]
        policy = population["seat_policy"]
        assert natural_groups[UUID(natural.state.metadata["content_index"]["population_group_ids"][
            sustained.state.metadata["content_index"]["population_group_ids"].index(str(group.id))
        ])].get("seat_policy") is None
        active_non_player = sum(
            1 for entity in sustained.state.entities.values()
            if entity.kind == "character" and entity.id != sustained.state.player_id
            and entity.status not in {"dead", "retired"}
            and UUID(entity.components["life"]["population_group_id"]) == group.id
        )
        assert policy["non_player_seat_capacity"] == active_non_player
        assert policy["promotion_serial"] == 0
        assert policy["eligible_min_age_years"] == 18
        assert policy["eligible_max_age_years"] == 64
        assert policy["lifespan_min_years"] == policy["lifespan_max_years"] == (
            100 if pack.pack_id == "long_life_cultivation" else 82
        )
        home_places = [sustained.state.entities[UUID(value)] for value in policy["home_place_ids"]]
        assert hidden_sites.isdisjoint({place.id for place in home_places})
        assert all(
            place.components["place"]["region_id"] == str(group.location_id)
            for place in home_places
        )
        assert policy["extra_given_names"] == list(pack.extra_given_names)
        assert policy["extra_family_names"] == list(pack.extra_family_names)
        assert policy["extra_roles"] == list(pack.extra_roles)


def test_player_projection_keeps_private_beliefs_and_author_index_private(catalog: ContentCatalog) -> None:
    pack = BACKGROUNDS[0]
    world = catalog.build_world(pack.pack_id, "Private View", "Avery")
    source = world.state.model_dump(mode="json")
    projected = project_player_state(source, str(world.state.player_id))
    rendered = json.dumps(projected, ensure_ascii=False)

    assert "content_index" not in projected["metadata"]
    assert "content_rules" not in projected["metadata"]
    assert all(profile.secret not in rendered for profile in pack.key_people)
    assert "knowledge" in projected
    assert all(item.get("owner_id") is None for item in projected["knowledge"])


def test_catalog_rejects_unknown_pack_and_blank_input(catalog: ContentCatalog) -> None:
    with pytest.raises(ValueError, match="Unknown background pack"):
        catalog.build_world("not_a_pack", "World", "Player")
    with pytest.raises(ValueError, match="title cannot be blank"):
        catalog.build_world("modern_city", "  ", "Player")
    with pytest.raises(ValueError, match="player_name cannot be blank"):
        catalog.build_world("modern_city", "World", "  ")


def test_catalog_draft_persists_through_world_application(catalog: ContentCatalog) -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    repository = WorldRepository(database)
    application = WorldApplication(repository, catalog)
    try:
        created = application.create_world(
            scope,
            "low_magic_feudal",
            "Persisted Feudal World",
            "Mira",
            "content-catalog-test",
        )
        view = repository.read_world(scope, UUID(str(created["world_id"])))
        assert view.state.metadata["pack_id"] == "low_magic_feudal"
        assert view.world.pack_version == "2.0.0"
        assert len(view.state.entities) == 457
        assert len(view.state.resources) >= 700
        assert view.state.player_id is not None
    finally:
        database.dispose()
