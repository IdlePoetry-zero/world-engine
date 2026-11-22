"""SQLite integration coverage for bilateral institutional credit extensions."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.errors import StaleVersion
from world_engine.domain.events import parse_event
from world_engine.domain.institutional_credit_events import InstitutionalCreditChangedEvent
from world_engine.domain.institutional_credit_extension_events import InstitutionalCreditExtensionChangedEvent
from world_engine.domain.institutional_credit_state import (
    CREDIT_MATURITY_HANDLER,
    credit_maturity_schedule,
)
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


CURRENCY_ID = "modern.credit"
RULESET = "institutional-credit-extension-storage.v1"
PRINCIPAL_MINOR = 100


def _organization(organization_id, signer_id, account_id):
    governance = GovernanceComponent(
        members=[signer_id],
        leader_id=signer_id,
        offices=[OfficeRecord(
            office_id="office.leader",
            title="Treasury Representative",
            holder_id=signer_id,
            authority=["organization.lead", "authority.represent", "authority.treasury"],
        )],
        succession_policy="appointment",
    )
    return WorldEntity(
        id=organization_id,
        kind="organization",
        name=f"Institution {organization_id.hex[:6]}",
        components={
            "governance": governance.model_dump(mode="json"),
            "treasury": TreasuryComponent(accounts={CURRENCY_ID: account_id}).model_dump(mode="json"),
        },
    )


def _initial_world():
    lender_id, borrower_id = uuid4(), uuid4()
    lender_signer_id, borrower_signer_id = uuid4(), uuid4()
    lender_treasury_id, borrower_treasury_id = uuid4(), uuid4()
    state = WorldState(
        player_id=lender_signer_id,
        entities={
            lender_signer_id: WorldEntity(id=lender_signer_id, kind="character", name="Lender representative"),
            borrower_signer_id: WorldEntity(id=borrower_signer_id, kind="character", name="Borrower representative"),
            lender_id: _organization(lender_id, lender_signer_id, lender_treasury_id),
            borrower_id: _organization(borrower_id, borrower_signer_id, borrower_treasury_id),
        },
        resources={
            lender_treasury_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance=str(PRINCIPAL_MINOR), owner_id=lender_id,
            ),
            borrower_treasury_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance="0", owner_id=borrower_id,
            ),
        },
    )
    return state, {
        "lender": lender_id,
        "borrower": borrower_id,
        "lender_signer": lender_signer_id,
        "borrower_signer": borrower_signer_id,
        "lender_treasury": lender_treasury_id,
        "borrower_treasury": borrower_treasury_id,
    }


def _evaluate(repository, scope, world_id, branch_id, actor_id, payload):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    action = parse_action(payload)
    resolution = RuleEngine().evaluate(
        view.state,
        action,
        RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=actor_id,
            ruleset_version=view.world.ruleset_version,
        ),
    )
    return view, command_id, action.model_dump(mode="json"), resolution


def _commit_rule_action(repository, scope, world_id, branch_id, actor_id, payload, *, idempotency_key):
    view, command_id, normalized, resolution = _evaluate(
        repository, scope, world_id, branch_id, actor_id, payload,
    )
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    assert len(resolution.events) == 1
    (event,) = resolution.events
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_state_version=view.branch.state_version,
        command_type=f"action.{normalized['kind']}",
        payload=normalized,
    )
    committed = repository.commit_command(scope, world_id, branch_id, command, [event])
    return event, committed, resolution


def _advance_world(repository, scope, world_id, branch_id, target_tick, idempotency_key):
    view = repository.read_world(scope, world_id, branch_id)
    job = SimulationJobs(repository).enqueue_world_advance(
        scope,
        world_id,
        branch_id,
        WorldAdvancePlan(started_tick=view.state.current_tick, target_tick=target_tick),
        idempotency_key=idempotency_key,
        speed="fast",
        depth="standard",
        expected_state_version=view.branch.state_version,
    )
    lease = repository.claim_job("credit-extension-maturity-worker", job_type=job.job_type)
    assert lease is not None
    simulation_jobs = SimulationJobs(repository)
    for _ in range(24):
        step = simulation_jobs.advance(scope, lease)
        saved = repository.read_job(scope, job.job_id)
        if saved.status.value == "succeeded":
            break
        assert saved.status.value not in {"failed", "paused", "cancelled"}, step
        assert getattr(step, "outcome", None) != "decision_required", step
    assert repository.read_job(scope, job.job_id).status.value == "succeeded"
    return repository.read_world(scope, world_id, branch_id)


def _typed_events(repository, scope, world_id, branch_id):
    page = repository.list_events(scope, world_id, branch_id, limit=100)
    return [parse_event({
        **record.payload,
        "type": record.type,
        "event_id": str(record.event_id),
        "visibility": record.visibility,
    }) for record in page.events]


def test_bilateral_credit_extension_replaces_overdue_boundary_and_replays(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'institutional-credit-extension.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    owner_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    source_state, ids = _initial_world()
    created = repository.create_world(
        owner_scope,
        NewWorld(title="Bilateral credit extension", ruleset_version=RULESET, state=source_state),
    )
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    lender_user, borrower_user = uuid4(), uuid4()
    repository.grant_membership(
        owner_scope, world_id, lender_user, MembershipRole.PLAYER,
        controlled_actor_ids=[ids["lender_signer"]],
    )
    repository.grant_membership(
        owner_scope, world_id, borrower_user, MembershipRole.PLAYER,
        controlled_actor_ids=[ids["borrower_signer"]],
    )
    lender_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=lender_user)
    borrower_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=borrower_user)

    try:
        offer_payload = {
            "kind": "offer_institutional_credit",
            "lender_organization_id": str(ids["lender"]),
            "borrower_organization_id": str(ids["borrower"]),
            "currency_resource_id": CURRENCY_ID,
            "principal_minor": PRINCIPAL_MINOR,
            "accepted_through_tick": 1,
            "maturity_tick": 2,
        }
        offer_event, _, offer_resolution = _commit_rule_action(
            repository, lender_scope, world_id, branch_id, ids["lender_signer"], offer_payload,
            idempotency_key="extension-test-credit-offer",
        )
        assert offer_resolution.outcome.code == "institutional_credit_offer"
        (offered,) = offer_event.resulting_contract,

        accept_payload = {
            "kind": "resolve_institutional_credit",
            "credit_id": str(offered.credit_id),
            "operation": "accept",
            "expected_revision": offered.revision,
        }
        acceptance_event, _, acceptance_resolution = _commit_rule_action(
            repository, borrower_scope, world_id, branch_id, ids["borrower_signer"], accept_payload,
            idempotency_key="extension-test-credit-accept",
        )
        assert acceptance_resolution.outcome.code == "institutional_credit_accept"
        active = acceptance_event.resulting_contract
        (original_schedule,) = repository.read_world(owner_scope, world_id, branch_id).state.scheduled_events
        assert original_schedule == credit_maturity_schedule(active)
        assert original_schedule.due_tick == active.maturity_tick + 1 == 3

        # Let the canonical worker cross the old inclusive maturity and record
        # arrears exactly at tick three. Its completed boundary is the one that
        # the accepted bilateral extension must replace.
        overdue_view = _advance_world(
            repository, owner_scope, world_id, branch_id, 3, "extension-test-to-original-overdue",
        )
        (overdue_contract,) = overdue_view.state.institutional_credit_contracts
        assert overdue_view.state.current_tick == 3
        assert overdue_contract.status == "overdue"
        assert overdue_contract.overdue_at_tick == 3
        (old_completed_schedule,) = overdue_view.state.scheduled_events
        assert old_completed_schedule.id == original_schedule.id
        assert old_completed_schedule.status == "completed"

        # Keep a sibling branch at the same overdue snapshot as the control:
        # extension commits on the source branch must not revise its contract,
        # schedule, or state hash.
        contrast_branch = repository.fork(
            owner_scope, world_id, branch_id, overdue_view.last_seq, "No extension control",
        )
        contrast_branch_id = contrast_branch.branch_id
        contrast_before = repository.read_world(owner_scope, world_id, contrast_branch_id)

        resources_before_extension = dict(overdue_view.state.resources)
        proposal_payload = {
            "kind": "propose_credit_extension",
            "credit_id": str(overdue_contract.credit_id),
            "organization_id": str(ids["borrower"]),
            "expected_revision": overdue_contract.revision,
            "new_maturity_tick": 10,
            "accept_through_tick": 5,
        }
        proposal_event, _, proposal_resolution = _commit_rule_action(
            repository, borrower_scope, world_id, branch_id, ids["borrower_signer"], proposal_payload,
            idempotency_key="extension-test-borrower-proposes",
        )
        assert isinstance(proposal_event, InstitutionalCreditExtensionChangedEvent)
        assert proposal_event.operation == "propose"
        proposed_contract = proposal_event.resulting_contract
        proposal = proposed_contract.extensions[-1]
        assert proposal.status == "proposed"
        assert proposed_contract.maturity_tick == overdue_contract.maturity_tick == 2
        assert proposed_contract.overdue_at_tick == overdue_contract.overdue_at_tick == 3
        proposed_view = repository.read_world(owner_scope, world_id, branch_id)
        assert proposed_view.state.current_tick == 3
        assert proposed_view.state.resources == resources_before_extension
        assert proposed_view.state.scheduled_events == overdue_view.state.scheduled_events

        # The proposing organization cannot consent to its own proposal.
        self_accept_payload = {
            "kind": "resolve_credit_extension",
            "credit_id": str(overdue_contract.credit_id),
            "extension_id": str(proposal_event.extension_id),
            "operation": "accept",
            "expected_revision": proposed_contract.revision,
        }
        _, _, _, self_accept_resolution = _evaluate(
            repository, borrower_scope, world_id, branch_id, ids["borrower_signer"], self_accept_payload,
        )
        assert self_accept_resolution.outcome.status == "rejected"
        assert self_accept_resolution.events == []
        after_self_accept = repository.read_world(owner_scope, world_id, branch_id)
        assert after_self_accept.state_hash == proposed_view.state_hash
        assert after_self_accept.last_seq == proposed_view.last_seq

        # The lender's separate representative accepts at the proposal's
        # current contract revision, preserving the prior breach in extension
        # history while resetting the live contract to its new window.
        accept_extension_payload = self_accept_payload | {
            "operation": "accept",
            "expected_revision": proposed_contract.revision,
        }
        extension_event, _, extension_resolution = _commit_rule_action(
            repository, lender_scope, world_id, branch_id, ids["lender_signer"], accept_extension_payload,
            idempotency_key="extension-test-lender-accepts",
        )
        assert isinstance(extension_event, InstitutionalCreditExtensionChangedEvent)
        assert extension_event.operation == "accept"
        assert extension_event.extension_id == proposal_event.extension_id
        assert extension_resolution.outcome.code == "credit_extension_accept"
        extended_contract = extension_event.resulting_contract
        accepted_extension = extended_contract.extensions[-1]
        assert accepted_extension.status == "accepted"
        assert accepted_extension.previous_overdue_tick == 3
        assert extended_contract.status == "active"
        assert extended_contract.maturity_tick == 10
        assert extended_contract.overdue_at_tick is None
        assert extended_contract.updated_tick == 3

        extended_view = repository.read_world(owner_scope, world_id, branch_id)
        assert extended_view.state.current_tick == 3
        assert extended_view.state.resources == resources_before_extension
        credit_schedules = [
            row for row in extended_view.state.scheduled_events if row.handler_id == CREDIT_MATURITY_HANDLER
        ]
        (new_schedule,) = credit_schedules
        assert new_schedule == credit_maturity_schedule(extended_contract)
        assert new_schedule.id != original_schedule.id
        assert new_schedule.due_tick == 11
        assert new_schedule.status == "queued"
        assert all(row.id != original_schedule.id for row in extended_view.state.scheduled_events)

        # The control branch still carries the old completed overdue schedule
        # and has no extension in its contract history.
        contrast_after = repository.read_world(owner_scope, world_id, contrast_branch_id)
        (contrast_contract,) = contrast_after.state.institutional_credit_contracts
        assert contrast_after.state_hash == contrast_before.state_hash
        assert contrast_contract.status == "overdue"
        assert contrast_contract.extensions == ()
        assert contrast_after.state.scheduled_events == [old_completed_schedule]

        # Existing worker dispatch follows the replaced state schedule: tick
        # ten is still inclusive and active, and the new arrears event fires
        # exactly one tick later at eleven.
        at_extended_maturity = _advance_world(
            repository, owner_scope, world_id, branch_id, 10, "extension-test-to-new-maturity",
        )
        (active_at_new_maturity,) = at_extended_maturity.state.institutional_credit_contracts
        assert at_extended_maturity.state.current_tick == 10
        assert active_at_new_maturity.status == "active"
        assert active_at_new_maturity.overdue_at_tick is None
        (queued_at_new_maturity,) = [
            row for row in at_extended_maturity.state.scheduled_events
            if row.handler_id == CREDIT_MATURITY_HANDLER
        ]
        assert queued_at_new_maturity == new_schedule

        overdue_again = _advance_world(
            repository, owner_scope, world_id, branch_id, 11, "extension-test-to-new-overdue",
        )
        (new_overdue_contract,) = overdue_again.state.institutional_credit_contracts
        assert overdue_again.state.current_tick == 11
        assert new_overdue_contract.status == "overdue"
        assert new_overdue_contract.overdue_at_tick == 11
        assert new_overdue_contract.extensions[-1].previous_overdue_tick == 3
        (completed_new_schedule,) = [
            row for row in overdue_again.state.scheduled_events
            if row.handler_id == CREDIT_MATURITY_HANDLER
        ]
        assert completed_new_schedule.id == new_schedule.id
        assert completed_new_schedule.status == "completed"

        typed_events = _typed_events(repository, owner_scope, world_id, branch_id)
        credit_events = [event for event in typed_events if isinstance(event, InstitutionalCreditChangedEvent)]
        extension_events = [event for event in typed_events if isinstance(event, InstitutionalCreditExtensionChangedEvent)]
        assert [event.operation for event in credit_events] == ["offer", "accept", "overdue", "overdue"]
        assert [event.operation for event in extension_events] == ["propose", "accept"]
        assert credit_events[2].resulting_contract.overdue_at_tick == 3
        assert credit_events[3].resulting_contract.overdue_at_tick == 11
        assert [event.extension_id for event in extension_events] == [proposal.extension_id, proposal.extension_id]

        replayed = repository.replay(owner_scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == overdue_again.state_hash
        assert replayed.state.institutional_credit_contracts == overdue_again.state.institutional_credit_contracts
        assert replayed.state.resources == overdue_again.state.resources
        assert replayed.state.scheduled_events == overdue_again.state.scheduled_events
        assert replayed.state.current_tick == 11

        backup = BackupApplication(repository)
        archive = backup.export_world(owner_scope, world_id)
        restored_database = Database(f"sqlite:///{(tmp_path / 'institutional-credit-extension-restored.sqlite').as_posix()}")
        restored_database.create_schema()
        restored_repository = WorldRepository(restored_database)
        try:
            with backup.validate_archive_file_streaming(BytesIO(archive)) as verified:
                imported = WorldBackupImporter(restored_repository).import_verified_backup(owner_scope, verified)
            assert imported.world_id == world_id
            restored = restored_repository.read_world(owner_scope, world_id, branch_id)
            assert restored.state_hash == overdue_again.state_hash
            assert restored.state.institutional_credit_contracts == overdue_again.state.institutional_credit_contracts
            assert restored.state.scheduled_events == overdue_again.state.scheduled_events
            assert restored_repository.replay(
                owner_scope, world_id, branch_id, from_genesis=True,
            ).state_hash == overdue_again.state_hash
            restored_contrast = restored_repository.read_world(owner_scope, world_id, contrast_branch_id)
            assert restored_contrast.state_hash == contrast_before.state_hash
            assert restored_contrast.state.institutional_credit_contracts[0].status == "overdue"
        finally:
            restored_database.dispose()
    finally:
        database.dispose()
