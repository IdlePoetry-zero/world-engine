from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS
from world_engine.domain.components import PlaceComponent, WaypointOpportunity
from world_engine.domain.models import AccessScope, CommandEnvelope, JobStatus, NewWorld, TimeAdvancedEvent
from world_engine.domain.travel_itinerary import plan_travel_itinerary
from world_engine.simulation import LongActionPlan
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _opportunity_rows(state):
    rows: list[tuple[str, WaypointOpportunity]] = []
    for place in state.entities.values():
        if place.kind not in {"place", "location"}:
            continue
        raw = place.components.get("place")
        if raw is None:
            continue
        component = PlaceComponent.model_validate(raw)
        rows.extend((str(place.id), opportunity) for opportunity in component.waypoint_opportunities)
    return sorted(rows, key=lambda row: (row[0], str(row[1].opportunity_id)))


def _stable_opportunity_values(state):
    return [
        (
            place_id,
            str(opportunity.opportunity_id),
            opportunity.title,
            opportunity.summary,
            opportunity.opens_tick,
            opportunity.closes_tick,
            opportunity.minimum_itinerary_elapsed_ticks,
        )
        for place_id, opportunity in _opportunity_rows(state)
    ]


@pytest.fixture(scope="module")
def catalog() -> ContentCatalog:
    return ContentCatalog()


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda item: item.pack_id)
@pytest.mark.parametrize("population_mode", ("natural", "sustained"))
def test_waypoint_opening_pack_preserves_capacity_and_authors_stable_public_openings(
    catalog, pack, population_mode,
):
    title = "Waypoint opening acceptance"
    player = "Mara Player"
    seed = 20261120
    first = catalog.build_world(
        pack.pack_id, title, player, seed=seed,
        population_mode=population_mode, pack_version="2.8.0",
    )
    second = catalog.build_world(
        pack.pack_id, title, player, seed=seed,
        population_mode=population_mode, pack_version="2.8.0",
    )
    state = first.state
    index = state.metadata["content_index"]
    ordinary_count = 240 if population_mode == "natural" else 264

    assert first.pack_version == state.metadata["pack_version"] == "2.8.0"
    assert state.metadata["population_mode"] == population_mode
    assert state.metadata["generation_seed"] == seed
    assert len(index["country_ids"]) == state.metadata["seed_counts"]["countries"] == 24
    assert len(index["organization_ids"]) == state.metadata["seed_counts"]["organizations"] == 24
    assert len(index["key_npc_ids"]) == state.metadata["seed_counts"]["key_npcs"] == 32
    assert len(index["other_resident_ids"]) == state.metadata["seed_counts"]["other_named_living_characters"] == ordinary_count
    assert state.metadata["seed_counts"]["places"] == 120
    assert sum(entity.kind == "polity" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "organization" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "place" for entity in state.entities.values()) == 120

    rows = _opportunity_rows(state)
    assert 3 <= len(rows) <= 4
    assert len({opportunity.opportunity_id for _, opportunity in rows}) == len(rows)
    assert all(opportunity.title and opportunity.summary for _, opportunity in rows)
    assert _stable_opportunity_values(state) == _stable_opportunity_values(second.state)
    notices = []
    for place_id, opportunity in rows:
        notice_id = uuid5(opportunity.opportunity_id, "notice")
        notice = state.entities[notice_id]
        matching_notice = second.state.entities[notice_id]
        assert notice.kind == matching_notice.kind == "item"
        assert notice.location_id == matching_notice.location_id == UUID(place_id)
        assert notice.name == matching_notice.name == f"告示｜{opportunity.title}"
        assert notice.components["public_description"]
        assert notice.components["public_description"] == matching_notice.components["public_description"]
        notices.append(notice)

    day_seconds = state.calendar.day_seconds
    for _, opportunity in rows:
        if opportunity.closes_tick is not None:
            assert opportunity.opens_tick == 0
            assert opportunity.closes_tick in {7 * day_seconds, 14 * day_seconds}
            assert opportunity.minimum_itinerary_elapsed_ticks == 0
        elif opportunity.minimum_itinerary_elapsed_ticks:
            assert opportunity.opens_tick is None and opportunity.closes_tick is None
            assert opportunity.minimum_itinerary_elapsed_ticks == 5 * day_seconds
        else:
            assert opportunity.opens_tick is None and opportunity.closes_tick is None

    # The player's ordinary outward route reaches region zero, where every
    # background has its first authored waypoint scene.
    region_zero_id = UUID(index["region_ids"][0])
    player_entity = state.entities[state.player_id]
    origin_id = player_entity.location_id
    assert origin_id is not None
    origin_routes = PlaceComponent.model_validate(state.entities[origin_id].components["place"]).routes
    opening_route = next(route for route in origin_routes if route.to_place_id == region_zero_id)
    assert opening_route.from_place_id == origin_id
    assert opening_route.is_open is True
    assert opening_route.access_tags == []
    assert any(place_id == str(region_zero_id) for place_id, _ in rows)

    sites = [
        entity for entity in state.entities.values()
        if entity.kind == "place" and entity.components.get("place", {}).get("place_type") == "site"
    ]
    assert len(sites) == 1 and sites[0].components.get("hidden") is True
    assert all(place_id != str(sites[0].id) for place_id, _ in rows)
    assert all(notice.location_id != sites[0].id for notice in notices)


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda item: item.pack_id)
def test_legacy_23_worlds_keep_waypoint_fields_absent(catalog, pack):
    state = catalog.build_world(
        pack.pack_id, "Legacy waypoint baseline", "Mara Player", seed=20261120,
        pack_version="2.3.0",
    ).state
    assert state.metadata["pack_version"] == "2.3.0"
    assert _opportunity_rows(state) == []
    assert all(
        "waypoint_opportunities" not in entity.components.get("place", {})
        for entity in state.entities.values()
        if entity.kind == "place"
    )


def _route_to(state, origin_id: UUID, destination_id: UUID):
    origin = state.entities[origin_id]
    return next(
        route for route in PlaceComponent.model_validate(origin.components["place"]).routes
        if route.from_place_id == origin_id and route.to_place_id == destination_id
    )


def test_leased_modern_itinerary_skips_an_expired_intermediate_opening(tmp_path, catalog):
    draft = catalog.build_world(
        "modern_city", "Expired waypoint opening", "Mara Player", seed=20261120,
        population_mode="natural", pack_version="2.8.0",
    )
    state = draft.state
    index = state.metadata["content_index"]
    actor_id = state.player_id
    origin_id = state.entities[actor_id].location_id
    region_zero_id = UUID(index["region_ids"][0])
    # Modern's finite-window market scene is settlement index two in region zero.
    finite_waypoint_id = UUID(index["settlement_ids"][2])
    route_ids = [
        _route_to(state, origin_id, region_zero_id).route_id,
        _route_to(state, region_zero_id, finite_waypoint_id).route_id,
        _route_to(state, finite_waypoint_id, region_zero_id).route_id,
    ]
    opportunities = PlaceComponent.model_validate(
        state.entities[finite_waypoint_id].components["place"],
    ).waypoint_opportunities
    finite_opening, = opportunities
    assert finite_opening.closes_tick == 7 * state.calendar.day_seconds

    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'waypoint-expiry.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    client = None
    try:
        created = repository.create_world(scope, draft)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        day_seconds = state.calendar.day_seconds
        advanced = repository.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=actor_id, idempotency_key="open-after-opening-window",
                expected_state_version=created.branch.state_version, command_type="fixture.clock",
            ),
            [TimeAdvancedEvent(to_tick=8 * day_seconds)],
        )
        checkpoint = repository.read_world(scope, world_id, branch_id)
        assert checkpoint.state.current_tick == 8 * day_seconds

        deadline = checkpoint.state.current_tick + sum(
            _route_to(checkpoint.state, source, destination).travel_ticks
            for source, destination in (
                (origin_id, region_zero_id),
                (region_zero_id, finite_waypoint_id),
                (finite_waypoint_id, region_zero_id),
            )
        ) + 1
        itinerary = plan_travel_itinerary(
            checkpoint.state,
            itinerary_id=UUID(int=0x280),
            actor_id=actor_id,
            route_ids=route_ids,
            deadline_tick=deadline,
        )
        jobs = SimulationJobs(repository)
        plan = LongActionPlan(
            actor_id=actor_id,
            started_tick=checkpoint.state.current_tick,
            target_tick=deadline,
            activity="travel",
            travel_itinerary=itinerary,
        )
        job = jobs.enqueue(
            scope, world_id, branch_id, plan, idempotency_key="expired-opening-itinerary",
            speed="balanced", depth="standard", expected_state_version=checkpoint.branch.state_version,
        )
        lease = repository.claim_job("expired-opening-worker:first", job_type=job.job_type)
        assert lease is not None and lease.job_id == job.job_id

        # Region zero has a non-expiring authored scene, so the live worker
        # pauses only after actually committing its first arrival.
        for _ in range(40):
            jobs.advance(scope, lease)
            saved = repository.read_job(scope, job.job_id)
            if saved.status != JobStatus.RUNNING:
                break
        assert saved.status is JobStatus.WAITING_INPUT
        arrived = repository.read_world(scope, world_id, branch_id)
        assert arrived.state.current_tick == checkpoint.state.current_tick + _route_to(
            arrived.state, origin_id, region_zero_id,
        ).travel_ticks
        assert arrived.state.entities[actor_id].location_id == region_zero_id
        assert saved.payload["itinerary_cursor"]["leg_index"] == 0
        assert len(arrived.state.waypoint_opportunity_decisions) == 0

        session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        client = TestClient(create_app(WorldApplication(repository, catalog), session))
        client.get("/api/v1/session").raise_for_status()
        decision_url = f"/api/v1/jobs/{job.job_id}/decision"
        decisions = client.get(decision_url).raise_for_status().json()["items"]
        assert len(decisions) == 1
        submitted = client.post(decision_url, json={
            "request_id": decisions[0]["request_id"], "option_id": "continue",
        })
        submitted.raise_for_status()
        continuation = repository.claim_job("expired-opening-worker:continue", job_type=job.job_type)
        assert continuation is not None and continuation.job_id == job.job_id

        for _ in range(40):
            jobs.advance(scope, continuation)
            saved = repository.read_job(scope, job.job_id)
            if saved.status != JobStatus.RUNNING:
                break
        assert saved.status is JobStatus.SUCCEEDED
        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == checkpoint.state.current_tick + sum(
            _route_to(final.state, source, destination).travel_ticks
            for source, destination in (
                (origin_id, region_zero_id),
                (region_zero_id, finite_waypoint_id),
                (finite_waypoint_id, region_zero_id),
            )
        )
        assert final.state.entities[actor_id].location_id == region_zero_id
        assert [item.status for item in final.state.travel_journeys] == ["arrived", "arrived", "arrived"]
        assert saved.payload["itinerary_cursor"]["leg_index"] == 2
        assert len(final.state.waypoint_opportunity_decisions) == 1
        recorded_place = final.state.entities[region_zero_id]
        assert any(
            opportunity.opportunity_id == final.state.waypoint_opportunity_decisions[0].opportunity_id
            for opportunity in PlaceComponent.model_validate(recorded_place.components["place"]).waypoint_opportunities
        )
        assert final.state.current_tick >= finite_opening.closes_tick
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
    finally:
        if client is not None:
            client.close()
        database.dispose()
