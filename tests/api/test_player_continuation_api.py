from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import InventoryComponent, InventoryLot
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EntityFieldChangedEvent,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class RecordingModels:
    def __init__(self):
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append(request)
        return SimpleNamespace(normalized_output={"answer": "The GM can still answer this question.", "references": []})


@pytest.fixture
def continuation_api(tmp_path):
    owner_id, tenant_id = uuid4(), uuid4()
    owner = AccessScope(tenant_id=tenant_id, user_id=owner_id)
    old_player_id, candidate_id, place_id = uuid4(), uuid4(), uuid4()
    account_id, lot_id = uuid4(), uuid4()
    state = WorldState(
        player_id=old_player_id,
        entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Cedar court"),
            old_player_id: WorldEntity(
                id=old_player_id,
                kind="character",
                name="Ari Chen",
                location_id=place_id,
                components={"inventory": InventoryComponent(lots=[InventoryLot(
                    lot_id=lot_id,
                    account_id=account_id,
                    resource_id="core.silver",
                    quantity=Decimal("7"),
                )])},
            ),
            candidate_id: WorldEntity(
                id=candidate_id,
                kind="character",
                name="Jing Zhou",
                location_id=place_id,
                components={"public_role": "herbalist"},
            ),
        },
        resources={account_id: ResourceAccount(
            resource_id="core.silver", balance=Decimal("7"), owner_id=old_player_id,
        )},
    )
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(owner, NewWorld(title="Continuation fixture", state=state))
    worlds = WorldApplication(repository, None)
    models = RecordingModels()
    operations = OperationsStore(tmp_path / "operations.db")
    conversations = ConversationApplication(worlds, models, ConversationStore(operations))
    session = LocalSession(str(owner_id), str(tenant_id), frozenset({"http://testserver"}))
    with TestClient(create_app(worlds, session, conversations=conversations)) as client:
        client.get("/api/v1/session").raise_for_status()
        yield SimpleNamespace(
            client=client,
            database=database,
            repository=repository,
            owner=owner,
            worlds=worlds,
            models=models,
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            old_player_id=old_player_id,
            candidate_id=candidate_id,
            account_id=account_id,
        )
    database.dispose()


def _options(client, world_id: UUID, branch_id: UUID):
    return client.get(
        f"/api/v1/worlds/{world_id}/continuation-options",
        params={"branch_id": str(branch_id)},
    )


def _continue(client, world_id, branch_id, previous_id, new_id, expected_version, key):
    return client.post(
        f"/api/v1/worlds/{world_id}/continue-after-death",
        headers={"Idempotency-Key": key},
        json={
            "branch_id": str(branch_id),
            "previous_player_id": str(previous_id),
            "new_player_id": str(new_id),
            "expected_state_version": str(expected_version),
        },
    )


def _record_death(repository, owner, world_id, branch_id, actor_id, *, key="fixture-death"):
    before = repository.read_world(owner, world_id, branch_id)
    event = EntityFieldChangedEvent(entity_id=actor_id, field_name="status", value="dead")
    result = repository.commit_command(
        owner,
        world_id,
        branch_id,
        CommandEnvelope(
            actor_id=actor_id,
            expected_state_version=before.branch.state_version,
            command_type="fixture.death",
            idempotency_key=key,
        ),
        [event],
    )
    return event, result


def test_continuation_requires_recorded_death_and_is_versioned_idempotent_and_branch_local(continuation_api):
    fixture = continuation_api
    client, repository, owner = fixture.client, fixture.repository, fixture.owner
    world_id, branch_id = fixture.world_id, fixture.branch_id
    old_id, new_id = fixture.old_player_id, fixture.candidate_id
    before = repository.read_world(owner, world_id, branch_id)
    old_inventory = before.state.entities[old_id].model_dump(mode="json")["components"]["inventory"]
    old_balance = before.state.resources[fixture.account_id].balance

    before_death_branch = repository.fork(owner, world_id, branch_id, 0, "Before death")
    options = _options(client, world_id, branch_id)
    assert options.status_code == 200
    assert options.json()["previous_player_id"] == str(old_id)
    assert options.json()["state_version"] == "0"
    assert options.json()["items"] == []
    missing_death = _continue(client, world_id, branch_id, old_id, new_id, 0, "before-death")
    assert missing_death.status_code == 409
    assert missing_death.json()["error"]["code"] == "PLAYER_DEATH_NOT_RECORDED"

    death, _ = _record_death(repository, owner, world_id, branch_id, old_id)
    dead = repository.read_world(owner, world_id, branch_id)
    assert dead.state.entities[old_id].status == "dead"
    assert dead.state.entities[old_id].model_dump(mode="json")["components"]["inventory"] == old_inventory

    earlier_options = _options(client, world_id, before_death_branch.branch_id)
    assert earlier_options.status_code == 200
    assert earlier_options.json()["items"] == []
    no_ancestor_death = _continue(
        client, world_id, before_death_branch.branch_id, old_id, new_id, 0, "branch-without-death",
    )
    assert no_ancestor_death.status_code == 409
    assert no_ancestor_death.json()["error"]["code"] == "PLAYER_DEATH_NOT_RECORDED"

    after_death_branch = repository.fork(owner, world_id, branch_id, dead.last_seq, "After death")
    available = _options(client, world_id, branch_id)
    assert available.status_code == 200
    assert available.json()["state_version"] == "1"
    assert available.json()["items"] == [{"actor_id": str(new_id), "name": "Jing Zhou", "role": "herbalist"}]
    stale_version = _continue(client, world_id, branch_id, old_id, new_id, 0, "stale-version")
    assert stale_version.status_code == 409
    assert stale_version.json()["error"]["code"] == "STALE_VERSION"

    first = _continue(client, world_id, branch_id, old_id, new_id, 1, "continue-exact")
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt["branch_id"] == str(branch_id)
    assert receipt["player_id"] == str(new_id)
    assert receipt["state_version"] == "2"
    retry = _continue(client, world_id, branch_id, old_id, new_id, 1, "continue-exact")
    assert retry.status_code == 200 and retry.json() == receipt
    mismatch = _continue(client, world_id, branch_id, old_id, uuid4(), 1, "continue-exact")
    assert mismatch.status_code == 409

    continued = repository.read_world(owner, world_id, branch_id)
    assert continued.state.player_id == new_id
    assert continued.state.entities[old_id].status == "dead"
    assert continued.state.entities[old_id].model_dump(mode="json")["components"]["inventory"] == old_inventory
    assert continued.state.resources[fixture.account_id].balance == old_balance
    assert continued.last_seq == dead.last_seq + 1
    assert continued.state.current_tick == dead.state.current_tick

    # A sibling fork at the same death boundary has its own player pointer and receipt namespace.
    sibling = repository.read_world(owner, world_id, after_death_branch.branch_id)
    assert sibling.state.player_id == old_id and sibling.state.entities[old_id].status == "dead"
    sibling_options = _options(client, world_id, after_death_branch.branch_id).json()
    assert [item["actor_id"] for item in sibling_options["items"]] == [str(new_id)]
    sibling_result = _continue(
        client, world_id, after_death_branch.branch_id, old_id, new_id, 1, "continue-exact",
    )
    assert sibling_result.status_code == 200, sibling_result.text
    assert sibling_result.json()["branch_id"] == str(after_death_branch.branch_id)
    assert sibling_result.json()["command_id"] != receipt["command_id"]
    assert repository.read_world(owner, world_id, after_death_branch.branch_id).state.player_id == new_id
    assert repository.read_world(owner, world_id, before_death_branch.branch_id).state.player_id == old_id
    persisted_events = repository.list_events(owner, world_id, branch_id, limit=10).events
    assert death.event_id in {event.event_id for event in persisted_events}


def _message(fixture, actor_id: UUID, *, mode: str, text: str):
    return fixture.client.post(
        f"/api/v1/worlds/{fixture.world_id}/messages",
        json={
            "request_id": str(uuid4()),
            "world_id": str(fixture.world_id),
            "branch_id": str(fixture.branch_id),
            "session_id": str(uuid4()),
            "actor_id": str(actor_id),
            "expected_state_version": "1",
            "mode": mode,
            "text": text,
        },
    )


def test_dead_player_cannot_roleplay_or_act_but_can_query_then_new_player_owns_conversation(continuation_api):
    fixture = continuation_api
    _record_death(
        fixture.repository, fixture.owner, fixture.world_id, fixture.branch_id, fixture.old_player_id,
    )
    before = fixture.repository.read_world(fixture.owner, fixture.world_id, fixture.branch_id)
    old_inventory = before.state.entities[fixture.old_player_id].model_dump(mode="json")["components"]["inventory"]

    dead_roleplay = _message(fixture, fixture.old_player_id, mode="roleplay", text="我想和 Jing 说话。")
    dead_action = _message(fixture, fixture.old_player_id, mode="action", text="我继续行动。")
    assert dead_roleplay.status_code == dead_action.status_code == 200
    assert dead_roleplay.json()["kind"] == dead_action.json()["kind"] == "clarification"
    assert not dead_roleplay.json()["world_effect"] and not dead_action.json()["world_effect"]
    assert fixture.models.calls == []

    gm_query = _message(fixture, fixture.old_player_id, mode="ask_gm", text="请说明当前角色的记录状态。")
    assert gm_query.status_code == 200
    assert gm_query.json()["kind"] == "query" and not gm_query.json()["world_effect"]
    assert len(fixture.models.calls) == 1

    response = _continue(
        fixture.client, fixture.world_id, fixture.branch_id, fixture.old_player_id,
        fixture.candidate_id, 1, "continue-for-conversation",
    )
    assert response.status_code == 200, response.text
    refreshed = fixture.client.get(
        f"/api/v1/worlds/{fixture.world_id}/view",
        params={"branch_id": str(fixture.branch_id), "mode": "player"},
    ).raise_for_status().json()
    assert refreshed["state"]["player_id"] == str(fixture.candidate_id)
    current_conversation = fixture.client.get(
        f"/api/v1/worlds/{fixture.world_id}/conversation",
        params={"branch_id": str(fixture.branch_id)},
    ).raise_for_status().json()
    assert current_conversation["actor_id"] == str(fixture.candidate_id)
    assert current_conversation["items"] == []
    after = fixture.repository.read_world(fixture.owner, fixture.world_id, fixture.branch_id)
    assert after.state.entities[fixture.old_player_id].status == "dead"
    assert after.state.entities[fixture.old_player_id].model_dump(mode="json")["components"]["inventory"] == old_inventory

    old_actor_retry = _message(fixture, fixture.old_player_id, mode="roleplay", text="我再试一次交谈。")
    assert old_actor_retry.status_code == 403
    assert len(fixture.models.calls) == 1
    assert fixture.repository.read_world(fixture.owner, fixture.world_id, fixture.branch_id).state.player_id == fixture.candidate_id


def test_formal_v2_restore_replays_continuation_pointer_and_older_sibling(continuation_api, tmp_path):
    fixture = continuation_api
    repository = fixture.repository
    owner = fixture.owner
    world_id, main_branch_id = fixture.world_id, fixture.branch_id
    previous_id, new_id = fixture.old_player_id, fixture.candidate_id

    death, _ = _record_death(repository, owner, world_id, main_branch_id, previous_id)
    dead_head = repository.read_world(owner, world_id, main_branch_id)
    sibling = repository.fork(owner, world_id, main_branch_id, dead_head.last_seq, "Before continuation")
    continuation = _continue(
        fixture.client, world_id, main_branch_id, previous_id, new_id, 1, "archive-continuation",
    )
    assert continuation.status_code == 200, continuation.text

    source_main = repository.replay(owner, world_id, main_branch_id, from_genesis=True)
    source_sibling = repository.replay(owner, world_id, sibling.branch_id, from_genesis=True)
    assert source_main.state.player_id == new_id
    assert source_sibling.state.player_id == previous_id
    assert source_sibling.state.entities[previous_id].status == "dead"

    backup = BackupApplication(repository)
    archive_bytes = backup.export_world(owner, world_id, format_version="2.0")
    destination_db = Database(f"sqlite:///{(tmp_path / 'restored-world.db').as_posix()}")
    destination_db.create_schema()
    try:
        destination = WorldRepository(destination_db)
        with backup.validate_archive(archive_bytes) as verified:
            assert verified.format_version == "2.0"
            import_result = WorldBackupImporter(destination).import_verified_backup(owner, verified)
            assert import_result.branch_count == 2
            assert import_result.event_count == 2

        restored_main = destination.replay(owner, world_id, main_branch_id, from_genesis=True)
        restored_sibling = destination.replay(owner, world_id, sibling.branch_id, from_genesis=True)
        assert restored_main.state_hash == source_main.state_hash
        assert restored_sibling.state_hash == source_sibling.state_hash
        assert restored_main.state.player_id == new_id
        assert restored_sibling.state.player_id == previous_id
        assert restored_sibling.state.entities[previous_id].status == "dead"

        main_events = destination.list_events(owner, world_id, main_branch_id, limit=10).events
        continuation_events = [event for event in main_events if event.type == "core.player_character_continued.v1"]
        assert len(continuation_events) == 1
        continued_payload = continuation_events[0].payload
        assert continued_payload["previous_player_id"] == str(previous_id)
        assert continued_payload["new_player_id"] == str(new_id)
        assert continued_payload["source_death_event_id"] == str(death.event_id)

        sibling_events = destination.list_events(owner, world_id, sibling.branch_id, limit=10).events
        assert not any(event.type == "core.player_character_continued.v1" for event in sibling_events)
        assert destination.read_world(owner, world_id, sibling.branch_id).branch.name == "Before continuation"
    finally:
        destination_db.dispose()
