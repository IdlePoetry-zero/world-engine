from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from world_engine.application.visibility import project_event, project_player_state
from world_engine.domain.promise_trust import PROMISE_TRUST_POLICY_ID, PromiseTrustReceipt, promise_trust_relation_id
from world_engine.domain.promise_trust_state import PromiseTrustBond, PromiseTrustProjection
from world_engine.domain.relationship_promises import promise_deadline_schedule_id


PROMISOR = "10000000-0000-4000-8000-000000000001"
PROMISEE = "20000000-0000-4000-8000-000000000002"
STRANGER = "30000000-0000-4000-8000-000000000003"
PROMISE_ID = "40000000-0000-4000-8000-000000000004"
EVENT_ID = "50000000-0000-4000-8000-000000000005"


def _promise_event(event_type: str) -> dict:
    returned = event_type != "core.promise_breached.v2"
    nested_type = "core.promise_item_returned.v1" if returned else "core.promise_breached.v1"
    time_field = "transfer_tick" if returned else "resolved_tick"
    return {
        "event_id": EVENT_ID,
        "world_id": "60000000-0000-4000-8000-000000000006",
        "branch_id": "70000000-0000-4000-8000-000000000007",
        "sequence": 9,
        "tick": 481,
        "type": event_type,
        "command_id": "80000000-0000-4000-8000-000000000008",
        "actor_id": PROMISOR,
        "visibility": {"policy": "author_only"},
        "payload": {
            "settlement": {
                "type": nested_type,
                "event_id": EVENT_ID,
                "promise_id": PROMISE_ID,
                time_field: 481,
                "visibility": {"policy": "subject_ids", "subject_ids": sorted([PROMISOR, PROMISEE])},
                **({"source_owner_id": PROMISOR, "recipient_owner_id": PROMISEE} if returned else {
                    "promisor_id": PROMISOR, "promisee_id": PROMISEE,
                }),
                "from_account_id": "private-account-from",
                "to_account_id": "private-account-to",
                "resource_id": "resource.silver",
                "quantity": "99.125",
            },
            "trust": {
                "previous_trust": "12.5",
                "delta": "-8",
                "from_id": PROMISEE,
                "to_id": PROMISOR,
                "relation_id": "private-relation",
            },
            **({"source_breach_event_id": "90000000-0000-4000-8000-000000000009"}
               if event_type == "core.promise_item_returned_late.v1" else {}),
        },
    }


@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        ("core.promise_item_returned.v2", "fulfilled"),
        ("core.promise_breached.v2", "breached"),
        ("core.promise_item_returned_late.v1", "late_returned"),
    ],
)
def test_v2_promise_settlement_projects_only_participant_safe_result(event_type: str, status: str):
    event = _promise_event(event_type)
    original = deepcopy(event)

    projected = project_event(event, PROMISEE)

    assert projected == {
        "event_id": EVENT_ID,
        "type": event_type,
        "tick": 481,
        "sequence": 9,
        "payload": {"promise_id": PROMISE_ID, "status": status},
    }
    assert event == original
    serialized = str(projected)
    for private_value in (
        "private-account-from", "private-account-to", "resource.silver", "99.125",
        "previous_trust", PROMISOR, PROMISEE, "private-relation", "command_id", "delta", "trust",
    ):
        assert private_value not in serialized


@pytest.mark.parametrize("event_type", [
    "core.promise_item_returned.v2", "core.promise_breached.v2", "core.promise_item_returned_late.v1",
])
def test_v2_promise_result_is_hidden_from_strangers(event_type: str):
    assert project_event(_promise_event(event_type), STRANGER) is None


@pytest.mark.parametrize(
    "visibility",
    [
        None,
        {"policy": "public", "subject_ids": [PROMISOR, PROMISEE]},
        {"policy": "subject_ids", "subject_ids": [PROMISOR, STRANGER]},
        {"policy": "subject_ids", "subject_ids": [PROMISOR, PROMISOR]},
        {"policy": "subject_ids", "subject_ids": [PROMISEE, PROMISOR]},
        {"policy": "subject_ids", "subject_ids": [PROMISOR, PROMISEE], "note": "extra"},
        {"policy": "subject_ids", "subject_ids": PROMISEE},
    ],
)
def test_malformed_nested_promise_visibility_fails_closed(visibility):
    event = _promise_event("core.promise_item_returned.v2")
    event["payload"]["settlement"]["visibility"] = visibility

    assert project_event(event, PROMISOR) is None


def test_v2_wrapper_and_nested_settlement_types_must_match_and_author_scope_remains_private():
    event = _promise_event("core.promise_item_returned.v2")
    event["payload"]["settlement"]["type"] = "core.promise_breached.v1"
    assert project_event(event, PROMISOR) is None

    event = _promise_event("core.promise_item_returned.v2")
    event["visibility"] = {"policy": "subject_ids", "subject_ids": sorted([PROMISOR, PROMISEE])}
    assert project_event(event, PROMISOR) is None


def test_v1_promise_event_still_requires_a_precomputed_safe_audience_view():
    event = {
        "event_id": EVENT_ID,
        "type": "core.promise_item_returned.v1",
        "tick": 481,
        "sequence": 9,
        "visibility": {"policy": "subject_ids", "subject_ids": sorted([PROMISOR, PROMISEE])},
        "payload": {"promise_id": PROMISE_ID, "quantity": "99.125"},
    }
    assert project_event(event, PROMISEE) is None


def _promise_player_state(*, actor_id=PROMISOR, late_return_event_id=None, trust_policy=True, trust_payload=True):
    world_id = UUID("60000000-0000-4000-8000-000000000006")
    promise_id = UUID(PROMISE_ID)
    promisor_id = UUID(PROMISOR)
    promisee_id = UUID(PROMISEE)
    breach_event_id = UUID("90000000-0000-4000-8000-000000000009")
    bond_id = promise_trust_relation_id(world_id, promisee_id, promisor_id)
    refs = (breach_event_id, late_return_event_id) if late_return_event_id else (breach_event_id,)
    bond = PromiseTrustBond(
        id=bond_id, world_id=world_id, from_id=promisee_id, to_id=promisor_id,
        revision=2 if late_return_event_id else 1, trust="-6" if late_return_event_id else "-8",
        policy_id=PROMISE_TRUST_POLICY_ID, success_budget_spent="0", evidence_refs=refs,
    )
    receipt = PromiseTrustReceipt(
        policy_id=PROMISE_TRUST_POLICY_ID, promise_id=promise_id, outcome_event_id=breach_event_id,
        outcome="breached", applied_delta="-8", late_return_event_id=late_return_event_id,
        late_return_delta="2" if late_return_event_id else None,
    )
    trust_projection = PromiseTrustProjection(
        world_id=world_id, bonds={bond_id: bond}, receipts={promise_id: receipt},
    ).to_payload()
    promise = {
        "id": PROMISE_ID,
        "promisor_id": PROMISOR,
        "promisee_id": PROMISEE,
        "private_text": "请归还药草",
        "terms": {"kind": "return_item", "resource_id": "cultivation.herb", "quantity": "2",
                  "promisee_account_id": "a0000000-0000-4000-8000-00000000000a"},
        "status": "breached",
        "created_tick": 10,
        "deadline_tick": 100,
        "deadline_schedule_id": str(promise_deadline_schedule_id(promise_id)),
        "resolved_tick": 101,
        "revision": 2,
        "outcome_event_id": str(breach_event_id),
    }
    if trust_policy:
        promise["trust_policy_id"] = PROMISE_TRUST_POLICY_ID
    state = {
        "player_id": actor_id,
        "current_tick": 200,
        "entities": {actor_id: {"id": actor_id, "kind": "character", "name": "Traveler"}},
        "resources": {}, "knowledge": [], "goals": [], "relations": [],
        "relationship_promises": [promise],
    }
    if trust_payload:
        state["promise_trust"] = trust_projection
    return state


def test_player_promise_projection_exposes_only_late_return_booleans():
    late_id = "a1000000-0000-4000-8000-00000000000a"
    unreturned = project_player_state(_promise_player_state(), PROMISOR)["own_promises"][0]
    assert unreturned["late_returned"] is False
    assert unreturned["can_return_late"] is True

    repaired = project_player_state(
        _promise_player_state(late_return_event_id=UUID(late_id)), PROMISOR,
    )["own_promises"][0]
    assert repaired["late_returned"] is True
    assert repaired["can_return_late"] is False

    promisee = project_player_state(_promise_player_state(actor_id=PROMISEE), PROMISEE)["own_promises"][0]
    assert promisee["late_returned"] is False
    assert promisee["can_return_late"] is False
    for projected in (unreturned, repaired, promisee):
        assert not {"trust", "receipt", "outcome_event_id", "late_return_event_id", "late_return_delta"} & projected.keys()
        assert "promise_trust" not in projected


@pytest.mark.parametrize(
    "state",
    [
        _promise_player_state(trust_policy=False),
        _promise_player_state(trust_payload=False),
    ],
)
def test_legacy_or_missing_trust_receipts_omit_late_return_controls(state):
    projected = project_player_state(state, PROMISOR)["own_promises"][0]
    assert "late_returned" not in projected
    assert "can_return_late" not in projected


def test_invalid_trust_receipt_does_not_enable_late_return_control():
    state = _promise_player_state()
    state["promise_trust"]["receipts"][PROMISE_ID]["outcome_event_id"] = "b1000000-0000-4000-8000-00000000000b"
    projected = project_player_state(state, PROMISOR)["own_promises"][0]
    assert "late_returned" not in projected
    assert "can_return_late" not in projected
