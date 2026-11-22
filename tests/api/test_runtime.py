from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
import sqlite3
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from world_engine.runtime import build_application, local_identity
from world_engine.domain.errors import StorageBusy
from world_engine.domain.models import CommandEnvelope, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState, utc_now


class EmptyTestCatalog:
    def list_packs(self):
        return []


def test_ruleset_upgrade_restores_same_history_and_records_new_events_under_target_version(tmp_path):
    app = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog(), run_simulation_worker=False)
    owner = local_identity(tmp_path)
    actor_id = uuid4()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        repository = app.state.worlds.repository
        state = WorldState(player_id=actor_id, entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Aster")})
        created = repository.create_world(owner, NewWorld(title="Rules source", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        repository.commit_command(owner, world_id, branch_id, CommandEnvelope(
            actor_id=actor_id, idempotency_key="before-upgrade", expected_state_version=0, command_type="core.tick",
        ), [TimeAdvancedEvent(to_tick=1)])
        before = repository.read_world(owner, world_id, branch_id)
        archive_uri = f"/api/v1/worlds/{world_id}/backup?format_version=2.0"
        source_archive = client.get(archive_uri).raise_for_status().content
        upload = {"Content-Type": "application/zip"}
        preview = client.post("/api/v1/restores/preview", headers=upload, content=source_archive).raise_for_status().json()
        upgrade, = preview["ruleset_upgrades"]
        assert preview["ruleset_version"] == "1.0.0" and upgrade["target_version"] == "1.1.0"
        chosen = upload | {"Target-Ruleset-Version": "1.1.0", "Preview-Id": upgrade["preview_id"]}
        assert client.post("/api/v1/restores", headers=chosen | {"Preview-Id": preview["preview_id"]},
                           content=source_archive).status_code == 409
        assert client.post("/api/v1/restores", headers=chosen | {"Target-Ruleset-Version": "1.2.0"},
                           content=source_archive).status_code == 422
        assert not (tmp_path / "restored").exists()
        pinned = client.post("/api/v1/restores", headers=upload | {"Preview-Id": preview["preview_id"]},
                             content=source_archive).raise_for_status().json()
        upgraded = client.post("/api/v1/restores", headers=chosen, content=source_archive).raise_for_status().json()
        retry = client.post("/api/v1/restores", headers=chosen, content=source_archive).raise_for_status().json()
        assert upgraded["ruleset_version"] == "1.1.0" and upgraded["source_ruleset_version"] == "1.0.0"
        assert retry["duplicate"] and retry["data_directory"] == upgraded["data_directory"]
        assert pinned["ruleset_version"] == "1.0.0" and pinned["data_directory"] != upgraded["data_directory"]
        assert repository.read_world(owner, world_id, branch_id) == before
    copy_path = Path(upgraded["data_directory"])
    copy_owner = local_identity(copy_path)
    copy = build_application(data_directory=copy_path, catalog=EmptyTestCatalog(), port=8001, run_simulation_worker=False)
    with TestClient(copy, base_url="http://127.0.0.1:8001") as client:
        client.get("/api/v1/session").raise_for_status()
        repository = copy.state.worlds.repository
        assert repository.replay(copy_owner, world_id, branch_id, from_genesis=True).state_hash == before.state_hash
        assert repository.read_world(copy_owner, world_id, branch_id).world.ruleset_version == "1.1.0"
        repository.commit_command(copy_owner, world_id, branch_id, CommandEnvelope(
            actor_id=actor_id, idempotency_key="after-upgrade", expected_state_version=1, command_type="core.tick",
        ), [TimeAdvancedEvent(to_tick=2)])
        assert [event.ruleset_version for event in repository.list_events(copy_owner, world_id, branch_id).events] == ["1.0.0", "1.1.0"]
        replay = repository.replay(copy_owner, world_id, branch_id, from_genesis=True)
        assert replay.state.current_tick == 2
        new_archive = client.get(archive_uri).raise_for_status().content
        checked = client.post("/api/v1/restores/preview", headers=upload, content=new_archive).raise_for_status().json()
        assert checked["ruleset_version"] == "1.1.0" and checked["ruleset_upgrades"] == []
        assert checked["state_hashes"] == {str(branch_id): replay.state_hash}


def test_identity_publication_is_atomic_and_corruption_is_not_replaced(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: local_identity(tmp_path), range(20)))
    assert len({(item.tenant_id, item.user_id) for item in identities}) == 1
    assert list(tmp_path.glob("*.tmp")) == []
    target = tmp_path / "local-identity.json"
    target.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        local_identity(tmp_path)
    assert target.read_text() == "broken"


def test_runtime_applies_migrations_and_preserves_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("WORLD_ENGINE_DATABASE_URL", raising=False)
    app = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        with pytest.raises(RuntimeError, match="Another local"):
            build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
        identity = client.get("/api/v1/session").json()
        providers = client.get("/api/v1/providers").json()["items"]
        assert {p["provider_id"] for p in providers} == {"deepseek", "ollama"}
        assert client.get("/api/v1/worlds").json() == {"items": []}
        assert client.put("/api/v1/settings", json={"local_only": True, "routes": {}}).status_code == 200
    with sqlite3.connect(tmp_path / "world-engine.db") as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()
    second = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
    with TestClient(second, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/session").json() == identity
        settings = client.get("/api/v1/settings").json()
        assert settings["local_only"] and settings["routes"] == {}
        assert len(client.get("/api/v1/providers").json()["items"]) == 2
    # Only opaque IDs are stored in the identity document, never session cookies.
    assert set(json.loads((tmp_path / "local-identity.json").read_text())) == {"tenant_id", "user_id"}


@pytest.mark.parametrize("expired_lease", [False, True])
def test_runtime_worker_picks_up_http_world_advance(tmp_path, expired_lease):
    app = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog(), run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        world = repo.create_world(scope, NewWorld(title="Clock worker", state=WorldState()))
        wid, bid = world.world.world_id, world.branch.branch_id
        job = client.post(f"/api/v1/worlds/{wid}/world-advance", headers={"Idempotency-Key": "runtime"}, json={
            "branch_id": str(bid), "expected_state_version": "0", "duration_seconds": 60,
        }).raise_for_status().json()
        assert job["status"] == "queued"
        if expired_lease:
            abandoned = repo.claim_job("abandoned-worker", lease_seconds=1, now=utc_now() - timedelta(seconds=5))
            assert abandoned is not None and str(abandoned.job_id) == job["job_id"]

    restarted = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
    with TestClient(restarted, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        repo = restarted.state.worlds.repository
        deadline = monotonic() + 5
        while monotonic() < deadline:
            status = client.get(f"/api/v1/jobs/{job['job_id']}").raise_for_status().json()
            if status["status"] not in {"queued", "running"}:
                break
            sleep(0.05)
        assert status["status"] == "succeeded"
        assert status["committed_through_tick"] == status["target_tick"] == "60"
        view = repo.read_world(scope, wid, bid)
        assert view.last_seq == 1 and view.state_hash == repo.replay(scope, wid, bid).state_hash


def test_runtime_worker_retries_one_locked_window_using_its_existing_lease(tmp_path, monkeypatch):
    app = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
    repository = app.state.worlds.repository
    scope = local_identity(tmp_path)
    world = repository.create_world(scope, NewWorld(title="Transient contention", state=WorldState()))
    wid, bid = world.world.world_id, world.branch.branch_id
    lock_seen = Event()

    @event.listens_for(repository.database.engine, "checkout")
    def shortened_checkout(connection, *_):
        connection.execute("PRAGMA busy_timeout=20")

    commit = repository.commit_command

    def contend_on_first_commit(*args, **kwargs):
        if lock_seen.is_set():
            return commit(*args, **kwargs)
        with closing(sqlite3.connect(tmp_path / "world-engine.db")) as holder:
            holder.execute("BEGIN IMMEDIATE")
            with pytest.raises(StorageBusy) as busy:
                commit(*args, **kwargs)
            lock_seen.set()
            raise busy.value

    monkeypatch.setattr(repository, "commit_command", contend_on_first_commit)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        queued = client.post(f"/api/v1/worlds/{wid}/world-advance", headers={"Idempotency-Key": "locked-window"}, json={
            "branch_id": str(bid), "expected_state_version": "0", "duration_seconds": 60,
        }).raise_for_status().json()
        job_id = UUID(queued["job_id"])
        deadline = monotonic() + 5
        while monotonic() < deadline:
            job = repository.read_job(scope, job_id)
            if job.status.value not in {"queued", "running"}:
                break
            sleep(0.05)
        assert lock_seen.is_set() and job.status.value == "succeeded"
        assert job.fencing_token == 1
        after = repository.read_world(scope, wid, bid)
        assert after.last_seq == 1 and after.state.current_tick == 60
        assert repository.replay(scope, wid, bid).state_hash == after.state_hash


@pytest.mark.parametrize("compact_ledger", [False, True], ids=["legacy-ledger", "compact-ledger"])
def test_restore_confirmation_reuses_verified_copy(tmp_path, compact_ledger):
    app = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog(), compact_ledger=compact_ledger)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        repository = app.state.worlds.repository
        view = repository.create_world(local_identity(tmp_path), NewWorld(title="Restore test", state=WorldState()))
        backup_path = f"/api/v1/worlds/{view.world.world_id}/backup"
        archive = client.get(backup_path, params={"format_version": "1.0"}).raise_for_status().content
        headers = {"Content-Type": "application/zip"}
        preview = client.post("/api/v1/restores/preview", headers=headers, content=archive).raise_for_status().json()
        invalid = client.post("/api/v1/restores", headers=headers | {"Preview-Id": "0" * 64}, content=archive)
        assert invalid.status_code == 409
        assert not (tmp_path / "restored").exists()
        headers["Preview-Id"] = preview["preview_id"]
        first = client.post("/api/v1/restores", headers=headers, content=archive).raise_for_status().json()
        retry = client.post("/api/v1/restores", headers=headers, content=archive).raise_for_status().json()
        assert first["duplicate"] is False and retry["duplicate"] is True
        assert first["data_directory"] == retry["data_directory"]
        assert list((tmp_path / "restored").iterdir()) == [Path(first["data_directory"])]
    copy = build_application(data_directory=Path(first["data_directory"]), catalog=EmptyTestCatalog(),
                             run_simulation_worker=False, port=8001)
    with TestClient(copy, base_url="http://127.0.0.1:8001") as client:
        client.get("/api/v1/session").raise_for_status()
        segmented = client.get(backup_path, params={"format_version": "2.0"}).raise_for_status().content
        checked = client.post("/api/v1/restores/preview", headers={"Content-Type": "application/zip"},
                              content=segmented).raise_for_status().json()
        assert preview["format_version"] == "1.0" and checked["format_version"] == "2.0"
        for invariant in ("world_id", "state_hashes", "ruleset_version", "pack_version", "event_count"):
            assert checked[invariant] == preview[invariant]
    reopened = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog(), run_simulation_worker=False)
    with TestClient(reopened, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        assert len(client.get("/api/v1/worlds").raise_for_status().json()["items"]) == 1
        for directory in (tmp_path, Path(first["data_directory"])):
            with sqlite3.connect(directory / "world-engine.db") as db:
                assert db.execute("SELECT type FROM sqlite_schema WHERE name='ledger_postings'").fetchone() == (
                    "view" if compact_ledger else "table",
                )
