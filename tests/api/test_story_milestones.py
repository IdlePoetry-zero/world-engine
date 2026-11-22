from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content.distant_station_story import build_archive as build_station_archive
from world_engine.content.gensokyo_reference import build_archive
from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.challenges import ChallengeDefinition, settle_challenge
from world_engine.domain.contests import ReplyContest
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import ScheduledEventCompletedEvent
from world_engine.domain.models import AccessScope, JobStatus, MembershipRole, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, ledger_postings
from world_engine.domain.story_nodes import (
    STORY_NODE_HANDLER, STORY_NODE_VERSION, StoryNode, StoryMilestoneResolvedEvent, settle_story_milestone,
)
from world_engine.runtime import build_application, local_identity
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.versions import LEGACY_RULESET_VERSION
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _run_http_job(client, repository, jobs, scope, world_id, branch, endpoint, duration, **params):
    version = repository.read_world(scope, world_id, branch).branch.state_version
    response = client.post(f"/api/v1/worlds/{world_id}/{endpoint}", headers={"Idempotency-Key": str(uuid4())}, json={
        "branch_id": str(branch), "expected_state_version": str(version),
        "duration_seconds": duration, **params,
    }).raise_for_status().json()
    job_id = UUID(response["job_id"])
    lease = repository.claim_job("story-worker")
    assert lease and lease.job_id == job_id
    for _ in range(12):
        jobs.advance(scope, lease)
        if repository.read_job(scope, job_id).status == JobStatus.SUCCEEDED:
            return
    pytest.fail("Simulation did not settle the story boundaries")


def _contest_http(client, repository, scope, world_id, branch, action, *, expected=200):
    view = repository.read_world(scope, world_id, branch)
    headers = {"Idempotency-Key": str(uuid4())}
    payload = {"branch_id": str(branch), "actor_id": str(view.state.player_id),
               "expected_state_version": str(view.branch.state_version), "action": action}
    url = f"/api/v1/worlds/{world_id}/contests"
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == expected, response.text
    if expected == 200:
        assert client.post(url, json=payload, headers=headers).raise_for_status().json() == response.json()
    return response.json()


def test_gensokyo_milestones_follow_committed_player_travel_across_forks_and_restore(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    jobs = SimulationJobs(repository)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        source = build_archive()
        headers = {"Content-Type": "application/zip", "Origin": "http://127.0.0.1:8000"}
        preview = client.post("/api/v1/packs/import/preview", content=source, headers=headers).raise_for_status().json()
        created = client.post("/api/v1/packs/import", content=source, headers=headers | {
            "Idempotency-Key": "gensokyo-story",
            "X-World-Pack-Sha256": preview["archive_sha256"], "X-Story-Opening": "scarlet-mist",
        }).raise_for_status().json()
        world_id = UUID(created["world_id"])
        main = UUID(created["active_branch_id"])
        path = f"/api/v1/worlds/{world_id}"
        fork = client.post(f"{path}/branches", headers={"Idempotency-Key": "the-fork"}, json={
            "branch_id": str(main), "sequence": "0", "label": "Go to the observation post",
        }).raise_for_status().json()
        other = UUID(fork["branch_id"])

        def state(branch):
            return client.get(f"{path}/view", params={"branch_id": str(branch)}).raise_for_status().json()["state"]

        def perform(branch, endpoint, duration, **params):
            _run_http_job(client, repository, jobs, scope, world_id, branch, endpoint, duration, **params)

        assert "story_milestones" not in state(main)
        assert "local_challenges" not in state(main)
        route = next(row for row in state(other)["travel_options"] if row["destination_name"] == "里外观测棚")
        perform(other, "long-actions", 60, activity="travel", route_id=route["route_id"])
        local = state(other)
        exercises = {row["title"]: row for row in local["local_challenges"]}
        contest, = local["local_contests"]
        assert not contest["can_invite"]
        assert "符卡登记演练" not in exercises
        assert exercises["灯符术法演练"]["fee"]["label"] == "试灯砂"

        def attempt(option, title):
            view = repository.read_world(scope, world_id, other)
            body = {"branch_id": str(other), "actor_id": str(view.state.player_id),
                    "expected_state_version": str(view.branch.state_version)}
            url = f"{path}/challenges/{option['challenge_id']}/attempt"
            headers = {"Idempotency-Key": title}
            result = client.post(url, json=body, headers=headers).raise_for_status().json()
            assert client.post(url, json=body, headers=headers).raise_for_status().json() == result
            return result["passed"]

        assert attempt(exercises["灯符术法演练"], "arcana")
        notes = state(other)["knowledge"]
        assert any("登记技艺名称" in row["claim"] for row in notes)
        assert not any("登记技艺名称" in row["claim"] for row in state(main)["knowledge"])
        assert attempt(exercises["妖力辨识"], "aura")
        assert not attempt(exercises["外界仪具辨用"], "tools")
        unlocked = next(row for row in state(other)["local_challenges"] if row["title"] == "符卡登记演练")
        assert attempt(unlocked, "declare")
        after_practice = repository.read_world(scope, world_id, other)
        assert after_practice.state.current_tick == 60
        assert len(after_practice.state.challenge_attempts) == 4
        assert [row.balance for row in after_practice.state.resources.values() if row.owner_id == after_practice.state.player_id] == [0]
        assert any(row["title"] == "符卡演练登记" for row in state(other)["story_milestones"])
        contest, = state(other)["local_contests"]
        assert contest["can_invite"]
        opponents = {row["name"]: row["id"] for row in contest["opponents"]}

        def invite(name):
            return _contest_http(client, repository, scope, world_id, other, {
                "operation": "invite", "definition_id": contest["definition_id"], "recipient_id": opponents[name], "routine": "lamp.step",
            })["contest_id"]

        def change(ident, operation, expected=200, **extras):
            return _contest_http(client, repository, scope, world_id, other,
                                 {"operation": operation, "contest_id": ident, **extras}, expected=expected)

        declined = invite("绘牌学徒秦澄")
        assert change(declined, "ask_npc")["status"] == "declined"
        assigned = invite("绘牌学徒秦澄")
        second_user = uuid4()
        controlled = UUID(opponents["绘牌学徒秦澄"])
        repository.grant_membership(scope, world_id, second_user, MembershipRole.PLAYER, controlled_actor_ids=[controlled])
        change(assigned, "ask_npc", expected=403)
        waiting = next(row for row in state(other)["local_contests"][0]["recent"] if row["contest_id"] == assigned)
        assert not waiting["can_ask_npc"]
        version = repository.read_world(scope, world_id, other).branch.state_version
        reply = app.state.worlds.change_contest(AccessScope(tenant_id=scope.tenant_id, user_id=second_user), world_id, other,
            controlled, ReplyContest(operation="reply", contest_id=UUID(assigned)), version, "own-decline")
        assert reply["status"] == "declined"

        practice = invite("守簿人笠秋")
        change(practice, "settle", expected=409)
        change(practice, "reply", expected=409, routine="lamp.control")
        assert change(practice, "ask_npc")["status"] == "accepted"
        result = change(practice, "settle")
        assert (result["proposer_wins"], result["recipient_wins"], result["rounds"]) == (2, 1, 3)
        assert result["winner_id"] == str(after_practice.state.player_id)
        assert any(row["title"] == "观测棚切磋纪事" for row in state(other)["story_milestones"])
        perform(other, "long-actions", 120, activity="wait")
        perform(main, "world-advance", 180)
        main_records = state(main)["story_milestones"]
        other_records = state(other)["story_milestones"][-2:]
        assert [row["resolved_tick"] for row in other_records] == ["120", "180"]
        assert "独立留下一份" in main_records[0]["summary"]
        assert "两名" in other_records[0]["summary"]
        assert "有待复核" in main_records[1]["summary"] and "两人独立核验" in other_records[1]["summary"]
        live = {branch: repository.read_world(scope, world_id, branch).state_hash for branch in (main, other)}
        for branch, digest in live.items():
            assert repository.replay(scope, world_id, branch, from_genesis=True).state_hash == digest

    backups = BackupApplication(repository)
    archive = backups.export_world(scope, world_id, format_version="2.0")
    database = Database(f"sqlite:///{(tmp_path / 'isolated-restore.db').as_posix()}")
    database.create_schema()
    try:
        restored = WorldRepository(database)
        with backups.validate_archive(archive) as checked:
            WorldBackupImporter(restored).import_verified_backup(scope, checked)
        for branch, digest in live.items():
            assert restored.replay(scope, world_id, branch, from_genesis=True).state_hash == digest
    finally:
        database.dispose()


def test_station_arrival_cascades_at_the_action_tick_and_moves_real_stock(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    jobs = SimulationJobs(repository)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        archive = build_station_archive()
        upload = {"Content-Type": "application/zip", "Origin": "http://127.0.0.1:8000"}
        preview = client.post("/api/v1/packs/import/preview", content=archive, headers=upload).raise_for_status().json()
        assert preview["can_import"]
        world = client.post("/api/v1/packs/import", content=archive, headers=upload | {
            "Idempotency-Key": "distant-station", "X-World-Pack-Sha256": preview["archive_sha256"], "X-Story-Opening": "arrival",
        }).raise_for_status().json()
        world_id, main = UUID(world["world_id"]), UUID(world["active_branch_id"])
        path = f"/api/v1/worlds/{world_id}"
        fork = client.post(f"{path}/branches", headers={"Idempotency-Key": "unprepared"}, json={
            "branch_id": str(main), "sequence": "0", "label": "Stay at the landing pad",
        }).raise_for_status().json()
        other = UUID(fork["branch_id"])

        def state(branch):
            return client.get(f"{path}/view", params={"branch_id": str(branch)}).raise_for_status().json()["state"]

        original = repository.read_world(scope, world_id, main)
        delayed = UUID(client.post(f"{path}/branches", headers={"Idempotency-Key": "delayed"}, json={
            "branch_id": str(main), "sequence": "0", "label": "Arrive after waiting",
        }).raise_for_status().json()["branch_id"])
        assert "story_milestones" not in state(main) and "story_reactions" not in state(main)
        route, = state(main)["travel_options"]
        assert repository.read_world(scope, world_id, main).state_hash == original.state_hash
        _run_http_job(client, repository, jobs, scope, world_id, main, "long-actions", 60,
                      activity="travel", route_id=route["route_id"])
        arrived = repository.read_world(scope, world_id, main)
        assert [(row.outcome, row.resolved_tick) for row in arrived.state.story_milestones] == [("supplied", 60), ("ready", 60)]
        assert [row["resolved_tick"] for row in state(main)["story_milestones"]] == ["60", "60"]
        events = repository.list_events(scope, world_id, main).events
        supplied, reported = events[-2:]
        assert supplied.command_id == reported.command_id == events[-3].command_id
        assert not supplied.commit_boundary and reported.commit_boundary
        transfer = supplied.payload["transfer"]
        assert arrived.state.resources[UUID(transfer["from_account_id"])].balance == 0
        assert arrived.state.resources[UUID(transfer["to_account_id"])].balance == 3
        typed = StoryMilestoneResolvedEvent.model_validate(supplied.payload | {"event_id": supplied.event_id})
        assert [posting.delta for posting in ledger_postings(typed)] == [-3, 3]
        assert "transfer" not in str(state(main))
        check, = state(main)["local_challenges"]
        assessed = client.post(f"{path}/challenges/{check['challenge_id']}/attempt",
            headers={"Idempotency-Key": "instrument-readout"}, json={
                "branch_id": str(main), "actor_id": str(arrived.state.player_id),
                "expected_state_version": str(arrived.branch.state_version),
            }).raise_for_status().json()
        assert assessed["passed"] is True
        contest, = state(main)["local_contests"]
        notice = _contest_http(client, repository, scope, world_id, main, {"operation": "invite", "definition_id": contest["definition_id"],
            "recipient_id": contest["opponents"][0]["id"], "routine": "station.readout"})
        _contest_http(client, repository, scope, world_id, main, {"operation": "ask_npc", "contest_id": notice["contest_id"]})
        result = _contest_http(client, repository, scope, world_id, main, {"operation": "settle", "contest_id": notice["contest_id"]})
        assert result["status"] == "settled" and result["winner_id"] == str(arrived.state.player_id)
        pending_audit, = [row for row in arrived.state.scheduled_events if row.due_tick == 90]
        assert pending_audit.status == "queued"
        assert "应急电路复查" not in str(state(main))

        _run_http_job(client, repository, jobs, scope, world_id, main, "world-advance", 120)
        _run_http_job(client, repository, jobs, scope, world_id, other, "world-advance", 180)
        _run_http_job(client, repository, jobs, scope, world_id, delayed, "world-advance", 30)
        _run_http_job(client, repository, jobs, scope, world_id, delayed, "long-actions", 60,
                      activity="travel", route_id=route["route_id"])
        _run_http_job(client, repository, jobs, scope, world_id, delayed, "world-advance", 90)
        settled = repository.read_world(scope, world_id, main)
        unprepared = repository.read_world(scope, world_id, other)
        delayed_state = repository.read_world(scope, world_id, delayed)
        assert [(row.outcome, row.resolved_tick) for row in settled.state.story_milestones][-1] == ("sheltered", 180)
        assert [(row.outcome, row.resolved_tick) for row in unprepared.state.story_milestones] == [("pulse", 180)]
        assert [(row.outcome, row.resolved_tick) for row in settled.state.story_milestones][-2] == ("reserve-tested", 90)
        assert [(row.outcome, row.resolved_tick) for row in delayed_state.state.story_milestones][-2] == ("reserve-tested", 120)
        assert repository.replay(scope, world_id, main, from_genesis=True).state_hash == settled.state_hash
        backups = BackupApplication(repository)
        with backups.validate_archive(backups.export_world(scope, world_id, format_version="2.0")) as backup:
            db = Database(f"sqlite:///{(tmp_path / 'station-restore.db').as_posix()}")
            db.create_schema()
            try:
                restored = WorldRepository(db)
                WorldBackupImporter(restored).import_verified_backup(scope, backup)
                assert restored.replay(scope, world_id, main, from_genesis=True).state_hash == settled.state_hash
                assert restored.replay(scope, world_id, delayed, from_genesis=True).state_hash == delayed_state.state_hash
            finally:
                db.dispose()


def test_later_opening_has_a_traversable_mountain_and_private_estate_entrance(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    jobs = SimulationJobs(repository)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        source = build_archive()
        headers = {"Content-Type": "application/zip", "Origin": "http://127.0.0.1:8000"}
        preview = client.post("/api/v1/packs/import/preview", content=source, headers=headers).raise_for_status().json()
        assert [row["opening_id"] for row in preview["story_openings"]] == ["scarlet-mist", "false-moon", "mountain-shrine"]
        world = client.post("/api/v1/packs/import", content=source, headers=headers | {
            "Idempotency-Key": "mountain-opening", "X-World-Pack-Sha256": preview["archive_sha256"], "X-Story-Opening": "mountain-shrine",
        }).raise_for_status().json()
        world_id, main = UUID(world["world_id"]), UUID(world["active_branch_id"])
        path = f"/api/v1/worlds/{world_id}"
        shore_branch = UUID(client.post(f"{path}/branches", headers={"Idempotency-Key": "lakeshore"}, json={
            "branch_id": str(main), "sequence": "0", "label": "Visit the lakeshore",
        }).raise_for_status().json()["branch_id"])

        def view(branch):
            return client.get(f"{path}/view", params={"branch_id": str(branch)}).raise_for_status().json()["state"]

        def travel(branch, title):
            route = next(item for item in view(branch)["travel_options"] if item["destination_name"] == title)
            _run_http_job(client, repository, jobs, scope, world_id, branch, "long-actions",
                          int(route["travel_ticks"]), activity="travel", route_id=route["route_id"])

        travel(shore_branch, "红魔馆外湖岸")
        assert "红魔馆" not in {item["destination_name"] for item in view(shore_branch)["travel_options"]}
        for name in ("博丽神社", "魔法森林", "妖怪之山"):
            travel(main, name)
        state = view(main)
        assert any(row["name"] == "山径水尺员蒲一径" for row in state["entities"].values())
        assert any(row["destination_name"] == "守矢神社" for row in state["travel_options"])
        _run_http_job(client, repository, jobs, scope, world_id, main, "world-advance", 240)
        assert "你见证的水位" in view(main)["story_milestones"][-1]["summary"]
        travel(main, "守矢神社")
        arrived = repository.read_world(scope, world_id, main)
        player = arrived.state.entities[arrived.state.player_id]
        assert arrived.state.entities[player.location_id].name == "守矢神社"
        assert repository.replay(scope, world_id, main, from_genesis=True).state_hash == arrived.state_hash


def test_milestone_reducer_enforces_conditions_and_immutable_event_scope():
    observer = uuid4()
    definition = {
        "title": "Distant stellar pulse", "visibility": "public",
        "fallback": {"key": "pulse", "effect": "occurred", "summary": "The pulse reached the outer station."},
        "variants": [{"outcome": {"key": "sheltered", "effect": "mitigated", "summary": "A shelter stayed powered."},
                      "when": [{"kind": "capability", "actor_id": str(observer), "capability": "station.power"},
                               {"kind": "skill", "actor_id": str(observer), "skill": "engineering", "minimum": 10}]}],
    }
    node = StoryNode.model_validate(definition)
    with pytest.raises(ValueError, match="immutable"):
        StoryNode.model_validate(definition | {"variants": [{
            **definition["variants"][0], "outcome": {"key": "blocked", "effect": "prevented", "summary": "It stopped."},
        }]})
    schedule = ScheduledEventRecord(id=uuid4(), due_tick=60, phase="observation", handler_id=STORY_NODE_HANDLER,
                                    handler_version=STORY_NODE_VERSION, payload=node.model_dump(mode="json"))
    resident = WorldEntity(id=observer, kind="character", components={"skills": {"skills": {"engineering": {"level": "20"}}}})
    state = WorldState(player_id=observer, entities={observer: resident}, scheduled_events=[schedule])
    with pytest.raises(InvalidEvent, match="cannot skip"):
        apply_event(state, TimeAdvancedEvent(to_tick=61))
    state = apply_event(state, TimeAdvancedEvent(to_tick=60))
    with pytest.raises(InvalidEvent, match="typed"):
        apply_event(state, ScheduledEventCompletedEvent(scheduled_event_id=schedule.id, expected_revision=schedule.revision))
    event = settle_story_milestone(state, schedule, uuid4())
    correct = event.milestone
    assert correct.outcome == "pulse"
    forged = StoryMilestoneResolvedEvent(milestone=correct.model_copy(update={"outcome": "sheltered"}))
    with pytest.raises(InvalidEvent, match="conditions"):
        apply_event(state, forged)
    assert apply_event(state, event).story_milestones == [correct]

    specialist = resident.model_copy(update={"components": resident.components | {
        "capabilities": {"grants": ["station.power"], "restrictions": []},
    }})
    qualified = WorldState(current_tick=60, player_id=observer, entities={observer: specialist}, scheduled_events=[schedule])
    mitigated = settle_story_milestone(qualified, schedule, uuid4())
    assert mitigated.milestone.outcome == "sheltered"
    assert apply_event(qualified, mitigated).story_milestones == [mitigated.milestone]

    emergency_outcome = StoryNode.model_validate(definition | {"prevention_possible": True, "variants": [{
        **definition["variants"][0], "outcome": {"key": "blocked", "effect": "prevented", "summary": "A local outage was avoided."},
    }]})
    local_schedule = schedule.model_copy(update={"payload": emergency_outcome.model_dump(mode="json")})
    local_state = WorldState(current_tick=60, player_id=observer, entities={observer: specialist}, scheduled_events=[local_schedule])
    assert settle_story_milestone(local_state, local_schedule, uuid4()).milestone.outcome == "blocked"


@pytest.mark.parametrize("discipline", ["cultivation", "physical", "examination", "technology", "battle_qi", "soul", "economics"])
def test_each_discipline_uses_the_same_growth_and_challenge_contract(discipline):
    actor, place, challenge = uuid4(), uuid4(), uuid4()
    skill_id = f"test.{discipline}"
    state = WorldState(player_id=actor, entities={
        place: WorldEntity(id=place, kind="place", name="Exam hall"),
        actor: WorldEntity(id=actor, kind="character", location_id=place, components={
            "skills": {"skills": {skill_id: {"level": "9.99"}}},
        }),
    }, challenges=[ChallengeDefinition(challenge_id=challenge, venue_id=place, title=discipline,
        description="Use the same authored checkpoint with a different competency.", skill_id=skill_id, pass_level="10")])
    initial_failure = settle_challenge(state, challenge, actor, uuid4())
    assert not initial_failure.attempt.passed
    failed_state = apply_event(state, initial_failure)
    with pytest.raises(InvalidEvent, match="已结算"):
        settle_challenge(failed_state, challenge, actor, uuid4())
    context = RuleContext(world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(), actor_id=actor,
                          ruleset_version=LEGACY_RULESET_VERSION)
    training = RuleEngine().evaluate(state, {"kind": "train", "skill_id": skill_id, "duration_ticks": 60}, context)
    assert training.outcome.status == "succeeded"
    for event in training.events:
        state = apply_event(state, event)
    qualified = settle_challenge(state, challenge, actor, uuid4())
    assert qualified.attempt.completed_tick == 60 and qualified.attempt.passed
