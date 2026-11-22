"""SQLite integration for institution-signed term credit and timed arrears."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.events import parse_event
from world_engine.domain.errors import InvalidEvent, StaleVersion
from world_engine.domain.institutional_credit import (
    accept_institutional_credit,
)
from world_engine.domain.institutional_credit_events import (
    InstitutionalCreditChangedEvent,
    InstitutionalCreditTransfer,
    institutional_credit_event_id,
    institutional_credit_offer_id,
)
from world_engine.domain.institutional_credit_state import (
    CREDIT_OFFER_COMMAND,
    CREDIT_RESOLUTION_COMMAND,
    institutional_credit_command_payload,
)
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    ResourceTransferredEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import ledger_postings
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.repository import WorldRepository


CURRENCY_ID = "modern.credit"
RULESET = "institutional-credit-storage.v1"
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
    treasury = TreasuryComponent(accounts={CURRENCY_ID: account_id})
    return WorldEntity(
        id=organization_id,
        kind="organization",
        name=f"Institution {organization_id.hex[:6]}",
        components={
            "governance": governance.model_dump(mode="json"),
            "treasury": treasury.model_dump(mode="json"),
        },
    )


def _initial_world():
    lender_id, borrower_id = uuid4(), uuid4()
    lender_signer_id, borrower_signer_id = uuid4(), uuid4()
    lender_treasury_id, borrower_treasury_id, lender_reserve_id = (uuid4() for _ in range(3))
    entities = {
        lender_signer_id: WorldEntity(id=lender_signer_id, kind="character", name="Lender representative"),
        borrower_signer_id: WorldEntity(id=borrower_signer_id, kind="character", name="Borrower representative"),
        lender_id: _organization(lender_id, lender_signer_id, lender_treasury_id),
        borrower_id: _organization(borrower_id, borrower_signer_id, borrower_treasury_id),
    }
    state = WorldState(
        player_id=lender_signer_id,
        entities=entities,
        resources={
            lender_treasury_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance="80", owner_id=lender_id,
            ),
            borrower_treasury_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance="0", owner_id=borrower_id,
            ),
            # This ordinary, same-owner holder account can fund the registered
            # treasury later without fabricating currency or bypassing transfer rules.
            lender_reserve_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance="20", owner_id=lender_id,
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
        "lender_reserve": lender_reserve_id,
    }


def _credit_event(
    *, scope, repository, world_id, branch_id, operation, contract, actor_id,
    actor_organization_id, expected_revision, transfer=None, command_id=None,
):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = command_id or uuid4()
    event = InstitutionalCreditChangedEvent(
        event_id=institutional_credit_event_id(command_id, RULESET, operation, contract.credit_id),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        actor_organization_id=actor_organization_id,
        ruleset_version=RULESET,
        operation=operation,
        expected_revision=expected_revision,
        resulting_contract=contract,
        transfer=transfer,
        visibility={"policy": "author_only"},
    )
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=f"institutional-credit:{contract.credit_id}:{operation}:{expected_revision}",
        expected_state_version=view.branch.state_version,
        command_type=CREDIT_OFFER_COMMAND if operation == "offer" else CREDIT_RESOLUTION_COMMAND,
        payload=institutional_credit_command_payload(event),
    )
    return event, command


def _commit_credit_event(
    scope, repository, world_id, branch_id, *, operation, contract, actor_id,
    actor_organization_id, expected_revision, transfer=None, command_id=None,
):
    event, command = _credit_event(
        scope=scope,
        repository=repository,
        world_id=world_id,
        branch_id=branch_id,
        operation=operation,
        contract=contract,
        actor_id=actor_id,
        actor_organization_id=actor_organization_id,
        expected_revision=expected_revision,
        transfer=transfer,
        command_id=command_id,
    )
    committed = repository.commit_command(scope, world_id, branch_id, command, [event])
    return event, committed


def _evaluate_rule_action(repository, scope, world_id, branch_id, actor_id, payload):
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
    view, command_id, normalized, resolution = _evaluate_rule_action(
        repository, scope, world_id, branch_id, actor_id, payload,
    )
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    assert len(resolution.events) == 1
    event, = resolution.events
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_state_version=view.branch.state_version,
        command_type=f"action.{normalized['kind']}",
        payload=normalized,
    )
    committed = repository.commit_command(scope, world_id, branch_id, command, resolution.events)
    return event, committed, resolution


def _typed_credit_events(repository, scope, world_id, branch_id):
    page = repository.list_events(scope, world_id, branch_id, limit=100)
    return [
        parse_event({
            **record.payload,
            "type": record.type,
            "event_id": str(record.event_id),
            "visibility": record.visibility,
        })
        for record in page.events
        if record.type == "core.institutional_credit_changed.v1"
    ]


def test_repository_commits_funded_institutional_credit_through_overdue_cure_and_replay(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'institutional-credit.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    source_state, ids = _initial_world()
    observer = WorldEntity(id=uuid4(), kind="character", name="Observer without signing authority")
    borrower_reserve_id, borrower_goods_id = uuid4(), uuid4()
    source_state = source_state.model_copy(update={
        "entities": source_state.entities | {observer.id: observer},
        "resources": source_state.resources | {
            borrower_reserve_id: ResourceAccount(resource_id=CURRENCY_ID, balance="8", owner_id=ids["borrower"]),
            borrower_goods_id: ResourceAccount(resource_id="inventory.cloth", balance="5.25", owner_id=ids["borrower"]),
        },
    })
    created = repository.create_world(
        scope,
        NewWorld(
            title="Institutional term credit",
            ruleset_version=RULESET,
            state=source_state,
        ),
    )
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    try:
        worlds = WorldApplication(repository, None)
        borrower_user, observer_user = uuid4(), uuid4()
        repository.grant_membership(
            scope, world_id, borrower_user, MembershipRole.PLAYER, controlled_actor_ids=[ids["borrower_signer"]],
        )
        repository.grant_membership(
            scope, world_id, observer_user, MembershipRole.PLAYER, controlled_actor_ids=[observer.id],
        )

        def view_for(user_id):
            session = LocalSession(str(user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
            with TestClient(create_app(worlds, session)) as client:
                assert client.get("/api/v1/session").status_code == 200
                response = client.get(f"/api/v1/worlds/{world_id}/view", params={"branch_id": str(branch_id)})
            assert response.status_code == 200, response.text
            return response.json()["state"]

        before_offer = repository.read_world(scope, world_id, branch_id)
        assert view_for(scope.user_id)["institutional_credit"]["institutional_credit_contracts"] == []
        offer_payload = {
            "kind": "offer_institutional_credit",
            "lender_organization_id": str(ids["lender"]),
            "borrower_organization_id": str(ids["borrower"]),
            "currency_resource_id": CURRENCY_ID,
            "principal_minor": PRINCIPAL_MINOR,
            "accepted_through_tick": 1,
            "maturity_tick": 2,
        }

        # A borrower-side representative cannot issue an offer on behalf of the
        # lender. Rule evaluation must reject before it constructs any event.
        _, _, _, unauthorized_offer = _evaluate_rule_action(
            repository, scope, world_id, branch_id, ids["borrower_signer"], offer_payload,
        )
        assert unauthorized_offer.outcome.status == "rejected"
        assert unauthorized_offer.events == []
        assert repository.read_world(scope, world_id, branch_id).state_hash == before_offer.state_hash

        offer_event, _, offer_resolution = _commit_rule_action(
            repository,
            scope,
            world_id,
            branch_id,
            ids["lender_signer"],
            offer_payload,
            idempotency_key="offer-institutional-credit",
        )
        assert offer_resolution.outcome.code == "institutional_credit_offer"
        assert isinstance(offer_event, InstitutionalCreditChangedEvent)
        offered = offer_event.resulting_contract
        assert offered.credit_id == institutional_credit_offer_id(offer_event.command_id)
        assert "total_repayable_minor" not in offer_event.model_dump(mode="json")
        assert "total_repayable_minor" not in institutional_credit_command_payload(offer_event)
        after_offer = repository.read_world(scope, world_id, branch_id)
        assert after_offer.state.institutional_credit_contracts == [offered]
        assert after_offer.state.resources[ids["lender_treasury"]].balance == Decimal("80")
        assert after_offer.state.resources[ids["borrower_treasury"]].balance == Decimal("0")
        assert after_offer.state.scheduled_events == []
        lender_credit = view_for(scope.user_id)["institutional_credit"]
        borrower_credit = view_for(borrower_user)["institutional_credit"]
        lender_row, = lender_credit["institutional_credit_contracts"]
        borrower_row, = borrower_credit["institutional_credit_contracts"]
        assert lender_credit["snapshot_tick"] == "0"
        assert lender_row["credit_id"] == str(offered.credit_id)
        assert lender_row["principal_minor"] == "100"
        assert lender_row["accepted_through_tick"] == "1"
        assert lender_row["maturity_tick"] == "2"
        assert lender_row["can_withdraw"] and not lender_row["can_accept"]
        assert borrower_row["can_accept"] and not borrower_row["can_withdraw"]
        observer_view = view_for(observer_user)
        assert "institutional_credit" not in observer_view
        assert "institutional_finance" not in observer_view
        lender_finance, = view_for(scope.user_id)["institutional_finance"]["organizations"]
        assert lender_finance["organization_id"] == str(ids["lender"])
        assert lender_finance["other_assets"] == []
        borrower_finance, = view_for(borrower_user)["institutional_finance"]["organizations"]
        currency_finance, = borrower_finance["currencies"]
        assert borrower_finance["other_assets"] == [{"resource_id": "inventory.cloth", "quantity": "5.25"}]
        assert currency_finance["treasury_balance_minor"] == "0"
        assert currency_finance["reserve_cash_minor"] == "8"
        assert currency_finance["liquidity_status"] == "no_due_credit"
        assert str(borrower_reserve_id) not in str(borrower_finance)
        assert str(borrower_goods_id) not in str(borrower_finance)

        # The borrower signs a correctly shaped acceptance, but the lender's
        # current treasury cannot cover principal. The attempted event is rejected
        # atomically and changes neither contract nor balances.
        accept_payload = {
            "kind": "resolve_institutional_credit",
            "credit_id": str(offered.credit_id),
            "operation": "accept",
            "expected_revision": offered.revision,
        }
        before_unfunded_action = repository.read_world(scope, world_id, branch_id)
        _, _, _, unauthorized_accept = _evaluate_rule_action(
            repository, scope, world_id, branch_id, ids["lender_signer"], accept_payload,
        )
        assert unauthorized_accept.outcome.status == "rejected"
        assert unauthorized_accept.events == []
        assert repository.read_world(scope, world_id, branch_id).state_hash == before_unfunded_action.state_hash

        _, _, _, unfunded_accept = _evaluate_rule_action(
            repository, scope, world_id, branch_id, ids["borrower_signer"], accept_payload,
        )
        assert unfunded_accept.outcome.status == "rejected"
        assert unfunded_accept.events == []
        assert repository.read_world(scope, world_id, branch_id).state_hash == before_unfunded_action.state_hash

        # A typed forged attempt still reaches storage, where reducer checks
        # independently reject the correctly shaped but unfunded transfer.
        unfunded_result = accept_institutional_credit(
            offered,
            actor_organization_id=ids["borrower"],
            current_tick=after_offer.state.current_tick,
            expected_revision=offered.revision,
        )
        unfunded_transfer = InstitutionalCreditTransfer(
            from_account_id=ids["lender_treasury"],
            to_account_id=ids["borrower_treasury"],
            currency_resource_id=CURRENCY_ID,
            amount_minor=PRINCIPAL_MINOR,
        )
        failed_event, failed_command = _credit_event(
            scope=scope,
            repository=repository,
            world_id=world_id,
            branch_id=branch_id,
            operation="accept",
            contract=unfunded_result,
            actor_id=ids["borrower_signer"],
            actor_organization_id=ids["borrower"],
            expected_revision=offered.revision,
            transfer=unfunded_transfer,
        )
        before_unfunded_accept = repository.read_world(scope, world_id, branch_id)
        with pytest.raises(InvalidEvent, match="enough available funds"):
            repository.commit_command(scope, world_id, branch_id, failed_command, [failed_event])
        after_unfunded_accept = repository.read_world(scope, world_id, branch_id)
        assert after_unfunded_accept.state_hash == before_unfunded_accept.state_hash
        assert after_unfunded_accept.branch.state_version == before_unfunded_accept.branch.state_version
        assert after_unfunded_accept.last_seq == before_unfunded_accept.last_seq
        assert after_unfunded_accept.state.resources[ids["lender_treasury"]].balance == Decimal("80")
        assert after_unfunded_accept.state.resources[ids["borrower_treasury"]].balance == Decimal("0")

        # Fund the same registered treasury from the lender's own holder reserve.
        funding_command = CommandEnvelope(
            actor_id=ids["lender_signer"],
            idempotency_key="fund-lender-treasury",
            expected_state_version=after_unfunded_accept.branch.state_version,
            command_type="core.transfer",
            payload={"quantity": "20"},
        )
        repository.commit_command(scope, world_id, branch_id, funding_command, [
            ResourceTransferredEvent(
                from_account_id=ids["lender_reserve"],
                to_account_id=ids["lender_treasury"],
                resource_id=CURRENCY_ID,
                quantity="20",
            ),
        ])
        funded = repository.read_world(scope, world_id, branch_id)
        assert funded.state.resources[ids["lender_treasury"]].balance == Decimal("100")
        assert funded.state.resources[ids["lender_reserve"]].balance == Decimal("0")

        # Two separately evaluated borrower acceptances are both valid against
        # the same funded offer and pinned branch version. Only the first may
        # commit; the second must fail stale without a second principal transfer.
        accept_view, accept_command_id, accept_normalized, acceptance_resolution = _evaluate_rule_action(
            repository, scope, world_id, branch_id, ids["borrower_signer"], accept_payload,
        )
        assert acceptance_resolution.outcome.status == "succeeded"
        (acceptance_event,) = acceptance_resolution.events
        acceptance_command = CommandEnvelope(
            command_id=accept_command_id,
            actor_id=ids["borrower_signer"],
            idempotency_key="accept-institutional-credit",
            expected_state_version=accept_view.branch.state_version,
            command_type=f"action.{accept_normalized['kind']}",
            payload=accept_normalized,
        )

        competing_view, competing_command_id, competing_normalized, competing_resolution = _evaluate_rule_action(
            repository, scope, world_id, branch_id, ids["borrower_signer"], accept_payload,
        )
        assert competing_resolution.outcome.status == "succeeded"
        (competing_event,) = competing_resolution.events
        competing_command = CommandEnvelope(
            command_id=competing_command_id,
            actor_id=ids["borrower_signer"],
            idempotency_key="competing-accept-institutional-credit",
            expected_state_version=competing_view.branch.state_version,
            command_type=f"action.{competing_normalized['kind']}",
            payload=competing_normalized,
        )
        assert competing_view.branch.state_version == accept_view.branch.state_version
        assert competing_command.expected_state_version == acceptance_command.expected_state_version
        assert competing_event.command_id != acceptance_event.command_id
        assert competing_event.resulting_contract == acceptance_event.resulting_contract
        assert competing_event.transfer == acceptance_event.transfer

        committed_acceptance = repository.commit_command(
            scope, world_id, branch_id, acceptance_command, [acceptance_event],
        )
        assert acceptance_resolution.outcome.code == "institutional_credit_accept"
        accepted = acceptance_event.resulting_contract
        assert acceptance_event.transfer.amount_minor == accepted.principal_minor == PRINCIPAL_MINOR
        assert acceptance_event.transfer.from_account_id == ids["lender_treasury"]
        assert acceptance_event.transfer.to_account_id == ids["borrower_treasury"]
        after_accept = repository.read_world(scope, world_id, branch_id)
        for unrelated_id in (borrower_reserve_id, borrower_goods_id, ids["lender_reserve"]):
            assert after_accept.state.resources[unrelated_id] == funded.state.resources[unrelated_id]
        with pytest.raises(StaleVersion):
            repository.commit_command(scope, world_id, branch_id, competing_command, [competing_event])
        after_competing_accept = repository.read_world(scope, world_id, branch_id)
        assert after_competing_accept.state_hash == after_accept.state_hash
        assert after_competing_accept.branch.state_version == after_accept.branch.state_version
        assert after_competing_accept.last_seq == after_accept.last_seq
        assert after_competing_accept.state.resources[ids["lender_treasury"]].balance == Decimal("0")
        assert after_competing_accept.state.resources[ids["borrower_treasury"]].balance == Decimal("100")

        # Exact command replay is idempotent even though its expected version is
        # now behind the head; it must not disburse the principal again.
        replayed_acceptance = repository.commit_command(
            scope, world_id, branch_id, acceptance_command, [acceptance_event],
        )
        after_idempotent_accept = repository.read_world(scope, world_id, branch_id)
        assert replayed_acceptance.state_version == committed_acceptance.state_version
        assert after_idempotent_accept.state_hash == after_accept.state_hash
        assert after_idempotent_accept.branch.state_version == after_accept.branch.state_version
        assert after_idempotent_accept.last_seq == after_accept.last_seq
        assert after_idempotent_accept.state.resources[ids["lender_treasury"]].balance == Decimal("0")
        assert after_idempotent_accept.state.resources[ids["borrower_treasury"]].balance == Decimal("100")

        assert after_accept.state.institutional_credit_contracts == [accepted]
        (maturity_schedule,) = after_accept.state.scheduled_events
        assert maturity_schedule.due_tick == accepted.maturity_tick + 1 == 3

        # The loan remains active at the inclusive maturity tick; arrears are
        # scheduled only for the following tick.
        jobs = SimulationJobs(repository)
        maturity_job = jobs.enqueue_world_advance(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(started_tick=after_accept.state.current_tick, target_tick=accepted.maturity_tick),
            idempotency_key="institutional-credit-inclusive-maturity",
            speed="fast",
            depth="standard",
            expected_state_version=after_accept.branch.state_version,
        )
        maturity_lease = repository.claim_job("institutional-credit-maturity-worker", job_type=maturity_job.job_type)
        assert maturity_lease is not None
        for _ in range(12):
            step = jobs.advance(scope, maturity_lease)
            saved_job = repository.read_job(scope, maturity_job.job_id)
            if saved_job.status.value == "succeeded":
                break
            assert saved_job.status.value not in {"failed", "paused", "cancelled"}, step
            assert getattr(step, "outcome", None) != "decision_required", step
        assert repository.read_job(scope, maturity_job.job_id).status.value == "succeeded"
        at_maturity = repository.read_world(scope, world_id, branch_id)
        (active_at_maturity,) = at_maturity.state.institutional_credit_contracts
        assert at_maturity.state.current_tick == accepted.maturity_tick == 2
        assert active_at_maturity.status == "active"

        # Let the normal leased worker advance the one-tick grace boundary.
        job = jobs.enqueue_world_advance(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(started_tick=at_maturity.state.current_tick, target_tick=maturity_schedule.due_tick),
            idempotency_key="institutional-credit-maturity",
            speed="fast",
            depth="standard",
            expected_state_version=at_maturity.branch.state_version,
        )
        lease = repository.claim_job("institutional-credit-maturity-worker", job_type=job.job_type)
        assert lease is not None
        for _ in range(12):
            step = jobs.advance(scope, lease)
            saved_job = repository.read_job(scope, job.job_id)
            if saved_job.status.value == "succeeded":
                break
            assert saved_job.status.value not in {"failed", "paused", "cancelled"}, step
            assert getattr(step, "outcome", None) != "decision_required", step
        assert repository.read_job(scope, job.job_id).status.value == "succeeded"
        overdue_view = repository.read_world(scope, world_id, branch_id)
        (overdue_contract,) = overdue_view.state.institutional_credit_contracts
        assert overdue_view.state.current_tick == maturity_schedule.due_tick == 3
        assert overdue_contract.status == "overdue"
        assert overdue_contract.overdue_at_tick == 3
        overdue_row, = view_for(borrower_user)["institutional_credit"]["institutional_credit_contracts"]
        assert overdue_row["status"] == "overdue"
        assert overdue_row["accepted_tick"] == "0"
        assert overdue_row["overdue_at_tick"] == "3"
        assert overdue_row["remaining_total_minor"] == "100"
        assert overdue_row["can_pay"] and not overdue_row["can_accept"]
        assert "institutional_credit" not in view_for(observer_user)
        overdue_finance, = view_for(borrower_user)["institutional_finance"]["organizations"]
        overdue_currency, = overdue_finance["currencies"]
        assert overdue_currency["due_credit_minor"] == "100"
        assert overdue_currency["overdue_credit_minor"] == "100"
        assert overdue_currency["reserve_cash_minor"] == "8"
        assert overdue_currency["future_credit_minor"] == "0"
        assert overdue_currency["liquidity_status"] == "covered"
        assert overdue_currency["due_credit_ids"] == [str(offered.credit_id)]
        assert overdue_currency["due_cash_shortfall_minor"] == "0"

        # Only the borrower can make repayments. A lender-side attempt is
        # rejected by the action rules before an event can be constructed.
        overdue_pay_payload = {
            "kind": "resolve_institutional_credit",
            "credit_id": str(overdue_contract.credit_id),
            "operation": "pay",
            "expected_revision": overdue_contract.revision,
            "amount_minor": 25,
        }
        before_unauthorized_pay = repository.read_world(scope, world_id, branch_id)
        _, _, _, unauthorized_pay = _evaluate_rule_action(
            repository, scope, world_id, branch_id, ids["lender_signer"], overdue_pay_payload,
        )
        assert unauthorized_pay.outcome.status == "rejected"
        assert unauthorized_pay.events == []
        assert repository.read_world(scope, world_id, branch_id).state_hash == before_unauthorized_pay.state_hash

        partial_event, _, partial_resolution = _commit_rule_action(
            repository,
            scope,
            world_id,
            branch_id,
            ids["borrower_signer"],
            overdue_pay_payload,
            idempotency_key="partial-institutional-credit-payment",
        )
        assert partial_resolution.outcome.code == "institutional_credit_pay"
        partial = partial_event.resulting_contract
        assert partial_event.transfer.amount_minor == 25
        assert partial_event.transfer.from_account_id == ids["borrower_treasury"]
        assert partial_event.transfer.to_account_id == ids["lender_treasury"]
        after_partial = repository.read_world(scope, world_id, branch_id)
        assert after_partial.state.resources[borrower_reserve_id] == source_state.resources[borrower_reserve_id]
        assert after_partial.state.resources[borrower_goods_id] == source_state.resources[borrower_goods_id]
        (partial_contract,) = after_partial.state.institutional_credit_contracts
        assert partial_contract.status == "overdue"
        assert partial_contract.repaid_minor == 25
        assert partial_contract.overdue_at_tick == 3

        final_event, _, final_resolution = _commit_rule_action(
            repository,
            scope,
            world_id,
            branch_id,
            ids["borrower_signer"],
            {
                "kind": "resolve_institutional_credit",
                "credit_id": str(partial_contract.credit_id),
                "operation": "pay",
                "expected_revision": partial_contract.revision,
                "amount_minor": 75,
            },
            idempotency_key="final-institutional-credit-payment",
        )
        assert final_resolution.outcome.code == "institutional_credit_pay"
        settled = final_event.resulting_contract
        assert final_event.transfer.amount_minor == 75

        final = repository.read_world(scope, world_id, branch_id)
        (final_contract,) = final.state.institutional_credit_contracts
        assert final_contract.status == "settled"
        assert final_contract.repaid_minor == PRINCIPAL_MINOR
        assert final_contract.remaining_total_minor == 0
        assert final_contract.overdue_at_tick == 3
        assert final.state.resources[ids["lender_treasury"]].balance == Decimal("100")
        assert final.state.resources[ids["borrower_treasury"]].balance == Decimal("0")

        committed_credit_events = _typed_credit_events(repository, scope, world_id, branch_id)
        for event in (acceptance_event, partial_event, final_event):
            typed = next(row for row in committed_credit_events if row.event_id == event.event_id)
            postings = ledger_postings(typed)
            assert len(postings) == 2
            assert sum((posting.delta for posting in postings), Decimal("0")) == 0
            assert all(posting.resource_id == CURRENCY_ID for posting in postings)
            with database.engine.connect() as connection:
                persisted = connection.exec_driver_sql(
                    "SELECT resource_id, delta FROM ledger_postings WHERE event_id = ?",
                    (event.event_id.hex,),
                ).fetchall()
            assert len(persisted) == 2
            assert sum((Decimal(row[1]) for row in persisted), Decimal("0")) == 0

        assert [row.operation for row in committed_credit_events] == ["offer", "accept", "overdue", "pay", "pay"]
        replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == final.state_hash
        assert replayed.state.institutional_credit_contracts == final.state.institutional_credit_contracts
        assert replayed.state.resources == final.state.resources
        assert replayed.state.current_tick == final.state.current_tick

        backup = BackupApplication(repository)
        archive = backup.export_world(scope, world_id)
        for index, validated in enumerate((
            backup.validate_archive(archive),
            backup.validate_archive_file_streaming(BytesIO(archive)),
        )):
            restored_database = Database(f"sqlite:///{(tmp_path / f'credit-restored-{index}.sqlite').as_posix()}")
            restored_database.create_schema()
            restored_repository = WorldRepository(restored_database)
            try:
                with validated as verified:
                    imported = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
                assert imported.world_id == world_id
                restored = restored_repository.read_world(scope, world_id, branch_id)
                assert restored.state_hash == final.state_hash
                assert restored.state.institutional_credit_contracts == final.state.institutional_credit_contracts
                assert restored_repository.replay(
                    scope, world_id, branch_id, from_genesis=True,
                ).state_hash == final.state_hash
            finally:
                restored_database.dispose()
    finally:
        database.dispose()
