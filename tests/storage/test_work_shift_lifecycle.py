"""SQLite proof for paid work-shift timeline, validation and restore."""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.components import NeedValue, NeedsComponent, TreasuryComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.domain.occupation_events import OccupationSource
from world_engine.domain.occupations import WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.work_shift import PaidWorkShiftPlan, WorkShiftLedgerEntry, WorkShiftTerms
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.simulation.contracts import LongActionPlan, RoutineWorkSchedule
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.models import LedgerPostingRow
from world_engine.storage.repository import WorldRepository


SHIFT_SECONDS = 86_400
RESOURCE_ID = "core.credits"
WAGE = Decimal("20.00")


def _state(*, employer_balance: str = "100.00"):
    worker_id, employer_id, workplace_id, worker_account_id, employer_account_id, posting_id = (
        uuid4() for _ in range(6)
    )
    source = OccupationSource(kind="genesis", reference_id="fixture.initial-employment")
    assignment_id = occupation_assignment_id(source, worker_id, posting_id)
    needs = NeedsComponent(
        food=NeedValue(value=90),
        water=NeedValue(value=90),
        rest=NeedValue(value=90),
    )
    terms = WorkShiftTerms(
        posting_id=posting_id,
        duration_seconds=SHIFT_SECONDS,
        resource_id=RESOURCE_ID,
        wage=WAGE,
    )
    state = WorldState(
        current_tick=0,
        player_id=worker_id,
        entities={
            worker_id: WorldEntity(
                id=worker_id,
                kind="character",
                name="Nadiya Patel",
                location_id=workplace_id,
                components={"needs": needs.model_dump(mode="json")},
            ),
            employer_id: WorldEntity(
                id=employer_id,
                kind="organization",
                name="Canal Survey Office",
                location_id=workplace_id,
                components={
                    "treasury": TreasuryComponent(
                        accounts={RESOURCE_ID: employer_account_id},
                    ).model_dump(mode="json"),
                },
            ),
            workplace_id: WorldEntity(
                id=workplace_id, kind="place", name="Survey Office",
            ),
        },
        resources={
            worker_account_id: ResourceAccount(
                resource_id=RESOURCE_ID, owner_id=worker_id, balance="2.25",
            ),
            employer_account_id: ResourceAccount(
                resource_id=RESOURCE_ID, owner_id=employer_id, balance=employer_balance,
            ),
        },
        work_postings=[WorkPosting(
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=workplace_id,
            title="Canal Surveyor",
            slots=1,
            valid_from_tick=0,
            revision=3,
        )],
        work_assignments=[WorkAssignment(
            assignment_id=assignment_id,
            actor_id=worker_id,
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=workplace_id,
            title="Canal Surveyor",
            started_tick=0,
            revision=2,
            source=source,
        )],
        metadata={"work_shift_terms": [terms.model_dump(mode="json")]},
    )
    return state, worker_id, employer_id, worker_account_id, employer_account_id, assignment_id


def _command_and_resolution(state, *, world_id, branch_id, ruleset_version, worker_id, assignment_id):
    payload = {"kind": "work_shift", "assignment_id": str(assignment_id)}
    intent = parse_action(payload)
    normalized = intent.model_dump(mode="json")
    command = CommandEnvelope(
        actor_id=worker_id,
        idempotency_key="paid-work-shift-001",
        expected_state_version=0,
        command_type="action.work_shift",
        payload=normalized,
    )
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command.command_id,
        actor_id=worker_id,
        ruleset_version=ruleset_version,
    )
    resolution = RuleEngine().evaluate(state, intent, context)
    return command, normalized, resolution


def _forged_payment(event: WorkShiftPaidEvent) -> WorkShiftPaidEvent:
    """Return a structurally valid but rule-inconsistent one-unit wage plan."""

    plan_values = event.plan.model_dump(mode="python")
    forged_wage = Decimal("1.00")
    terms_values = event.plan.terms.model_dump(mode="python")
    plan_values["terms"] = WorkShiftTerms.model_validate(terms_values | {"wage": forged_wage})
    worker, employer = event.plan.ledger_entries
    worker_values = worker.model_dump(mode="python")
    employer_values = employer.model_dump(mode="python")
    plan_values["ledger_entries"] = (
        WorkShiftLedgerEntry.model_validate(worker_values | {
            "amount": forged_wage,
            "balance_after": worker.balance_before + forged_wage,
        }),
        WorkShiftLedgerEntry.model_validate(employer_values | {
            "amount": forged_wage.copy_negate(),
            "balance_after": employer.balance_before - forged_wage,
        }),
    )
    forged_plan = PaidWorkShiftPlan.model_validate(plan_values)
    return WorkShiftPaidEvent.model_validate(
        event.model_dump(mode="python") | {"plan": forged_plan},
    )


def test_leased_shift_requires_the_authorized_occurrence(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'leased-work.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        state, worker_id, _, worker_account, _, assignment_id = _state()
        created = repository.create_world(scope, NewWorld(title="Recurring work", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        before = repository.read_world(scope, world_id, branch_id)
        jobs = SimulationJobs(repository)
        command, _, resolution = _command_and_resolution(
            state, world_id=world_id, branch_id=branch_id,
            ruleset_version=created.world.ruleset_version,
            worker_id=worker_id, assignment_id=assignment_id,
        )

        def enqueue(first_shift_tick):
            plan = LongActionPlan(
                actor_id=worker_id, started_tick=0, target_tick=SHIFT_SECONDS * 3, activity="work",
                work_schedule=RoutineWorkSchedule(
                    assignment_id=assignment_id, first_shift_tick=first_shift_tick, repeat_every_days=1,
                ),
            )
            return jobs.enqueue(scope, world_id, branch_id, plan, idempotency_key=f"shift-at-{first_shift_tick}",
                                speed="balanced", depth="deep")

        later = enqueue(60)
        lease = repository.claim_job("later-worker")
        assert lease is not None and lease.job_id == later.job_id
        with pytest.raises(InvalidEvent, match="outside the authorized schedule"):
            repository.commit_command(scope, world_id, branch_id, command, resolution.events, lease=lease)
        assert repository.read_world(scope, world_id, branch_id).state_hash == before.state_hash
        repository.finish_job(lease, JobStatus.CANCELLED)

        due = enqueue(0)
        lease = repository.claim_job("due-worker")
        assert lease is not None and lease.job_id == due.job_id
        repository.commit_command(scope, world_id, branch_id, command, resolution.events, lease=lease)
        repository.commit_command(scope, world_id, branch_id, command, resolution.events, lease=lease)
        after = repository.read_world(scope, world_id, branch_id)
        assert after.state.resources[worker_account].balance == state.resources[worker_account].balance + WAGE
        saved = repository.read_job(scope, due.job_id)
        assert saved.committed_through_tick == SHIFT_SECONDS
        progress = saved.payload["work_progress"]
        assert progress == {
            "shifts_paid": 1, "wages_by_resource": {RESOURCE_ID: "20.00"},
            "last_paid_tick": str(SHIFT_SECONDS), "balance_resource_id": RESOURCE_ID,
            "balance_after_last_pay": "22.25",
        }
        observer_id = uuid4()
        repository.grant_membership(scope, world_id, observer_id, MembershipRole.OBSERVER)
        application = WorldApplication(repository, ContentCatalog())
        for user_id in (scope.user_id, observer_id):
            session = LocalSession(str(user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
            with TestClient(create_app(application, session)) as client:
                client.get("/api/v1/session").raise_for_status()
                detail = client.get(f"/api/v1/jobs/{due.job_id}")
                if user_id == scope.user_id:
                    assert detail.raise_for_status().json()["work_progress"] == progress
                else:
                    assert detail.status_code == 404
                    listed = client.get(f"/api/v1/worlds/{world_id}/jobs", params={"branch_id": str(branch_id)}).raise_for_status().json()
                    assert listed["items"] == []
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


def test_paused_work_adopts_manual_care_and_keeps_the_original_remaining_schedule(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'work-resume.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        state, worker_id, _, worker_account, _, assignment_id = _state()
        worker = state.entities[worker_id]
        state = state.model_copy(update={"entities": state.entities | {worker_id: worker.model_copy(update={
            "components": worker.components | {"needs": NeedsComponent(rest=NeedValue(value=100)).model_dump(mode="json")},
        })}})
        created = repository.create_world(scope, NewWorld(title="Resume recurring work", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        plan = LongActionPlan(
            actor_id=worker_id, started_tick=0, target_tick=SHIFT_SECONDS * 3, activity="work",
            work_schedule=RoutineWorkSchedule(
                assignment_id=assignment_id, first_shift_tick=0, repeat_every_days=1,
            ),
        )
        job = jobs.enqueue(scope, world_id, branch_id, plan, idempotency_key="resume-scheduled-shifts",
                           speed="balanced", depth="deep")
        lease = repository.claim_job("first-shift")
        assert lease is not None and lease.job_id == job.job_id
        assert jobs.advance(scope, lease).next_tick == SHIFT_SECONDS
        repository.control_job(scope, job.job_id, "pause")
        before_rest = repository.read_world(scope, world_id, branch_id)

        rest = parse_action({"kind": "rest", "duration_ticks": 600})
        command = CommandEnvelope(
            actor_id=worker_id, idempotency_key="rest-between-paid-windows",
            expected_state_version=before_rest.branch.state_version, command_type="action.rest",
            payload=rest.model_dump(mode="json"),
        )
        result = RuleEngine().evaluate(before_rest.state, rest, RuleContext(
            world_id=world_id, branch_id=branch_id, command_id=command.command_id,
            actor_id=worker_id, ruleset_version=created.world.ruleset_version,
        ))
        repository.commit_command(scope, world_id, branch_id, command, result.events)
        rested = repository.read_world(scope, world_id, branch_id)
        assert rested.state.current_tick == SHIFT_SECONDS + 600

        application = WorldApplication(repository, ContentCatalog())
        session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        with TestClient(create_app(application, session)) as client:
            client.get("/api/v1/session").raise_for_status()
            endpoint = f"/api/v1/jobs/{job.job_id}"
            for _ in range(2):
                assert client.post(endpoint + "/control", json={"action": "resume"}).raise_for_status().json()["status"] == "queued"
            public = client.get(endpoint).raise_for_status().json()
            assert public["work_resume"] == {"skipped_shifts": 1, "next_shift_tick": str(SHIFT_SECONDS * 2)}
        assert repository.read_world(scope, world_id, branch_id).state_hash == rested.state_hash

        lease = repository.claim_job("remaining-shifts")
        assert lease is not None and lease.job_id == job.job_id
        for _ in range(8):
            jobs.advance(scope, lease)
            saved = repository.read_job(scope, job.job_id)
            if saved.status != JobStatus.RUNNING:
                break
        assert saved.status == JobStatus.SUCCEEDED
        assert saved.payload["plan"] == plan.model_dump(mode="json")
        assert saved.payload["work_progress"]["shifts_paid"] == 2
        records = repository.list_events(scope, world_id, branch_id, limit=100).events
        assert [record.payload["plan"]["shift_started_tick"] for record in records if record.type == "core.work_shift_paid.v1"] == [
            0, SHIFT_SECONDS * 2,
        ]
        after = repository.read_world(scope, world_id, branch_id)
        assert after.state.current_tick == SHIFT_SECONDS * 3
        assert after.state.resources[worker_account].balance == Decimal("42.25")
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


def test_paid_work_shift_timeline_posts_exact_wage_and_restores(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'work-shift-source.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    restored_database = Database(f"sqlite:///{(tmp_path / 'work-shift-restored.db').as_posix()}")
    restored_database.create_schema()
    restored_repository = WorldRepository(restored_database)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        state, worker_id, employer_id, worker_account_id, employer_account_id, assignment_id = _state()
        created = repository.create_world(scope, NewWorld(
            title="Paid work shift",
            ruleset_version="work-shift-test.v1",
            state=state,
        ))
        world_id, branch_id, version = created.world.world_id, created.branch.branch_id, created.world.ruleset_version
        command, normalized, result = _command_and_resolution(
            state, world_id=world_id, branch_id=branch_id, ruleset_version=version,
            worker_id=worker_id, assignment_id=assignment_id,
        )

        assert normalized == {"kind": "work_shift", "assignment_id": str(assignment_id)}
        assert result.outcome.status == "succeeded"
        assert result.elapsed_ticks == SHIFT_SECONDS
        assert result.events[0].type == "core.time_advanced.v1"
        assert result.events[0].to_tick == SHIFT_SECONDS
        needs_events = [event for event in result.events if getattr(event, "component_name", None) == "needs"]
        assert len(needs_events) == 1
        payment_events = [event for event in result.events if isinstance(event, WorkShiftPaidEvent)]
        assert len(payment_events) == 1
        payment = payment_events[0]
        assert payment.plan.actor_id == worker_id
        assert payment.plan.completed_tick == SHIFT_SECONDS
        assert [entry.amount for entry in payment.plan.ledger_entries] == [WAGE, -WAGE]
        assert sum((entry.amount for entry in payment.plan.ledger_entries), Decimal("0")) == 0

        # The repository rejects omitted or forged effects before any part of
        # the clock, needs, wage, event or ledger transaction can commit.
        with pytest.raises(InvalidEvent, match="wage settlement"):
            repository.commit_command(
                scope, world_id, branch_id, command,
                [event for event in result.events if not isinstance(event, WorkShiftPaidEvent)],
            )
        forged_events = [
            _forged_payment(event) if isinstance(event, WorkShiftPaidEvent) else event
            for event in result.events
        ]
        with pytest.raises(InvalidEvent, match="rule-generated clock, needs and wage events"):
            repository.commit_command(scope, world_id, branch_id, command, forged_events)
        unchanged = repository.read_world(scope, world_id, branch_id)
        assert unchanged.last_seq == 0
        assert unchanged.branch.state_version == 0
        assert unchanged.state.current_tick == 0
        assert unchanged.state.resources[worker_account_id].balance == Decimal("2.25")
        assert unchanged.state.resources[employer_account_id].balance == Decimal("100.00")
        assert repository.replay(scope, world_id, branch_id).state_hash == unchanged.state_hash

        committed = repository.commit_command(scope, world_id, branch_id, command, result.events)
        after = repository.read_world(scope, world_id, branch_id)
        assert after.branch.state_version == 1
        assert after.state.current_tick == SHIFT_SECONDS
        assert after.state.resources[worker_account_id].balance == Decimal("22.25")
        assert after.state.resources[employer_account_id].balance == Decimal("80.00")
        needs = NeedsComponent.model_validate(after.state.entities[worker_id].components["needs"])
        assert (needs.food.value, needs.water.value, needs.rest.value) == (72, 60, 66)

        with database.session_factory() as session:
            ledger = session.scalars(
                select(LedgerPostingRow)
                .where(LedgerPostingRow.event_id == payment.event_id)
                .order_by(LedgerPostingRow.posting_index)
            ).all()
        assert [
            (row.account_id, row.resource_id, Decimal(row.delta)) for row in ledger
        ] == [
            (worker_account_id, RESOURCE_ID, WAGE),
            (employer_account_id, RESOURCE_ID, -WAGE),
        ]
        assert sum((Decimal(row.delta) for row in ledger), Decimal("0")) == 0
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == committed.state_hash

        audit = repository.list_events(scope, world_id, branch_id, after_seq=0)
        paid_audit = next(row for row in audit.events if row.type == "core.work_shift_paid.v1")
        assert paid_audit.visibility == {"policy": "author_only"}
        assert paid_audit.actor_id == worker_id

        backup_app = BackupApplication(repository)
        with backup_app.validate_archive(backup_app.export_world(scope, world_id)) as verified:
            restored = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
            assert restored.world_id == world_id
            assert restored.event_count == len(result.events)
        restored_view = restored_repository.read_world(scope, world_id, branch_id)
        assert restored_view.state_hash == after.state_hash
        assert restored_view.state.resources[worker_account_id].balance == Decimal("22.25")
        assert restored_repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash

        poor_state, poor_worker, _, poor_worker_account, poor_employer_account, poor_assignment = _state(
            employer_balance="5.00",
        )
        poor_world = repository.create_world(scope, NewWorld(
            title="Underfunded work shift",
            ruleset_version="work-shift-test.v1",
            state=poor_state,
        ))
        poor_command, _poor_normalized, poor_result = _command_and_resolution(
            poor_state, world_id=poor_world.world.world_id, branch_id=poor_world.branch.branch_id,
            ruleset_version=poor_world.world.ruleset_version, worker_id=poor_worker,
            assignment_id=poor_assignment,
        )
        assert poor_result.outcome.status == "rejected"
        assert poor_result.outcome.code == "insufficient_employer_funds"
        assert poor_result.events == []
        assert poor_result.elapsed_ticks == 0
        unchanged_poor = repository.read_world(scope, poor_world.world.world_id, poor_world.branch.branch_id)
        assert unchanged_poor.state.current_tick == 0
        assert unchanged_poor.branch.state_version == 0
        assert unchanged_poor.state.resources[poor_worker_account].balance == Decimal("2.25")
        assert unchanged_poor.state.resources[poor_employer_account].balance == Decimal("5.00")
    finally:
        restored_database.dispose()
        database.dispose()
