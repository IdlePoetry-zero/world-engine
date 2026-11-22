from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.visibility import project_event
from world_engine.application.worlds import WorldApplication
from world_engine.domain.errors import Conflict
from world_engine.domain.models import (
    AccessScope, CommandEnvelope, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState, utc_now,
)
from world_engine.storage.database import Database
from world_engine.storage.event_rows import append_event_row
from world_engine.storage.models import BranchRow, EventPayloadRow, EventRow
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def history_repository(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'history.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        yield repository
    finally:
        database.dispose()


def _new_world(repository):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, place_id = uuid4(), uuid4()
    created = repository.create_world(scope, NewWorld(
        title="Player history payload test",
        state=WorldState(
            player_id=player_id,
            entities={
                player_id: WorldEntity(
                    id=player_id, kind="character", name="Reader", location_id=place_id,
                ),
                place_id: WorldEntity(id=place_id, kind="place", name="Archive"),
            },
        ),
    ))
    repository.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        CommandEnvelope(
            actor_id=player_id, idempotency_key="history-parent-event",
            expected_state_version=0, command_type="core.tick",
        ),
        [TimeAdvancedEvent(to_tick=1)],
    )
    child = repository.fork(
        scope, created.world.world_id, created.branch.branch_id, 1, "history-child",
    )
    return scope, created.world.world_id, child.branch_id, player_id


def _append_raw_event(
    repository,
    *,
    world_id,
    branch_id,
    sequence,
    player_id,
    event_type="core.background_routine_advanced.v1",
    visibility=None,
    payload=None,
):
    row = EventRow(
        branch_id=branch_id,
        sequence=sequence,
        event_id=uuid4(),
        world_id=world_id,
        tick=0,
        event_type=event_type,
        command_id=uuid4(),
        actor_id=player_id,
        causation_json=[],
        correlation_id=None,
        ruleset_version="test.rules.v1",
        payload_json=payload or {"large_private_fact": "x" * 90_000},
        visibility_json=visibility or {"policy": "author_only"},
        state_version_after=1,
        commit_boundary=True,
        committed_at=utc_now(),
    )
    with repository._transaction(write=True) as session:
        append_event_row(session, row)
        branch = session.get(BranchRow, branch_id)
        branch.head_seq = sequence
        session.flush()


def _project(page, actor_id):
    result = []
    for event in page.events:
        projected = project_event(event.model_dump(mode="json"), actor_id, entities={})
        if projected is not None:
            result.append(projected | {"branch_id": event.branch_id, "can_fork": False})
    return result


def test_player_history_skips_only_exact_hidden_types_and_keeps_ancestor_pagination(history_repository):
    repository = history_repository
    scope, world_id, branch_id, player_id = _new_world(repository)
    actor = str(player_id)
    _append_raw_event(repository, world_id=world_id, branch_id=branch_id, sequence=2,
                      player_id=player_id, event_type="core.background_routine_advanced.v1")
    _append_raw_event(repository, world_id=world_id, branch_id=branch_id, sequence=3,
                      player_id=player_id, event_type="core.renewable_resource_harvested.v1")
    _append_raw_event(
        repository, world_id=world_id, branch_id=branch_id, sequence=4,
        player_id=player_id, visibility={"policy": "public"},
        payload={"large_private_fact": "x" * 90_000, "audience_views": {"public": {"text": "public"}}},
    )
    _append_raw_event(
        repository, world_id=world_id, branch_id=branch_id, sequence=5,
        player_id=player_id, visibility={"policy": "subject_ids", "subject_ids": [actor]},
        payload={"large_private_fact": "x" * 90_000, "audience_views": {actor: {"text": "for player"}}},
    )
    _append_raw_event(
        repository, world_id=world_id, branch_id=branch_id, sequence=6,
        player_id=player_id, event_type="core.renewable_resource_harvested.v1",
        visibility={"policy": "subject_ids", "subject_ids": [str(uuid4())]},
        payload={"large_private_fact": "x" * 90_000, "audience_views": {actor: {"text": "must stay private"}}},
    )
    _append_raw_event(
        repository, world_id=world_id, branch_id=branch_id, sequence=7,
        player_id=player_id, event_type="core.renewable_resource_harvested.v1",
        visibility={"unexpected": "malformed"}, payload={"large_private_fact": "x" * 90_000},
    )

    expected_projected = []
    for after in (0, 3, 6):
        baseline = repository.list_events(
            scope, world_id, branch_id, after_seq=after, limit=3, include_ancestors=True,
        )
        optimized = repository.list_player_history_events(
            scope, world_id, branch_id, after_seq=after, limit=3, include_ancestors=True,
        )
        assert [event.sequence for event in optimized.events] == [event.sequence for event in baseline.events]
        assert optimized.next_cursor == baseline.next_cursor
        assert optimized.has_more == baseline.has_more
        assert _project(optimized, actor) == _project(baseline, actor)
        for old_event, new_event in zip(baseline.events, optimized.events, strict=True):
            if old_event.visibility != {"policy": "author_only"}:
                assert new_event.payload == old_event.payload
        expected_projected.extend(_project(optimized, actor))

    assert expected_projected == []
    assert repository.list_player_history_events(
        scope, world_id, branch_id, after_seq=0, limit=3, include_ancestors=False,
    ).next_cursor == 4

    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session)
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        response = client.get(
            f"/api/v1/worlds/{world_id}/history",
            params={"branch_id": str(branch_id), "mode": "player"},
        )
        assert response.status_code == 200
        assert response.json()["items"] == expected_projected
        assert response.json()["next_cursor"] is None


def test_player_history_skips_corrupt_exact_author_only_payload_but_falls_back_for_other_visibility(history_repository):
    repository = history_repository
    scope, world_id, branch_id, player_id = _new_world(repository)
    _append_raw_event(
        repository, world_id=world_id, branch_id=branch_id, sequence=2,
        player_id=player_id,
    )
    with repository._transaction(write=True) as session:
        row = session.get(EventRow, (branch_id, 2))
        sidecar = session.get(EventPayloadRow, row.event_id)
        assert sidecar is not None
        sidecar.compressed_bytes = b"corrupt"

    page = repository.list_player_history_events(scope, world_id, branch_id)
    assert page.next_cursor == 2
    assert page.events[-1].payload == {}
    with pytest.raises(Conflict, match="compressed event payload failed verification"):
        repository.list_events(scope, world_id, branch_id)

    for visibility in ({"policy": "public"}, {"unexpected": "malformed"}):
        with repository._transaction(write=True) as session:
            row = session.get(EventRow, (branch_id, 2))
            row.visibility_json = visibility
        with pytest.raises(Conflict, match="compressed event payload failed verification"):
            repository.list_player_history_events(scope, world_id, branch_id)


def test_player_history_still_decodes_other_author_only_events(history_repository, monkeypatch):
    repository = history_repository
    scope, world_id, branch_id, player_id = _new_world(repository)
    _append_raw_event(
        repository, world_id=world_id, branch_id=branch_id, sequence=2,
        player_id=player_id, event_type="core.promise_item_returned.v2",
        payload={"settlement": {"player_safe": True}},
    )
    decoded = []
    original = __import__("world_engine.storage.repository", fromlist=["read_event_payload"]).read_event_payload

    def observe(row, **kwargs):
        decoded.append(row.event_type)
        return original(row, **kwargs)

    monkeypatch.setattr("world_engine.storage.repository.read_event_payload", observe)
    page = repository.list_player_history_events(scope, world_id, branch_id)
    assert page.events[-1].type == "core.promise_item_returned.v2"
    assert page.events[-1].payload == {"settlement": {"player_safe": True}}
    assert "core.promise_item_returned.v2" in decoded
