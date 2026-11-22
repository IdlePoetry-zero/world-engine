from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.promise_events import (
    PromiseDeadlineBreachedEvent,
    PromiseItemReturnedEvent,
    participant_visibility,
    promise_event_id,
)
from world_engine.domain.promise_trust import (
    PROMISE_TRUST_POLICY_ID,
    PromiseTrustPlan,
    PromiseTrustReceipt,
)
from world_engine.domain.relationship_promises import ReturnLotTransfer, promise_return_lot_id
from world_engine.domain.promise_trust_events import (
    PromiseDeadlineBreachedWithTrustEvent,
    PromiseItemReturnedWithTrustEvent,
)


WORLD_ID = UUID(int=100)
BRANCH_ID = UUID(int=101)
COMMAND_ID = UUID(int=102)
PROMISE_ID = UUID(int=103)
PROMISOR_ID = UUID(int=104)
PROMISEE_ID = UUID(int=105)
RULESET = "rules.promise-trust-wrapper.test.v1"
SCHEDULE_ID = UUID(int=106)
SOURCE_ACCOUNT_ID = UUID(int=107)
RECIPIENT_ACCOUNT_ID = UUID(int=108)
SOURCE_LOT_ID = UUID(int=109)


def _return_settlement() -> PromiseItemReturnedEvent:
    event_id = promise_event_id(COMMAND_ID, RULESET, "item_returned")
    return PromiseItemReturnedEvent(
        event_id=event_id,
        command_id=COMMAND_ID,
        promise_id=PROMISE_ID,
        expected_promise_revision=1,
        actor_id=PROMISOR_ID,
        source_owner_id=PROMISOR_ID,
        recipient_owner_id=PROMISEE_ID,
        from_account_id=SOURCE_ACCOUNT_ID,
        to_account_id=RECIPIENT_ACCOUNT_ID,
        source_account_type="holder",
        recipient_account_type="holder",
        resource_id="cultivation.herb",
        quantity="2",
        transfer_tick=50,
        scheduled_event_id=SCHEDULE_ID,
        expected_schedule_revision=1,
        expected_source_inventory_revision=1,
        expected_recipient_inventory_revision=None,
        expected_source_account_balance="2",
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


def _breach_settlement() -> PromiseDeadlineBreachedEvent:
    return PromiseDeadlineBreachedEvent(
        event_id=promise_event_id(COMMAND_ID, RULESET, "breached"),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        ruleset_version=RULESET,
        actor_id=UUID(int=110),
        promise_id=PROMISE_ID,
        expected_promise_revision=1,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        resolved_tick=101,
        scheduled_event_id=SCHEDULE_ID,
        expected_schedule_revision=1,
        visibility=participant_visibility(PROMISOR_ID, PROMISEE_ID),
    )


def _plan(settlement: PromiseItemReturnedEvent | PromiseDeadlineBreachedEvent) -> PromiseTrustPlan:
    on_time = isinstance(settlement, PromiseItemReturnedEvent)
    outcome = "on_time_fulfilled" if on_time else "breached"
    effective_tick = settlement.transfer_tick if on_time else settlement.resolved_tick
    from_id = settlement.recipient_owner_id if on_time else settlement.promisee_id
    to_id = settlement.source_owner_id if on_time else settlement.promisor_id
    delta = "3" if on_time else "-8"
    return PromiseTrustPlan(
        policy_id=PROMISE_TRUST_POLICY_ID,
        promise_id=settlement.promise_id,
        outcome=outcome,
        outcome_event_id=settlement.event_id,
        world_id=settlement.world_id,
        branch_id=settlement.branch_id,
        command_id=settlement.command_id,
        actor_id=settlement.actor_id,
        ruleset_version=settlement.ruleset_version,
        effective_tick=effective_tick,
        relation_id=UUID(int=111),
        from_id=from_id,
        to_id=to_id,
        expected_relation_revision=None,
        expected_previous_trust=None,
        expected_budget_policy_id=None,
        expected_success_budget_spent="0",
        applied_delta=delta,
        resulting_trust=delta,
        next_budget_policy_id=PROMISE_TRUST_POLICY_ID,
        next_success_budget_spent="3" if on_time else "0",
        evidence_refs_to_append=(settlement.event_id,),
        receipt=PromiseTrustReceipt(
            policy_id=PROMISE_TRUST_POLICY_ID,
            promise_id=settlement.promise_id,
            outcome_event_id=settlement.event_id,
            outcome=outcome,
            applied_delta=delta,
        ),
    )


def _valid_wrappers():
    returned = _return_settlement()
    breach = _breach_settlement()
    return [
        PromiseItemReturnedWithTrustEvent(
            event_id=returned.event_id,
            settlement=returned,
            trust=_plan(returned),
        ),
        PromiseDeadlineBreachedWithTrustEvent(
            event_id=breach.event_id,
            settlement=breach,
            trust=_plan(breach),
        ),
    ]


def test_v2_wrappers_keep_v1_settlement_and_private_top_level_visibility():
    for wrapper in _valid_wrappers():
        assert wrapper.event_id == wrapper.settlement.event_id
        assert wrapper.visibility == {"policy": "author_only"}
        assert wrapper.world_id == wrapper.settlement.world_id
        assert wrapper.branch_id == wrapper.settlement.branch_id
        assert wrapper.command_id == wrapper.settlement.command_id
        assert wrapper.actor_id == wrapper.settlement.actor_id
        assert wrapper.ruleset_version == wrapper.settlement.ruleset_version
        assert wrapper.promise_id == wrapper.settlement.promise_id
        dumped = wrapper.model_dump(mode="python")
        assert dumped["settlement"]["type"] in {
            "core.promise_item_returned.v1", "core.promise_breached.v1",
        }
        assert set(dumped) == {"type", "event_id", "settlement", "trust", "visibility"}
        assert "world_id" not in dumped


@pytest.mark.parametrize("index", [0, 1], ids=["return", "breach"])
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("world_id",), UUID(int=201)),
        (("branch_id",), UUID(int=202)),
        (("command_id",), UUID(int=203)),
        (("actor_id",), UUID(int=204)),
        (("ruleset_version",), "rules.other"),
        (("promise_id",), UUID(int=205)),
        (("outcome",), "late_return"),
        (("effective_tick",), 777),
        (("from_id",), UUID(int=206)),
        (("to_id",), UUID(int=207)),
        (("outcome_event_id",), UUID(int=208)),
        (("receipt", "outcome_event_id"), UUID(int=209)),
    ],
    ids=[
        "world", "branch", "command", "actor", "ruleset", "promise", "outcome",
        "tick", "from", "to", "audit-event", "receipt-event",
    ],
)
def test_v2_wrapper_rejects_trust_plan_mismatches(index, path, replacement):
    wrapper = _valid_wrappers()[index]
    payload = wrapper.model_dump(mode="python")
    plan = deepcopy(payload["trust"])
    target = plan
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    payload["trust"] = plan

    with pytest.raises(ValidationError):
        type(wrapper).model_validate(payload)


@pytest.mark.parametrize("index", [0, 1], ids=["return", "breach"])
def test_v2_wrapper_rejects_wrong_id_or_visibility(index):
    wrapper = _valid_wrappers()[index]
    payload = wrapper.model_dump(mode="python")
    payload["event_id"] = UUID(int=210)
    with pytest.raises(ValidationError):
        type(wrapper).model_validate(payload)

    payload = wrapper.model_dump(mode="python")
    payload["visibility"] = {"policy": "public"}
    with pytest.raises(ValidationError):
        type(wrapper).model_validate(payload)
