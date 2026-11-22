from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.family_authoring import family_authoring_router
from world_engine.api.security import LocalSession
from world_engine.application.family_authoring import FamilyAuthoringApplication
from world_engine.application.worlds import WorldApplication
from world_engine.domain.errors import AccessDenied
from world_engine.domain.family_kinship import GenesisKinshipSource, KinshipLink
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def family_world(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    parent_id, child_id, place_id = uuid4(), uuid4(), uuid4()
    database = Database(f"sqlite:///{(tmp_path / 'family.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(scope, NewWorld(
        title="Family authoring fixture",
        ruleset_version="2.0.0",
        state=WorldState(
            current_tick=7_200,
            player_id=parent_id,
            entities={
                parent_id: WorldEntity(
                    id=parent_id, kind="character", name="Ari", location_id=place_id,
                ),
                child_id: WorldEntity(
                    id=child_id, kind="character", name="Bea", location_id=place_id,
                ),
                place_id: WorldEntity(id=place_id, kind="place", name="Harbor House"),
            },
        ),
    ))
    service = FamilyAuthoringApplication(repository)
    yield scope, repository, service, created.world.world_id, created.branch.branch_id, parent_id, child_id
    database.dispose()


def _http_scope(request):
    return AccessScope(
        tenant_id=UUID(request.state.tenant_id),
        user_id=UUID(request.state.principal_id),
    )


def _client(scope, repository, service):
    session = LocalSession(
        str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}),
    )
    app = create_app(WorldApplication(repository, None), session)
    app.include_router(family_authoring_router(service, _http_scope))
    client = TestClient(app)
    client.get("/api/v1/session").raise_for_status()
    return client


def test_http_create_and_end_are_private_exact_tick_replayable_and_idempotent(family_world):
    scope, repository, service, world_id, branch_id, parent_id, child_id = family_world
    before = repository.read_world(scope, world_id, branch_id)
    path = f"/api/v1/worlds/{world_id}/author/kinship-links"

    with _client(scope, repository, service) as client:
        created_response = client.post(
            path,
            headers={"Idempotency-Key": "birth-link-1"},
            json={
                "branch_id": str(branch_id), "expected_state_version": "0",
                "parent_id": str(parent_id), "child_id": str(child_id),
                "relationship": "birth",
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        assert created["state_version"] == "1"
        assert created["duplicate"] is False
        assert created["world_time_effect"] is False

        # A retry with the original version is recognized before stale-version
        # validation and does not append a second fact.
        retry = client.post(
            path,
            headers={"Idempotency-Key": "birth-link-1"},
            json={
                "branch_id": str(branch_id), "expected_state_version": "0",
                "parent_id": str(parent_id), "child_id": str(child_id),
                "relationship": "birth",
            },
        )
        assert retry.status_code == 201
        assert retry.json() == created | {"duplicate": True}

        changed_retry = client.post(
            path,
            headers={"Idempotency-Key": "birth-link-1"},
            json={
                "branch_id": str(branch_id), "expected_state_version": "0",
                "parent_id": str(child_id), "child_id": str(parent_id),
                "relationship": "birth",
            },
        )
        assert changed_retry.status_code == 409

        end_path = f"{path}/{created['link_id']}/end"
        end_body = {
            "branch_id": str(branch_id), "expected_state_version": "1",
            "expected_created_tick": str(before.state.current_tick),
            "expected_relationship": "birth",
        }
        ended_response = client.post(
            end_path, headers={"Idempotency-Key": "end-link-1"}, json=end_body,
        )
        assert ended_response.status_code == 200, ended_response.text
        ended = ended_response.json()
        assert ended["state_version"] == "2"
        assert ended["link_id"] == created["link_id"]

        ended_retry = client.post(
            end_path, headers={"Idempotency-Key": "end-link-1"}, json=end_body,
        )
        assert ended_retry.status_code == 200
        assert ended_retry.json() == ended | {"duplicate": True}

        stale_new_command = client.post(
            path,
            headers={"Idempotency-Key": "birth-link-2"},
            json={
                "branch_id": str(branch_id), "expected_state_version": "0",
                "parent_id": str(parent_id), "child_id": str(child_id),
                "relationship": "adoptive",
            },
        )
        assert stale_new_command.status_code == 409

    after = repository.read_world(scope, world_id, branch_id)
    assert after.state.current_tick == before.state.current_tick
    assert after.branch.state_version == 2
    assert len(after.state.kinship_links) == 1
    assert after.state.kinship_links[0].ended_tick == before.state.current_tick
    assert after.last_seq == 2
    assert repository.replay(scope, world_id, branch_id).state_hash == after.state_hash

    # Raw audit is available to authors and keeps participant details private.
    audit = repository.list_events(scope, world_id, branch_id, after_seq=0)
    assert [row.type for row in audit.events] == [
        "core.kinship_link_recorded.v1", "core.kinship_link_ended.v1",
    ]
    assert all(row.visibility == {"policy": "author_only"} for row in audit.events)
    recorded = audit.events[0].payload["link"]
    assert recorded["source"] == {"kind": "event", "event_id": created["event_id"]}


def test_only_owner_or_editor_can_author_kinship_links(family_world):
    scope, repository, service, world_id, branch_id, parent_id, child_id = family_world
    player_scope = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    repository.grant_membership(scope, world_id, player_scope.user_id, MembershipRole.PLAYER)

    with pytest.raises(AccessDenied):
        service.record_link(
            player_scope, world_id, branch_id, 0,
            parent_id=parent_id, child_id=child_id, relationship="birth",
            idempotency_key="not-authorized",
        )

    after = repository.read_world(scope, world_id, branch_id)
    assert after.branch.state_version == 0
    assert after.state.kinship_links == []


def test_http_can_end_genesis_link_created_before_tick_zero(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    parent_id, child_id, place_id, link_id = uuid4(), uuid4(), uuid4(), uuid4()
    link = KinshipLink(
        link_id=link_id,
        parent_id=parent_id,
        child_id=child_id,
        relationship="birth",
        created_tick=-86_400,
        source=GenesisKinshipSource(kind="genesis", reference_id="archive.family-record"),
    )
    database = Database(f"sqlite:///{(tmp_path / 'negative-tick-family.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(scope, NewWorld(
        title="Historical family fixture",
        state=WorldState(
            player_id=parent_id,
            entities={
                parent_id: WorldEntity(id=parent_id, kind="character", name="Ari", location_id=place_id),
                child_id: WorldEntity(id=child_id, kind="character", name="Bea", location_id=place_id),
                place_id: WorldEntity(id=place_id, kind="place", name="Harbor House"),
            },
            kinship_links=[link],
        ),
    ))
    service = FamilyAuthoringApplication(repository)
    try:
        with _client(scope, repository, service) as client:
            response = client.post(
                f"/api/v1/worlds/{created.world.world_id}/author/kinship-links/{link_id}/end",
                headers={"Idempotency-Key": "end-historical-link"},
                json={
                    "branch_id": str(created.branch.branch_id),
                    "expected_state_version": "0",
                    "expected_created_tick": "-86400",
                    "expected_relationship": "birth",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["state_version"] == "1"

            outside_clock = client.post(
                f"/api/v1/worlds/{created.world.world_id}/author/kinship-links/{link_id}/end",
                headers={"Idempotency-Key": "bad-clock"},
                json={
                    "branch_id": str(created.branch.branch_id),
                    "expected_state_version": "1",
                    "expected_created_tick": str(2**63),
                },
            )
            assert outside_clock.status_code == 422

        final = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert final.state.kinship_links[0].created_tick == -86_400
        assert final.state.kinship_links[0].ended_tick == 0
        assert repository.replay(scope, created.world.world_id, created.branch.branch_id).state_hash == final.state_hash
    finally:
        database.dispose()
