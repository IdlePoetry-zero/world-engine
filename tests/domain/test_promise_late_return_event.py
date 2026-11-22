from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.promise_events import PromiseItemReturnedEvent, participant_visibility, promise_event_id
from world_engine.domain.promise_late_return_event import PromiseItemReturnedLateEvent
from world_engine.domain.promise_trust import PROMISE_TRUST_POLICY_ID, PromiseTrustPlan, PromiseTrustReceipt
from world_engine.domain.relationship_promises import ReturnLotTransfer, promise_return_lot_id


WORLD_ID = UUID(int=100)
BRANCH_ID = UUID(int=101)
COMMAND_ID = UUID(int=102)
BREACH_COMMAND_ID = UUID(int=103)
PROMISE_ID = UUID(int=104)
PROMISOR_ID = UUID(int=105)
PROMISEE_ID = UUID(int=106)
RULESET = "rules.promise-late-return.test.v1"
SCHEDULE_ID = UUID(int=107)
SOURCE_ACCOUNT_ID = UUID(int=108)
RECIPIENT_ACCOUNT_ID = UUID(int=109)
SOURCE_LOT_ID = UUID(int=110)
BREACH_EVENT_ID = promise_event_id(BREACH_COMMAND_ID, RULESET, "breached")


def _settlement() -> PromiseItemReturnedEvent:
    event_id = promise_event_id(COMMAND_ID, RULESET, "item_returned")
    return PromiseItemReturnedEvent(
        event_id=event_id,
        command_id=COMMAND_ID,
        promise_id=PROMISE_ID,
        expected_promise_revision=2,
        actor_id=PROMISOR_ID,
        source_owner_id=PROMISOR_ID,
        recipient_owner_id=PROMISEE_ID,
        from_account_id=SOURCE_ACCOUNT_ID,
        to_account_id=RECIPIENT_ACCOUNT_ID,
        source_account_type="holder",
        recipient_account_type="holder",
        resource_id="cultivation.herb",
        quantity="2",
        transfer_tick=150,
        scheduled_event_id=SCHEDULE_ID,
        expected_schedule_revision=2,
        expected_source_inventory_revision=3,
        expected_recipient_inventory_revision=2,
        expected_source_account_balance="5",
        expected_recipient_account_balance="0",
        lots=(ReturnLotTransfer(
            source_lot_id=SOURCE_LOT_ID,
            received_lot_id=promise_return_lot_id(event_id, SOURCE_LOT_ID),
            quantity="2",
        ),),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        ruleset_version=RULESET,
        visibility=participant_visibility(PROMISOR_ID, PROMISEE_ID),
    )


def _plan(settlement: PromiseItemReturnedEvent) -> PromiseTrustPlan:
    return PromiseTrustPlan(
        policy_id=PROMISE_TRUST_POLICY_ID,
        promise_id=settlement.promise_id,
        outcome="late_return",
        outcome_event_id=settlement.event_id,
        world_id=settlement.world_id,
        branch_id=settlement.branch_id,
        command_id=settlement.command_id,
        actor_id=settlement.actor_id,
        ruleset_version=settlement.ruleset_version,
        effective_tick=settlement.transfer_tick,
        relation_id=UUID(int=111),
        from_id=settlement.recipient_owner_id,
        to_id=settlement.source_owner_id,
        expected_relation_revision=2,
        expected_previous_trust="-8",
        expected_budget_policy_id=PROMISE_TRUST_POLICY_ID,
        expected_success_budget_spent="0",
        applied_delta="2",
        resulting_trust="-6",
        next_budget_policy_id=PROMISE_TRUST_POLICY_ID,
        next_success_budget_spent="0",
        evidence_refs_to_append=(BREACH_EVENT_ID, settlement.event_id),
        receipt=PromiseTrustReceipt(
            policy_id=PROMISE_TRUST_POLICY_ID,
            promise_id=settlement.promise_id,
            outcome_event_id=BREACH_EVENT_ID,
            outcome="breached",
            applied_delta="-8",
            late_return_event_id=settlement.event_id,
            late_return_delta="2",
        ),
    )


def _valid_event() -> PromiseItemReturnedLateEvent:
    settlement = _settlement()
    return PromiseItemReturnedLateEvent(
        event_id=settlement.event_id,
        settlement=settlement,
        source_breach_event_id=BREACH_EVENT_ID,
        trust=_plan(settlement),
    )


def test_late_return_event_binds_nested_return_breach_receipt_and_repair():
    event = _valid_event()

    assert event.type == "core.promise_item_returned_late.v1"
    assert event.event_id == event.settlement.event_id
    assert event.source_breach_event_id == event.trust.receipt.outcome_event_id == BREACH_EVENT_ID
    assert event.trust.receipt.outcome == "breached"
    assert event.trust.receipt.late_return_event_id == event.event_id
    assert event.trust.receipt.late_return_delta == event.trust.applied_delta == 2
    assert event.trust.evidence_refs_to_append == (BREACH_EVENT_ID, event.event_id)
    assert event.visibility == {"policy": "author_only"}
    assert event.world_id == WORLD_ID
    assert event.branch_id == BRANCH_ID
    assert event.command_id == COMMAND_ID
    assert event.actor_id == PROMISOR_ID
    assert event.ruleset_version == RULESET
    assert event.promise_id == PROMISE_ID


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    [
        ("settlement", "world_id", UUID(int=201)),
        ("settlement", "branch_id", UUID(int=202)),
        ("settlement", "command_id", UUID(int=203)),
        ("settlement", "actor_id", UUID(int=204)),
        ("settlement", "ruleset_version", "rules.other"),
        ("settlement", "promise_id", UUID(int=205)),
        ("settlement", "transfer_tick", 777),
        ("settlement", "source_owner_id", UUID(int=206)),
        ("settlement", "recipient_owner_id", UUID(int=207)),
        ("trust", "world_id", UUID(int=208)),
        ("trust", "branch_id", UUID(int=209)),
        ("trust", "command_id", UUID(int=210)),
        ("trust", "actor_id", UUID(int=211)),
        ("trust", "ruleset_version", "rules.other"),
        ("trust", "promise_id", UUID(int=212)),
        ("trust", "outcome", "breached"),
        ("trust", "effective_tick", 778),
        ("trust", "from_id", UUID(int=213)),
        ("trust", "to_id", UUID(int=214)),
        ("trust", "outcome_event_id", UUID(int=215)),
    ],
)
def test_late_return_event_rejects_settlement_and_plan_mismatches(section, field, replacement):
    event = _valid_event()
    payload = event.model_dump(mode="python")
    payload[section][field] = replacement

    with pytest.raises(ValidationError):
        PromiseItemReturnedLateEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("outcome", "on_time_fulfilled"),
        ("outcome_event_id", UUID(int=301)),
        ("late_return_event_id", UUID(int=302)),
        ("late_return_delta", "1"),
    ],
)
def test_late_return_event_rejects_receipt_that_does_not_extend_original_breach(field, replacement):
    payload = _valid_event().model_dump(mode="python")
    receipt = deepcopy(payload["trust"]["receipt"])
    receipt[field] = replacement
    payload["trust"]["receipt"] = receipt

    with pytest.raises(ValidationError):
        PromiseItemReturnedLateEvent.model_validate(payload)


def test_late_return_event_rejects_wrong_source_breach_id_or_evidence_order():
    payload = _valid_event().model_dump(mode="python")
    payload["source_breach_event_id"] = UUID(int=401)
    with pytest.raises(ValidationError):
        PromiseItemReturnedLateEvent.model_validate(payload)

    payload = _valid_event().model_dump(mode="python")
    payload["trust"]["evidence_refs_to_append"] = (payload["event_id"], BREACH_EVENT_ID)
    with pytest.raises(ValidationError):
        PromiseItemReturnedLateEvent.model_validate(payload)


def test_late_return_event_rejects_wrong_outer_id_or_visibility():
    payload = _valid_event().model_dump(mode="python")
    payload["event_id"] = UUID(int=501)
    with pytest.raises(ValidationError):
        PromiseItemReturnedLateEvent.model_validate(payload)

    payload = _valid_event().model_dump(mode="python")
    payload["visibility"] = {"policy": "public"}
    with pytest.raises(ValidationError):
        PromiseItemReturnedLateEvent.model_validate(payload)
