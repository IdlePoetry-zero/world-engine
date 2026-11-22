import asyncio
import base64
import hashlib
import json
import struct
import zlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.adapters.models.contracts import RoleTask
from world_engine.api.security import LocalSession
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.backups import BackupApplication
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.visibility import project_player_state
from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import PlaceComponent, PlaceRoute
from world_engine.domain.errors import AccessDenied, StaleVersion
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.simulation import WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository
from world_engine.runtime import _invoke_worker_model_decision


@pytest.fixture
def authoring(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player = uuid4()
    place = uuid4()
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(scope, NewWorld(title="Authoring fixture", state=WorldState(
        player_id=player, entities={
            player: WorldEntity(id=player, kind="character", name="Writer", location_id=place,
                components={"public_role": "student", "personality": {"values": ["loyalty"]}}),
            place: WorldEntity(id=place, kind="place", name="Library"),
        })))
    service = AuthoringApplication(repository, OperationsStore(tmp_path / "operations.db"))
    yield scope, repository, service, created.world.world_id, created.branch.branch_id, player
    database.dispose()


def test_preview_is_non_mutating_commit_is_replayable_and_idempotent(authoring):
    scope, repository, service, world, branch, player = authoring
    before = repository.read_world(scope, world, branch)
    preview = service.preview(scope, world, branch, player, "public_role", "scholar", "Revise occupation")
    assert preview["before"] == "student" and preview["after"] == "scholar"
    assert repository.read_world(scope, world, branch).state_hash == before.state_hash
    assert "event" not in preview
    first = service.commit(scope, world, UUID(preview["preview_id"]), preview["expected_version"])
    second = service.commit(scope, world, UUID(preview["preview_id"]), preview["expected_version"])
    assert first["state_version"] == second["state_version"] == "1"
    assert second["duplicate"] is True
    after = repository.read_world(scope, world, branch)
    assert after.state.current_tick == before.state.current_tick
    assert after.state.entities[player].components["public_role"] == "scholar"
    assert repository.replay(scope, world, branch).state_hash == after.state_hash
    assert repository.replay(scope, world, branch, through_seq=0).state_hash == before.state_hash


def test_world_template_update_forks_pinned_checkpoint_and_keeps_source_history(authoring):
    scope, repository, author, fixture_world, fixture_branch, player = authoring
    opening = repository.read_world(scope, fixture_world, fixture_branch).state.model_dump(mode="json")
    manifest = WorldPackManifest(pack_id="library-opening", version="1.0.0", title="Library opening")
    initial = write_world_pack(manifest=manifest, template={"scenario": opening})
    upload = {"Origin": "http://testserver", "Content-Type": "application/zip"}
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    with TestClient(create_app(WorldApplication(repository, SimpleNamespace(list_packs=lambda: [])), session,
                               authoring=author)) as client:
        client.get("/api/v1/session").raise_for_status()
        package_preview = client.post("/api/v1/packs/import/preview", headers=upload, content=initial).raise_for_status().json()
        created = client.post("/api/v1/packs/import", content=initial, headers=upload | {
            "X-World-Pack-Sha256": package_preview["archive_sha256"], "Idempotency-Key": "initial-library",
        }).raise_for_status().json()
        world_id, branch_id = UUID(created["world_id"]), UUID(created["active_branch_id"])
        endpoint = f"/api/v1/worlds/{world_id}/template-updates"
        prior = author.preview(scope, world_id, branch_id, player, "personality",
                               {"values": ["loyalty"], "traits": ["reserved"]}, "Earlier character development")
        author.commit(scope, world_id, UUID(prior["preview_id"]), prior["expected_version"])
        before = repository.read_world(scope, world_id, branch_id)
        opening["entities"][str(player)]["components"]["personality"]["values"] = ["curiosity"]
        update = write_world_pack(manifest=manifest.model_copy(update={"version": "1.1.0"}), template={"scenario": opening})
        draft = client.post(f"{endpoint}/preview", params={"branch_id": branch_id}, headers=upload, content=update).raise_for_status().json()
        assert draft["can_apply"] and draft["changes"][0]["fields"] == ["values"]
        assert repository.read_world(scope, world_id, branch_id).branch == before.branch
        assert len(repository.list_branches(scope, world_id)) == 1
        payload = {"preview_id": draft["preview_id"], "label": "Library template 1.1.0"}
        committed = client.post(endpoint, json=payload).raise_for_status().json()
        child_id = UUID(committed["branch_id"])
        child = repository.read_world(scope, world_id, child_id)
        assert child.branch.parent_branch_id == branch_id and child.branch.fork_seq == before.last_seq
        assert child.state.current_tick == before.state.current_tick
        assert child.state.entities[player].components["personality"]["values"] == ["curiosity"]
        assert child.state.entities[player].components["personality"]["traits"] == ["reserved"]
        assert repository.replay(scope, world_id, child_id, from_genesis=True).state_hash == child.state_hash
        assert repository.read_world(scope, world_id, branch_id).state_hash == before.state_hash
        assert client.post(endpoint, json=payload).raise_for_status().json()["duplicate"]

        stale = client.post(f"{endpoint}/preview", params={"branch_id": branch_id}, headers=upload, content=update).raise_for_status().json()
        edit = author.preview(scope, world_id, branch_id, player, "personality", {"values": ["justice"]}, "Player story")
        author.commit(scope, world_id, UUID(edit["preview_id"]), edit["expected_version"])
        expired_source = client.post(endpoint, json={"preview_id": stale["preview_id"], "label": "Stale"})
        assert expired_source.status_code == 409
        assert expired_source.json()["error"]["code"] == "TEMPLATE_SOURCE_CHANGED"
        conflict = client.post(f"{endpoint}/preview", params={"branch_id": branch_id}, headers=upload, content=update).raise_for_status().json()
        assert not conflict["can_apply"] and conflict["changes"][0]["status"] == "conflict"
        incompatible = write_world_pack(manifest=manifest.model_copy(update={"version": "2.0.0"}),
                                        template={"scenario": opening, "ruleset_version": "2.0.0"})
        report = client.post(f"{endpoint}/preview", params={"branch_id": branch_id}, headers=upload,
                             content=incompatible).raise_for_status().json()
        assert not report["can_apply"] and "2.0.0" in report["changes"][0]["message"]
        assert len(repository.list_branches(scope, world_id)) == 2


def test_author_conflicts_identify_magic_and_broken_routes_before_write(authoring):
    scope, repository, author, world, branch, player = authoring
    original = repository.read_world(scope, world, branch).state
    mundane = original.model_copy(update={"metadata": {"content_rules": {"magic": {"enabled": False}}}})
    created = repository.create_world(scope, NewWorld(title="Mundane", state=mundane))
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    place = original.entities[player].location_id
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, SimpleNamespace(list_packs=lambda: [])), session, authoring=author)
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()

        def edit(entity_id, component, value):
            return client.post(f"/api/v1/worlds/{world_id}/interventions/preview", json={
                "branch_id": str(branch_id), "patch_proposal": {"entity_id": str(entity_id),
                "component_name": component, "replacement": value, "reason": "Author validation"},
            })

        magic = edit(player, "capabilities", {"grants": ["world.travel", "magic.limited_practice"]})
        assert magic.status_code == 422
        failure = magic.json()["error"]
        assert failure["code"] == "AUTHOR_RULE_CONFLICT"
        assert (failure["field_errors"][0]["path"], failure["field_errors"][0]["type"]) == (
            "patch_proposal.replacement.grants.1", "content_rules.magic.enabled",
        )
        missing_place = str(uuid4())
        route = {"route_id": str(uuid4()), "from_place_id": str(place), "to_place_id": missing_place,
                 "travel_ticks": 120, "is_open": True}
        broken = edit(place, "place", {"routes": [route]})
        assert broken.status_code == 422
        fields = broken.json()["error"]["field_errors"]
        assert any(row["path"] == "patch_proposal.replacement.routes.0.to_place_id" for row in fields)
        assert missing_place not in broken.text
        assert edit(player, "capabilities", {"grants": ["world.travel"]}).is_success
        assert repository.read_world(scope, world_id, branch_id).branch == created.branch

        invalid = mundane.model_dump(mode="json")
        invalid["entities"][str(player)]["components"]["capabilities"] = {"grants": ["magic.limited_practice"]}
        pack = write_world_pack(manifest=WorldPackManifest(pack_id="mundane", version="1.0.0", title="Mundane"),
                                template={"scenario": invalid})
        preview = client.post("/api/v1/packs/import/preview", content=pack,
                              headers={"Content-Type": "application/zip"}).raise_for_status().json()
        assert not preview["can_import"]
        assert preview["field_errors"][0]["path"] == f"scenario.entities.{player}.components.capabilities.grants.0"


def test_author_personality_lock_preserves_value_until_explicit_unlock(authoring):
    scope, repository, author, world_id, branch_id, actor_id = authoring
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=author)
    prefix = f"/api/v1/worlds/{world_id}"
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()

        def preview(replacement):
            return client.post(f"{prefix}/interventions/preview", json={
                "branch_id": str(branch_id), "patch_proposal": {"entity_id": str(actor_id),
                    "component_name": "personality", "replacement": replacement, "reason": "Review personality settings"},
            })

        def commit(response):
            reviewed = response.raise_for_status().json()
            return client.post(f"{prefix}/interventions", json={"preview_id": reviewed["preview_id"],
                "expected_version": reviewed["expected_version"]}).raise_for_status().json()

        commit(preview({"values": ["loyalty"], "locked_fields": ["values"]}))
        locked = repository.read_world(scope, world_id, branch_id)
        assert locked.state.entities[actor_id].components["personality"]["locked_fields"] == ["values"]
        player_view = client.get(f"{prefix}/view?branch_id={branch_id}").raise_for_status().json()
        assert "locked_fields" not in json.dumps(player_view)

        rejected = preview({"values": ["curiosity"], "locked_fields": []})
        assert rejected.status_code == 422
        fields = rejected.json()["error"]["field_errors"]
        assert [(field["path"], field["type"]) for field in fields] == [
            ("patch_proposal.replacement.values", "personality.locked_fields"),
        ]
        assert repository.read_world(scope, world_id, branch_id).state_hash == locked.state_hash
        commit(preview({"values": ["loyalty"], "traits": ["direct"]}))
        continued = repository.read_world(scope, world_id, branch_id)
        assert continued.state.entities[actor_id].components["personality"]["locked_fields"] == ["values"]
        branch = repository.fork(scope, world_id, branch_id, continued.last_seq, "Still locked")
        pack = BackupApplication(repository)
        with pack.validate_archive(pack.export_world(scope, world_id)) as verified:
            preserved = verified.load_head_state(branch.branch_id).entities[actor_id].components["personality"]
            assert preserved["locked_fields"] == ["values"]

        commit(preview({"values": ["loyalty"], "traits": ["direct"], "locked_fields": []}))
        commit(preview({"values": ["curiosity"], "traits": ["direct"]}))
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state.entities[actor_id].components[
            "personality"]["values"] == ["curiosity"]
        assert repository.replay(scope, world_id, branch.branch_id, from_genesis=True).state.entities[actor_id].components[
            "personality"]["values"] == ["loyalty"]


def test_dynamic_npc_revision_and_stale_preview_rejection(authoring):
    scope, repository, service, world, branch, player = authoring
    first = service.preview(scope, world, branch, player, "personality", {"values": ["independence"]}, "Change character values")
    stale = service.preview(scope, world, branch, player, "public_role", "teacher", "Change occupation")
    service.commit(scope, world, UUID(first["preview_id"]), first["expected_version"])
    personality = repository.read_world(scope, world, branch).state.entities[player].components["personality"]
    assert personality["values"] == ["independence"] and personality["revision"] == 2
    with pytest.raises(StaleVersion):
        service.commit(scope, world, UUID(stale["preview_id"]), stale["expected_version"])


def test_player_control_is_not_author_permission(authoring):
    scope, repository, service, world, branch, player = authoring
    other = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    repository.grant_membership(scope, world, other.user_id, MembershipRole.PLAYER, controlled_actor_ids=[player])
    with pytest.raises(AccessDenied):
        service.preview(other, world, branch, player, "public_role", "ruler", "Attempted privilege escalation")


def test_daily_routine_uses_author_preview_and_stays_out_of_player_projection(authoring):
    scope, repository, service, world, branch, player = authoring
    before = repository.read_world(scope, world, branch)
    place = next(entity.id for entity in before.state.entities.values() if entity.kind == "place")
    replacement = {"windows": [{
        "window_id": str(uuid4()), "start_second": 21_600, "duration_seconds": 25_200,
        "activity": "rest", "place_id": str(place),
    }, {
        "window_id": str(uuid4()), "start_second": 46_800, "duration_seconds": 28_800,
        "activity": "work", "place_id": str(place),
    }]}
    preview = service.preview(scope, world, branch, player, "daily_routine", replacement, "Set a daily routine")
    assert preview["before"] is None
    assert preview["after"]["revision"] == 1
    assert repository.read_world(scope, world, branch).state_hash == before.state_hash

    result = service.commit(scope, world, UUID(preview["preview_id"]), preview["expected_version"])
    assert result["state_version"] == "1"
    after = repository.read_world(scope, world, branch)
    assert after.state.current_tick == before.state.current_tick
    assert len(after.state.entities[player].components["daily_routine"]["windows"]) == 2
    player_projection = project_player_state(after.state.model_dump(mode="json"), str(player))
    assert "daily_routine" not in player_projection["entities"][str(player)]["components"]
    assert repository.replay(scope, world, branch).state_hash == after.state_hash


def test_character_preview_commit_and_private_records(authoring):
    scope, repository, service, world, branch, _ = authoring
    before = repository.read_world(scope, world, branch)
    place = next(entity.id for entity in before.state.entities.values() if entity.kind == "place")
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    path = f"/api/v1/worlds/{world}"
    with TestClient(app) as client:
        client.get("/api/v1/session")
        draft = {
            "branch_id": str(branch), "expected_state_version": "0", "name": "Archivist",
            "location_id": str(place), "public_role": "librarian", "values": ["accuracy"],
            "description": "Carries a blue notebook.", "secret": "Hidden passage", "goal": "Preserve census",
        }
        preview_path = path + "/author/characters/preview"
        commit_path = path + "/author/characters/commit"
        preview = client.post(preview_path, json=draft).raise_for_status().json()
        competing = client.post(preview_path, json=draft | {"name": "Other"}).raise_for_status().json()
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash
        body = {"preview_id": preview["preview_id"]}
        result = client.post(commit_path, json=body).raise_for_status().json()
        assert result["state_version"] == "1" and result["duplicate"] is False
        assert client.post(commit_path, json=body).raise_for_status().json() == result | {"duplicate": True}
        assert client.post(commit_path, json={"preview_id": competing["preview_id"]}).status_code == 409
        player_view = client.get(path + "/view").raise_for_status()
        assert "Archivist" in player_view.text
        assert "Hidden passage" not in player_view.text and "Preserve census" not in player_view.text
    after = repository.read_world(scope, world, branch)
    assert after.state.current_tick == before.state.current_tick
    assert after.last_seq == 3
    assert after.state.goals[0].owner_id == UUID(preview["entity_id"])
    assert "auto_complete" not in after.state.goals[0].model_dump(mode="json")
    assert after.state.knowledge[0].holder_id == UUID(preview["entity_id"])
    assert repository.replay(scope, world, branch).state_hash == after.state_hash

@pytest.mark.parametrize("existing", [False, True], ids=["new-character", "existing-character"])
def test_authored_destination_goal_settles_on_actual_npc_arrival_without_world_opt_in(authoring, existing):
    scope, repository, service, _, _, player = authoring
    start, destination, existing_actor = uuid4(), uuid4(), uuid4()
    route = PlaceRoute(route_id=uuid4(), from_place_id=start, to_place_id=destination, travel_ticks=60)
    opening = WorldState(player_id=player, entities={
        start: WorldEntity(id=start, kind="place", name="Library", components={
            "place": PlaceComponent(place_type="site", routes=[route]).model_dump(mode="json"),
        }),
        destination: WorldEntity(id=destination, kind="place", name="River station"),
        player: WorldEntity(id=player, kind="character", name="Writer", location_id=start),
        **({existing_actor: WorldEntity(id=existing_actor, kind="character", name="Archivist", location_id=start,
                                       components={"capabilities": {"grants": ["world.travel"]}})} if existing else {}),
    })
    created = repository.create_world(scope, NewWorld(title="Authored destination", state=opening))
    world, branch = created.world.world_id, created.branch.branch_id
    opening_hash = repository.read_world(scope, world, branch).state_hash
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    prefix = f"/api/v1/worlds/{world}"
    draft = {"branch_id": str(branch), "expected_state_version": "0",
             "goal_destination_id": str(destination), "review_count": 1}
    if existing:
        draft.update(actor_id=str(existing_actor), goal_description="Inspect the station")
        goal_key, route_prefix = "goal_description", "/author/npc-reviews"
    else:
        draft.update(name="Archivist", location_id=str(start), goal="Inspect the station")
        goal_key, route_prefix = "goal", "/author/characters"
    with TestClient(app) as client:
        client.get("/api/v1/session")
        path = prefix + route_prefix + "/preview"
        for update in ({goal_key: " "}, {"goal_destination_id": str(start)}, {"goal_destination_id": str(player)}):
            assert client.post(path, json=draft | update).status_code == 422
        preview = client.post(path, json=draft).raise_for_status().json()
        preview_destination = preview["goal"]["conditions"][0]["target_id"] if existing else preview["draft"]["goal_destination_id"]
        assert preview_destination == str(destination)
        assert repository.read_world(scope, world, branch).state_hash == opening_hash
        commit = prefix + route_prefix + "/commit"
        client.post(commit, json={"preview_id": preview["preview_id"]}).raise_for_status()
        assert client.post(commit, json={"preview_id": preview["preview_id"]}).raise_for_status().json()["duplicate"] is True
        assert "Inspect the station" not in client.get(prefix + "/view").raise_for_status().text

    actor = UUID(preview["entity_id"])
    before = repository.read_world(scope, world, branch)
    goal, = before.state.goals
    assert goal.auto_complete and goal.conditions[0].subject_id == actor and goal.conditions[0].target_id == destination
    assert goal.status == "active" and before.state.current_tick == 0
    assert before.state.metadata == opening.metadata
    due = int(preview["review_schedule"]["due_tick"])
    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=due + 60),
                                     idempotency_key="author-travel", speed="balanced", depth="standard")
    lease = repository.claim_job("author-travel", lease_seconds=180)
    assert lease is not None

    class MoveModel:
        async def generate(self, scope, request):
            if request.role_task is RoleTask.NPC_PLAN_REVIEW:
                return SimpleNamespace(normalized_output={"verdict": "approve"})
            if request.role_task is RoleTask.MEMORY_SUMMARY:
                return SimpleNamespace(normalized_output={"outlook": "defer"})
            assert request.role_task is RoleTask.NPC_DECISION
            return SimpleNamespace(normalized_output={
                "kind": "plan", "goal_ids": [str(goal.id)], "reason": "Travel to the station.",
                "steps": [{"capability_id": "world.action.move.v1", "arguments": {
                    "route_id": str(route.route_id), "destination_id": str(destination),
                }}],
            })

    saw_plan_review = False
    saw_reflection = False
    for _ in range(20):
        resolution = jobs.advance(scope, lease)
        job = repository.read_job(scope, job.job_id)
        if job.status.value == "succeeded":
            break
        for request in resolution.decision_requests:
            assert request.actor_id == actor
            if request.kind == "npc.plan_review":
                assert request.at_tick == due
                saw_plan_review = True
                decision = asyncio.run(_invoke_worker_model_decision(
                    MoveModel(), repository, scope, lease, job, request,
                ))
                assert decision.npc_planning_status == "ready", decision.npc_planning_rejection
                jobs.advance(scope, lease, model_decision=decision)
            elif request.kind == "npc.reflection":
                assert request.at_tick == due + 60
                saw_reflection = True
                response = asyncio.run(_invoke_worker_model_decision(
                    MoveModel(), repository, scope, lease, job, request,
                ))
                assert response is None
            else:
                pytest.fail(f"Unexpected worker request kind: {request.kind}")
    assert job.status.value == "succeeded"
    assert saw_plan_review and saw_reflection
    final = repository.read_world(scope, world, branch)
    fulfilled, = final.state.goals
    assert final.state.entities[actor].location_id == destination
    assert (fulfilled.status, fulfilled.updated_tick) == ("succeeded", due + 60)
    assert len(fulfilled.evidence_refs) == 1
    assert repository.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash


def test_author_rehearsal_is_version_pinned_private_and_read_only(authoring):
    scope, repository, service, world, branch, player = authoring
    first = repository.read_world(scope, world, branch)
    location = str(first.state.entities[player].location_id)
    draft = {
        "name": "Archivist", "location_id": location, "public_role": "Surveyor", "values": ["accuracy"],
        "traits": [], "description": "", "secret": "Own source", "goal": "Protect originals",
    }
    own = service.preview_character(scope, world, branch, 0, draft)
    service.commit_character(scope, world, UUID(own["preview_id"]))
    other = service.preview_character(scope, world, branch, 1, draft | {
        "name": "Other", "secret": "Unseen source", "goal": "Unseen goal",
    })
    service.commit_character(scope, world, UUID(other["preview_id"]))
    before = repository.read_world(scope, world, branch)
    available = next(record.id for record in before.state.knowledge if str(record.holder_id) == own["entity_id"])
    unavailable = next(record.id for record in before.state.knowledge if str(record.holder_id) == other["entity_id"])
    calls = []
    reference = available

    async def generate(actor_scope, request):
        assert actor_scope.user_id == scope.user_id
        assert request.role_task == RoleTask.AUTHOR_ASSIST
        calls.append(request)
        context = json.loads(request.messages[-1].content)
        assert context["actor"]["name"] == "Archivist"
        assert "Unseen source" not in request.messages[-1].content and "Unseen goal" not in request.messages[-1].content
        if "choice" in context:
            assert context["choice"] == "negotiate" and context["tone"] == "cautious"
            assert "Own source" not in request.messages[-1].content and "Protect originals" not in request.messages[-1].content
            assert "author-only motivation" not in request.messages[-1].content
            return SimpleNamespace(normalized_output={"reply": "I need to verify this."})
        assert context["actor"]["personality"]["values"] == ["accuracy"]
        assert "Own source" in request.messages[-1].content and "Protect originals" in request.messages[-1].content
        return SimpleNamespace(normalized_output={
            "choice": "negotiate", "tone": "cautious", "motivation": "author-only motivation",
            "uncertainty": "The source remains unclear.", "referenced_fact_ids": [str(reference)],
        })

    operations = SimpleNamespace(store=service.operations, generate=generate)
    url = f"/api/v1/worlds/{world}/author/npc-rehearsals"
    body = {
        "request_id": str(uuid4()), "branch_id": str(branch), "expected_state_version": "2",
        "actor_id": own["entity_id"], "scenario": "rebuttal",
    }
    for role_scope in (scope, AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())):
        owner = role_scope == scope
        if not owner:
            repository.grant_membership(scope, world, role_scope.user_id, MembershipRole.PLAYER)
        session = LocalSession(str(role_scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        app = create_app(WorldApplication(repository, None), session, authoring=service, operations=operations)
        with TestClient(app) as client:
            client.get("/api/v1/session")
            if not owner:
                assert client.post(url, json=body).status_code == 403
                assert len(calls) == 13
                continue
            assert client.post(url, json=body | {"expected_state_version": "1"}).status_code == 409
            assert client.post(url, json=body | {"scenario": "unknown"}).status_code == 422
            assert not calls
            for scenario in ("inducement", "threat", "family_conflict", "rebuttal", "scarcity", "absence"):
                report = client.post(url, json=body | {"scenario": scenario}).raise_for_status().json()
                assert report["scenario"] == scenario and report["state_hash"] == before.state_hash
                assert report["tick"] == "0" and report["world_effect"] is False
                assert report["result"]["choice"] == "negotiate"
                assert report["references"][0]["claim"] == "Own source"
            reference = unavailable
            assert client.post(url, json=body).status_code == 502

    after = repository.read_world(scope, world, branch)
    assert (after.state_hash, after.last_seq, after.branch.state_version) == (
        before.state_hash, before.last_seq, before.branch.state_version,
    )


def test_author_schedules_goal_reviews_for_new_and_existing_npcs(authoring, tmp_path):
    scope, repository, service, world, branch, player = authoring
    before = repository.read_world(scope, world, branch)
    day = before.state.calendar.day_seconds
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    prefix = f"/api/v1/worlds/{world}/author"
    draft = {
        "branch_id": str(branch), "expected_state_version": "0", "name": "Archivist",
        "location_id": str(before.state.entities[player].location_id), "goal": "Preserve the census",
    }
    with TestClient(app) as client:
        client.get("/api/v1/session")
        new_preview = client.post(prefix + "/characters/preview", json=draft | {
            "review_count": 2, "review_interval_days": 3,
        }).raise_for_status().json()
        schedule = new_preview["review_schedule"]
        assert (schedule["due_tick"], schedule["repeat_ticks"], schedule["max_runs"]) == (str(day), str(3 * day), 2)
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash
        body = {"preview_id": new_preview["preview_id"]}
        assert client.post(prefix + "/npc-reviews/commit", json=body).status_code == 404
        first = client.post(prefix + "/characters/commit", json=body).raise_for_status().json()

        second_draft = draft | {"name": "Cartographer", "expected_state_version": first["state_version"]}
        second_preview = client.post(prefix + "/characters/preview", json=second_draft).raise_for_status().json()
        assert "review_schedule" not in second_preview
        second = client.post(prefix + "/characters/commit", json={
            "preview_id": second_preview["preview_id"],
        }).raise_for_status().json()
        enrollment = {
            "branch_id": str(branch), "expected_state_version": second["state_version"], "review_count": 1,
        }
        assert client.post(prefix + "/npc-reviews/preview", json=enrollment | {"actor_id": str(player)}).status_code >= 400
        assert client.post(prefix + "/npc-reviews/preview", json=enrollment | {
            "actor_id": first["entity_id"],
        }).status_code >= 400
        pending = client.post(prefix + "/npc-reviews/preview", json=enrollment | {
            "actor_id": second["entity_id"],
        }).raise_for_status().json()
        assert pending["review_schedule"]["due_tick"] == str(day)
        assert pending["review_schedule"]["repeat_ticks"] is None
        assert repository.read_world(scope, world, branch).branch.state_version == int(second["state_version"])
        body = {"preview_id": pending["preview_id"]}
        result = client.post(prefix + "/npc-reviews/commit", json=body).raise_for_status().json()
        assert client.post(prefix + "/npc-reviews/commit", json=body).raise_for_status().json() == result | {"duplicate": True}

    after = repository.read_world(scope, world, branch)
    assert after.state.current_tick == before.state.current_tick
    assert {row.payload["actor_id"] for row in after.state.scheduled_events} == {first["entity_id"], second["entity_id"]}
    assert repository.replay(scope, world, branch, from_genesis=True).state_hash == after.state_hash

    backup = BackupApplication(repository)
    restored_db = Database(f"sqlite:///{(tmp_path / 'restored-reviews.db').as_posix()}")
    try:
        restored_db.create_schema()
        restored_repo = WorldRepository(restored_db)
        with backup.validate_archive(backup.export_world(scope, world)) as verified:
            WorldBackupImporter(restored_repo).import_verified_backup(scope, verified)
        restored = restored_repo.replay(scope, world, branch, from_genesis=True)
        assert restored.state_hash == after.state_hash
        assert restored.state.scheduled_events == after.state.scheduled_events
    finally:
        restored_db.dispose()

    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(
        scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=day + 60),
        idempotency_key="check-author-scheduled-npc-review", speed="balanced", depth="standard",
        expected_state_version=after.branch.state_version,
    )
    lease = repository.claim_job("check-author-scheduled-npc-review", job_type=job.job_type)
    assert lease is not None and lease.job_id == job.job_id
    progress = jobs.advance(scope, lease)
    if not progress.decision_requests:
        progress = jobs.advance(scope, lease)
    decision, = progress.decision_requests
    assert decision.at_tick == day and decision.kind == "npc.plan_review"


def test_character_card_import_previews_only_supported_data_and_reuses_author_commit(authoring):
    scope, repository, service, world, branch, _ = authoring
    before = repository.read_world(scope, world, branch)
    place = next(entity.id for entity in before.state.entities.values() if entity.kind == "place")
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    path = f"/api/v1/worlds/{world}/author/characters"
    raw = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {
        "name": "Census Archivist", "description": "Carries notes.", "personality": "Patient",
        "system_prompt": "Untrusted prompt", "extensions": {"danger": "Untrusted payload"},
    }}
    card_json = "\ufeff" + json.dumps(raw, indent=2) + "\n  "
    body = {"branch_id": str(branch), "expected_state_version": "0",
            "location_id": str(place), "card_json": card_json}
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        response = client.post(path + "/import/preview", json=body).raise_for_status()
        preview = response.json()
        assert preview["import_report"] == {
            "card_version": "2.0", "mapped_fields": ["description", "name", "personality"],
            "unmapped_fields": ["extensions", "system_prompt"],
            "source_retained_on_commit": True,
        }
        assert "Untrusted" not in response.text
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash
        assert client.post(path + "/import/preview", json=body | {"card_json": '{"name":"a","name":"b"}'}).status_code == 422
        bad_unicode = r'{"name":"\ud800"}'
        assert client.post(path + "/import/preview", json=body | {"card_json": bad_unicode}).status_code == 422
        legacy = client.post(path + "/import/preview", json=body | {
            "card_json": '\ufeff{"name":"Legacy Archivist","description":"Checks dates.","data":{"qa":true}}',
        }).raise_for_status().json()
        assert legacy["import_report"]["card_version"] == "1"
        assert legacy["import_report"]["unmapped_fields"] == ["data"]
        result = client.post(path + "/commit", json={"preview_id": preview["preview_id"]}).raise_for_status()
        assert result.json()["state_version"] == "1"
        author = client.get(f"/api/v1/worlds/{world}/history", params={
            "branch_id": str(branch), "mode": "author",
        }).raise_for_status().json()
        creation, = author["items"]
        source = creation["payload"]["character_card_source"]
        assert source == {
            "card_version": "2.0", "source_format": "json",
            "original_json_base64": base64.b64encode(card_json.encode("utf-8")).decode("ascii"),
            "sha256": hashlib.sha256(card_json.encode("utf-8")).hexdigest(),
        }
        player_history = client.get(f"/api/v1/worlds/{world}/history", params={
            "branch_id": str(branch), "mode": "player",
        }).raise_for_status()
        assert "Untrusted" not in player_history.text
        player_world = client.get(f"/api/v1/worlds/{world}/view", params={
            "branch_id": str(branch), "mode": "player",
        }).raise_for_status()
        assert "Untrusted" not in player_world.text
    after = repository.read_world(scope, world, branch)
    character = after.state.entities[UUID(preview["entity_id"])]
    assert character.name == "Census Archivist"
    assert character.components["public_description"] == "Carries notes."
    assert character.components["personality"]["traits"] == ["Patient"]
    assert after.last_seq == 1 and after.state.current_tick == before.state.current_tick
    assert repository.replay(scope, world, branch).state_hash == after.state_hash
    assert "Untrusted" not in after.state.model_dump_json()


def test_png_character_card_preview_and_commit_use_the_normal_author_flow(authoring, monkeypatch, tmp_path):
    scope, repository, service, world, branch, _ = authoring
    before = repository.read_world(scope, world, branch)
    place = next(entity.id for entity in before.state.entities.values() if entity.kind == "place")
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    path = f"/api/v1/worlds/{world}/author/characters"
    location = {"branch_id": str(branch), "expected_state_version": "0", "location_id": str(place)}
    card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {
        "name": "Signal Keeper", "description": "Maintains the lanterns.", "personality": "Observant",
        "extensions": {"fixture": "Private card metadata sentinel"},
    }}

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    card_json = json.dumps(card) + "\n  "
    metadata = b"ccv3\0" + base64.b64encode(card_json.encode("utf-8"))
    png = b"\x89PNG\r\n\x1a\n" + b"".join((
        chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
        chunk(b"tEXt", metadata),
        chunk(b"IDAT", zlib.compress(b"\0\0\0\0")),
        chunk(b"IEND", b""),
    ))
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        upload = path + "/import/png/preview"
        headers = {"Content-Type": "image/png"}
        assert client.post(upload, params=location, content=png).status_code == 415
        assert client.post(upload, params=location, headers=headers, content=b"bad png").status_code == 422
        with monkeypatch.context() as bounded:
            bounded.setattr("world_engine.api.authoring.MAX_CARD_PNG_BYTES", len(png) - 1)
            streamed = (png[offset:offset + 16] for offset in range(0, len(png), 16))
            assert client.post(upload, params=location, headers=headers, content=streamed).status_code == 413
        assert client.post(
            upload, params=location | {"expected_state_version": "1"}, headers=headers, content=png,
        ).status_code == 409
        response = client.post(upload, params=location, headers=headers, content=png).raise_for_status()
        preview = response.json()
        assert preview["import_report"] == {
            "card_version": "3.0", "mapped_fields": ["description", "name", "personality"],
            "unmapped_fields": ["extensions"],
            "source_retained_on_commit": True,
        }
        assert "Private card metadata sentinel" not in response.text
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash
        client.post(path + "/commit", json={"preview_id": preview["preview_id"]}).raise_for_status()

    after = repository.read_world(scope, world, branch)
    character = after.state.entities[UUID(preview["entity_id"])]
    assert character.name == "Signal Keeper"
    assert character.components["public_description"] == "Maintains the lanterns."
    assert character.components["personality"]["traits"] == ["Observant"]
    assert after.last_seq == 1 and after.state.current_tick == before.state.current_tick
    assert repository.replay(scope, world, branch, from_genesis=True).state_hash == after.state_hash
    assert "Private card metadata sentinel" not in after.state.model_dump_json()

    backup = BackupApplication(repository)
    restored_db = Database(f"sqlite:///{(tmp_path / 'restored-card.db').as_posix()}")
    try:
        restored_db.create_schema()
        restored_repo = WorldRepository(restored_db)
        with backup.validate_archive(backup.export_world(scope, world, format_version="2.0")) as verified:
            WorldBackupImporter(restored_repo).import_verified_backup(scope, verified)
        restored = restored_repo.replay(scope, world, branch, from_genesis=True)
        assert restored.state_hash == after.state_hash
        creation, = restored_repo.list_events(scope, world, branch).events
        assert creation.payload["character_card_source"] == {
            "card_version": "3.0", "source_format": "png",
            "original_json_base64": base64.b64encode(card_json.encode("utf-8")).decode("ascii"),
            "sha256": hashlib.sha256(card_json.encode("utf-8")).hexdigest(),
        }
    finally:
        restored_db.dispose()


def test_world_book_previews_privately_then_commits_only_confirmed_public_refs(authoring, tmp_path):
    scope, repository, service, world, branch, _ = authoring
    before = repository.read_world(scope, world, branch)
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    path = f"/api/v1/worlds/{world}"
    imports = path + "/author/world-books"
    document = {"entries": {
        "0": {"key": ["Copperbell Harbor"], "comment": "Tide seals", "content": "Pilots use blue wax."},
        "1": {"key": ["Copperbell Harbor"], "comment": "Private cargo", "content": "Private fixture entry 427."},
        "2": {"disable": True, "comment": "Old rule", "content": "Retired fixture entry 813."},
    }, "extensions": {"fixture": "Opaque book extension 239."}}
    original = "\ufeff" + json.dumps(document, indent=2) + "\n  "
    preview_args = {"branch_id": str(branch), "expected_state_version": "0",
                    "title": "Harbor guides", "book_json": original}
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        view_params = {"branch_id": str(branch), "mode": "author"}
        empty = client.get(path + "/view", params=view_params).raise_for_status().json()
        assert empty["state"]["world_book_entries"] == []
        assert client.post(imports + "/preview", json=preview_args | {"expected_state_version": "1"}).status_code == 409
        preview_response = client.post(imports + "/preview", json=preview_args).raise_for_status()
        preview = preview_response.json()
        assert "Opaque book extension" not in preview_response.text
        assert {row["source_key"] for row in preview["entries"]} == {"0", "1"}
        assert [row["source_key"] for row in preview["skipped_entries"]] == ["2"]
        assert repository.read_world(scope, world, branch).state_hash == before.state_hash
        selection = {"preview_id": preview["preview_id"]}
        assert client.post(imports + "/commit", json=selection | {"public_keys": ["2"]}).status_code == 422
        commit = selection | {"public_keys": ["0"]}
        reply = client.post(imports + "/commit", json=commit).raise_for_status().json()
        assert reply == {"entry_count": 2, "state_version": "1", "duplicate": False}
        assert client.post(imports + "/commit", json=commit).raise_for_status().json() == reply | {"duplicate": True}
        assert client.post(imports + "/commit", json=selection | {"public_keys": ["1"]}).status_code == 409

        author = client.get(path + "/view", params=view_params).raise_for_status().json()
        entries = author["state"]["world_book_entries"]
        assert {entry["source_key"]: entry["visibility"] for entry in entries} == {
            "0": "public", "1": "author_only",
        }
        for endpoint in ("/view", "/history"):
            visible = client.get(path + endpoint, params=view_params | {"mode": "player"}).raise_for_status()
            assert "Private fixture entry" not in visible.text
            assert "original_json_base64" not in visible.text

    after = repository.replay(scope, world, branch, from_genesis=True)
    assert after.state.current_tick == before.state.current_tick and after.last_seq == 1
    backup = BackupApplication(repository)
    restored_db = Database(f"sqlite:///{(tmp_path / 'restored-book.db').as_posix()}")
    try:
        restored_db.create_schema()
        restored_repo = WorldRepository(restored_db)
        with backup.validate_archive(backup.export_world(scope, world, format_version="2.0")) as verified:
            WorldBackupImporter(restored_repo).import_verified_backup(scope, verified)
        assert restored_repo.replay(scope, world, branch, from_genesis=True).state_hash == after.state_hash
        imported, = restored_repo.list_events(scope, world, branch).events
        assert imported.type == "core.world_book_imported.v1"
        assert base64.b64decode(imported.payload["original_json_base64"]).decode("utf-8") == original
    finally:
        restored_db.dispose()


def test_legacy_occupation_route_is_versioned_and_empty_set_has_no_commit(authoring):
    scope, repository, service, world, branch, _ = authoring
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    path = f"/api/v1/worlds/{world}/branches/{branch}/author/occupations/settle-legacy-vacancies"
    before = repository.read_world(scope, world, branch)
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        body = {"command_id": str(uuid4()), "expected_state_version": "0"}
        assert client.post(path, json=body).raise_for_status().json() == {"settled": False, "commit": None}
        assert client.post(path, json=body | {"expected_state_version": "1"}).status_code == 409
    assert repository.read_world(scope, world, branch).state_hash == before.state_hash


def test_author_ballot_maintenance_route_checks_version_and_does_not_create_a_missing_ballot(authoring):
    scope, repository, service, world, branch, _ = authoring
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, None), session, authoring=service)
    base = f"/api/v1/worlds/{world}/branches/{branch}"
    path = base + f"/author/leadership-ballots/{uuid4()}/cancel-invalidated"
    before = repository.read_world(scope, world, branch)
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        author = client.get(f"/api/v1/worlds/{world}/view", params={"branch_id": str(branch), "mode": "author"})
        assert author.raise_for_status().json()["state"]["invalidated_leadership_ballot_ids"] == []
        body = {"command_id": str(uuid4()), "expected_state_version": "0", "expected_ballot_revision": 1}
        assert client.post(path, json=body).status_code == 404
        assert client.post(path, json=body | {"expected_state_version": "1"}).status_code == 409
        assert client.post(path, json=body | {"expected_ballot_revision": "1"}).status_code == 422
    assert repository.read_world(scope, world, branch).state_hash == before.state_hash
