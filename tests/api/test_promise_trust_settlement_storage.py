from __future__ import annotations

import io
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from world_engine.agents.knowledge import build_agent_knowledge
from world_engine.application.backups import BackupApplication
from world_engine.application.npc_planning import _planning_promises
from world_engine.application.visibility import project_player_state
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GoalRecord, InventoryComponent, InventoryLot
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.promise_events import PromiseItemReturnedEvent
from world_engine.domain.promise_late_return_event import PromiseItemReturnedLateEvent
from world_engine.domain.promise_trust_events import (
    PromiseItemReturnedWithTrustEvent,
)
from world_engine.domain.promise_trust_state import PromiseTrustProjection
from world_engine.domain.reducers import state_hash
from world_engine.domain.story_nodes import StoryReaction
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.promise_actions import assess_return_promise, build_fulfill_return_promise_event
from world_engine.simulation.contracts import DecisionResponse, WorldAdvancePlan
from world_engine.simulation.engine import SimulationEngine, SimulationPolicy
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.models import LedgerPostingRow
from world_engine.storage.repository import WorldRepository


TRUST_POLICY_ID = "core.promise_trust.v1"
RESOURCE_ID = "core.herb"
RULESET = "1.0.0"


def _fixture_state():
    place_id, promisor_id, promisee_id = (uuid4() for _ in range(3))
    source_account_id, recipient_account_id = uuid4(), uuid4()
    first_lot_id, second_lot_id = uuid4(), uuid4()
    state = WorldState(
        current_tick=0,
        player_id=promisor_id,
        entities={
            place_id: WorldEntity(
                id=place_id,
                kind="place",
                name="Workshop",
                components={"place": {"place_type": "settlement"}},
            ),
            promisor_id: WorldEntity(
                id=promisor_id,
                kind="character",
                name="Ari",
                status="active",
                location_id=place_id,
                components={
                    "inventory": InventoryComponent(lots=[
                        InventoryLot(
                            lot_id=first_lot_id,
                            account_id=source_account_id,
                            resource_id=RESOURCE_ID,
                            quantity="1",
                            quality=91,
                            expiry_tick=100,
                            origin_event_id=uuid4(),
                        ),
                        InventoryLot(
                            lot_id=second_lot_id,
                            account_id=source_account_id,
                            resource_id=RESOURCE_ID,
                            quantity="4",
                            quality=73,
                            expiry_tick=None,
                            origin_event_id=uuid4(),
                        ),
                    ]).model_dump(mode="json")
                },
            ),
            promisee_id: WorldEntity(
                id=promisee_id,
                kind="character",
                name="Bea",
                status="active",
                location_id=place_id,
                components={"inventory": InventoryComponent().model_dump(mode="json")},
            ),
        },
        resources={
            source_account_id: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance="5",
                owner_id=promisor_id,
                account_type="holder",
            ),
            recipient_account_id: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance="0",
                owner_id=promisee_id,
                account_type="holder",
            ),
        },
        metadata={"promise_trust_policy_id": TRUST_POLICY_ID},
    )
    return state, promisor_id, promisee_id, source_account_id, recipient_account_id, first_lot_id, second_lot_id


def _rule_context(view, actor_id: UUID, command_id: UUID) -> RuleContext:
    return RuleContext(
        world_id=view.world.world_id,
        branch_id=view.branch.branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=view.world.ruleset_version,
    )


def _commit_action(repository, scope, world_id, branch_id, actor_id, kind, payload, *, key, causation_ids=()):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    normalized_payload = {"kind": kind, **payload}
    resolution = RuleEngine().evaluate(
        view.state,
        normalized_payload,
        _rule_context(view, actor_id, command_id),
    )
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    result = repository.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            command_id=command_id,
            actor_id=actor_id,
            idempotency_key=key,
            expected_state_version=view.branch.state_version,
            command_type=f"action.{kind}",
            payload=normalized_payload,
            causation_ids=list(causation_ids),
        ),
        resolution.events,
    )
    return result, resolution.events[0]


def _run_world_advance(repository, scope, world_id, branch_id, *, key: str, target_tick: int):
    view = repository.read_world(scope, world_id, branch_id)
    jobs = SimulationJobs(
        repository,
        SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=3_600)),
    )
    job = jobs.enqueue_world_advance(
        scope,
        world_id,
        branch_id,
        WorldAdvancePlan(started_tick=view.state.current_tick, target_tick=target_tick),
        idempotency_key=key,
        speed="balanced",
        depth="deep",
        expected_state_version=view.branch.state_version,
    )
    lease = repository.claim_job(f"promise-worker:{key}", job_type="simulation.world_advance")
    assert lease is not None
    for _ in range(12):
        jobs.advance(scope, lease)
        if repository.read_job(scope, job.job_id).status.value not in {"queued", "running"}:
            break
    completed = repository.read_job(scope, job.job_id)
    assert completed.status.value == "succeeded", completed.model_dump(mode="json")
    return completed


def _trust_for_pair(state, from_id: UUID, to_id: UUID, branch_id: UUID):
    projection = PromiseTrustProjection.from_payload(state.promise_trust, branch_id=branch_id)
    bond = next(
        bond for bond in projection.bonds.values()
        if bond.from_id == from_id and bond.to_id == to_id
    )
    return projection, bond


def test_trust_settlements_are_atomic_and_fork_local_with_legacy_v1_blocked(tmp_path):
    (
        state,
        promisor_id,
        promisee_id,
        source_account_id,
        recipient_account_id,
        first_lot_id,
        second_lot_id,
    ) = _fixture_state()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'promise-trust-storage.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        created = repository.create_world(scope, NewWorld(title="Promise trust integration", state=state))
        world_id, source_branch_id = created.world.world_id, created.branch.branch_id

        recorded_result, recorded_event = _commit_action(
            repository,
            scope,
            world_id,
            source_branch_id,
            promisor_id,
            "record_return_promise",
            {
                "promisee_id": str(promisee_id),
                "resource_id": RESOURCE_ID,
                "quantity": "2.5",
                "deadline_tick": 50,
                "private_text": "I will return two and a half herbs.",
            },
            key="record-on-time-promise",
        )
        recorded = repository.read_world(scope, world_id, source_branch_id)
        first_promise, = recorded.state.relationship_promises
        assert first_promise.trust_policy_id == TRUST_POLICY_ID
        first_schedule, = recorded.state.scheduled_events
        assert first_schedule.handler_id == "relationship_promise.deadline"
        assert first_schedule.handler_version == "2.0.0"
        assert recorded_event.promise.trust_policy_id == TRUST_POLICY_ID
        assert recorded_result.events[0].type == "core.relationship_promise_recorded.v1"
        assert recorded_event.world_id == world_id and recorded_event.branch_id == source_branch_id

        return_result, returned_event = _commit_action(
            repository,
            scope,
            world_id,
            source_branch_id,
            promisor_id,
            "fulfill_return_promise",
            {"promise_id": str(first_promise.id)},
            key="fulfill-on-time-promise",
        )
        assert isinstance(returned_event, PromiseItemReturnedWithTrustEvent)
        assert returned_event.type == "core.promise_item_returned.v2"
        assert returned_event.world_id == world_id
        assert returned_event.branch_id == source_branch_id
        assert returned_event.settlement.type == "core.promise_item_returned.v1"
        assert returned_event.trust.world_id == world_id
        assert returned_event.trust.branch_id == source_branch_id
        assert returned_event.trust.command_id == returned_event.command_id
        assert returned_event.trust.outcome == "on_time_fulfilled"
        assert returned_event.trust.from_id == promisee_id
        assert returned_event.trust.to_id == promisor_id
        assert returned_event.trust.applied_delta == Decimal("3")
        assert return_result.events[0].type == "core.promise_item_returned.v2"

        settlement = returned_event.settlement
        assert [(lot.source_lot_id, lot.quantity) for lot in settlement.lots] == [
            (first_lot_id, Decimal("1")),
            (second_lot_id, Decimal("1.5")),
        ]
        after_return = repository.read_world(scope, world_id, source_branch_id)
        assert after_return.state.resources[source_account_id].balance == Decimal("2.5")
        assert after_return.state.resources[recipient_account_id].balance == Decimal("2.5")
        source_lots = after_return.state.entities[promisor_id].components["inventory"]["lots"]
        assert [(lot["lot_id"], Decimal(lot["quantity"])) for lot in source_lots] == [
            (str(second_lot_id), Decimal("2.5")),
        ]
        recipient_lots = after_return.state.entities[promisee_id].components["inventory"]["lots"]
        assert [(lot["lot_id"], Decimal(lot["quantity"])) for lot in recipient_lots] == [
            (str(row.received_lot_id), row.quantity) for row in settlement.lots
        ]
        assert [lot["quality"] for lot in recipient_lots] == [91, 73]

        with database.session_factory() as session:
            ledger_rows = session.scalars(
                select(LedgerPostingRow)
                .where(LedgerPostingRow.event_id == settlement.event_id)
                .order_by(LedgerPostingRow.posting_index)
            ).all()
        assert [(row.account_id, row.resource_id, Decimal(row.delta)) for row in ledger_rows] == [
            (source_account_id, RESOURCE_ID, Decimal("-2.5")),
            (recipient_account_id, RESOURCE_ID, Decimal("2.5")),
        ]

        projection, parent_bond = _trust_for_pair(
            after_return.state, promisee_id, promisor_id, source_branch_id,
        )
        assert parent_bond.trust == Decimal("3")
        assert parent_bond.success_budget_spent == Decimal("3")
        assert parent_bond.evidence_refs == (settlement.event_id,)
        assert projection.receipts[first_promise.id].applied_delta == Decimal("3")
        assert after_return.state.relations == []

        _, second_recorded_event = _commit_action(
            repository,
            scope,
            world_id,
            source_branch_id,
            promisor_id,
            "record_return_promise",
            {
                "promisee_id": str(promisee_id),
                "resource_id": RESOURCE_ID,
                "quantity": "1",
                "deadline_tick": 1,
                "private_text": "I will return one more herb.",
            },
            key="record-second-promise",
        )
        parent_before_fork = repository.read_world(scope, world_id, source_branch_id)
        second_promise = next(
            promise for promise in parent_before_fork.state.relationship_promises
            if promise.id != first_promise.id
        )
        assert second_promise.trust_policy_id == TRUST_POLICY_ID
        assert second_recorded_event.promise.id == second_promise.id

        # A bare v1 fulfillment has all the old transfer fields, but this world
        # pins trust for the promise. Storage must reject the attempt atomically.
        legacy_command_id = uuid4()
        legacy_context = _rule_context(parent_before_fork, promisor_id, legacy_command_id)
        legacy_event = build_fulfill_return_promise_event(
            parent_before_fork.state,
            legacy_context,
            promise_id=second_promise.id,
        )
        assert isinstance(legacy_event, PromiseItemReturnedEvent)
        assert legacy_event.type == "core.promise_item_returned.v1"
        with pytest.raises(InvalidEvent, match="atomic v2"):
            repository.commit_command(
                scope,
                world_id,
                source_branch_id,
                CommandEnvelope(
                    command_id=legacy_command_id,
                    actor_id=promisor_id,
                    idempotency_key="legacy-v1-omits-trust",
                    expected_state_version=parent_before_fork.branch.state_version,
                    command_type="action.fulfill_return_promise",
                    payload={"kind": "fulfill_return_promise", "promise_id": str(second_promise.id)},
                ),
                [legacy_event],
            )
        unchanged_after_legacy = repository.read_world(scope, world_id, source_branch_id)
        assert unchanged_after_legacy.state_hash == parent_before_fork.state_hash
        assert unchanged_after_legacy.last_seq == parent_before_fork.last_seq

        fork = repository.fork(
            scope,
            world_id,
            source_branch_id,
            parent_before_fork.last_seq,
            "Breach on child only",
        )
        child_branch_id = fork.branch_id
        child_before = repository.read_world(scope, world_id, child_branch_id)
        assert child_before.state_hash == parent_before_fork.state_hash
        _run_world_advance(
            repository,
            scope,
            world_id,
            child_branch_id,
            key="resolve-promise-breach-on-child",
            target_tick=2,
        )

        child_after = repository.read_world(scope, world_id, child_branch_id)
        source_after_child = repository.read_world(scope, world_id, source_branch_id)
        assert source_after_child.state_hash == parent_before_fork.state_hash
        assert source_after_child.last_seq == parent_before_fork.last_seq
        assert source_after_child.state.relationship_promises[-1].status == "active"
        assert child_after.state.current_tick == 2
        breached_promise = next(
            promise for promise in child_after.state.relationship_promises
            if promise.id == second_promise.id
        )
        assert breached_promise.status == "breached"
        assert breached_promise.resolved_tick == 2
        child_schedule = next(
            schedule for schedule in child_after.state.scheduled_events
            if schedule.id == second_promise.deadline_schedule_id
        )
        assert child_schedule.handler_version == "2.0.0"
        assert child_schedule.status == "completed"

        child_local_events = repository.list_events(
            scope, world_id, child_branch_id, include_ancestors=False,
        ).events
        breach_records = [item for item in child_local_events if item.type == "core.promise_breached.v2"]
        assert len(breach_records) == 1
        breach_record = breach_records[0]
        assert breach_record.branch_id == child_branch_id
        assert breach_record.payload["trust"]["world_id"] == str(world_id)
        assert breach_record.payload["trust"]["branch_id"] == str(child_branch_id)
        assert breach_record.payload["trust"]["outcome"] == "breached"
        assert Decimal(breach_record.payload["trust"]["applied_delta"]) == Decimal("-8")

        child_projection, child_bond = _trust_for_pair(
            child_after.state, promisee_id, promisor_id, child_branch_id,
        )
        assert child_bond.trust == Decimal("-5")
        assert child_bond.success_budget_spent == Decimal("3")
        assert child_bond.evidence_refs == (settlement.event_id, UUID(breach_record.payload["trust"]["outcome_event_id"]))
        assert child_projection.receipts[first_promise.id].applied_delta == Decimal("3")
        assert child_projection.receipts[second_promise.id].applied_delta == Decimal("-8")
        assert child_after.state.relations == []

        assert repository.replay(scope, world_id, source_branch_id).state_hash == parent_before_fork.state_hash
        assert repository.replay(scope, world_id, child_branch_id).state_hash == child_after.state_hash
        # The source branch retains only its own +3 effect; the child inherited
        # that exact projection and added a branch-pinned -8 breach.
        _, source_bond_after = _trust_for_pair(
            source_after_child.state, promisee_id, promisor_id, source_branch_id,
        )
        assert source_bond_after.trust == Decimal("3")

        # A late return transfers physical stock without rewriting the breach
        # settlement or its completed schedule. The host command must cite the
        # exact committed breach as its sole cause.
        before_late = repository.read_world(scope, world_id, child_branch_id)
        breach_schedule_before = next(
            schedule for schedule in before_late.state.scheduled_events
            if schedule.id == second_promise.deadline_schedule_id
        )
        breach_record_before = next(
            item for item in repository.list_events(
                scope, world_id, child_branch_id, include_ancestors=False,
            ).events if item.event_id == breach_record.event_id
        )
        missing_cause_command_id = uuid4()
        late_payload = {"kind": "late_return_promise", "promise_id": str(second_promise.id)}
        missing_cause_resolution = RuleEngine().evaluate(
            before_late.state,
            late_payload,
            _rule_context(before_late, promisor_id, missing_cause_command_id),
        )
        assert missing_cause_resolution.outcome.status == "succeeded"
        missing_cause_event, = missing_cause_resolution.events
        assert isinstance(missing_cause_event, PromiseItemReturnedLateEvent)
        assert missing_cause_event.source_breach_event_id == breach_record.event_id
        with pytest.raises(InvalidEvent, match="exactly its committed breach"):
            repository.commit_command(
                scope,
                world_id,
                child_branch_id,
                CommandEnvelope(
                    command_id=missing_cause_command_id,
                    actor_id=promisor_id,
                    idempotency_key="late-return-without-cause",
                    expected_state_version=before_late.branch.state_version,
                    command_type="action.late_return_promise",
                    payload=late_payload,
                ),
                [missing_cause_event],
            )
        unchanged_without_cause = repository.read_world(scope, world_id, child_branch_id)
        assert unchanged_without_cause.state_hash == before_late.state_hash
        assert unchanged_without_cause.last_seq == before_late.last_seq

        late_result, late_event = _commit_action(
            repository,
            scope,
            world_id,
            child_branch_id,
            promisor_id,
            "late_return_promise",
            {"promise_id": str(second_promise.id)},
            key="late-return-with-exact-cause",
            causation_ids=(breach_record.event_id,),
        )
        assert isinstance(late_event, PromiseItemReturnedLateEvent)
        assert late_event.type == "core.promise_item_returned_late.v1"
        assert late_event.world_id == world_id
        assert late_event.branch_id == child_branch_id
        assert late_event.source_breach_event_id == breach_record.event_id
        assert late_event.trust.world_id == world_id
        assert late_event.trust.branch_id == child_branch_id
        assert late_event.trust.outcome == "late_return"
        assert late_event.trust.applied_delta == Decimal("2")
        assert late_event.trust.resulting_trust == Decimal("-3")
        assert late_result.events[0].type == "core.promise_item_returned_late.v1"
        assert late_result.events[0].causation_ids == [breach_record.event_id]
        assert [(lot.source_lot_id, lot.quantity) for lot in late_event.settlement.lots] == [
            (second_lot_id, Decimal("1")),
        ]

        after_late = repository.read_world(scope, world_id, child_branch_id)
        assert after_late.state.resources[source_account_id].balance == Decimal("1.5")
        assert after_late.state.resources[recipient_account_id].balance == Decimal("3.5")
        late_source_lots = after_late.state.entities[promisor_id].components["inventory"]["lots"]
        assert [(lot["lot_id"], Decimal(lot["quantity"])) for lot in late_source_lots] == [
            (str(second_lot_id), Decimal("1.5")),
        ]
        late_recipient_lots = after_late.state.entities[promisee_id].components["inventory"]["lots"]
        assert [(lot["lot_id"], Decimal(lot["quantity"])) for lot in late_recipient_lots] == [
            (str(row.received_lot_id), row.quantity)
            for row in (*settlement.lots, *late_event.settlement.lots)
        ]
        assert [lot["quality"] for lot in late_recipient_lots] == [91, 73, 73]
        with database.session_factory() as session:
            late_ledger_rows = session.scalars(
                select(LedgerPostingRow)
                .where(LedgerPostingRow.event_id == late_event.event_id)
                .order_by(LedgerPostingRow.posting_index)
            ).all()
        assert [(row.account_id, row.resource_id, Decimal(row.delta)) for row in late_ledger_rows] == [
            (source_account_id, RESOURCE_ID, Decimal("-1")),
            (recipient_account_id, RESOURCE_ID, Decimal("1")),
        ]

        still_breached = next(
            promise for promise in after_late.state.relationship_promises
            if promise.id == second_promise.id
        )
        breach_schedule_after = next(
            schedule for schedule in after_late.state.scheduled_events
            if schedule.id == second_promise.deadline_schedule_id
        )
        assert still_breached.status == "breached"
        assert still_breached.resolved_tick == second_promise.deadline_tick + 1
        assert still_breached.outcome_event_id == breach_record.event_id
        assert breach_schedule_after == breach_schedule_before
        child_projection, child_bond = _trust_for_pair(
            after_late.state, promisee_id, promisor_id, child_branch_id,
        )
        breach_receipt = child_projection.receipts[second_promise.id]
        assert child_bond.trust == Decimal("-3")
        assert child_bond.success_budget_spent == Decimal("3")
        assert child_bond.evidence_refs == (
            settlement.event_id, breach_record.event_id, late_event.event_id,
        )
        assert breach_receipt.outcome == "breached"
        assert breach_receipt.outcome_event_id == breach_record.event_id
        assert breach_receipt.applied_delta == Decimal("-8")
        assert breach_receipt.late_return_event_id == late_event.event_id
        assert breach_receipt.late_return_delta == Decimal("2")
        assert child_projection.receipts[first_promise.id].applied_delta == Decimal("3")
        assert after_late.state.relations == []

        repeated = RuleEngine().evaluate(
            after_late.state,
            late_payload,
            _rule_context(after_late, promisor_id, uuid4()),
        )
        assert repeated.outcome.status == "rejected"
        assert repeated.outcome.code == "promise_action_unavailable"
        assert repeated.events == []
        assert repository.read_world(scope, world_id, child_branch_id).state_hash == after_late.state_hash

        breach_record_after = next(
            item for item in repository.list_events(
                scope, world_id, child_branch_id, include_ancestors=False,
            ).events if item.event_id == breach_record.event_id
        )
        assert breach_record_after.payload == breach_record_before.payload
        assert breach_record_after.command_id == breach_record_before.command_id
        assert repository.replay(scope, world_id, child_branch_id).state_hash == after_late.state_hash
        assert repository.read_world(scope, world_id, source_branch_id).state_hash == parent_before_fork.state_hash

        expected_hashes = {
            source_branch_id: parent_before_fork.state_hash,
            child_branch_id: after_late.state_hash,
        }
        expected_event_ids = {
            branch_id: tuple(
                item.event_id for item in repository.list_events(scope, world_id, branch_id).events
            )
            for branch_id in expected_hashes
        }
        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        restored_database = Database(f"sqlite:///{(tmp_path / 'promise-trust-restored.db').as_posix()}")
        restored_database.create_schema()
        try:
            with backup_app.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
                imported = WorldBackupImporter(WorldRepository(restored_database)).import_verified_backup(
                    scope, verified,
                )
            assert imported.branch_count == 2
            assert imported.state_hashes == expected_hashes
            restored_repository = WorldRepository(restored_database)
            for branch_id, expected_hash in expected_hashes.items():
                restored = restored_repository.read_world(scope, world_id, branch_id)
                assert restored.state_hash == expected_hash
                assert restored_repository.replay(scope, world_id, branch_id).state_hash == expected_hash
                restored_ids = tuple(
                    item.event_id for item in restored_repository.list_events(scope, world_id, branch_id).events
                )
                assert restored_ids == expected_event_ids[branch_id]
            restored_child = restored_repository.read_world(scope, world_id, child_branch_id)
            restored_projection, restored_bond = _trust_for_pair(
                restored_child.state, promisee_id, promisor_id, child_branch_id,
            )
            assert restored_bond.trust == Decimal("-3")
            assert restored_projection.receipts[second_promise.id].late_return_event_id == late_event.event_id
            restored_late_record = next(
                item for item in restored_repository.list_events(
                    scope, world_id, child_branch_id, include_ancestors=False,
                ).events if item.event_id == late_event.event_id
            )
            assert restored_late_record.causation_ids == [breach_record.event_id]
        finally:
            restored_database.dispose()
    finally:
        database.dispose()


@pytest.mark.parametrize("externally_controlled", [False, True])
def test_promise_deadline_can_change_story_without_player_participation(tmp_path, externally_controlled):
    state, promisor, promisee, *_ = _fixture_state()
    player = uuid4()
    entities = state.entities | {player: WorldEntity(id=player, kind="character", name="Observer")}
    outcome_id, reversed_id = uuid4(), uuid4()
    trust = {"kind": "promise_trust", "observer_id": promisee, "counterpart_id": promisor,
             "comparison": "at_most", "threshold": "-1"}
    node = {"title": "A strained partnership", "visibility": "author_only",
            "participant_ids": [promisee, promisor],
            "fallback": {"key": "trust-fell", "effect": "occurred", "summary": "A workshop partnership cools."}}
    state = WorldState.model_validate(state.model_dump(mode="python") | {
        "player_id": player,
        "entities": entities,
        "metadata": state.metadata | ({"npc_return_commitment_reviews_v1": True} if externally_controlled else {}),
        "story_reactions": [
            StoryReaction.model_validate({"reaction_id": outcome_id, "when": [trust], "node": node}),
            StoryReaction.model_validate({"reaction_id": reversed_id, "when": [
                trust | {"observer_id": promisor, "counterpart_id": promisee},
            ], "node": node}),
        ],
    })
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'npc-story-trust.db').as_posix()}")
    database.create_schema()
    try:
        repo = WorldRepository(database)
        created = repo.create_world(scope, NewWorld(title="Workshop", state=state))
        world, main = created.world.world_id, created.branch.branch_id
        _commit_action(repo, scope, world, main, promisor, "record_return_promise", {
            "promisee_id": str(promisee), "resource_id": RESOURCE_ID, "quantity": "1",
            "deadline_tick": 4, "private_text": "I will return the herb before closing.",
        }, key="npc-promise")
        before = repo.read_world(scope, world, main)
        assert not before.state.story_milestones
        actor = before.state.entities[promisor]
        promise, = before.state.relationship_promises
        assert assess_return_promise(before.state, actor, promise, visible_counterpart_ids={promisee}) == (
            "recipient_available_here", True,
        )
        assert assess_return_promise(before.state, actor, promise, visible_counterpart_ids=set()) == (
            "not_observed_here", True,
        )
        raw_state = before.state.model_dump(mode="json")
        for actor_id in (promisor, promisee, player):
            person = before.state.entities[actor_id]
            knowledge = build_agent_knowledge(before.state, actor_id)
            rows = _planning_promises(before.state, person, project_player_state(raw_state, str(actor_id)), knowledge)
            if actor_id == promisor:
                assert rows[0]["fulfillment_preflight"] == {
                    "delivery_at_current_place": "recipient_available_here",
                    "has_deliverable_stock": True,
                    "ticks_until_deadline": 4,
                }
            elif actor_id == promisee:
                assert rows[0]["role"] == "promisee" and "fulfillment_preflight" not in rows[0]
            else:
                assert not rows
        empty_actor = actor.model_copy(update={"components": {"inventory": {"lots": []}}})
        assert assess_return_promise(before.state, empty_actor, promise, visible_counterpart_ids={promisee}) == (
            "recipient_available_here", False,
        )
        child = repo.fork(scope, world, main, before.last_seq, "Wait past closing").branch_id
        if externally_controlled:
            guest = uuid4()
            repo.grant_membership(scope, world, guest, MembershipRole.PLAYER)
            repo.grant_actor_control(scope, world, guest, promisor)
        _run_world_advance(repo, scope, world, child, key="npc-deadline", target_tick=5)
        after = repo.read_world(scope, world, child)
        record, = after.state.story_milestones
        _, bond = _trust_for_pair(after.state, promisee, promisor, child)
        assert bond.trust == Decimal("-8")
        if externally_controlled:
            goal, = after.state.goals
            assert goal.status == "failed"
            reviews = [row for row in repo.list_events(scope, world, child).events
                       if row.type == "core.npc_plan_review_resolved.v1"]
            assert len(reviews) == 3
            assert {row.payload["reason"] for row in reviews} == {"npc_review_actor_user_controlled"}
            assert not after.state.npc_plans
        promise, = after.state.relationship_promises
        assert promise.status == "breached" and promise.resolved_tick == 5  # The deadline tick is inclusive.
        assert (record.schedule_id, record.outcome, record.resolved_tick) == (outcome_id, "trust-fell", 5)
        assert repo.read_world(scope, world, main).state_hash == before.state_hash
        assert repo.replay(scope, world, child).state_hash == after.state_hash

        backup = BackupApplication(repo)
        archive = backup.export_world(scope, world)
        restored = Database(f"sqlite:///{(tmp_path / 'npc-story-trust-restored.db').as_posix()}")
        restored.create_schema()
        try:
            restored_repo = WorldRepository(restored)
            with backup.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
                WorldBackupImporter(restored_repo).import_verified_backup(scope, verified)
            assert restored_repo.replay(scope, world, child).state_hash == after.state_hash
        finally:
            restored.dispose()
    finally:
        database.dispose()


@pytest.mark.parametrize(("use_streaming_backup", "automatic", "defer_count"), [
    (False, False, 0), (False, True, 0), (True, True, 2),
])
def test_npc_executes_its_own_return_plan_and_story_replays(tmp_path, use_streaming_backup, automatic, defer_count):
    state, promisor, promisee, source_account, recipient_account, *_ = _fixture_state()
    player, milestone_id = uuid4(), uuid4()
    goal = GoalRecord(id=uuid4(), owner_id=promisor, title="Return the borrowed herb", deadline=4)
    state = WorldState.model_validate(state.model_dump(mode="python") | {
        "player_id": player,
        "entities": state.entities | {player: WorldEntity(id=player, kind="character", name="Observer")},
        "goals": [] if automatic else [goal],
        "metadata": state.metadata | ({"npc_return_commitment_reviews_v1": True} if automatic else {}),
        "story_reactions": [StoryReaction.model_validate({
            "reaction_id": milestone_id,
            "when": [{"kind": "promise_trust", "observer_id": promisee, "counterpart_id": promisor,
                      "comparison": "at_least", "threshold": "1"}],
            "node": {"title": "A workshop keeps its word", "visibility": "author_only",
                     "fallback": {"key": "trust-rose", "effect": "occurred", "summary": "The borrower returned the herb."}},
        })],
    })
    draft = NewWorld(title="Autonomous workshop", state=state)
    if not automatic:
        schedule = build_npc_plan_review_schedule(state, draft.world_id, promisor, 4)
        state = WorldState.model_validate(state.model_dump(mode="python") | {"scheduled_events": [schedule]})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    db = Database(f"sqlite:///{(tmp_path / 'npc-return.db').as_posix()}")
    db.create_schema()
    restored_db = Database(f"sqlite:///{(tmp_path / 'npc-return-copy.db').as_posix()}")
    restored_db.create_schema()
    try:
        repository = WorldRepository(db)
        world, branch = draft.world_id, draft.branch_id
        repository.create_world(scope, draft.model_copy(update={"state": state}))
        _commit_action(repository, scope, world, branch, promisor, "record_return_promise", {
            "promisee_id": str(promisee), "resource_id": RESOURCE_ID, "quantity": "1",
            "deadline_tick": 4, "private_text": "I'll bring the herb back before closing.",
        }, key="create-active-npc-promise")
        before = repository.read_world(scope, world, branch)
        promise, = before.state.relationship_promises
        opening_review_tick = 2 if automatic else promise.deadline_tick
        goal, = before.state.goals
        if automatic:
            assert goal.deadline == promise.deadline_tick
            assert goal.id == uuid5(promise.id, "npc.commitment:return")
            schedule = next(row for row in before.state.scheduled_events if row.handler_id == "npc.goal_review")
            assert schedule.due_tick == opening_review_tick and schedule.max_runs == 3
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope, world, branch, WorldAdvancePlan(started_tick=0, target_tick=6),
            idempotency_key="npc-return-and-advance", speed="balanced", depth="deep",
            expected_state_version=before.branch.state_version,
        )
        lease = repository.claim_job("npc-item-return", job_type=job.job_type)
        assert lease is not None
        def wait_for_review():
            for _ in range(8):
                resolution = jobs.advance(scope, lease)
                if resolution.decision_requests:
                    request, = resolution.decision_requests
                    return request, repository.read_world(scope, world, branch)
            pytest.fail("Expected the actor's commitment review before the deadline.")

        request, reviewed = wait_for_review()
        assert request.at_tick == opening_review_tick
        for _ in range(defer_count):
            jobs.advance(scope, lease, model_decision=DecisionResponse(
                request_id=request.request_id, state_version=request.state_version, at_tick=request.at_tick,
                source="policy", option_id="plan", npc_planning_status="defer",
                npc_plan_state_hash=state_hash(reviewed.state),
            ))
            request, reviewed = wait_for_review()
            assert reviewed.state.relationship_promises[0].status == "active"
            assert reviewed.state.resources[source_account].balance == Decimal("5")
        review_tick = opening_review_tick + defer_count
        assert request.at_tick == review_tick
        plan = NpcPlan(
            plan_id=uuid5(request.request_id, f"npc.plan.v1:{promisor}:{request.state_version}"),
            actor_id=promisor, goal_ids=[goal.id], revision=1, status="ready",
            created_tick=review_tick, updated_tick=review_tick, next_review_tick=review_tick, current_step=0,
            steps=[NpcPlanStep(capability_id="world.action.fulfill_return_promise.v1",
                               arguments={"promise_id": str(promise.id)})],
            reason="Return the herb while its recipient is here.",
        )
        jobs.advance(scope, lease, model_decision=DecisionResponse(
            request_id=request.request_id, state_version=request.state_version, at_tick=review_tick, source="policy",
            option_id="plan", npc_planning_status="ready", npc_plan=plan,
            npc_plan_state_hash=state_hash(reviewed.state),
        ))
        for _ in range(22):
            jobs.advance(scope, lease)
            if repository.read_job(scope, job.job_id).status == "succeeded":
                break
        assert repository.read_job(scope, job.job_id).status == "succeeded"
        result = repository.read_world(scope, world, branch)
        returned, = result.state.relationship_promises
        assert returned.status == "fulfilled" and returned.resolved_tick == review_tick
        if automatic:
            completed_goal, = result.state.goals
            assert completed_goal.status == "succeeded" and completed_goal.evidence_refs == [returned.outcome_event_id]
        assert result.state.resources[source_account].balance == Decimal("4")
        assert result.state.resources[recipient_account].balance == Decimal("1")
        assert result.state.npc_plans[0].status == "completed"
        milestone, = result.state.story_milestones
        assert (milestone.schedule_id, milestone.resolved_tick) == (milestone_id, review_tick)
        assert repository.replay(scope, world, branch).state_hash == result.state_hash

        backup = BackupApplication(repository)
        archive = backup.export_world(scope, world)
        restored_repo = WorldRepository(restored_db)
        importer = WorldBackupImporter(restored_repo)
        if use_streaming_backup:
            with backup.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
                importer.import_verified_backup(scope, verified)
        else:
            importer.import_verified_backup(scope, backup.validate_archive(archive))
        assert restored_repo.replay(scope, world, branch).state_hash == result.state_hash
    finally:
        restored_db.dispose()
        db.dispose()
