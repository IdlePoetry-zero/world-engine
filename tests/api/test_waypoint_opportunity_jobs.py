from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.adapters.models import RoleTask
from world_engine.api.app import create_app
from world_engine.api.contracts import MessageRequest
from world_engine.api.security import LocalSession
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.visibility import project_event
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.components import PlaceComponent, PlaceRoute, WaypointOpportunity
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EntityFieldChangedEvent,
    JobStatus,
    MembershipRole,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.domain.waypoint_opportunity_events import (
    WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE,
)
from world_engine.rules import RuleEngine
from world_engine.rules.contracts import RuleContext
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


DAY_SECONDS = 86_400
WORLD_YEAR_TICKS = 360 * DAY_SECONDS
LATE_WAYPOINT_TICK = 120 * DAY_SECONDS


def _id(value: int) -> UUID:
    return UUID(int=value)


def _waypoint_world(
    *,
    closes_tick: int | None = None,
    first_route_ticks: int = 20,
    second_route_ticks: int = 30,
) -> tuple[WorldState, dict[str, UUID]]:
    actor_id = _id(1)
    origin_id, waypoint_id, destination_id = _id(2), _id(3), _id(4)
    first_route_id, second_route_id, opportunity_id = _id(5), _id(6), _id(7)
    archive_id = _id(8)
    opportunity = WaypointOpportunity(
        opportunity_id=opportunity_id,
        title="旧天文台",
        summary="在旧天文台稍作停留，看看海湾的星位。",
        closes_tick=closes_tick,
    )
    first_route = PlaceRoute(
        route_id=first_route_id,
        from_place_id=origin_id,
        to_place_id=waypoint_id,
        travel_ticks=first_route_ticks,
        risk=Decimal("0"),
    )
    second_route = PlaceRoute(
        route_id=second_route_id,
        from_place_id=waypoint_id,
        to_place_id=destination_id,
        travel_ticks=second_route_ticks,
        risk=Decimal("0"),
    )
    state = WorldState(
        current_tick=0,
        player_id=actor_id,
        entities={
            actor_id: WorldEntity(
                id=actor_id,
                kind="character",
                name="Traveler",
                location_id=origin_id,
                components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
            ),
            origin_id: WorldEntity(
                id=origin_id,
                kind="place",
                name="Departure Shore",
                components={"place": PlaceComponent(
                    place_type="settlement", routes=[first_route],
                ).model_dump(mode="json")},
            ),
            waypoint_id: WorldEntity(
                id=waypoint_id,
                kind="place",
                name="Old Observatory",
                components={"place": PlaceComponent(
                    place_type="site", routes=[second_route], waypoint_opportunities=[opportunity],
                ).model_dump(mode="json")},
            ),
            destination_id: WorldEntity(
                id=destination_id,
                kind="place",
                name="Arrival Harbor",
                components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
            ),
            archive_id: WorldEntity(
                id=archive_id, kind="place", name="Hidden chart archive", location_id=waypoint_id,
                components={"hidden": True},
            ),
        },
    )
    return state, {
        "actor": actor_id,
        "origin": origin_id,
        "waypoint": waypoint_id,
        "destination": destination_id,
        "first_route": first_route_id,
        "second_route": second_route_id,
        "opportunity": opportunity_id,
        "archive": archive_id,
    }


def _create_fixture(
    tmp_path,
    *,
    closes_tick: int | None = None,
    player_control: bool = False,
    first_route_ticks: int = 20,
    second_route_ticks: int = 30,
    duration_seconds: int = 720,
):
    state, ids = _waypoint_world(
        closes_tick=closes_tick,
        first_route_ticks=first_route_ticks,
        second_route_ticks=second_route_ticks,
    )
    owner_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'waypoint-opportunity.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(owner_scope, NewWorld(
        world_id=_id(101), branch_id=_id(102), title="Waypoint choice", state=state,
    ))
    scope = owner_scope
    if player_control:
        scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=uuid4())
        repository.grant_membership(
            owner_scope,
            created.world.world_id,
            scope.user_id,
            MembershipRole.PLAYER,
            controlled_actor_ids=(ids["actor"],),
        )
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    app = create_app(WorldApplication(repository, ContentCatalog()), session)
    client = TestClient(app)
    client.get("/api/v1/session").raise_for_status()
    jobs = SimulationJobs(repository)
    path = f"/api/v1/worlds/{created.world.world_id}/long-actions"
    queued = client.post(
        path,
        headers={"Idempotency-Key": "waypoint-itinerary"},
        json={
            "branch_id": str(created.branch.branch_id),
            "expected_state_version": "0",
            "activity": "travel",
            "duration_seconds": duration_seconds,
            "route_ids": [str(ids["first_route"]), str(ids["second_route"])],
        },
    )
    queued.raise_for_status()
    job_id = UUID(queued.json()["job_id"])
    lease = repository.claim_job("waypoint-initial", job_type="simulation.long_action")
    assert lease is not None and lease.job_id == job_id
    return database, repository, scope, client, jobs, created, job_id, lease, ids


def _advance_until_status(repository, scope, client, jobs, created, job_id, lease, expected):
    for _ in range(40):
        jobs.advance(scope, lease)
        record = repository.read_job(scope, job_id)
        if record.status != JobStatus.RUNNING:
            break
    assert record.status is expected
    return record


def _wait_at_waypoint(repository, scope, client, jobs, created, job_id, lease, ids, *, expected_tick=20):
    record = _advance_until_status(
        repository, scope, client, jobs, created, job_id, lease, JobStatus.WAITING_INPUT,
    )
    view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    assert view.state.current_tick == expected_tick
    assert view.state.entities[ids["actor"]].location_id == ids["waypoint"]
    assert record.committed_through_tick == expected_tick
    assert record.payload["itinerary_cursor"]["leg_index"] == 0
    assert [journey.status for journey in view.state.travel_journeys] == ["arrived"]
    status_url = f"/api/v1/jobs/{job_id}"
    waiting_status = client.get(status_url).raise_for_status().json()
    assert waiting_status["status"] == "waiting_input"
    assert waiting_status["reason_code"] == "waypoint_opportunity"
    decisions = client.get(status_url + "/decision").raise_for_status().json()["items"]
    assert len(decisions) == 1
    decision = decisions[0]
    assert {option["option_id"] for option in decision["options"]} == {"continue", "stop"}
    return record, decision


def _advance_until_status_with_ticks(repository, scope, jobs, created, job_id, lease, expected):
    ticks = []
    for _ in range(80):
        jobs.advance(scope, lease)
        record = repository.read_job(scope, job_id)
        ticks.append(repository.read_world(
            scope, created.world.world_id, created.branch.branch_id,
        ).state.current_tick)
        if record.status is not JobStatus.RUNNING:
            break
    assert record.status is expected
    return record, ticks


def _answer(client, job_id, decision, option_id):
    response = client.post(
        f"/api/v1/jobs/{job_id}/decision",
        json={"request_id": decision["request_id"], "option_id": option_id},
    )
    response.raise_for_status()
    return response.json()


def _new_lease(repository, job_id, worker_name):
    lease = repository.claim_job(worker_name, job_type="simulation.long_action")
    assert lease is not None and lease.job_id == job_id
    return lease


def _assert_private_choice(repository, scope, world_id, branch_id, actor_id, choice):
    matching = [
        event for event in repository.list_events(scope, world_id, branch_id, limit=200).events
        if event.type == WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE
    ]
    assert len(matching) == 1
    event = matching[0].model_dump(mode="json")
    assert event["visibility"] == {"policy": "subject_ids", "subject_ids": [str(actor_id)]}
    own_view = project_event(event, str(actor_id))
    assert own_view is not None and own_view["payload"]["choice"] == choice
    assert set(own_view["payload"]) == {"choice", "journey_id"}
    assert project_event(event, str(_id(99))) is None
    return event


def test_continue_choice_resumes_same_itinerary_under_a_new_lease(tmp_path):
    database, repository, scope, client, jobs, created, job_id, first_lease, ids = _create_fixture(
        tmp_path, player_control=True,
    )
    try:
        waiting, decision = _wait_at_waypoint(
            repository, scope, client, jobs, created, job_id, first_lease, ids,
        )
        original_plan = waiting.payload["plan"]
        assert _answer(client, job_id, decision, "continue")["status"] == "queued"
        queued_record = repository.read_job(scope, job_id)
        assert queued_record.result["reason_code"] == "waypoint_opportunity"
        queued_status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
        assert queued_status["status"] == "queued"
        assert queued_status["reason_code"] is None
        assert client.get(f"/api/v1/jobs/{job_id}/decision").raise_for_status().json()["items"] == []

        continuation = _new_lease(repository, job_id, "waypoint-continue")
        assert continuation.fencing_token > first_lease.fencing_token
        consumed = jobs.advance(scope, continuation)
        assert getattr(consumed, "outcome", None) == "advanced"
        running_record = repository.read_job(scope, job_id)
        assert running_record.status is JobStatus.RUNNING
        assert "pending_decision" not in running_record.payload
        assert running_record.result["reason_code"] == "waypoint_opportunity"
        running_status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
        assert running_status["status"] == "running"
        assert running_status["reason_code"] is None
        assert client.get(f"/api/v1/jobs/{job_id}/decision").raise_for_status().json()["items"] == []
        final = _advance_until_status(
            repository, scope, client, jobs, created, job_id, continuation, JobStatus.SUCCEEDED,
        )
        succeeded_status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
        assert succeeded_status["status"] == "succeeded"
        assert succeeded_status["reason_code"] == "travel_itinerary_completed"
        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert final.payload["plan"] == original_plan
        assert final.committed_through_tick == 50
        assert view.state.current_tick == 50
        assert view.state.entities[ids["actor"]].location_id == ids["destination"]
        assert [(item.route_id, item.status) for item in view.state.travel_journeys] == [
            (ids["first_route"], "arrived"), (ids["second_route"], "arrived"),
        ]
        assert view.state.waypoint_opportunity_decisions[0].choice == "continue"
        assert not any(record.subject_id == ids["archive"] for record in view.state.knowledge)
        _assert_private_choice(
            repository, scope, created.world.world_id, created.branch.branch_id, ids["actor"], "continue",
        )
        assert repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True).state_hash == view.state_hash

        owner_scope = AccessScope(tenant_id=scope.tenant_id, user_id=created.world.owner_id)
        restored_database = Database(f"sqlite:///{(tmp_path / 'waypoint-restored.db').as_posix()}")
        restored_database.create_schema()
        restored_repository = WorldRepository(restored_database)
        try:
            backup = BackupApplication(repository, scratch_directory=tmp_path / "waypoint-backup-scratch")
            archive = backup.validate_archive(backup.export_world(
                owner_scope, created.world.world_id, format_version="2.0",
            ))
            WorldBackupImporter(restored_repository).import_verified_backup(owner_scope, archive)
            restored = restored_repository.read_world(owner_scope, created.world.world_id, created.branch.branch_id)
            assert restored.state_hash == view.state_hash
            assert restored.state.waypoint_opportunity_decisions == view.state.waypoint_opportunity_decisions
            assert restored_repository.replay(
                owner_scope, created.world.world_id, created.branch.branch_id, from_genesis=True,
            ).state_hash == restored.state_hash
        finally:
            restored_database.dispose()
    finally:
        client.close()
        database.dispose()


def test_stop_choice_pauses_before_next_leg_then_resume_uses_same_plan(tmp_path):
    database, repository, scope, client, jobs, created, job_id, first_lease, ids = _create_fixture(tmp_path)
    try:
        waiting, decision = _wait_at_waypoint(
            repository, scope, client, jobs, created, job_id, first_lease, ids,
        )
        original_plan = waiting.payload["plan"]
        _answer(client, job_id, decision, "stop")
        response_lease = _new_lease(repository, job_id, "waypoint-stop")
        stopped = _advance_until_status(
            repository, scope, client, jobs, created, job_id, response_lease, JobStatus.PAUSED,
        )
        assert stopped.result["reason_code"] == "waypoint_opportunity_stop"
        stopped_status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
        assert stopped_status["status"] == "paused"
        assert stopped_status["reason_code"] == "waypoint_opportunity_stop"
        assert stopped.payload["plan"] == original_plan
        assert stopped.payload["itinerary_cursor"] == waiting.payload["itinerary_cursor"]
        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert view.state.current_tick == 20
        assert view.state.entities[ids["actor"]].location_id == ids["waypoint"]
        assert [(item.route_id, item.status) for item in view.state.travel_journeys] == [
            (ids["first_route"], "arrived"),
        ]
        assert view.state.waypoint_opportunity_decisions[0].choice == "stop"
        _assert_private_choice(
            repository, scope, created.world.world_id, created.branch.branch_id, ids["actor"], "stop",
        )

        search_action = {"kind": "explore", "place_id": str(ids["waypoint"])}
        search = RuleEngine().evaluate(view.state, search_action, RuleContext(
            world_id=created.world.world_id, branch_id=created.branch.branch_id, command_id=_id(103),
            actor_id=ids["actor"], ruleset_version=created.world.ruleset_version,
        ))
        assert search.outcome.code == "discovered"
        repository.commit_command(scope, created.world.world_id, created.branch.branch_id, CommandEnvelope(
            command_id=_id(103), actor_id=ids["actor"], idempotency_key="investigate-the-waypoint",
            expected_state_version=view.branch.state_version, command_type="action.explore", payload=search_action,
        ), search.events)

        resumed = client.post(f"/api/v1/jobs/{job_id}/control", json={"action": "resume"}).raise_for_status().json()
        assert resumed["status"] == "queued"
        resumed_status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
        assert resumed_status["reason_code"] is None
        assert repository.read_job(scope, job_id).payload["plan"] == original_plan
        next_lease = _new_lease(repository, job_id, "waypoint-after-stop")
        assert next_lease.fencing_token > response_lease.fencing_token
        active_status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
        assert active_status["status"] == "running"
        assert active_status["reason_code"] is None
        final = _advance_until_status(
            repository, scope, client, jobs, created, job_id, next_lease, JobStatus.SUCCEEDED,
        )
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert final.payload["plan"] == original_plan
        assert after.state.current_tick == 650
        assert after.state.entities[ids["actor"]].location_id == ids["destination"]
        assert [(item.route_id, item.status) for item in after.state.travel_journeys] == [
            (ids["first_route"], "arrived"), (ids["second_route"], "arrived"),
        ]
        assert after.state.waypoint_opportunity_decisions[0].choice == "stop"
        assert any(record.subject_id == ids["archive"] for record in after.state.knowledge)
        assert repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True).state_hash == after.state_hash
    finally:
        client.close()
        database.dispose()


@pytest.mark.parametrize("choice_submitted_before_change", [False, True])
def test_stale_same_tick_choice_is_paused_then_reprompted_with_new_request_id(tmp_path, choice_submitted_before_change):
    database, repository, scope, client, jobs, created, job_id, first_lease, ids = _create_fixture(tmp_path)
    try:
        waiting, decision = _wait_at_waypoint(
            repository, scope, client, jobs, created, job_id, first_lease, ids,
        )
        if choice_submitted_before_change:
            assert _answer(client, job_id, decision, "continue")["status"] == "queued"
        before = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        command = CommandEnvelope(
            actor_id=ids["actor"],
            idempotency_key="same-tick-waypoint-change",
            expected_state_version=before.branch.state_version,
            command_type="core.rename",
            payload={"name": "Traveler at the Observatory"},
        )
        repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            command,
            [EntityFieldChangedEvent(entity_id=ids["actor"], field_name="name", value="Traveler at the Observatory")],
        )
        changed = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert changed.state.current_tick == before.state.current_tick == 20
        assert changed.branch.state_version == before.branch.state_version + 1

        if choice_submitted_before_change:
            stale_lease = _new_lease(repository, job_id, "waypoint-stale-answer")
            jobs.advance(scope, stale_lease)
        else:
            assert _answer(client, job_id, decision, "continue")["status"] == "paused"
        paused = repository.read_job(scope, job_id)
        assert paused.status == JobStatus.PAUSED
        assert paused.result["reason_code"] == "waypoint_opportunity_stale"
        assert "pending_decision" not in paused.payload
        assert paused.payload["plan"] == waiting.payload["plan"]
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.waypoint_opportunity_decisions == []

        resumed = client.post(f"/api/v1/jobs/{job_id}/control", json={"action": "resume"}).raise_for_status().json()
        assert resumed["status"] == "queued"
        lease = _new_lease(repository, job_id, "waypoint-fresh-prompt")
        fresh = _advance_until_status(
            repository, scope, client, jobs, created, job_id, lease, JobStatus.WAITING_INPUT,
        )
        assert fresh.payload["plan"] == waiting.payload["plan"]
        assert fresh.payload["itinerary_cursor"] == waiting.payload["itinerary_cursor"]
        new_requests = client.get(f"/api/v1/jobs/{job_id}/decision").raise_for_status().json()["items"]
        assert len(new_requests) == 1
        assert new_requests[0]["request_id"] != decision["request_id"]
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.current_tick == 20
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.waypoint_opportunity_decisions == []
    finally:
        client.close()
        database.dispose()


def test_opportunity_is_not_offered_at_its_exclusive_close_tick(tmp_path):
    database, repository, scope, client, jobs, created, job_id, lease, ids = _create_fixture(
        tmp_path, closes_tick=20,
    )
    try:
        record = _advance_until_status(
            repository, scope, client, jobs, created, job_id, lease, JobStatus.SUCCEEDED,
        )
        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert record.committed_through_tick == 50
        assert view.state.current_tick == 50
        assert [journey.route_id for journey in view.state.travel_journeys] == [
            ids["first_route"], ids["second_route"],
        ]
        assert view.state.waypoint_opportunity_decisions == []
        assert repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True).state_hash == view.state_hash
    finally:
        client.close()
        database.dispose()


@pytest.mark.parametrize(
    ("closes_tick", "expects_waypoint_prompt"),
    [(None, True), (LATE_WAYPOINT_TICK, False)],
    ids=["open-after-months", "expired-at-months-late-arrival"],
)
def test_authorized_one_world_year_itinerary_has_one_late_waypoint_boundary(
    tmp_path, closes_tick, expects_waypoint_prompt,
):
    """One queued player trip advances real route time and writes an ordered receipt ledger."""
    database, repository, scope, client, jobs, created, job_id, first_lease, ids = _create_fixture(
        tmp_path,
        closes_tick=closes_tick,
        player_control=True,
        first_route_ticks=LATE_WAYPOINT_TICK,
        second_route_ticks=WORLD_YEAR_TICKS - LATE_WAYPOINT_TICK,
        duration_seconds=WORLD_YEAR_TICKS,
    )
    try:
        queued = repository.read_job(scope, job_id)
        assert queued.payload["plan"]["target_tick"] == WORLD_YEAR_TICKS
        itinerary = queued.payload["plan"]["travel_itinerary"]
        assert itinerary["deadline_tick"] == WORLD_YEAR_TICKS
        assert [leg["travel_ticks"] for leg in itinerary["legs"]] == [
            LATE_WAYPOINT_TICK, WORLD_YEAR_TICKS - LATE_WAYPOINT_TICK,
        ]
        assert len(repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)) == 1

        if expects_waypoint_prompt:
            waiting, first_ticks = _advance_until_status_with_ticks(
                repository, scope, jobs, created, job_id, first_lease, JobStatus.WAITING_INPUT,
            )
            assert len(first_ticks) >= 3
            view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
            assert LATE_WAYPOINT_TICK in first_ticks
            assert view.state.current_tick == LATE_WAYPOINT_TICK
            assert view.state.entities[ids["actor"]].location_id == ids["waypoint"]
            assert waiting.committed_through_tick == LATE_WAYPOINT_TICK
            assert waiting.payload["itinerary_cursor"]["leg_index"] == 0
            decisions = client.get(f"/api/v1/jobs/{job_id}/decision").raise_for_status().json()["items"]
            assert len(decisions) == 1
            decision = decisions[0]
            assert {option["option_id"] for option in decision["options"]} == {"continue", "stop"}

            # The player gives one itinerary request and one waypoint answer;
            # monthly route progress is committed by the worker without more
            # player messages or more queued jobs.
            assert _answer(client, job_id, decision, "continue")["status"] == "queued"
            continuation = _new_lease(repository, job_id, "annual-waypoint-continue")
            final, finish_ticks = _advance_until_status_with_ticks(
                repository, scope, jobs, created, job_id, continuation, JobStatus.SUCCEEDED,
            )
            assert len(finish_ticks) >= 3
            assert WORLD_YEAR_TICKS in finish_ticks
            assert final.committed_through_tick == WORLD_YEAR_TICKS
        else:
            final, worker_ticks = _advance_until_status_with_ticks(
                repository, scope, jobs, created, job_id, first_lease, JobStatus.SUCCEEDED,
            )
            assert len(worker_ticks) >= 3
            assert LATE_WAYPOINT_TICK in worker_ticks
            assert WORLD_YEAR_TICKS in worker_ticks
            assert final.committed_through_tick == WORLD_YEAR_TICKS

        view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert view.state.current_tick == WORLD_YEAR_TICKS
        assert view.state.entities[ids["actor"]].location_id == ids["destination"]
        assert [(journey.route_id, journey.status) for journey in view.state.travel_journeys] == [
            (ids["first_route"], "arrived"), (ids["second_route"], "arrived"),
        ]
        assert len(repository.list_jobs(scope, created.world.world_id, created.branch.branch_id)) == 1

        ledger = repository.list_events(
            scope, created.world.world_id, created.branch.branch_id, limit=500,
        ).events
        time_ticks = [
            event.payload["to_tick"] for event in ledger if event.type == "core.time_advanced.v1"
        ]
        assert time_ticks == [LATE_WAYPOINT_TICK, WORLD_YEAR_TICKS]

        journey_rows = [
            event for event in ledger if event.type == "core.travel_journey_changed.v1"
        ]
        journey_timeline = [
            (
                event.payload["operation"],
                UUID(event.payload["journey"]["route_id"]),
                event.payload["journey"]["departed_tick"],
                event.payload["journey"]["arrival_tick"],
            )
            for event in journey_rows
        ]
        expected_timeline = [
            ("depart", ids["first_route"], 0, LATE_WAYPOINT_TICK),
            ("arrive", ids["first_route"], 0, LATE_WAYPOINT_TICK),
        ]
        expected_timeline.extend([
            ("depart", ids["second_route"], LATE_WAYPOINT_TICK, WORLD_YEAR_TICKS),
            ("arrive", ids["second_route"], LATE_WAYPOINT_TICK, WORLD_YEAR_TICKS),
        ])
        assert journey_timeline == expected_timeline

        decisions = [
            event for event in ledger
            if event.type == WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE
        ]
        assert len(decisions) == int(expects_waypoint_prompt)
        if expects_waypoint_prompt:
            assert view.state.waypoint_opportunity_decisions[0].choice == "continue"
            assert view.state.waypoint_opportunity_decisions[0].at_tick == LATE_WAYPOINT_TICK
            _assert_private_choice(
                repository, scope, created.world.world_id, created.branch.branch_id,
                ids["actor"], "continue",
            )
            first_arrival_seq = next(
                event.sequence for event in journey_rows
                if event.payload["operation"] == "arrive"
                and UUID(event.payload["journey"]["route_id"]) == ids["first_route"]
            )
            second_departure_seq = next(
                event.sequence for event in journey_rows
                if event.payload["operation"] == "depart"
                and UUID(event.payload["journey"]["route_id"]) == ids["second_route"]
            )
            assert first_arrival_seq < decisions[0].sequence < second_departure_seq
        else:
            assert view.state.waypoint_opportunity_decisions == []

        assert repository.replay(
            scope, created.world.world_id, created.branch.branch_id, from_genesis=True,
        ).state_hash == view.state_hash
    finally:
        client.close()
        database.dispose()


class _FakeWaypointGm:
    def __init__(self):
        self.calls = []
        self.contexts = []

    async def generate(self, _scope, model_request):
        self.calls.append(model_request)
        assert model_request.role_task is RoleTask.GM_ANSWER
        request_body = json.loads(model_request.messages[-1].content)
        self.contexts.append(request_body["context"])
        return SimpleNamespace(normalized_output={"answer": "这里有一个已记录的停留选择。", "references": []})


def _conversation_api(tmp_path, repository, scope):
    operations = OperationsStore(tmp_path / "waypoint-chat-operations.db")
    models = _FakeWaypointGm()
    worlds = WorldApplication(repository, ContentCatalog())
    service = ConversationApplication(worlds, models, ConversationStore(operations))
    session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
    client = TestClient(create_app(worlds, session, conversations=service))
    client.get("/api/v1/session").raise_for_status()
    return client, operations, models


def _message_body(created, ids, *, request_id, session_id, expected_state_version, mode, text):
    return MessageRequest(
        request_id=request_id,
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        session_id=session_id,
        actor_id=ids["actor"],
        expected_state_version=str(expected_state_version),
        mode=mode,
        text=text,
    ).model_dump(mode="json")


def _record_chat_waypoint_choice(repository, scope, jobs, world_id, branch_id, job_id, expected_choice):
    lease = repository.claim_job(f"waypoint-chat-worker:{expected_choice}", job_type="simulation.long_action")
    assert lease is not None and lease.job_id == job_id
    for _ in range(40):
        jobs.advance(scope, lease)
        view = repository.read_world(scope, world_id, branch_id)
        if view.state.waypoint_opportunity_decisions:
            break
        if repository.read_job(scope, job_id).status is not JobStatus.RUNNING:
            break
    view = repository.read_world(scope, world_id, branch_id)
    assert len(view.state.waypoint_opportunity_decisions) == 1
    assert view.state.waypoint_opportunity_decisions[0].choice == expected_choice
    return lease, view


@pytest.mark.parametrize(
    ("mode", "text", "choice"),
    [
        ("auto", "在此停留", "stop"),
        ("action", "在此停留", "stop"),
        ("auto", "继续原行程", "continue"),
        ("action", "继续原行程", "continue"),
    ],
)
def test_chat_waypoint_text_routes_to_owned_choice_without_provider_and_recovers(
    tmp_path, mode, text, choice,
):
    database, repository, scope, job_client, jobs, created, job_id, first_lease, ids = _create_fixture(
        tmp_path, player_control=True,
    )
    chat_client = None
    try:
        waiting, decision = _wait_at_waypoint(
            repository, scope, job_client, jobs, created, job_id, first_lease, ids,
        )
        job_client.close()
        chat_client, operations, models = _conversation_api(tmp_path, repository, scope)
        session_id = uuid4()
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        before_query = repository.read_world(scope, world_id, branch_id)

        # The exact same phrase in ask_gm stays a read-only query and exposes
        # only the actor's current, owned waiting choice context.
        query_id = uuid4()
        query_body = _message_body(
            created, ids, request_id=query_id, session_id=session_id,
            expected_state_version=before_query.branch.state_version, mode="ask_gm", text=text,
        )
        query_response = chat_client.post(
            f"/api/v1/worlds/{world_id}/messages", json=query_body,
            headers={"Idempotency-Key": str(query_id)},
        )
        query_response.raise_for_status()
        query_reply = query_response.json()
        assert query_reply["kind"] == "query" and query_reply["world_effect"] is False
        assert len(models.calls) == 1
        waypoint_context = models.contexts[0]["own_waiting_waypoint_choices"]
        assert len(waypoint_context) == 1
        assert waypoint_context[0]["job_id"] == str(job_id)
        assert len(waypoint_context[0]["options"]) == 2
        after_query = repository.read_world(scope, world_id, branch_id)
        assert after_query.state_hash == before_query.state_hash
        assert repository.read_job(scope, job_id).status is JobStatus.WAITING_INPUT

        # The explicit player choice attaches to the one waiting prompt but
        # does not write world state until a worker lease commits its event.
        message_id = uuid4()
        action_body = _message_body(
            created, ids, request_id=message_id, session_id=session_id,
            expected_state_version=after_query.branch.state_version, mode=mode, text=text,
        )
        action_response = chat_client.post(
            f"/api/v1/worlds/{world_id}/messages", json=action_body,
            headers={"Idempotency-Key": str(message_id)},
        )
        action_response.raise_for_status()
        reply = action_response.json()
        assert reply["kind"] == "action"
        assert reply["action_status"] == "succeeded"
        assert reply["job_id"] == str(job_id)
        assert reply["world_effect"] is False and reply["elapsed_ticks"] == "0"
        assert len(models.calls) == 1, "an explicit waypoint choice must not call a provider"
        queued = repository.read_job(scope, job_id)
        assert queued.status is JobStatus.QUEUED
        assert queued.payload["pending_decision"]["request_id"] == decision["request_id"]
        assert queued.payload["pending_decision"]["option_id"] == choice
        assert queued.payload["waypoint_chat_receipts"][str(message_id)]["accepted"] is True
        unchanged = repository.read_world(scope, world_id, branch_id)
        assert unchanged.state_hash == before_query.state_hash
        assert unchanged.state.waypoint_opportunity_decisions == []

        # An exact resend returns the same receipt, with no second queueing or
        # model call.
        resent = chat_client.post(
            f"/api/v1/worlds/{world_id}/messages", json=action_body,
            headers={"Idempotency-Key": str(message_id)},
        )
        resent.raise_for_status()
        assert resent.json() == reply
        assert len(models.calls) == 1
        assert len(repository.list_jobs(scope, world_id, branch_id)) == 1

        # The actual simulation lease commits the private choice. A failed
        # operations row is then recovered from the durable job receipt.
        _, committed = _record_chat_waypoint_choice(
            repository, scope, jobs, world_id, branch_id, job_id, choice,
        )
        identity = operations.identity(scope)
        with operations.connect() as db:
            updated = db.execute(
                "UPDATE message_requests SET status='failed' WHERE tenant=? AND principal=? AND id=?",
                (*identity, str(message_id)),
            ).rowcount
        assert updated == 1
        recovered_status = chat_client.get(f"/api/v1/commands/{message_id}")
        recovered_status.raise_for_status()
        assert recovered_status.json() == {"status": "succeeded", "result": reply}
        recovered_resend = chat_client.post(
            f"/api/v1/worlds/{world_id}/messages", json=action_body,
            headers={"Idempotency-Key": str(message_id)},
        )
        recovered_resend.raise_for_status()
        assert recovered_resend.json() == reply
        assert len(committed.state.waypoint_opportunity_decisions) == 1
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == committed.state_hash
    finally:
        job_client.close()
        if chat_client is not None:
            chat_client.close()
        database.dispose()


def test_chat_stale_waypoint_choice_is_rejected_without_mutation_and_recovers(tmp_path):
    database, repository, scope, job_client, jobs, created, job_id, first_lease, ids = _create_fixture(tmp_path)
    chat_client = None
    try:
        waiting, _ = _wait_at_waypoint(
            repository, scope, job_client, jobs, created, job_id, first_lease, ids,
        )
        job_client.close()
        before = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            CommandEnvelope(
                actor_id=ids["actor"], idempotency_key="chat-waypoint-same-tick-change",
                expected_state_version=before.branch.state_version, command_type="core.rename",
                payload={"name": "Traveler after the observatory"},
            ),
            [EntityFieldChangedEvent(
                entity_id=ids["actor"], field_name="name", value="Traveler after the observatory",
            )],
        )
        current = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert current.state.current_tick == before.state.current_tick == 20

        chat_client, operations, models = _conversation_api(tmp_path, repository, scope)
        message_id, session_id = uuid4(), uuid4()
        body = _message_body(
            created, ids, request_id=message_id, session_id=session_id,
            expected_state_version=current.branch.state_version, mode="action", text="在此停留",
        )
        response = chat_client.post(
            f"/api/v1/worlds/{created.world.world_id}/messages", json=body,
            headers={"Idempotency-Key": str(message_id)},
        )
        response.raise_for_status()
        reply = response.json()
        assert reply["kind"] == "action" and reply["action_status"] == "rejected"
        assert reply["world_effect"] is False and reply["job_id"] == str(job_id)
        assert models.calls == []
        paused = repository.read_job(scope, job_id)
        assert paused.status is JobStatus.PAUSED
        assert paused.result["reason_code"] == "waypoint_opportunity_stale"
        assert paused.payload["plan"] == waiting.payload["plan"]
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert after.state.current_tick == 20
        assert after.state.waypoint_opportunity_decisions == []

        identity = operations.identity(scope)
        with operations.connect() as db:
            updated = db.execute(
                "UPDATE message_requests SET status='failed' WHERE tenant=? AND principal=? AND id=?",
                (*identity, str(message_id)),
            ).rowcount
        assert updated == 1
        recovered_status = chat_client.get(f"/api/v1/commands/{message_id}")
        recovered_status.raise_for_status()
        assert recovered_status.json() == {"status": "succeeded", "result": reply}

        resumed = chat_client.post(f"/api/v1/jobs/{job_id}/control", json={"action": "resume"})
        resumed.raise_for_status()
        assert resumed.json()["status"] == "queued"
        fresh_lease = _new_lease(repository, job_id, "waypoint-chat-stale-requeue")
        fresh = _advance_until_status(
            repository, scope, chat_client, jobs, created, job_id, fresh_lease, JobStatus.WAITING_INPUT,
        )
        fresh_requests = chat_client.get(f"/api/v1/jobs/{job_id}/decision").raise_for_status().json()["items"]
        assert len(fresh_requests) == 1
        assert fresh_requests[0]["request_id"] != waiting.result["decision_requests"][0]["request_id"]
        assert fresh.payload["plan"] == waiting.payload["plan"]
        after_reprompt = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert after_reprompt.state_hash == after.state_hash
        assert after_reprompt.state.waypoint_opportunity_decisions == []
    finally:
        job_client.close()
        if chat_client is not None:
            chat_client.close()
        database.dispose()
