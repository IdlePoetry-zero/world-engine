"""Leased worker integration for private NPC meeting attendance choices."""

from __future__ import annotations

import json
from io import BytesIO
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.visibility import project_event
from world_engine.domain.components import (
    DailyRoutineComponent,
    DailyRoutineWindow,
    LifeComponent,
    PersonalityComponent,
    TreasuryComponent,
)
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.npc_commitments import MEETING_COMMITMENT_HANDLER
from world_engine.domain.npc_attendance_events import NpcAttendanceSettledEvent, NpcAttendanceStartedEvent
from world_engine.domain.npc_meeting_attendance import NpcMeetingAttendanceDecisionRecordedEvent
from world_engine.domain.occupation_events import OccupationSource
from world_engine.domain.occupations import WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.relationship_invitation_events import RelationInvitationChangedEvent
from world_engine.domain.relationship_invitations import InvitationMeeting
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.rules import RuleContext, RuleEngine
from world_engine.runtime import _invoke_worker_model_decision
from world_engine.simulation.contracts import DecisionResponse, WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


DAY_SECONDS = 86_400
YEAR_DAYS = 360
YEAR_TICKS = DAY_SECONDS * YEAR_DAYS


def _character(entity_id, name, place_id, *, private_value=None):
    life = LifeComponent(
        birth_tick=-(25 * YEAR_TICKS),
        lifespan_ticks=80 * YEAR_TICKS,
        maturity_ticks=18 * YEAR_TICKS,
        life_stage="adult",
    )
    components = {"life": life.model_dump(mode="python")}
    if private_value is not None:
        components["personality"] = PersonalityComponent(
            values=[private_value],
        ).model_dump(mode="python")
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        status="active",
        location_id=place_id,
        components=components,
    )


def _commit_invitation_action(repository, scope, world_id, branch_id, actor_id, kind, payload, *, key):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    normalized = {"kind": kind, **payload}
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=view.world.ruleset_version,
    )
    resolution = RuleEngine().evaluate(view.state, normalized, context)
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    assert len(resolution.events) == 1
    commit = repository.commit_command(scope, world_id, branch_id, CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=key,
        expected_state_version=view.branch.state_version,
        command_type=f"action.{kind}",
        payload=normalized,
    ), resolution.events)
    return commit, resolution.events[0]


def _make_accepted_invitation(repository, scope, world_id, branch_id, player_id, npc_id, *, start, end, key):
    _, proposal = _commit_invitation_action(
        repository, scope, world_id, branch_id, player_id,
        "propose_relationship_invitation",
        {"target_id": str(npc_id), "meeting": {"start_tick": start, "end_tick": end}},
        key=f"{key}-propose",
    )
    invitation_id = proposal.resulting_invitation.invitation_id
    _, acceptance = _commit_invitation_action(
        repository, scope, world_id, branch_id, npc_id,
        "resolve_relationship_invitation",
        {"invitation_id": str(invitation_id), "operation": "accept"},
        key=f"{key}-accept",
    )
    assert acceptance.operation == "accept"
    return invitation_id


def _advance_to_meeting_decision(jobs, repository, scope, lease, *, tick):
    boundary = jobs.advance(scope, lease)
    assert boundary.outcome == "advanced"
    assert boundary.next_tick == tick
    assert boundary.metrics.ticks_advanced > 0

    pending = jobs.advance(scope, lease)
    assert pending.outcome == "decision_required"
    assert pending.events == ()
    decision, = pending.decision_requests
    assert decision.kind == "npc.meeting_attendance"
    assert decision.decision_owner == "model"
    assert decision.at_tick == tick
    return decision


@pytest.mark.asyncio
async def test_leased_meeting_attendance_attend_defer_recovery_replay_and_restore(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'meeting-attendance-source.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, attend_id, defer_id, place_id = (uuid4() for _ in range(4))
    state = WorldState(
        player_id=player_id,
        calendar={"day_seconds": DAY_SECONDS, "year_days": YEAR_DAYS},
        entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Riverside Courtyard"),
            player_id: _character(player_id, "Lin Yue", place_id),
            attend_id: _character(attend_id, "Anwen Cairn", place_id, private_value="ANWEN_PRIVATE_VALUE"),
            defer_id: _character(defer_id, "Beren Alder", place_id, private_value="BEREN_PRIVATE_VALUE"),
        },
    )
    try:
        created = repository.create_world(
            scope,
            NewWorld(title="Meeting attendance worker", ruleset_version="meeting-worker-test.v1", state=state),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        invitation_ids = {
            attend_id: _make_accepted_invitation(
                repository, scope, world_id, branch_id, player_id, attend_id,
                start=10, end=20, key="attendance-choice",
            ),
            defer_id: _make_accepted_invitation(
                repository, scope, world_id, branch_id, player_id, defer_id,
                start=30, end=40, key="defer-choice",
            ),
        }
        initial = repository.read_world(scope, world_id, branch_id)
        assert {item.meeting.start_tick for item in initial.state.relationship_invitations} == {10, 30}
        assert all(item.status == "accepted" for item in initial.state.relationship_invitations)

        jobs = SimulationJobs(repository)
        job = jobs.enqueue(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=50),
            idempotency_key="npc-meeting-attendance-worker",
            speed="balanced",
            depth="standard",
            expected_state_version=initial.branch.state_version,
        )
        lease = repository.claim_job("meeting-attendance-worker-before-recovery")
        assert lease is not None and lease.job_id == job.job_id

        model_calls = []

        class RoleAwareMeetingModel:
            async def generate(self, received_scope, request):
                assert received_scope == scope
                assert request.role_task is RoleTask.NPC_DECISION
                assert request.trace_context.world_id == str(world_id)
                assert request.trace_context.branch_id == str(branch_id)
                assert request.trace_context.job_id == str(job.job_id)
                body = json.loads(request.messages[1].content)
                assert set(body) == {"title", "options", "meeting"}
                assert request.output_schema["properties"]["option_id"]["enum"] == ["attend", "defer"]
                meeting = body["meeting"]
                actor_name = meeting["actor"]["name"]
                prompt_text = "\n".join(message.content for message in request.messages)
                choice_by_actor = {"Anwen Cairn": "attend", "Beren Alder": "defer"}
                assert actor_name in choice_by_actor
                other_private = "BEREN_PRIVATE_VALUE" if actor_name == "Anwen Cairn" else "ANWEN_PRIVATE_VALUE"
                assert other_private not in prompt_text
                assert set(meeting) == {
                    "actor", "counterpart", "own_relationship_impression", "meeting_interval",
                    "current_tick", "co_location_fact",
                }
                assert set(meeting["actor"]) == {"name", "traits", "values"}
                assert set(meeting["counterpart"]) == {"display_name"}
                assert "invitation_id" not in prompt_text
                assert "invitation notes" not in prompt_text
                model_calls.append((actor_name, choice_by_actor[actor_name]))
                return SimpleNamespace(
                    normalized_output={"option_id": choice_by_actor[actor_name]},
                    call_trace=SimpleNamespace(request_id=f"meeting-call-{len(model_calls)}"),
                )

        model = RoleAwareMeetingModel()

        # The first real answer is durably pinned, then the worker is paused and
        # reacquired to prove that recovery consumes the receipt without asking again.
        first_request = _advance_to_meeting_decision(jobs, repository, scope, lease, tick=10)
        first_answer = await _invoke_worker_model_decision(
            model, repository, scope, lease, repository.read_job(scope, job.job_id), first_request,
        )
        assert first_answer.option_id == "attend"
        assert repository.record_model_decision(
            lease, first_answer, expected_state_version=first_request.state_version,
        ) == "pending"
        repository.control_job(scope, job.job_id, "pause")
        repository.control_job(scope, job.job_id, "resume")

        recovered_repository = WorldRepository(database)
        recovered_jobs = SimulationJobs(recovered_repository)
        recovered_lease = recovered_repository.claim_job("meeting-attendance-worker-after-recovery")
        assert recovered_lease is not None and recovered_lease.job_id == lease.job_id
        assert recovered_lease.fencing_token > lease.fencing_token
        attended = recovered_jobs.advance(scope, recovered_lease)
        assert attended.outcome == "advanced"
        assert attended.next_tick == attended.metrics.from_tick == 10
        assert attended.metrics.ticks_advanced == 0
        assert model_calls == [("Anwen Cairn", "attend")]
        attendance_event, attendance_marker = attended.events
        assert isinstance(attendance_event, RelationInvitationChangedEvent)
        assert attendance_event.operation == "attend"
        assert attendance_event.actor_id == attend_id
        assert isinstance(attendance_marker, NpcMeetingAttendanceDecisionRecordedEvent)
        assert attendance_marker.record.choice == "attend"
        assert attendance_marker.record.actor_id == attend_id
        assert attendance_marker.visibility == {"policy": "author_only"}
        after_attend = recovered_repository.read_world(scope, world_id, branch_id)
        assert after_attend.state.current_tick == 10
        first_invitation = next(
            item for item in after_attend.state.relationship_invitations
            if item.invitation_id == invitation_ids[attend_id]
        )
        assert [(row.actor_id, row.attended_tick) for row in first_invitation.meeting_attendance] == [(attend_id, 10)]
        assert player_id not in {row.actor_id for row in first_invitation.meeting_attendance}
        assert after_attend.state.entities[player_id] == initial.state.entities[player_id]

        # At the second, disjoint meeting the other NPC defers. The marker is
        # durable, but it neither adds attendance nor moves the clock.
        second_request = _advance_to_meeting_decision(
            recovered_jobs, recovered_repository, scope, recovered_lease, tick=30,
        )
        second_answer = await _invoke_worker_model_decision(
            model, recovered_repository, scope, recovered_lease,
            recovered_repository.read_job(scope, job.job_id), second_request,
        )
        assert second_answer.option_id == "defer"
        deferred = recovered_jobs.advance(scope, recovered_lease, model_decision=second_answer)
        assert deferred.outcome == "advanced"
        assert deferred.next_tick == deferred.metrics.from_tick == 30
        assert deferred.metrics.ticks_advanced == 0
        marker, = deferred.events
        assert isinstance(marker, NpcMeetingAttendanceDecisionRecordedEvent)
        assert marker.record.choice == "defer"
        assert marker.record.actor_id == defer_id
        assert marker.visibility == {"policy": "author_only"}
        after_defer = recovered_repository.read_world(scope, world_id, branch_id)
        second_invitation = next(
            item for item in after_defer.state.relationship_invitations
            if item.invitation_id == invitation_ids[defer_id]
        )
        assert second_invitation.status == "accepted"
        assert second_invitation.revision == 2
        assert second_invitation.meeting_attendance == []
        assert after_defer.state.entities[player_id] == initial.state.entities[player_id]

        # Advancing beyond both intervals must not reissue either model choice.
        completed = recovered_jobs.advance(scope, recovered_lease)
        assert completed.outcome == "completed"
        assert recovered_repository.read_job(scope, job.job_id).status.value == "succeeded"
        assert model_calls == [("Anwen Cairn", "attend"), ("Beren Alder", "defer")]

        final = recovered_repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == 50
        records = recovered_repository.list_events(scope, world_id, branch_id, limit=100)
        markers = [event for event in records.events if event.type == "core.npc_meeting_attendance_decision_recorded.v1"]
        assert len(markers) == 2
        assert all(event.visibility == {"policy": "author_only"} for event in markers)
        player_page = recovered_repository.list_player_history_events(scope, world_id, branch_id, limit=100)
        assert all(
            project_event(event.model_dump(mode="json"), str(player_id)) is None
            for event in player_page.events if event.type == "core.npc_meeting_attendance_decision_recorded.v1"
        )
        assert recovered_repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash

        backup = BackupApplication(recovered_repository)
        archive = backup.export_world(scope, world_id)
        with backup.validate_archive(archive) as eager_verified:
            assert eager_verified.world.world_id == world_id
        restored_database = Database(f"sqlite:///{(tmp_path / 'meeting-attendance-restored.sqlite').as_posix()}")
        restored_database.create_schema()
        try:
            restored_repository = WorldRepository(restored_database)
            with backup.validate_archive_file_streaming(BytesIO(archive)) as verified:
                assert verified.world.world_id == world_id
                WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
            restored = restored_repository.read_world(scope, world_id, branch_id)
            assert restored.state_hash == final.state_hash
            assert restored.state.current_tick == 50
            assert restored_repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
            restored_markers = [
                event for event in restored_repository.list_events(scope, world_id, branch_id, limit=100).events
                if event.type == "core.npc_meeting_attendance_decision_recorded.v1"
            ]
            assert [event.payload["record"]["choice"] for event in restored_markers] == ["attend", "defer"]
            assert all(event.visibility == {"policy": "author_only"} for event in restored_markers)
        finally:
            restored_database.dispose()
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_active_registered_npc_shift_settles_before_open_meeting_decision(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'meeting-work-shift.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, worker_id, employer_id, place_id, worker_account_id, employer_account_id, posting_id = (
        uuid4() for _ in range(7)
    )
    source = OccupationSource(kind="genesis", reference_id="fixture.npc-meeting-work")
    assignment_id = occupation_assignment_id(source, worker_id, posting_id)
    terms = WorkShiftTerms(
        posting_id=posting_id,
        duration_seconds=100,
        resource_id="core.credits",
        wage="2.50",
    )
    worker = _character(worker_id, "Beren Alder", place_id, private_value="BEREN_SHIFT_PRIVATE")
    worker_payload = worker.model_dump(mode="json")
    worker_payload["components"]["daily_routine"] = DailyRoutineComponent(windows=[DailyRoutineWindow(
        window_id=uuid4(),
        start_second=0,
        duration_seconds=200,
        activity="work",
        place_id=place_id,
    )]).model_dump(mode="json")
    worker = WorldEntity.model_validate(worker_payload)
    state = WorldState(
        player_id=player_id,
        entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Riverside Courtyard"),
            player_id: _character(player_id, "Lin Yue", place_id),
            worker_id: worker,
            employer_id: WorldEntity(
                id=employer_id,
                kind="company",
                name="Canal Survey Office",
                location_id=place_id,
                components={"treasury": TreasuryComponent(
                    accounts={"core.credits": employer_account_id},
                ).model_dump(mode="python")},
            ),
        },
        resources={
            worker_account_id: ResourceAccount(
                resource_id="core.credits", owner_id=worker_id, balance="1.00",
            ),
            employer_account_id: ResourceAccount(
                resource_id="core.credits", owner_id=employer_id, balance="10.00",
            ),
        },
        work_postings=[WorkPosting(
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=place_id,
            title="Registry clerk",
            slots=1,
            valid_from_tick=0,
        )],
        work_assignments=[WorkAssignment(
            assignment_id=assignment_id,
            actor_id=worker_id,
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=place_id,
            title="Registry clerk",
            started_tick=0,
            source=source,
        )],
        metadata={"work_shift_terms": [terms.model_dump(mode="json")]},
    )
    try:
        created = repository.create_world(
            scope,
            NewWorld(title="Meeting and NPC shift", ruleset_version="meeting-work-shift-test.v1", state=state),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        invitation_id = _make_accepted_invitation(
            repository, scope, world_id, branch_id, player_id, worker_id,
            start=150, end=250, key="worker-meeting",
        )
        jobs = SimulationJobs(repository)
        job = jobs.enqueue(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=300),
            idempotency_key="npc-shift-before-meeting-choice",
            speed="balanced",
            depth="standard",
            expected_state_version=repository.read_world(scope, world_id, branch_id).branch.state_version,
        )
        lease = repository.claim_job("meeting-work-shift-worker")
        assert lease is not None and lease.job_id == job.job_id

        model_calls = []

        class MeetingModel:
            async def generate(self, received_scope, request):
                assert received_scope == scope
                assert request.role_task is RoleTask.NPC_DECISION
                body = json.loads(request.messages[1].content)
                assert body["meeting"]["actor"]["name"] == "Beren Alder"
                assert body["meeting"]["current_tick"] == 200
                assert body["meeting"]["meeting_interval"] == {
                    "start_tick": 150,
                    "end_tick": 250,
                    "start_inclusive": True,
                    "end_exclusive": True,
                }
                model_calls.append((request.role_task, body["meeting"]["current_tick"]))
                return SimpleNamespace(
                    normalized_output={"option_id": "attend"},
                    call_trace=SimpleNamespace(request_id="meeting-after-shift-call"),
                )

        model = MeetingModel()

        to_shift_start = jobs.advance(scope, lease)
        assert to_shift_start.outcome == "advanced" and to_shift_start.next_tick == 100
        started = jobs.advance(scope, lease)
        start_event, = started.events
        assert isinstance(start_event, NpcAttendanceStartedEvent)
        assert started.next_tick == started.metrics.from_tick == 100
        assert started.metrics.ticks_advanced == 0
        active = repository.read_world(scope, world_id, branch_id)
        attendance, = active.state.npc_attendance
        assert (attendance.actor_id, attendance.starts_at_tick, attendance.ends_at_tick) == (worker_id, 100, 200)

        # The invitation opens while the registered shift is still active. The
        # worker advances to the shift end and does not request a model choice.
        to_meeting_start = jobs.advance(scope, lease)
        assert to_meeting_start.outcome == "advanced" and to_meeting_start.next_tick == 150
        assert model_calls == []
        busy_window = jobs.advance(scope, lease)
        assert busy_window.outcome == "advanced" and busy_window.next_tick == 200
        assert model_calls == []
        at_end = repository.read_world(scope, world_id, branch_id)
        assert at_end.state.current_tick == 200
        assert len(at_end.state.npc_attendance) == 1
        assert at_end.state.npc_attendance[0].ends_at_tick == 200

        settled = jobs.advance(scope, lease)
        settlement, = settled.events
        assert isinstance(settlement, NpcAttendanceSettledEvent)
        assert settlement.effective_tick == settlement.attendance.ends_at_tick == 200
        assert settlement.payment is not None
        assert settled.next_tick == settled.metrics.from_tick == 200
        assert settled.metrics.ticks_advanced == 0
        after_settlement = repository.read_world(scope, world_id, branch_id)
        assert after_settlement.state.current_tick == 200
        assert after_settlement.state.npc_attendance == []
        assert after_settlement.state.resources[worker_account_id].balance == Decimal("3.50")
        assert after_settlement.state.resources[employer_account_id].balance == Decimal("7.50")
        assert model_calls == []

        pending = jobs.advance(scope, lease)
        assert pending.outcome == "decision_required"
        decision, = pending.decision_requests
        assert decision.kind == "npc.meeting_attendance"
        assert decision.at_tick == 200
        answer = await _invoke_worker_model_decision(
            model, repository, scope, lease, repository.read_job(scope, job.job_id), decision,
        )
        assert answer.option_id == "attend"
        attended = jobs.advance(scope, lease, model_decision=answer)
        assert attended.outcome == "advanced"
        assert attended.next_tick == attended.metrics.from_tick == 200
        assert attended.metrics.ticks_advanced == 0
        marker = next(event for event in attended.events if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent))
        assert marker.record.choice == "attend"
        assert marker.record.actor_id == worker_id
        final_at_meeting = repository.read_world(scope, world_id, branch_id)
        invitation = next(
            item for item in final_at_meeting.state.relationship_invitations
            if item.invitation_id == invitation_id
        )
        assert [(row.actor_id, row.attended_tick) for row in invitation.meeting_attendance] == [(worker_id, 200)]
        assert final_at_meeting.state.current_tick == 200
        assert model_calls == [(RoleTask.NPC_DECISION, 200)]

        finished = jobs.advance(scope, lease)
        assert finished.outcome == "completed"
        assert repository.read_job(scope, job.job_id).status.value == "succeeded"
        assert model_calls == [(RoleTask.NPC_DECISION, 200)]
    finally:
        database.dispose()



@pytest.mark.asyncio
async def test_npc_meeting_commitment_prepares_journeys_and_expires_only_the_unfulfilled_goal(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'meeting-commitments.db').as_posix()}")
    db.create_schema()
    restored_db = Database(f"sqlite:///{(tmp_path / 'meeting-commitments-restored.db').as_posix()}")
    restored_db.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player, robin, alex, village, venue, route = (uuid4() for _ in range(6))
    people = {ident: _character(ident, name, village) for ident, name in ((player, "Observer"), (robin, "Robin"), (alex, "Alex"))}
    for ident in (robin, alex):
        character = people[ident]
        people[ident] = character.model_copy(update={"components": character.components | {
            "known_places": [str(village), str(venue)], "capabilities": {"grants": ["world.travel"]},
        }})
    state = WorldState(
        player_id=player,
        calendar={"day_seconds": DAY_SECONDS, "year_days": YEAR_DAYS},
        entities=people | {
            village: WorldEntity(id=village, kind="place", name="Workshop", components={"place": {
                "place_type": "settlement", "routes": [{"route_id": route, "from_place_id": village, "to_place_id": venue, "travel_ticks": 60}],
            }}),
            venue: WorldEntity(id=venue, kind="place", name="Gardens", components={"place": {"place_type": "site"}}),
        },
    )
    try:
        repository = WorldRepository(db)
        draft = NewWorld(title="Booked meeting", state=state)
        repository.create_world(scope, draft)
        world, branch = draft.world_id, draft.branch_id
        _, proposal = _commit_invitation_action(repository, scope, world, branch, robin, "propose_relationship_invitation", {
            "target_id": str(alex), "meeting": {"start_tick": 300, "end_tick": 400, "venue_id": str(venue)},
        }, key="propose-venue")
        invitation_id = proposal.resulting_invitation.invitation_id
        assert not repository.read_world(scope, world, branch).state.goals
        _commit_invitation_action(repository, scope, world, branch, alex, "resolve_relationship_invitation", {
            "invitation_id": str(invitation_id), "operation": "accept",
        }, key="accept-venue")
        agreed = repository.read_world(scope, world, branch)
        assert {goal.owner_id for goal in agreed.state.goals} == {alex, robin}
        assert {row.due_tick for row in agreed.state.scheduled_events if row.handler_id == "npc.goal_review"} == {2}
        expiry, = [row for row in agreed.state.scheduled_events if row.handler_id == MEETING_COMMITMENT_HANDLER]
        assert expiry.due_tick == 400
        fork = repository.fork(scope, world, branch, agreed.last_seq, "Keep accepted proposals").branch_id

        class AttendanceModel:
            async def generate(self, identity, request):
                context = json.loads(request.messages[-1].content)["meeting"]
                assert context["meeting_interval"]["venue_name"] == "Gardens"
                actor_name = context["actor"]["name"]
                assert actor_name in {"Robin", "Alex"}
                return SimpleNamespace(normalized_output={"option_id": "attend" if actor_name == "Robin" else "defer"},
                                       call_trace=SimpleNamespace(request_id=f"choice-{actor_name}"))

        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=401),
            idempotency_key="meeting-commitment", speed="balanced", depth="standard",
            expected_state_version=agreed.branch.state_version)
        lease = repository.claim_job("meeting-preparer")
        assert lease and lease.job_id == job.job_id
        review_ticks = {}
        for _ in range(65):
            if repository.read_job(scope, job.job_id).status == "succeeded":
                break
            outcome = jobs.advance(scope, lease)
            if not outcome.decision_requests:
                continue
            request, = outcome.decision_requests
            current = repository.read_world(scope, world, branch)
            if request.kind == "npc.plan_review":
                actor_id = request.actor_id
                assert actor_id in {robin, alex}
                review_ticks.setdefault(actor_id, []).append(request.at_tick)
                is_already_there = current.state.entities[actor_id].location_id == venue
                plan = None
                if not is_already_there:
                    goal, = [goal for goal in current.state.goals if goal.owner_id == actor_id]
                    plan = NpcPlan(
                        plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor_id}:{request.state_version}"),
                        actor_id=actor_id, goal_ids=[goal.id], revision=1, status="ready", current_step=0,
                        created_tick=request.at_tick, updated_tick=request.at_tick, next_review_tick=request.at_tick,
                        steps=[NpcPlanStep(capability_id="world.action.move.v1",
                                           arguments={"destination_id": str(venue), "route_id": str(route)})],
                        reason="Walk to the agreed venue before the appointed time.",
                    )
                answer = DecisionResponse(
                    request_id=request.request_id, state_version=request.state_version, at_tick=request.at_tick,
                    source="policy", option_id="plan", npc_planning_status="ready" if plan is not None else "defer",
                    npc_plan=plan, npc_plan_state_hash=current.state_hash,
                )
                jobs.advance(scope, lease, model_decision=answer)
            else:
                assert request.kind == "npc.meeting_attendance" and request.at_tick == 300
                answer = await _invoke_worker_model_decision(
                    AttendanceModel(), repository, scope, lease, repository.read_job(scope, job.job_id), request,
                )
                jobs.advance(scope, lease, model_decision=answer)

        assert repository.read_job(scope, job.job_id).status == "succeeded"
        final = repository.read_world(scope, world, branch)
        assert final.state.entities[player].location_id == village
        assert {trip.actor_id for trip in final.state.travel_journeys if trip.arrival_tick == 62} == {robin, alex}
        assert {actor: ticks[0] for actor, ticks in review_ticks.items()} == {robin: 2, alex: 2}
        assert {(goal.owner_id, goal.status, goal.updated_tick) for goal in final.state.goals} == {
            (robin, "succeeded", 300), (alex, "failed", 400),
        }
        invitation, = final.state.relationship_invitations
        assert invitation.status == "accepted" and [row.actor_id for row in invitation.meeting_attendance] == [robin]
        assert repository.read_world(scope, world, fork).state_hash == agreed.state_hash
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash

        backup = BackupApplication(repository)
        restored = WorldRepository(restored_db)
        with backup.validate_archive_file_streaming(BytesIO(backup.export_world(scope, world))) as verified:
            WorldBackupImporter(restored).import_verified_backup(scope, verified)
        assert restored.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
    finally:
        restored_db.dispose()
        db.dispose()
