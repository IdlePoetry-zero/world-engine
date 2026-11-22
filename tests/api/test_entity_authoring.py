from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.entity_authoring import EntityAuthoringApplication
from world_engine.application.operations import OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.environmental_source_events import EnvironmentalSourceAccountOpenedEvent
from world_engine.domain.errors import AccessDenied, InvalidEvent, NotFound
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import ResourceAccountOpenedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.application.backups import BackupApplication
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def entity_authoring(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, region_id, grain_account_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(
                id=player_id,
                kind="character",
                name="Mira",
                location_id=region_id,
                components={"inventory": {"lots": []}, "public_role": "resident"},
            ),
            region_id: WorldEntity(
                id=region_id,
                kind="place",
                name="River Country",
                components={"place": {"place_type": "region", "access_policy": "open"}},
            ),
        },
        resources={
            grain_account_id: ResourceAccount(
                resource_id="food.grain", balance="0", owner_id=player_id,
            ),
        },
        metadata={"resource_catalog": {"food.grain": "Grain"}},
    )
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(scope, NewWorld(title="Entity authoring fixture", state=state))
    authoring = AuthoringApplication(repository, OperationsStore(tmp_path / "operations.db"))
    entity_authoring = EntityAuthoringApplication(repository, authoring.operations)
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=authoring)
    yield scope, repository, created.world.world_id, created.branch.branch_id, player_id, region_id, app, entity_authoring
    database.dispose()


def test_place_preview_commit_and_replay_are_version_pinned_and_idempotent(entity_authoring):
    scope, repository, world, branch, player_id, region_id, app, _ = entity_authoring
    before = repository.read_world(scope, world, branch)
    path = f"/api/v1/worlds/{world}/author/places"
    draft = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "name": "River Market",
        "place_type": "settlement",
        "access_policy": "restricted",
        "region_id": str(region_id),
        "description": "A small riverside market.",
        "environment": {"sound": "busy", "bridge_count": 2},
        "routes": [{
            "to_place_id": str(region_id),
            "travel_ticks": 120,
            "risk": "0.1",
            "access_tags": [],
            "travel_modes": ["foot"],
            "is_open": True,
        }],
    }

    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post(path + "/preview", json=draft).raise_for_status().json()
        assert preview["world_time_effect"] is False
        assert preview["entity"]["kind"] == "place"
        assert preview["entity"]["components"]["place"]["region_id"] == str(region_id)
        assert preview["entity"]["components"]["place"]["routes"][0]["to_place_id"] == str(region_id)
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash

        commit_body = {"preview_id": preview["preview_id"]}
        first = client.post(path + "/commit", json=commit_body).raise_for_status().json()
        second = client.post(path + "/commit", json=commit_body).raise_for_status().json()

    assert first["entity_id"] == preview["entity_id"]
    assert first["state_version"] == "1" and first["duplicate"] is False
    assert second["state_version"] == "1" and second["duplicate"] is True
    after = repository.read_world(scope, world, branch)
    assert after.state.current_tick == before.state.current_tick
    assert after.state.entities[UUID(preview["entity_id"])].name == "River Market"
    assert repository.replay(scope, world, branch).state_hash == after.state_hash


def test_place_harvest_opens_finite_source_and_replays_atomic_bundle(entity_authoring):
    scope, repository, world, branch, player_id, _, app, _ = entity_authoring
    path = f"/api/v1/worlds/{world}/author/places"
    draft = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "name": "New Grain Reach",
        "place_type": "region",
        "access_policy": "open",
        "renewable_harvests": [{
            "policy_id": "harvest.grain",
            "resource_id": "food.grain",
            "period_ticks": 86400,
            "maximum_total": "12",
            "recipients": [{
                "entity_id": str(player_id),
                "account_id": str(next(iter(repository.read_world(scope, world, branch).state.resources))),
                "per_period": "3",
            }],
        }],
    }

    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post(path + "/preview", json=draft).raise_for_status().json()
        source = preview["created_accounts"][0]
        place = preview["entity"]["components"]["place"]
        assert source["account_type"] == "environmental_source"
        assert source["balance"] == source["credit_limit"] == "0"
        assert place["renewable_harvests"][0]["source_account_id"] == source["account_id"]
        result = client.post(path + "/commit", json={"preview_id": preview["preview_id"]}).raise_for_status().json()

    after = repository.read_world(scope, world, branch)
    source_account = after.state.resources[UUID(source["account_id"])]
    assert result["state_version"] == "1"
    assert after.last_seq == 3
    assert source_account.account_type == "environmental_source"
    assert source_account.balance == source_account.credit_limit == 0
    assert repository.replay(scope, world, branch).state_hash == after.state_hash


def test_environmental_source_creation_bundle_survives_archive_import_and_replay(entity_authoring, tmp_path):
    scope, repository, world, branch, player_id, _, app, _ = entity_authoring
    path = f"/api/v1/worlds/{world}/author/places"
    account_id = next(iter(repository.read_world(scope, world, branch).state.resources))
    draft = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "name": "Archive Ready Region",
        "place_type": "region",
        "access_policy": "open",
        "renewable_harvests": [{
            "policy_id": "archive.grain",
            "resource_id": "food.grain",
            "period_ticks": 86400,
            "maximum_total": "4",
            "recipients": [{
                "entity_id": str(player_id),
                "account_id": str(account_id),
                "per_period": "1",
            }],
        }],
    }
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post(path + "/preview", json=draft).raise_for_status().json()
        client.post(path + "/commit", json={"preview_id": preview["preview_id"]}).raise_for_status()

    expected = repository.read_world(scope, world, branch)
    archive_app = BackupApplication(repository)
    archive = archive_app.export_world(scope, world)
    destination_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    destination_db.create_schema()
    destination = WorldRepository(destination_db)
    try:
        with archive_app.validate_archive(archive) as verified:
            WorldBackupImporter(destination).import_verified_backup(scope, verified)
        restored = destination.replay(scope, world, branch, from_genesis=True)
        assert restored.state_hash == expected.state_hash
        restored_source = restored.state.resources[UUID(preview["created_accounts"][0]["account_id"])]
        assert restored_source.account_type == "environmental_source"
        assert restored_source.balance == restored_source.credit_limit == 0
    finally:
        destination_db.dispose()


def test_author_permissions_allow_editors_and_reject_players(entity_authoring):
    scope, repository, world, branch, player_id, region_id, _, service = entity_authoring
    editor_scope = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    player_scope = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    repository.grant_membership(scope, world, editor_scope.user_id, MembershipRole.EDITOR)
    repository.grant_membership(
        scope, world, player_scope.user_id, MembershipRole.PLAYER, controlled_actor_ids=[player_id],
    )
    draft = {
        "name": "Editor Place",
        "place_type": "site",
        "access_policy": "open",
        "description": "Created by an authorized editor.",
    }

    preview = service.preview_place(editor_scope, world, branch, 0, draft)
    committed = service.commit_place(editor_scope, world, UUID(preview["preview_id"]))
    assert committed["entity_id"] == preview["entity_id"]
    with pytest.raises(AccessDenied):
        service.preview_place(player_scope, world, branch, 1, draft | {"name": "Player Place"})


def test_preview_is_scoped_to_the_principal_that_created_it(entity_authoring):
    scope, repository, world, branch, _, _, _, service = entity_authoring
    editor_scope = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    repository.grant_membership(scope, world, editor_scope.user_id, MembershipRole.EDITOR)
    preview = service.preview_place(scope, world, branch, 0, {
        "name": "Owner Draft",
        "place_type": "site",
        "access_policy": "open",
        "description": "Kept in the owner's preview namespace.",
    })

    with pytest.raises(NotFound):
        service.commit_place(editor_scope, world, UUID(preview["preview_id"]))
    assert repository.read_world(scope, world, branch).branch.state_version == 0


def test_environmental_source_account_event_is_rejected_outside_place_authoring(entity_authoring):
    scope, repository, world, branch, _, region_id, _, _ = entity_authoring
    event = EnvironmentalSourceAccountOpenedEvent(
        account_id=uuid4(), owner_id=region_id, resource_id="food.grain",
    )
    command = CommandEnvelope(
        actor_id=scope.user_id,
        command_type="arbitrary.entity_setup",
        idempotency_key="unscoped-environmental-source",
        expected_state_version=0,
    )

    with pytest.raises(AccessDenied, match="explicit place authorship"):
        repository.commit_command(scope, world, branch, command, [event])


def test_creation_rejects_unreferenced_account_without_partial_commit(entity_authoring):
    scope, repository, world, branch, _, _, _, service = entity_authoring
    before = repository.read_world(scope, world, branch)
    preview = service.preview_organization(scope, world, branch, 0, {
        "name": "Assembly", "kind": "organization", "treasury_resources": ["food.grain"],
    })
    preview_id = UUID(preview["preview_id"])
    document = service._load_preview(scope, world, preview_id)
    events = [parse_event(event) for event in document["events"]]
    events.insert(-1, ResourceAccountOpenedEvent(
        account_id=uuid4(), owner_id=UUID(preview["entity_id"]), resource_id="water",
    ))
    command = CommandEnvelope(
        command_id=preview_id, actor_id=scope.user_id, command_type="author.create_organization",
        idempotency_key=f"author:{preview_id}", expected_state_version=0,
    )
    with pytest.raises(InvalidEvent, match="exactly match"):
        repository.commit_command(scope, world, branch, command, events)
    after = repository.read_world(scope, world, branch)
    assert (after.last_seq, after.state_hash) == (before.last_seq, before.state_hash)


def test_organization_preview_creates_only_requested_zero_balance_treasury_accounts(entity_authoring):
    scope, repository, world, branch, player_id, region_id, app, _ = entity_authoring
    before = repository.read_world(scope, world, branch)
    path = f"/api/v1/worlds/{world}/author/organizations"
    draft = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "name": "Riverside Cooperative",
        "kind": "organization",
        "location_id": str(region_id),
        "public_role": "Shared grain storage",
        "description": "A cooperative for local storage.",
        "governance": {
            "charter": "Members decide storage terms together.",
            "members": [str(player_id)],
            "leader_id": str(player_id),
            "offices": [],
            "succession_policy": "election",
            "decision_threshold": "0.6",
            "authority_tags": [],
            "charter_clause_ids": [],
            "membership_recruitment": [],
        },
        "treasury_resources": ["food.grain"],
    }

    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post(path + "/preview", json=draft).raise_for_status().json()
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash
        assert preview["entity"]["components"]["governance"]["leader_id"] == str(player_id)
        treasury = preview["entity"]["components"]["treasury"]
        account_id = treasury["accounts"]["food.grain"]
        assert preview["created_accounts"] == [{
            "account_id": account_id,
            "resource_id": "food.grain",
            "owner_id": preview["entity_id"],
            "account_type": "holder",
            "balance": "0",
        }]
        commit_body = {"preview_id": preview["preview_id"]}
        first = client.post(path + "/commit", json=commit_body).raise_for_status().json()
        second = client.post(path + "/commit", json=commit_body).raise_for_status().json()

    after = repository.read_world(scope, world, branch)
    account = after.state.resources[UUID(account_id)]
    assert account.account_type == "holder" and account.balance == 0
    assert first["state_version"] == second["state_version"] == "1"
    assert first["duplicate"] is False and second["duplicate"] is True
    assert after.state.current_tick == before.state.current_tick
    assert repository.replay(scope, world, branch).state_hash == after.state_hash


def test_organization_rejects_noncharacter_or_missing_governance_members(entity_authoring):
    scope, repository, world, branch, _, region_id, app, _ = entity_authoring
    path = f"/api/v1/worlds/{world}/author/organizations/preview"
    base = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "name": "Invalid Membership",
        "kind": "polity",
        "location_id": str(region_id),
        "governance": {"members": [str(region_id)]},
    }

    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        response = client.post(path, json=base)
        assert response.status_code == 422
        missing = client.post(path, json=base | {"governance": {"members": [str(uuid4())]}})
        assert missing.status_code == 422

    assert repository.read_world(scope, world, branch).branch.state_version == 0


def test_place_rejects_unknown_harvest_resource(entity_authoring):
    scope, repository, world, branch, player_id, _, app, _ = entity_authoring
    path = f"/api/v1/worlds/{world}/author/places"
    draft = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "name": "Unlisted Region",
        "place_type": "region",
        "access_policy": "open",
        "renewable_harvests": [{
            "policy_id": "harvest.unknown",
            "resource_id": "unlisted.resource",
            "period_ticks": 86400,
            "maximum_total": "10",
            "recipients": [{
                "entity_id": str(player_id),
                "account_id": str(next(iter(repository.read_world(scope, world, branch).state.resources))),
                "per_period": "1",
            }],
        }],
    }
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        assert client.post(path + "/preview", json=draft).status_code == 422


def test_competing_entity_previews_cannot_commit_against_changed_version(entity_authoring):
    scope, repository, world, branch, _, _, app, _ = entity_authoring
    path = f"/api/v1/worlds/{world}/author/places"
    base = {
        "branch_id": str(branch),
        "expected_state_version": "0",
        "place_type": "site",
        "access_policy": "open",
    }
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        first = client.post(path + "/preview", json=base | {"name": "First Place"}).raise_for_status().json()
        second = client.post(path + "/preview", json=base | {"name": "Second Place"}).raise_for_status().json()
        client.post(path + "/commit", json={"preview_id": second["preview_id"]}).raise_for_status()
        stale = client.post(path + "/commit", json={"preview_id": first["preview_id"]})
        assert stale.status_code == 409

    after = repository.read_world(scope, world, branch)
    assert after.branch.state_version == 1
    assert UUID(first["entity_id"]) not in after.state.entities
    assert UUID(second["entity_id"]) in after.state.entities


def test_request_models_reject_client_generated_account_and_route_ids():
    from world_engine.api.entity_authoring import PlaceDraftRequest, OrganizationDraftRequest

    branch = uuid4()
    with pytest.raises(ValueError):
        PlaceDraftRequest.model_validate({
            "branch_id": branch,
            "expected_state_version": "0",
            "name": "Harbor",
            "place_type": "site",
            "access_policy": "open",
            "routes": [{
                "route_id": str(uuid4()),
                "from_place_id": str(uuid4()),
                "to_place_id": str(uuid4()),
                "travel_ticks": 5,
                "risk": "0",
                "is_open": True,
            }],
        })
    with pytest.raises(ValueError):
        OrganizationDraftRequest.model_validate({
            "branch_id": branch,
            "expected_state_version": "0",
            "name": "Guild",
            "kind": "organization",
            "treasury_resources": ["food.grain"],
            "accounts": {"food.grain": str(uuid4())},
        })
    with pytest.raises(ValueError):
        OrganizationDraftRequest.model_validate({
            "branch_id": branch,
            "expected_state_version": "0",
            "name": "Guild",
            "kind": "organization",
            "governance": {"revision": 7, "charter": "Author-written."},
        })
