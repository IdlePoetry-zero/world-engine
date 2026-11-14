"""HTTP boundaries exercised against real temporary SQLite storage."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.worlds import WorldApplication
from world_engine.domain.models import NewWorld, WorldEntity, WorldState
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class FixtureCatalog:
    """Explicit test fixture, not a shipped content pack."""

    def list_packs(self):
        return [{"pack_id": "fixture", "title": "Fixture", "version": "1"}]

    def build_world(self, pack_id, title, player_name):
        player, npc, place = uuid4(), uuid4(), uuid4()
        return NewWorld(title=title, state=WorldState(
            player_id=player,
            entities={
                place: WorldEntity(id=place, kind="place", name="Market"),
                player: WorldEntity(id=player, kind="character", name=player_name, location_id=place),
                npc: WorldEntity(id=npc, kind="character", name="Trader", location_id=place,
                                 components={"secret": "hidden allegiance", "public_role": "merchant"}),
            },
            metadata={"pack_id": pack_id, "author_secret": "future betrayal"},
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
