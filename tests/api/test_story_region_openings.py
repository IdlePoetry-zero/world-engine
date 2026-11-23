from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi.testclient import TestClient

from world_engine.application.quoted_speech_guard import visible_dialogue_targets
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.components import PlaceComponent, PlaceRoute
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import JobStatus, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.story_nodes import (
    STORY_NODE_HANDLER, STORY_NODE_RESOLVED, STORY_NODE_VERSION,
    StoryMilestoneResolvedEvent, StoryNode, settle_story_milestone,
)
from world_engine.runtime import build_application, local_identity


def _id(name):
    return uuid5(NAMESPACE_URL, f"world-engine:test:story-region-opening:{name}")


def _scenario(*, opens_venue=True):
    player, pier, venue, keeper = map(_id, ("player", "pier", "venue", "keeper"))
    route = PlaceRoute(route_id=_id("route"), from_place_id=pier, to_place_id=venue, travel_ticks=60)
    entities = [
        WorldEntity(id=player, kind="character", name="Traveler", location_id=pier,
                    components={"capabilities": {"grants": ["world.travel"]}}),
        WorldEntity(id=pier, kind="place", name="North Pier",
                    components={"place": PlaceComponent(routes=[route])}),
        WorldEntity(id=venue, kind="place", name="Tide Observatory", status="closed",
                    components={"hidden": True, "place": PlaceComponent(place_type="building")}),
    ]
    node = {
        "title": "Observatory reopening", "visibility": "public",
        "fallback": {"key": "reopened", "effect": "occurred", "summary": "The Tide Observatory opens to travelers."},
    }
    if opens_venue:
        node["opens_place_ids"] = [str(venue)]
    return {
        "player_id": str(player),
        "entities": {str(entity.id): entity.model_dump(mode="json") for entity in entities},
        "scheduled_events": [{
            "id": str(_id("reopening")), "due_tick": 60, "phase": "observation",
            "handler_id": STORY_NODE_HANDLER, "handler_version": STORY_NODE_VERSION, "payload": node,
        }],
        "story_arrivals": [{
            "trigger_id": str(_id("reopening")), "outcome": "reopened",
            "actor": {"id": str(keeper), "kind": "character", "name": "Tide Keeper", "location_id": str(venue)},
            "knowledge": [{
                "id": str(_id("tide-log")), "holder_id": str(keeper), "claim": "The oldest tide log is in the blue drawer.",
                "provenance": [{"kind": "pack", "reference_id": "observatory-opening"}],
                "visibility": {"policy": "private", "subject_ids": [str(keeper)]},
            }],
        }],
    }


def test_scheduled_opening_admits_the_keeper_and_makes_the_venue_reachable(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    jobs = SimulationJobs(repository)
    player, venue, keeper, route = map(_id, ("player", "venue", "keeper", "route"))
    origin = "http://127.0.0.1:8000"
    source = write_world_pack(
        manifest=WorldPackManifest(pack_id="observatory-opening", version="1.0.0", title="North Coast"),
        template={"story_openings": [{
            "opening_id": "before", "title": "At the pier", "position": "Before the tide turns",
            "known_history": [], "scenario": _scenario(),
        }]},
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        upload = {"Content-Type": "application/zip", "Origin": origin}
        preview = client.post("/api/v1/packs/import/preview", headers=upload, content=source).raise_for_status().json()
        assert preview["can_import"], preview
        created = client.post("/api/v1/packs/import", content=source, headers=upload | {
            "Idempotency-Key": "observatory-opening", "X-World-Pack-Sha256": preview["archive_sha256"],
            "X-Story-Opening": "before",
        }).raise_for_status().json()
        world_id, branch = map(UUID, (created["world_id"], created["active_branch_id"]))
        path = f"/api/v1/worlds/{world_id}"

        def view(branch_id=branch):
            return client.get(f"{path}/view", params={"branch_id": str(branch_id)}).raise_for_status().json()

        def enqueue(endpoint, key, seconds, **params):
            return client.post(f"{path}/{endpoint}", headers={"Idempotency-Key": key}, json={
                "branch_id": str(branch), "expected_state_version": view()["branch"]["state_version"],
                "duration_seconds": seconds, **params,
            })

        def run(response):
            job_id = UUID(response.raise_for_status().json()["job_id"])
            lease = repository.claim_job("observatory-opening-worker")
            assert lease and lease.job_id == job_id
            for _ in range(12):
                jobs.advance(scope, lease)
                job = repository.read_job(scope, job_id)
                if job.status == JobStatus.SUCCEEDED:
                    return
                assert job.status in {JobStatus.QUEUED, JobStatus.RUNNING}, job
            pytest.fail(f"Simulation job {job_id} did not finish")

        run(enqueue("world-advance", "stop-before-reopening", 59))
        before = repository.read_world(scope, world_id, branch)
        assert before.state.current_tick == 59
        assert before.state.entities[venue].status == "closed"
        assert before.state.entities[venue].components["hidden"] is True
        assert keeper not in before.state.entities and not before.state.knowledge
        player_before = view()["state"]
        assert str(venue) not in player_before["entities"]
        assert str(keeper) not in player_before["entities"]
        assert not player_before["travel_options"] and not player_before["itinerary_route_options"]
        assert "story_milestones" not in player_before
        assert "Tide Observatory" not in str(player_before)
        assert str(keeper) not in visible_dialogue_targets(before.state, player)
        denied = enqueue("long-actions", "closed-route", 60, activity="travel", route_ids=[str(route)])
        assert denied.status_code == 422, denied.text
        assert repository.read_world(scope, world_id, branch).state_hash == before.state_hash

        fork = UUID(client.post(f"{path}/branches", headers={"Idempotency-Key": "before-reopening"}, json={
            "branch_id": str(branch), "sequence": str(before.last_seq), "label": "Stay before reopening",
        }).raise_for_status().json()["branch_id"])

        run(enqueue("world-advance", "reopen-observatory", 1))
        opened = repository.read_world(scope, world_id, branch)
        milestone, = opened.state.story_milestones
        assert milestone.resolved_tick == opened.state.current_tick == 60
        assert milestone.opened_place_ids == (venue,)
        assert milestone.arrival_actor_ids == (keeper,)
        assert opened.state.entities[venue].status == "active"
        assert not opened.state.entities[venue].components.get("hidden")
        assert opened.state.entities[keeper].location_id == venue
        receipt, = [record for record in repository.list_events(scope, world_id, branch).events
                    if record.type == STORY_NODE_RESOLVED]
        assert receipt.payload["milestone"]["opened_place_ids"] == [str(venue)]
        assert receipt.payload["milestone"]["arrival_actor_ids"] == [str(keeper)]
        note, = opened.state.knowledge
        assert (note.holder_id, note.received_tick, note.source_event_id) == (keeper, 60, receipt.event_id)

        player_opened = view()["state"]
        available, = player_opened["travel_options"]
        assert available == {"route_id": str(route), "destination_name": "Tide Observatory", "travel_ticks": "60"}
        public_milestone, = player_opened["story_milestones"]
        assert public_milestone["resolved_tick"] == "60"
        assert public_milestone["summary"] == "The Tide Observatory opens to travelers."
        assert str(keeper) not in player_opened["entities"] and not player_opened["knowledge"]
        assert str(keeper) not in visible_dialogue_targets(opened.state, player)
        assert view(fork)["state"]["travel_options"] == []
        assert repository.replay(scope, world_id, fork, from_genesis=True).state_hash == before.state_hash

        run(enqueue("long-actions", "visit-observatory", 60, activity="travel", route_id=available["route_id"]))
        arrived = repository.read_world(scope, world_id, branch)
        assert arrived.state.current_tick == 120
        assert arrived.state.entities[player].location_id == venue
        arrived_player = view()["state"]
        assert arrived_player["entities"][str(keeper)]["name"] == "Tide Keeper"
        assert not arrived_player["knowledge"]
        assert str(keeper) in visible_dialogue_targets(arrived.state, player)
        assert repository.replay(scope, world_id, branch, from_genesis=True).state_hash == arrived.state_hash


@pytest.mark.parametrize("opens_venue", [True, False])
def test_opening_receipt_must_match_registered_story_node(opens_venue):
    state = WorldState.model_validate(_scenario(opens_venue=opens_venue) | {"current_tick": 60})
    schedule, = state.scheduled_events
    event = settle_story_milestone(state, schedule, _id("reopening-receipt"))
    event_payload = event.model_dump(mode="json")
    venue, keeper = map(_id, ("venue", "keeper"))

    if opens_venue:
        assert event.milestone.opened_place_ids == (venue,)
        del event_payload["milestone"]["opened_place_ids"]
    else:
        assert "opens_place_ids" not in StoryNode.model_validate(schedule.payload).model_dump(mode="json")
        assert "opened_place_ids" not in event_payload["milestone"]
        event_payload["milestone"]["opened_place_ids"] = [str(venue)]

    forged = StoryMilestoneResolvedEvent.model_validate(event_payload)
    with pytest.raises(InvalidEvent, match="world-state conditions"):
        apply_event(state, forged)
    assert state.entities[venue].status == "closed" and keeper not in state.entities
    resolved = apply_event(state, event)
    assert resolved.entities[venue].status == ("active" if opens_venue else "closed")
    assert (keeper in resolved.entities) == opens_venue
