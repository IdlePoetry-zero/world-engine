"""HTTP boundaries exercised against real temporary SQLite storage."""

import json
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import AuthenticatedIdentity, LocalSession
from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.application.worlds import WorldApplication
from world_engine.domain.models import AccessScope, CommandEnvelope, EntityFieldChangedEvent, MembershipRole, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.models import NewJob
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class FixtureCatalog:
    """Explicit test fixture, not a shipped content pack."""

    def list_packs(self):
        return [{"pack_id": "fixture", "title": "Fixture", "version": "1"}]

    def build_world(self, pack_id, title, player_name, *, seed=None):
        player, npc, place = uuid4(), uuid4(), uuid4()
        return NewWorld(title=title, state=WorldState(
            player_id=player,
            entities={
                place: WorldEntity(id=place, kind="place", name="Market"),
                player: WorldEntity(id=player, kind="character", name=player_name, location_id=place),
                npc: WorldEntity(id=npc, kind="character", name="Trader", location_id=place,
                                 components={"personality": {"values": ["hidden allegiance"]}, "public_role": "merchant"}),
            },
            metadata={"pack_id": pack_id, "author_secret": "future betrayal"}
            | ({"generation_seed": seed} if seed is not None else {}),
        ))


@pytest.fixture
def service(tmp_path):
    url = f"sqlite:///{(tmp_path / 'worlds.db').as_posix()}"
    database = Database(url)
    database.create_schema()
    application = WorldApplication(WorldRepository(database), FixtureCatalog())
    session = LocalSession(str(uuid4()), str(uuid4()), frozenset({"http://testserver"}))
    with TestClient(create_app(application, session)) as client:
        client.get("/api/v1/session")
        yield client, application, session, url
    database.dispose()


def create(client, key="creation-1", title="Integration"):
    return client.post("/api/v1/worlds", headers={"Idempotency-Key": key}, json={
        "pack_id": "fixture", "title": title, "player_name": "Traveler",
    })


def test_world_pack_preview_import_and_author_download(service):
    client, application, _, _ = service
    scenario = application.catalog.build_world("fixture", "Portable", "Traveler").state
    manifest = WorldPackManifest(pack_id="portable-market", version="1.0.0", title="Portable Market")
    source = write_world_pack(manifest=manifest, template={
        "scenario": scenario.model_dump(mode="json"), "future_theme": {"color": "teal"},
    })
    upload = {"Content-Type": "application/zip", "Origin": "http://testserver"}
    preview = client.post("/api/v1/packs/import/preview", content=source, headers=upload).raise_for_status().json()
    assert preview["can_import"] and preview["contains_author_material"]
    assert preview["compatibility"]["unsupported_fields"] == [{"section": "template", "field": "future_theme"}]
    assert client.get("/api/v1/worlds").json()["items"] == []
    confirm = upload | {"X-World-Pack-Sha256": preview["archive_sha256"], "Idempotency-Key": "pack-1"}
    different = write_world_pack(manifest=manifest.model_copy(update={"title": "Another"}), template={
        "scenario": scenario.model_dump(mode="json"),
    })
    mismatch = client.post("/api/v1/packs/import", content=different, headers=confirm)
    assert mismatch.status_code == 409
    assert client.get("/api/v1/worlds").json()["items"] == []

    world = client.post("/api/v1/packs/import", content=source, headers=confirm).raise_for_status().json()
    assert not world["duplicate"] and world["title"] == "Portable Market"
    path = f"/api/v1/worlds/{world['world_id']}"
    player = client.get(f"{path}/view").raise_for_status()
    assert "future betrayal" not in player.text and "future_theme" not in player.text
    author = client.get(f"{path}/view", params={"mode": "author"}).raise_for_status()
    assert "future betrayal" in author.text
    download = client.get(f"{path}/source-world-pack").raise_for_status()
    assert download.headers["content-disposition"].startswith('attachment; filename="author-world-pack-')
    assert download.content == source
    history = {"branch_id": world["active_branch_id"]}
    assert client.get(f"{path}/history", params=history).raise_for_status().json()["has_source_world_pack"]
    assert "has_source_world_pack" not in client.get(
        f"{path}/history", params=history | {"mode": "player"},
    ).raise_for_status().json()
    replay = client.post("/api/v1/packs/import", content=source, headers=confirm).raise_for_status().json()
    assert replay["duplicate"] and replay["world_id"] == world["world_id"]

    incompatible = write_world_pack(manifest=manifest, template={
        "scenario": scenario.model_dump(mode="json"), "ruleset_version": "2.0.0",
    })
    report = client.post("/api/v1/packs/import/preview", content=incompatible, headers=upload).raise_for_status().json()
    assert not report["can_import"] and "2.0.0" in report["blockers"][0]
    blocked = client.post("/api/v1/packs/import", content=incompatible, headers=confirm | {
        "X-World-Pack-Sha256": report["archive_sha256"], "Idempotency-Key": "pack-incompatible",
    })
    assert blocked.status_code == 422
    assert len(client.get("/api/v1/worlds").json()["items"]) == 1


def test_story_world_pack_previews_history_and_starts_a_selected_genesis(service):
    client, application, _, _ = service
    baseline = application.catalog.build_world("fixture", "Story", "Traveler").state
    opening = {
        "opening_id": "before", "title": "Before the rain", "position": "Before the market opens",
        "known_history": [], "scenario": baseline.model_dump(mode="json"),
    }
    later = opening | {
        "opening_id": "after", "title": "After the rain", "position": "After the flood",
        "known_history": ["The river rose."], "scenario": opening["scenario"] | {"current_tick": 500},
    }
    source = write_world_pack(manifest=WorldPackManifest(pack_id="portable-story", version="1.0.0", title="Story"),
                              template={"story_openings": [opening, later]})
    upload = {"Content-Type": "application/zip", "Origin": "http://testserver"}
    response = client.post("/api/v1/packs/import/preview", headers=upload, content=source).raise_for_status()
    report = response.json()
    assert report["can_import"]
    actor = baseline.entities[baseline.player_id]
    place = baseline.entities[actor.location_id]
    assert report["story_openings"] == [
        {key: value for key, value in item.items() if key != "scenario"}
        | {"opening_state": {"player_name": "Traveler", "place_name": place.name}}
        for item in (opening, later)
    ]
    assert "future betrayal" not in response.text
    headers = upload | {"X-World-Pack-Sha256": report["archive_sha256"], "Idempotency-Key": "story-1"}
    no_selection = client.post("/api/v1/packs/import", headers=headers, content=source)
    assert no_selection.json()["error"]["code"] == "STORY_OPENING_REQUIRED"
    assert not client.get("/api/v1/worlds").json()["items"]

    def confirm(key, opening_id):
        return client.post("/api/v1/packs/import", content=source, headers=headers | {
            "Idempotency-Key": key, "X-Story-Opening": opening_id,
        })

    assert confirm("story-1", "unknown").status_code == 422
    first = confirm("story-1", "after").raise_for_status().json()
    assert first["current_tick"] == "500"
    assert confirm("story-1", "after").raise_for_status().json()["duplicate"]
    assert confirm("story-1", "before").status_code == 409
    second = confirm("story-2", "before").raise_for_status().json()
    assert second["world_id"] != first["world_id"] and second["current_tick"] == "0"
    author = client.get(f"/api/v1/worlds/{first['world_id']}/view", params={"mode": "author"}).raise_for_status().json()
    assert author["state"]["metadata"]["world_pack_story_opening_id"] == "after"


def test_story_player_setup_is_opt_in_and_only_changes_the_selected_players_food_need(service):
    client, application, _, _ = service
    story = application.catalog.build_world("fixture", "Story", "Traveler").state
    scenario = story.model_dump(mode="json")
    player_id = str(story.player_id)
    scenario["entities"][player_id]["components"] = {
        "needs": {"food": {"value": 15}, "water": {"value": 90}}, "capabilities": {"grants": ["world.travel"]},
    }
    opening = {"opening_id": "before", "title": "Before", "position": "Market day",
               "known_history": [], "scenario": scenario}
    profile = {"profile_id": "untouched-by-hunger", "title": "River Spirit",
               "description": "Does not need ordinary food.", "food_requirement": "not_required"}
    source = write_world_pack(manifest=WorldPackManifest(pack_id="test-story", version="1.0.0", title="Story"),
                              template={"story_openings": [
                                  opening | {"player_profiles": [profile]},
                                  opening | {"opening_id": "fixed"},
                              ]})
    base_headers = {"Content-Type": "application/zip", "Origin": "http://testserver"}
    report = client.post("/api/v1/packs/import/preview", headers=base_headers, content=source).raise_for_status().json()
    assert report["story_openings"][0]["player_profiles"] == [profile]
    assert "player_profiles" not in report["story_openings"][1]
    headers = base_headers | {"X-Story-Opening": "before", "X-World-Pack-Sha256": report["archive_sha256"]}

    def import_opening(key, setup=None, opening_id="before"):
        request_headers = headers | {"Idempotency-Key": key, "X-Story-Opening": opening_id}
        if setup is not None:
            request_headers["X-Player-Setup"] = quote(json.dumps(setup, ensure_ascii=False), safe="")
        return client.post("/api/v1/packs/import", headers=request_headers, content=source)

    customization = {"profile_id": profile["profile_id"], "name": "雾月", "description": "河上旅行者"}
    result = import_opening("custom", customization).raise_for_status().json()
    path = "/api/v1/worlds/" + result["world_id"]
    projected = client.get(path + "/view").raise_for_status().json()["state"]
    player = projected["entities"][player_id]
    assert player["name"] == "雾月" and player["components"]["public_role"] == "River Spirit"
    assert "河上旅行者" in player["components"]["public_description"]
    assert player["components"]["needs"].get("food") is None
    assert player["components"]["needs"]["water"]["value"] == 90
    assert player["components"]["capabilities"]["grants"] == ["world.travel"]
    assert import_opening("custom", customization | {"name": " 雾月 "}).json()["duplicate"]
    assert import_opening("custom", customization | {"description": "不同角色"}).status_code == 409
    assert import_opening("unknown", customization | {"profile_id": "unknown"}).json()["error"]["code"] == "PLAYER_PROFILE_UNKNOWN"
    assert import_opening("fixed", customization, opening_id="fixed").json()["error"]["code"] == "PLAYER_SETUP_UNAVAILABLE"
    assert import_opening("tamper", customization | {"capabilities": ["world.everything"]}).json()["error"]["code"] == "PLAYER_SETUP_INVALID"
    default = import_opening("default").raise_for_status().json()
    default_state = client.get(f"/api/v1/worlds/{default['world_id']}/view").raise_for_status().json()["state"]
    assert default_state["entities"][player_id]["components"]["needs"]["food"]["value"] == 15
    assert import_opening("default", {}).raise_for_status().json()["duplicate"]


def test_world_creation_seed_is_optional_and_bound_to_the_request(service):
    client, _, _, _ = service
    body = {"pack_id": "fixture", "title": "Seeded", "player_name": "Traveler", "seed": -7}
    headers = {"Idempotency-Key": "seeded-world"}
    world = client.post("/api/v1/worlds", json=body, headers=headers).raise_for_status().json()
    path = f"/api/v1/worlds/{world['world_id']}/view?mode=author"
    assert client.get(path).raise_for_status().json()["state"]["metadata"]["generation_seed"] == -7
    duplicate = client.post("/api/v1/worlds", json=body, headers=headers).raise_for_status().json()
    assert duplicate["world_id"] == world["world_id"]
    assert client.post("/api/v1/worlds", json={**body, "seed": 8}, headers=headers).status_code == 409
    assert client.post("/api/v1/worlds", json={**body, "seed": True}, headers=headers).status_code == 422


def test_npc_autonomy_opt_in_is_bound_to_creation_idempotency_and_applied_before_persist(service, monkeypatch):
    import sys
    from types import ModuleType

    client, _, _, _ = service
    endpoint = "/api/v1/worlds"
    base = {"pack_id": "fixture", "title": "Autonomy", "player_name": "Traveler"}
    legacy = client.post(endpoint, json=base, headers={"Idempotency-Key": "autonomy-omitted"})
    legacy.raise_for_status()
    explicit_off = client.post(
        endpoint, json=base | {"npc_autonomy_mode": "off"},
        headers={"Idempotency-Key": "autonomy-explicit-off"},
    ).raise_for_status().json()
    legacy_author = client.get(
        f"{endpoint}/{legacy.json()['world_id']}/view?mode=author",
    ).raise_for_status().json()
    off_author = client.get(
        f"{endpoint}/{explicit_off['world_id']}/view?mode=author",
    ).raise_for_status().json()
    assert legacy_author["state"]["metadata"]["creation_payload_hash"] == off_author["state"]["metadata"]["creation_payload_hash"]

    calls = []

    def mark_enabled(state, *, world_id):
        calls.append(world_id)
        return state.model_copy(update={
            "metadata": {**state.metadata, "npc_autonomy_test_marker": "limited_v1"},
        })

    npc_autonomy_opening = ModuleType("world_engine.content.npc_autonomy_opening")
    npc_autonomy_opening.enable_limited_npc_autonomy = mark_enabled
    monkeypatch.setitem(sys.modules, npc_autonomy_opening.__name__, npc_autonomy_opening)
    limited_body = base | {"npc_autonomy_mode": "limited_v1"}
    headers = {"Idempotency-Key": "autonomy-limited"}
    limited = client.post(endpoint, json=limited_body, headers=headers).raise_for_status().json()
    limited_view = client.get(
        f"{endpoint}/{limited['world_id']}/view?mode=author",
    ).raise_for_status().json()
    assert limited_view["state"]["metadata"]["npc_autonomy_test_marker"] == "limited_v1"
    assert calls == [UUID(limited["world_id"])]
    assert limited_view["state"]["metadata"]["creation_payload_hash"] != legacy_author["state"]["metadata"]["creation_payload_hash"]

    duplicate = client.post(endpoint, json=limited_body, headers=headers).raise_for_status().json()
    assert duplicate["world_id"] == limited["world_id"]
    assert calls == [UUID(limited["world_id"])]
    assert client.post(endpoint, json=base, headers=headers).status_code == 409


def test_diverse_npc_autonomy_is_public_opt_in_and_respects_pack_version_availability(tmp_path):
    from world_engine.domain.npc_plan_review import NPC_PLAN_REVIEW_HANDLER_ID
    from world_engine.runtime import build_application

    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        endpoint = "/api/v1/worlds"
        packs = {item["pack_id"]: item for item in client.get("/api/v1/packs").raise_for_status().json()["items"]}
        feudal = packs["low_magic_feudal"]
        assert "2.9.0" not in feudal["available_versions"]
        assert "2.9.0" in feudal["sustained_available_versions"]

        natural_body = {
            "pack_id": "low_magic_feudal", "pack_version": "2.9.0",
            "title": "Natural feudal", "player_name": "Traveler",
            "npc_autonomy_mode": "diverse_v1",
        }
        rejected = client.post(
            endpoint, json=natural_body, headers={"Idempotency-Key": "diverse-feudal-29-natural"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"

        body = natural_body | {"population_mode": "sustained"}
        headers = {"Idempotency-Key": "diverse-feudal-29-sustained"}
        created = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        path = f"{endpoint}/{created['world_id']}/view?mode=author"
        world_view = client.get(path).raise_for_status().json()
        state = world_view["state"]
        assert created["pack_version"] == state["metadata"]["pack_version"] == "2.9.0"
        assert state["metadata"]["population_mode"] == "sustained"
        assert state["metadata"]["npc_autonomy_profile"] == "diverse_v1"
        review_schedules = [
            item for item in state["scheduled_events"]
            if item["handler_id"] == NPC_PLAN_REVIEW_HANDLER_ID
        ]
        assert len(review_schedules) == 3
        assert {item["max_runs"] for item in review_schedules} == {1}
        first_review = min(review_schedules, key=lambda item: int(item["due_tick"]))
        travel_actor_id = first_review["payload"]["actor_id"]
        reach_goals = [
            goal for goal in state["goals"]
            if goal["owner_id"] == travel_actor_id
            and goal["status"] == "active"
            and len(goal["conditions"]) == 1
            and goal["conditions"][0]["kind"] == "reach_location"
        ]
        assert len(reach_goals) == 1
        assert reach_goals[0]["conditions"][0]["subject_id"] == travel_actor_id
        assert travel_actor_id != state["player_id"]

        jobs_path = f"{endpoint}/{created['world_id']}/jobs?branch_id={world_view['branch']['branch_id']}"
        assert client.get(jobs_path).raise_for_status().json()["items"] == []
        duplicate = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        assert duplicate["world_id"] == created["world_id"]
        duplicate_state = client.get(path).raise_for_status().json()["state"]
        assert len([
            item for item in duplicate_state["scheduled_events"]
            if item["handler_id"] == NPC_PLAN_REVIEW_HANDLER_ID
        ]) == 3
        assert client.post(
            endpoint, json=body | {"npc_autonomy_mode": "off"}, headers=headers,
        ).status_code == 409


def test_diverse_npc_autonomy_rejects_custom_catalog_worlds_with_public_422(service):
    client, _, _, _ = service
    response = client.post(
        "/api/v1/worlds",
        json={
            "pack_id": "fixture", "title": "Custom fixture", "player_name": "Traveler",
            "npc_autonomy_mode": "diverse_v1",
        },
        headers={"Idempotency-Key": "diverse-custom-fixture"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NPC_AUTONOMY_PROFILE_UNAVAILABLE"
    assert client.get("/api/v1/worlds").raise_for_status().json()["items"] == []


def test_sustained_mode_with_shipped_catalog_uses_seats_and_reserves_idempotency(tmp_path):
    from world_engine.runtime import build_application

    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        endpoint = "/api/v1/worlds"
        headers = {"Idempotency-Key": "sustained-world"}
        body = {
            "pack_id": "long_life_cultivation", "title": "Capacity", "player_name": "Traveler",
            "seed": 0, "population_mode": "sustained",
        }
        created = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        path = f"{endpoint}/{created['world_id']}/view?mode=author"
        state = client.get(path).raise_for_status().json()["state"]
        assert state["metadata"]["generation_seed"] == 0
        assert state["metadata"]["population_mode"] == "sustained"
        seats = [
            entity["components"]["population"]["seat_policy"] for entity in state["entities"].values()
            if "seat_policy" in entity["components"].get("population", {})
        ]
        assert len(seats) == 12
        duplicate = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        assert duplicate["world_id"] == created["world_id"]
        assert client.post(endpoint, json={**body, "population_mode": "natural"}, headers=headers).status_code == 409
        assert client.post(endpoint, json={**body, "population_mode": "custom"}, headers=headers).status_code == 422


def test_public_create_selects_optional_family_23_and_persists_occupations(tmp_path):
    from world_engine.runtime import build_application

    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        packs = client.get("/api/v1/packs").raise_for_status().json()["items"]
        assert all(pack["version"] == "2.0.0" for pack in packs)
        assert all(pack["available_versions"] == ["2.0.0", "2.2.0", "2.3.0", "2.8.0", "2.12.0", "2.13.0"] for pack in packs)

        endpoint = "/api/v1/worlds"
        headers = {"Idempotency-Key": "family-occupation-23"}
        body = {
            "pack_id": "long_life_cultivation",
            "pack_version": "2.3.0",
            "title": "Family and work",
            "player_name": "Traveler",
        }
        created = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        view = client.get(f"{endpoint}/{created['world_id']}/view?mode=author").raise_for_status().json()
        state = view["state"]

        assert created["pack_version"] == view["world"]["pack_version"] == "2.3.0"
        assert state["metadata"]["pack_version"] == "2.3.0"
        assert len(state["kinship_links"]) > 0
        assert len(state["work_postings"]) == 12
        assert len(state["metadata"]["work_shift_terms"]) == 12
        assert state.get("work_assignments", []) == []


def test_public_create_offers_turnover_pack_only_in_sustained_mode(tmp_path):
    from world_engine.runtime import build_application

    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        packs = client.get("/api/v1/packs").raise_for_status().json()["items"]
        assert all("2.4.0" not in pack["available_versions"] for pack in packs)
        assert all("2.5.0" not in pack["available_versions"] for pack in packs)
        by_pack = {pack["pack_id"]: pack for pack in packs}
        assert by_pack["modern_city"]["sustained_available_versions"] == [
            "2.4.0", "2.5.0", "2.6.0", "2.7.0", "2.10.0",
        ]
        assert by_pack["low_magic_feudal"]["sustained_available_versions"] == ["2.4.0", "2.9.0"]
        assert by_pack["long_life_cultivation"]["sustained_available_versions"] == ["2.4.0", "2.11.0"]
        assert "2.9.0" not in by_pack["low_magic_feudal"]["available_versions"]

        endpoint = "/api/v1/worlds"
        headers = {"Idempotency-Key": "sustained-turnover-24"}
        body = {"pack_id": "modern_city", "pack_version": "2.4.0", "title": "Turnover", "player_name": "Traveler"}
        rejected = client.post(endpoint, json=body, headers=headers)
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"

        v25_natural = client.post(endpoint, json={
            "pack_id": "modern_city", "pack_version": "2.5.0", "title": "Natural 2.5", "player_name": "Traveler",
        }, headers={"Idempotency-Key": "modern-occupation-25-natural"})
        assert v25_natural.status_code == 422
        assert v25_natural.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"

        v25_wrong_pack = client.post(endpoint, json={
            "pack_id": "long_life_cultivation", "pack_version": "2.5.0", "title": "Wrong Pack", "player_name": "Traveler",
            "population_mode": "sustained",
        }, headers={"Idempotency-Key": "modern-occupation-25-wrong-pack"})
        assert v25_wrong_pack.status_code == 422
        assert v25_wrong_pack.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"

        v210_natural = client.post(endpoint, json={
            "pack_id": "modern_city", "pack_version": "2.10.0", "title": "Natural 2.10",
            "player_name": "Traveler",
        }, headers={"Idempotency-Key": "modern-civic-210-natural"})
        assert v210_natural.status_code == 422
        assert v210_natural.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"

        v210_wrong_pack = client.post(endpoint, json={
            "pack_id": "low_magic_feudal", "pack_version": "2.10.0", "title": "Wrong Pack 2.10",
            "player_name": "Traveler", "population_mode": "sustained",
        }, headers={"Idempotency-Key": "modern-civic-210-wrong-pack"})
        assert v210_wrong_pack.status_code == 422
        assert v210_wrong_pack.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"

        v29_natural = client.post(endpoint, json={
            "pack_id": "low_magic_feudal", "pack_version": "2.9.0", "title": "Natural 2.9",
            "player_name": "Traveler",
        }, headers={"Idempotency-Key": "feudal-occupation-29-natural"})
        assert v29_natural.status_code == 422
        assert v29_natural.json()["error"]["code"] == "PACK_VERSION_UNAVAILABLE"
        assert client.get(endpoint).raise_for_status().json()["items"] == []

        body["population_mode"] = "sustained"
        created = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        assert created["pack_version"] == "2.4.0"
        path = f"{endpoint}/{created['world_id']}/view?mode=author"
        state = client.get(path).raise_for_status().json()["state"]
        assert state["metadata"]["population_mode"] == "sustained"
        active_named = [entity for entity in state["entities"].values()
                        if entity["kind"] == "character" and entity["status"] == "active" and entity["name"]]
        assert len(active_named) == 297
        assert client.post(endpoint, json=body, headers=headers).raise_for_status().json()["world_id"] == created["world_id"]

        v25_headers = {"Idempotency-Key": "modern-occupation-25-sustained"}
        v25_body = {
            "pack_id": "modern_city", "pack_version": "2.5.0", "title": "Regional Skills",
            "player_name": "Traveler", "population_mode": "sustained",
        }
        created_v25 = client.post(endpoint, json=v25_body, headers=v25_headers).raise_for_status().json()
        assert created_v25["pack_version"] == "2.5.0"
        v25_state = client.get(
            f"{endpoint}/{created_v25['world_id']}/view?mode=author",
        ).raise_for_status().json()["state"]
        assert v25_state["metadata"]["pack_version"] == "2.5.0"
        assert v25_state["metadata"]["population_mode"] == "sustained"
        groups = [
            entity for entity in v25_state["entities"].values()
            if entity["kind"] == "group" and "population" in entity["components"]
        ]
        assert len(groups) == 12
        assert all("occupation_profile" in group["components"]["population"]["seat_policy"] for group in groups)
        assert client.post(endpoint, json=v25_body, headers=v25_headers).raise_for_status().json()["world_id"] == created_v25["world_id"]

        v29_headers = {"Idempotency-Key": "feudal-occupation-29-sustained"}
        v29_body = {
            "pack_id": "low_magic_feudal", "pack_version": "2.9.0", "title": "Feudal Occupations",
            "player_name": "Traveler", "population_mode": "sustained",
        }
        created_v29 = client.post(endpoint, json=v29_body, headers=v29_headers).raise_for_status().json()
        assert created_v29["pack_version"] == "2.9.0"
        v29_state = client.get(
            f"{endpoint}/{created_v29['world_id']}/view?mode=author",
        ).raise_for_status().json()["state"]
        assert v29_state["metadata"]["pack_version"] == "2.9.0"
        assert v29_state["metadata"]["population_mode"] == "sustained"
        assert len(v29_state["metadata"]["content_index"]["other_resident_ids"]) == 264
        assert len(v29_state["metadata"]["content_index"]["country_ids"]) == 24
        assert len(v29_state["metadata"]["content_index"]["organization_ids"]) == 24
        v29_groups = [
            entity for entity in v29_state["entities"].values()
            if entity["kind"] == "group" and "population" in entity["components"]
        ]
        assert len(v29_groups) == 12
        assert all(
            group["components"]["population"]["seat_policy"]["maintain_civic_occupations"] is True
            for group in v29_groups
        )


def test_backup_download_and_restore_preview_leave_original_intact(service, monkeypatch):
    client, _, _, _ = service
    world = create(client).json()
    path = f"/api/v1/worlds/{world['world_id']}"
    before = client.get(path + "/view?mode=author").json()
    download = client.get(path + "/backup")
    assert download.status_code == 200
    assert download.headers["cache-control"] == "no-store"
    assert int(download.headers["content-length"]) == len(download.content)
    assert download.headers["content-disposition"].endswith('.webackup"')
    preview_path = "/api/v1/restores/preview"
    headers = {"Content-Type": "application/zip"}
    buffer = download.content
    segments = (buffer[offset:offset + 64] for offset in range(0, len(buffer), 64))
    preview = client.post(preview_path, headers=headers, content=segments)
    assert preview.status_code == 200, preview.text
    assert preview.json()["state_hashes"] == {world["active_branch_id"]: before["state_hash"]}
    assert preview.json()["ruleset_version"] == world["ruleset_version"]
    assert preview.json()["pack_version"] == world["pack_version"]
    segmented = client.get(path + "/backup", params={"format_version": "2.0"}).raise_for_status()
    converted = client.post(preview_path, headers=headers, content=segmented.content).raise_for_status().json()
    assert converted["format_version"] == "2.0"
    assert converted["state_hashes"] == preview.json()["state_hashes"]
    assert converted["ruleset_version"] == preview.json()["ruleset_version"]
    assert client.post(preview_path, headers=headers, content=b"invalid").status_code == 422
    assert client.post(preview_path, content=download.content).status_code == 415
    monkeypatch.setattr("world_engine.api.app.MAX_COMPRESSED_ARCHIVE_BYTES", len(buffer) - 1)
    segments = (buffer[offset:offset + 64] for offset in range(0, len(buffer), 64))
    assert client.post(preview_path, headers=headers, content=segments).status_code == 413
    assert client.get(path + "/view?mode=author").json()["state_hash"] == before["state_hash"]
    assert len(client.get("/api/v1/worlds").json()["items"]) == 1


def test_backup_download_closes_archive_on_asgi_disconnect():
    import asyncio
    import io

    from starlette.requests import ClientDisconnect
    from world_engine.api.app import BackupDownload

    archive = io.BytesIO(b"staged backup")
    response = BackupDownload(archive, len(archive.getvalue()), uuid4())

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            raise BrokenPipeError

    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert archive.closed


def test_external_identity_replaces_local_cookie_without_cross_tenant_access(service):
    client, application, session, _ = service
    world = create(client).json()
    other_tenant = str(uuid4())

    class IdentityFixture:
        async def authenticate(self, request):
            if request.headers.get("Authorization") == "Bearer test-account":
                return AuthenticatedIdentity(session.principal_id, other_tenant)
            return None

    app = create_app(application, session, identity_provider=IdentityFixture())
    with TestClient(app) as other:
        path = f"/api/v1/worlds/{world['world_id']}"
        assert other.get("/api/v1/session").status_code == 401
        assert other.get(path + "/player-report").status_code == 401
        other.cookies.set(session.cookie_name, session.token)
        assert other.get("/api/v1/worlds").status_code == 401
        headers = {"Authorization": "Bearer test-account"}
        authenticated = other.get("/api/v1/session", headers=headers).raise_for_status()
        assert "set-cookie" not in authenticated.headers
        assert other.get("/api/v1/worlds", headers=headers).json() == {"items": []}
        assert other.get(path + "/view", headers=headers).status_code == 404
        assert other.get(path + "/player-report", headers=headers).status_code == 404


def test_creation_retries_and_persistence(service):
    client, _, session, url = service
    first = create(client)
    assert first.status_code == 201, first.text
    world = first.json()
    assert world["current_tick"] == "0" and world["state_version"] == "0"
    repeated = create(client)
    assert repeated.json()["world_id"] == world["world_id"]
    assert repeated.json()["duplicate"] is True
    assert create(client, title="Different").status_code == 409
    assert len(client.get("/api/v1/worlds").json()["items"]) == 1
    # Reopen storage with a different engine, not the same in-memory repository.
    reopened = Database(url)
    try:
        app = create_app(WorldApplication(WorldRepository(reopened), FixtureCatalog()), session)
        with TestClient(app) as other:
            other.get("/api/v1/session")
            assert other.get("/api/v1/worlds").json()["items"][0]["world_id"] == world["world_id"]
    finally:
        reopened.dispose()


def test_world_library_returns_active_header_without_materializing_world_state(service, monkeypatch):
    client, application, _, _ = service
    created = create(client).raise_for_status().json()
    expected = client.get(
        f"/api/v1/worlds/{created['world_id']}/view", params={"mode": "author"},
    ).raise_for_status().json()["world"]

    def unexpected_state_read(*_args, **_kwargs):
        pytest.fail("World library should read active branch counters without loading state JSON.")

    monkeypatch.setattr(application.repository, "read_world", unexpected_state_read)
    response = client.get("/api/v1/worlds")

    assert response.status_code == 200
    assert response.json() == {"items": [expected]}


def test_player_projection_and_explicit_author_view(service):
    client, _, _, _ = service
    world_id = create(client).json()["world_id"]
    player = client.get(f"/api/v1/worlds/{world_id}/view")
    assert player.status_code == 200
    assert "hidden allegiance" not in player.text and "future betrayal" not in player.text
    assert "creation_payload_hash" not in player.text
    assert "merchant" in player.text
    author = client.get(f"/api/v1/worlds/{world_id}/view?mode=author")
    assert author.status_code == 200
    assert "hidden allegiance" in author.text
    assert author.json()["mode"] == "author"


def test_player_report_download_matches_player_view_and_leaves_world_intact(service):
    client, _, _, _ = service
    world = create(client).raise_for_status().json()
    path = f"/api/v1/worlds/{world['world_id']}"
    alternate = client.post(
        path + "/branches",
        json={"branch_id": world["active_branch_id"], "sequence": "0", "label": "Alternate"},
        headers={"Idempotency-Key": "report-alternate"},
    ).raise_for_status().json()
    branches_before = client.get(path + "/branches").raise_for_status().json()
    for branch_id in (None, alternate["branch_id"]):
        params = {"branch_id": branch_id} if branch_id else {}
        before = client.get(path + "/view", params={**params, "mode": "author"}).raise_for_status()
        assert "hidden allegiance" in before.text and "future betrayal" in before.text
        player = client.get(path + "/view", params=params).raise_for_status().json()
        assert player["branch"]["branch_id"] == (branch_id or world["active_branch_id"])

        # Even an owner attempting to select author mode receives the player projection.
        report = client.get(path + "/player-report", params={**params, "mode": "author"}).raise_for_status()
        assert report.headers["content-type"] == "application/json"
        assert report.headers["content-disposition"] == 'attachment; filename="player-report.json"'
        assert report.headers["cache-control"] == "no-store"
        assert report.json() == {"format": "world-engine-player-report", "version": 1, "view": player}
        for marker in ("hidden allegiance", "future betrayal", "creation_payload_hash"):
            assert marker not in report.text
        assert "merchant" in report.text

        after = client.get(path + "/view", params={**params, "mode": "author"}).raise_for_status()
        assert after.json() == before.json()
    assert client.get(path + "/branches").raise_for_status().json() == branches_before


def test_player_projection_cache_revalidates_head_and_returns_detached_state(service, monkeypatch):
    client, application, session, _ = service
    world_id = UUID(create(client).json()["world_id"])
    scope = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    cold_reference = WorldApplication(application.repository, application.catalog).view_world(scope, world_id)

    verified_projections = 0
    project_verified = application.repository.project_verified_player_view

    def count_verified_projections(*args, **kwargs):
        nonlocal verified_projections
        verified_projections += 1
        return project_verified(*args, **kwargs)

    def unexpected_state_read(*_args, **_kwargs):
        pytest.fail("Player views should use the repository projection instead of reading a detached world.")

    monkeypatch.setattr(application.repository, "project_verified_player_view", count_verified_projections)
    monkeypatch.setattr(application.repository, "read_world", unexpected_state_read)
    first = application.view_world(scope, world_id)
    assert first == cold_reference
    first["state"]["entities"]["caller-mutation"] = {"private": True}

    def unexpected_projection_rebuild(*_args, **_kwargs):
        pytest.fail("A verified hot player view should reuse its bounded state projection.")

    monkeypatch.setattr(application, "_build_player_projection", unexpected_projection_rebuild)
    second = application.view_world(scope, world_id)

    assert verified_projections == 2
    assert second == cold_reference
    assert "caller-mutation" not in second["state"]["entities"]


def test_player_projection_cache_uses_fresh_actor_grant_for_same_head(service):
    client, application, session, _ = service
    world_id = UUID(create(client).json()["world_id"])
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    full_view = application.repository.read_world(owner, world_id)
    player_id = full_view.state.player_id
    other_actor_id = next(
        entity.id for entity in full_view.state.entities.values()
        if entity.kind == "character" and entity.id != player_id
    )
    player_user = uuid4()
    application.repository.grant_membership(
        owner, world_id, player_user, MembershipRole.PLAYER, controlled_actor_ids=(player_id,),
    )
    player_scope = AccessScope(tenant_id=owner.tenant_id, user_id=player_user)

    first = application.view_world(player_scope, world_id)
    assert first["state"]["player_id"] == str(player_id)
    application.repository.grant_membership(
        owner, world_id, player_user, MembershipRole.PLAYER, controlled_actor_ids=(other_actor_id,),
    )
    second = application.view_world(player_scope, world_id)

    assert second["state_hash"] == first["state_hash"]
    assert second["state"]["player_id"] == str(other_actor_id)


def test_player_projection_cache_refreshes_hosted_npc_truce_permission(service, monkeypatch):
    client, application, session, _ = service
    world_id = UUID(create(client).json()["world_id"])
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    full_view = application.repository.read_world(owner, world_id)
    player_id = full_view.state.player_id
    opponent_id = next(
        entity.id for entity in full_view.state.entities.values()
        if entity.kind == "character" and entity.id != player_id
    )
    player_user = uuid4()
    application.repository.grant_membership(
        owner, world_id, player_user, MembershipRole.PLAYER, controlled_actor_ids=(player_id,),
    )

    def projected_truce(_scope, _view, actor_id):
        return {"player_id": str(actor_id), "entities": {}, "combat_truce_options": {
            "opponent_id": str(opponent_id), "proposed_by_self": True,
        }}

    monkeypatch.setattr(application, "_build_player_projection", projected_truce)
    first = application.view_world(owner, world_id)
    assert first["state"]["combat_truce_options"]["can_request_response"] is True
    same_head = application.repository.read_verified_player_head(owner, world_id)[1]

    application.repository.grant_actor_control(owner, world_id, player_user, opponent_id)

    def unexpected_projection_rebuild(*_args, **_kwargs):
        pytest.fail("Changing hosted-NPC authorization must not require a state projection rebuild.")

    monkeypatch.setattr(application, "_build_player_projection", unexpected_projection_rebuild)
    second = application.view_world(owner, world_id)

    assert application.repository.read_verified_player_head(owner, world_id)[1].state_hash == same_head.state_hash
    assert second["state"]["combat_truce_options"]["can_request_response"] is False
    assert second["state_hash"] == first["state_hash"]


def test_other_tenant_cannot_read_world_even_with_valid_session(service):
    client, application, _, _ = service
    world_id = UUID(create(client).json()["world_id"])
    stranger = LocalSession(str(uuid4()), str(uuid4()), frozenset({"http://testserver"}))
    with TestClient(create_app(application, stranger)) as other:
        other.get("/api/v1/session")
        assert other.get("/api/v1/worlds").json() == {"items": []}
        response = other.get(f"/api/v1/worlds/{world_id}/view?mode=author")
        assert response.status_code in (403, 404)
        assert "hidden allegiance" not in response.text


def test_history_exposes_author_events_only_to_author_roles(service):
    client, application, session, _ = service
    world = create(client).json()
    world_id = UUID(world["world_id"])
    branch_id = UUID(world["active_branch_id"])
    repository = application.repository
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    view = repository.read_world(owner, world_id, branch_id)
    repository.commit_command(owner, world_id, branch_id, CommandEnvelope(
        actor_id=view.state.player_id, command_type="wait", idempotency_key="history-fixture",
        expected_state_version=0,
    ), [TimeAdvancedEvent(to_tick=60)])

    path = f"/api/v1/worlds/{world_id}/history?branch_id={branch_id}"
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["items"][0]["sequence"] == "1"
    assert client.get(path + "&after=1").json() == {"items": [], "next_cursor": None}
    assert client.get(path + "&after=-1").status_code == 422
    assert client.get(path + "&mode=auto").json()["mode"] == "author"
    assert client.get(path + "&mode=player").json() == {"mode": "player", "items": [], "next_cursor": None}

    player = uuid4()
    character = next(row.id for row in view.state.entities.values()
                     if row.kind == "character" and row.id != view.state.player_id)
    repository.grant_membership(owner, world_id, player, MembershipRole.PLAYER, controlled_actor_ids=(character,))
    player_session = LocalSession(str(player), session.tenant_id, frozenset({"http://testserver"}))
    with TestClient(create_app(application, player_session)) as player_reader:
        player_reader.get("/api/v1/session")
        assert player_reader.get(path).status_code == 403
        assert player_reader.get(path + "&mode=auto").json() == {"mode": "player", "items": [], "next_cursor": None}

    observer = uuid4()
    repository.grant_membership(owner, world_id, observer, MembershipRole.OBSERVER)
    observer_session = LocalSession(str(observer), session.tenant_id, frozenset({"http://testserver"}))
    with TestClient(create_app(application, observer_session)) as reader:
        reader.get("/api/v1/session")
        assert reader.get(path).status_code == 403
        assert reader.get(path + "&mode=player").status_code == 403


def test_branch_fork_requires_complete_commit_and_replays_idempotently(service):
    client, application, session, _ = service
    world = create(client).json()
    world_id = UUID(world["world_id"])
    branch_id = UUID(world["active_branch_id"])
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    repository = application.repository
    view = repository.read_world(owner, world_id, branch_id)
    repository.commit_command(owner, world_id, branch_id, CommandEnvelope(
        actor_id=view.state.player_id, command_type="wait", idempotency_key="fork-fixture",
        expected_state_version=0,
    ), [TimeAdvancedEvent(to_tick=60), TimeAdvancedEvent(to_tick=120)])
    path = f"/api/v1/worlds/{world_id}"
    events = client.get(f"{path}/history?branch_id={branch_id}").json()["items"]
    assert [event["can_fork"] for event in events] == [False, True]

    request = {"branch_id": str(branch_id), "sequence": "1", "label": "Alternate"}
    headers = {"Idempotency-Key": "fork-example"}
    assert client.post(path + "/branches", json=request, headers=headers).status_code == 409
    request["sequence"] = "2"
    created = client.post(path + "/branches", json=request, headers=headers)
    assert created.status_code == 201, created.text
    branch = created.json()
    assert branch["head_seq"] == "2" and branch["current_tick"] == "120"
    assert client.post(path + "/branches", json=request, headers=headers).json() == branch
    assert client.post(path + "/branches", json=request | {"sequence": "0"}, headers=headers).status_code == 409
    assert len(client.get(path + "/branches").json()["items"]) == 2
    forked = repository.read_world(owner, world_id, UUID(branch["branch_id"]))
    assert forked.state_hash == repository.read_world(owner, world_id, branch_id).state_hash


def test_succession_causation_search_respects_branch_lineage(service):
    client, application, session, _ = service
    world = create(client).json()
    world_id = UUID(world["world_id"])
    branch_id = UUID(world["active_branch_id"])
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    repository = application.repository
    before = repository.read_world(owner, world_id, branch_id)
    target = next(entity.id for entity in before.state.entities.values() if entity.name == "Trader")
    without_death = repository.fork(owner, world_id, branch_id, 0, "Earlier")
    death = EntityFieldChangedEvent(entity_id=target, field_name="status", value="dead")
    repository.commit_command(owner, world_id, branch_id, CommandEnvelope(
        actor_id=before.state.player_id, command_type="fixture.death",
        idempotency_key="death", expected_state_version=0,
    ), [death])
    with_death = repository.fork(owner, world_id, branch_id, 1, "Later")
    assert repository.find_death_events(owner, world_id, without_death.branch_id, {target}) == {}
    assert repository.find_death_events(owner, world_id, with_death.branch_id, {target}) == {target: death.event_id}


def test_job_status_hides_internal_payload_and_rejects_other_tenants(service):
    client, application, session, _ = service
    world = create(client).json()
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    job = application.repository.enqueue_job(owner, NewJob(
        world_id=UUID(world["world_id"]), branch_id=UUID(world["active_branch_id"]),
        job_type="simulation", idempotency_key="status-test", payload={"internal_plan": "private"},
    ))
    path = f"/api/v1/jobs/{job.job_id}"
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["status"] == job.status.value
    assert "payload" not in response.json()
    assert "fencing_token" not in response.json()

    stranger = LocalSession(str(uuid4()), str(uuid4()), frozenset({"http://testserver"}))
    with TestClient(create_app(application, stranger)) as other:
        other.get("/api/v1/session")
        assert other.get(path).status_code in {403, 404}


def test_simulation_job_reads_expose_exact_committed_version_for_same_tick_commits(service):
    client, application, session, _ = service
    world = create(client).json()
    world_id = UUID(world["world_id"])
    branch_id = UUID(world["active_branch_id"])
    owner = AccessScope(tenant_id=UUID(session.tenant_id), user_id=UUID(session.principal_id))
    repository = application.repository
    initial = repository.read_world(owner, world_id, branch_id)
    trader = next(entity.id for entity in initial.state.entities.values() if entity.name == "Trader")

    # Two durable state commits at the same clock tick advance the branch
    # version independently of its clock.
    for index, name in enumerate(("Trader One", "Trader Two")):
        before = repository.read_world(owner, world_id, branch_id)
        repository.commit_command(owner, world_id, branch_id, CommandEnvelope(
            actor_id=before.state.player_id, command_type="fixture.same_tick_review",
            idempotency_key=f"same-tick-review-{index}", expected_state_version=before.branch.state_version,
        ), [EntityFieldChangedEvent(entity_id=trader, field_name="name", value=name)])

    same_tick = repository.read_world(owner, world_id, branch_id)
    assert same_tick.state.current_tick == initial.state.current_tick == 0
    assert same_tick.branch.state_version == 2

    committed = repository.enqueue_job(owner, NewJob(
        world_id=world_id, branch_id=branch_id, job_type="simulation.long_action",
        idempotency_key="same-tick-committed-version",
        payload={
            "plan": {}, "initial_state_version": 0,
            "committed_state_version": same_tick.branch.state_version,
            "private_worker_detail": "must stay private",
        },
    ))
    legacy = repository.enqueue_job(owner, NewJob(
        world_id=world_id, branch_id=branch_id, job_type="simulation.world_advance",
        idempotency_key="legacy-version-fallback",
        payload={"plan": {}, "initial_state_version": 2**63 - 1},
    ))
    malformed = repository.enqueue_job(owner, NewJob(
        world_id=world_id, branch_id=branch_id, job_type="simulation.long_action",
        idempotency_key="reject-boolean-version",
        payload={"plan": {}, "initial_state_version": 2, "committed_state_version": True},
    ))

    status_path = f"/api/v1/jobs/{committed.job_id}"
    status = client.get(status_path).raise_for_status().json()
    assert status["committed_state_version"] == str(same_tick.branch.state_version)
    assert status["status"] == "queued"
    assert "payload" not in status and "private_worker_detail" not in status

    listed = client.get(f"/api/v1/worlds/{world_id}/jobs?branch_id={branch_id}").raise_for_status().json()["items"]
    listed_by_id = {item["job_id"]: item for item in listed}
    assert listed_by_id[str(committed.job_id)]["committed_state_version"] == "2"
    assert listed_by_id[str(legacy.job_id)]["committed_state_version"] == str(2**63 - 1)
    assert "committed_state_version" not in listed_by_id[str(malformed.job_id)]

    # Claiming the queued job changes lease/status metadata without changing
    # the already committed branch head or the version reported by status.
    lease = repository.claim_job("same-tick-status-test", job_type="simulation.long_action")
    assert lease is not None and lease.job_id == committed.job_id
    running = client.get(status_path).raise_for_status().json()
    after_lease = repository.read_world(owner, world_id, branch_id)
    assert running["status"] == "running"
    assert running["committed_state_version"] == "2"
    assert after_lease.state.current_tick == 0
    assert after_lease.branch.state_version == same_tick.branch.state_version
    assert after_lease.state_hash == same_tick.state_hash
