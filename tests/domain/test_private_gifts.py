from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.components import InventoryComponent, InventoryLot, PlaceComponent
from world_engine.domain.errors import InsufficientResources, InvalidEvent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, ledger_postings, state_hash
from world_engine.domain.private_gifts import (
    PrivateGiftChangedEvent,
    PrivateGiftOffer,
    plan_private_gift_offer,
    plan_private_gift_resolution,
    private_gift_event_id,
    private_gift_id,
    private_gift_recipient_account_id,
    private_gift_received_lot_id,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
OFFER_COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACCEPT_COMMAND_ID = UUID("30000000-0000-4000-8000-000000000004")
REQUESTED_COMMAND_ID = UUID("30000000-0000-4000-8000-000000000005")
GIVER_ID = UUID("40000000-0000-4000-8000-000000000004")
RECIPIENT_ID = UUID("40000000-0000-4000-8000-000000000005")
REMOTE_ID = UUID("40000000-0000-4000-8000-000000000006")
PLACE_ID = UUID("50000000-0000-4000-8000-000000000005")
GIVER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
RECIPIENT_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
EMPTY_GIVER_ACCOUNT_ID = UUID("50000000-0000-4000-8000-000000000004")
SOURCE_LOT_A_ID = UUID("80000000-0000-4000-8000-000000000008")
SOURCE_LOT_B_ID = UUID("90000000-0000-4000-8000-000000000009")
ORIGIN_EVENT_A_ID = UUID("a0000000-0000-4000-8000-00000000000a")
ORIGIN_EVENT_B_ID = UUID("b0000000-0000-4000-8000-00000000000b")
RULESET = "private-gift-test.v1"


def _state(
    *,
    recipient_account: bool = True,
    recipient_inventory_revision: int | None = None,
) -> WorldState:
    source_lots = [
        InventoryLot(
            lot_id=SOURCE_LOT_A_ID,
            account_id=GIVER_ACCOUNT_ID,
            resource_id="core.food",
            quantity="1.5",
            quality=87,
            expiry_tick=200,
            origin_event_id=ORIGIN_EVENT_A_ID,
        ),
        InventoryLot(
            lot_id=SOURCE_LOT_B_ID,
            account_id=GIVER_ACCOUNT_ID,
            resource_id="core.food",
            quantity="4",
            quality=54,
            expiry_tick=500,
            origin_event_id=ORIGIN_EVENT_B_ID,
        ),
    ]
    entities = {
        PLACE_ID: WorldEntity(
            id=PLACE_ID,
            kind="place",
            components={"place": PlaceComponent().model_dump(mode="json")},
        ),
        GIVER_ID: WorldEntity(
            id=GIVER_ID,
            kind="character",
            location_id=PLACE_ID,
            components={"inventory": InventoryComponent(lots=source_lots).model_dump(mode="json")},
        ),
        RECIPIENT_ID: WorldEntity(
            id=RECIPIENT_ID,
            kind="character",
            location_id=PLACE_ID,
            components=(
                {"inventory": InventoryComponent(revision=recipient_inventory_revision).model_dump(mode="json")}
                if recipient_inventory_revision is not None else {}
            ),
        ),
        REMOTE_ID: WorldEntity(
            id=REMOTE_ID,
            kind="character",
            location_id=None,
            components={},
        ),
    }
    resources = {
        GIVER_ACCOUNT_ID: ResourceAccount(
            resource_id="core.food", balance="5.5", owner_id=GIVER_ID,
        ),
    }
    if recipient_account:
        resources[RECIPIENT_ACCOUNT_ID] = ResourceAccount(
            resource_id="core.food", balance="1", owner_id=RECIPIENT_ID,
        )
    return WorldState(current_tick=100, entities=entities, resources=resources)


def _offer(state: WorldState, *, expires_at_tick: int | None = None, quantity="3") -> PrivateGiftChangedEvent:
    return plan_private_gift_offer(
        state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=OFFER_COMMAND_ID,
        actor_id=GIVER_ID,
        ruleset_version=RULESET,
        recipient_id=RECIPIENT_ID,
        resource_id="core.food",
        quantity=quantity,
        expires_at_tick=expires_at_tick,
    )


def _resolve(
    state: WorldState,
    offer: PrivateGiftOffer,
    *,
    actor_id: UUID = RECIPIENT_ID,
    command_id: UUID = ACCEPT_COMMAND_ID,
    operation: str = "accept",
    requested_by: UUID | None = None,
) -> PrivateGiftChangedEvent:
    return plan_private_gift_resolution(
        state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=RULESET,
        gift_id=offer.gift_id,
        expected_revision=offer.revision,
        operation=operation,
        requested_by=requested_by,
    )


def test_offer_is_private_command_bound_and_does_not_reserve_or_move_stock():
    state = _state()
    before = state_hash(state)
    offer_event = _offer(state)

    assert offer_event.type == "core.private_gift_changed.v1"
    assert offer_event.operation == "offer"
    assert offer_event.offer.gift_id == private_gift_id(OFFER_COMMAND_ID)
    assert offer_event.event_id == private_gift_event_id(
        OFFER_COMMAND_ID, RULESET, "offer", offer_event.offer.gift_id,
    )
    assert offer_event.offer.source_account_id == GIVER_ACCOUNT_ID
    assert offer_event.offer.offered_tick == state.current_tick
    assert offer_event.offer.expires_at_tick == state.current_tick + 7 * state.calendar.day_seconds
    assert offer_event.visibility == {
        "policy": "subject_ids",
        "subject_ids": sorted((str(GIVER_ID), str(RECIPIENT_ID))),
    }
    assert "requested_by" not in offer_event.model_dump(mode="json")
    assert "transfer" not in offer_event.model_dump(mode="json")
    assert state_hash(state) == before
    assert not state.private_gifts
    assert state.resources[GIVER_ACCOUNT_ID].balance == Decimal("5.5")


@pytest.mark.parametrize(
    ("actor_id", "updates", "message"),
    [
        (GIVER_ID, {"location_id": None}, "same registered place"),
        (RECIPIENT_ID, {"status": "dead"}, "active character"),
    ],
)
def test_offer_requires_active_distinct_co_located_characters(actor_id, updates, message):
    state = _state()
    changed = state.model_copy(update={
        "entities": {
            **state.entities,
            actor_id: state.entities[actor_id].model_copy(update=updates),
        }
    })
    with pytest.raises(InvalidEvent, match=message):
        _offer(changed)


def test_offer_rejects_self_remote_expired_and_unfunded_proposals():
    state = _state()
    with pytest.raises(InvalidEvent, match="distinct characters"):
        plan_private_gift_offer(
            state,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=OFFER_COMMAND_ID,
            actor_id=GIVER_ID,
            ruleset_version=RULESET,
            recipient_id=GIVER_ID,
            resource_id="core.food",
            quantity="1",
        )
    with pytest.raises(InvalidEvent, match="same registered place"):
        plan_private_gift_offer(
            state,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=OFFER_COMMAND_ID,
            actor_id=GIVER_ID,
            ruleset_version=RULESET,
            recipient_id=REMOTE_ID,
            resource_id="core.food",
            quantity="1",
        )
    with pytest.raises(InvalidEvent, match="future"):
        _offer(state, expires_at_tick=state.current_tick)
    with pytest.raises(InsufficientResources):
        _offer(state, quantity="6")


def test_offer_selects_a_deterministic_holder_that_can_cover_account_and_fifo_quantity():
    state = _state()
    resources = dict(state.resources)
    resources[EMPTY_GIVER_ACCOUNT_ID] = ResourceAccount(
        resource_id="core.food", balance="1", owner_id=GIVER_ID,
    )
    state = state.model_copy(update={"resources": resources})

    event = _offer(state)
    assert event.offer.source_account_id == GIVER_ACCOUNT_ID


def test_accept_rederives_fifo_and_atomically_copies_lot_metadata_and_balances():
    state = _state(recipient_inventory_revision=3)
    offer = _offer(state)
    offered_state = apply_event(state, offer.model_dump(mode="json"))
    acceptance = _resolve(offered_state, offer.offer)
    assert acceptance.transfer is not None
    assert acceptance.transfer.from_account_id == GIVER_ACCOUNT_ID
    assert acceptance.transfer.to_account_id == RECIPIENT_ACCOUNT_ID
    assert acceptance.transfer.quantity == Decimal("3")
    assert acceptance.transfer.source_inventory_revision == 1
    assert acceptance.transfer.destination_inventory_revision == 3
    assert [row.source_lot_id for row in acceptance.transfer.lots] == [SOURCE_LOT_A_ID, SOURCE_LOT_B_ID]
    assert [row.quantity for row in acceptance.transfer.lots] == [Decimal("1.5"), Decimal("1.5")]

    settled = apply_event(offered_state, acceptance.model_dump(mode="json"))
    giver_inventory = InventoryComponent.model_validate(settled.entities[GIVER_ID].components["inventory"])
    recipient_inventory = InventoryComponent.model_validate(settled.entities[RECIPIENT_ID].components["inventory"])
    received = recipient_inventory.lots

    assert [lot.lot_id for lot in received] == [
        private_gift_received_lot_id(acceptance.event_id, SOURCE_LOT_A_ID),
        private_gift_received_lot_id(acceptance.event_id, SOURCE_LOT_B_ID),
    ]
    assert [(lot.quality, lot.expiry_tick, lot.origin_event_id) for lot in received] == [
        (87, 200, ORIGIN_EVENT_A_ID),
        (54, 500, ORIGIN_EVENT_B_ID),
    ]
    assert [(lot.quantity, lot.revision) for lot in giver_inventory.lots] == [(Decimal("2.5"), 2)]
    assert giver_inventory.revision == 2
    assert recipient_inventory.revision == 4
    assert settled.resources[GIVER_ACCOUNT_ID].balance == Decimal("2.5")
    assert settled.resources[RECIPIENT_ACCOUNT_ID].balance == Decimal("4")
    assert settled.private_gifts[0].status == "accepted"
    assert settled.private_gifts[0].resolved_tick == state.current_tick
    assert [(row.account_id, row.delta) for row in ledger_postings(acceptance)] == [
        (GIVER_ACCOUNT_ID, Decimal("-3")),
        (RECIPIENT_ACCOUNT_ID, Decimal("3")),
    ]

    tampered_transfer = acceptance.transfer.model_copy(update={"lots": tuple(reversed(acceptance.transfer.lots))})
    tampered_event = acceptance.model_copy(update={"transfer": tampered_transfer})
    with pytest.raises(InvalidEvent, match="authoritative transition"):
        apply_event(offered_state, tampered_event)


def test_accept_creates_deterministic_recipient_account_when_none_exists():
    state = _state(recipient_account=False)
    offer = _offer(state)
    offered_state = apply_event(state, offer)
    acceptance = _resolve(offered_state, offer.offer)
    assert acceptance.transfer is not None
    expected_account = private_gift_recipient_account_id(offer.offer.gift_id)
    assert acceptance.transfer.to_account_id == expected_account
    assert acceptance.transfer.destination_account_created is True

    settled = apply_event(offered_state, acceptance)
    assert settled.resources[expected_account] == ResourceAccount(
        resource_id="core.food", balance="3", owner_id=RECIPIENT_ID,
    )
    receiver_inventory = InventoryComponent.model_validate(settled.entities[RECIPIENT_ID].components["inventory"])
    assert receiver_inventory.revision == 1
    assert all(lot.account_id == expected_account for lot in receiver_inventory.lots)


def test_recipient_acceptance_requires_same_offer_place_and_cannot_accept_after_expiry():
    state = _state()
    offer_event = _offer(state, expires_at_tick=101)
    offered_state = apply_event(state, offer_event)
    moved_recipient = offered_state.model_copy(update={
        "entities": {
            **offered_state.entities,
            RECIPIENT_ID: offered_state.entities[RECIPIENT_ID].model_copy(update={"location_id": REMOTE_ID}),
        }
    })
    with pytest.raises(InvalidEvent, match="same registered place"):
        _resolve(moved_recipient, offer_event.offer)

    expired = offered_state.model_copy(update={"current_tick": 101})
    with pytest.raises(InvalidEvent, match="expired"):
        _resolve(expired, offer_event.offer)


def test_decline_and_withdraw_are_participant_owned_and_may_be_remote_or_expired():
    state = _state()
    offer_event = _offer(state, expires_at_tick=101)
    offered_state = apply_event(state, offer_event)
    moved = offered_state.model_copy(update={
        "current_tick": 101,
        "entities": {
            **offered_state.entities,
            GIVER_ID: offered_state.entities[GIVER_ID].model_copy(update={"location_id": None}),
            RECIPIENT_ID: offered_state.entities[RECIPIENT_ID].model_copy(update={"location_id": None}),
        },
    })
    declined = _resolve(moved, offer_event.offer, operation="decline")
    assert declined.offer.status == "declined"
    assert declined.transfer is None
    withdrawn = _resolve(moved, offer_event.offer, actor_id=GIVER_ID, operation="withdraw")
    assert withdrawn.offer.status == "withdrawn"

    with pytest.raises(InvalidEvent, match="designated"):
        _resolve(moved, offer_event.offer, actor_id=GIVER_ID, operation="decline")


def test_npc_response_is_bound_to_giver_request_and_recipient_actor():
    state = _state()
    offer_event = _offer(state)
    offered_state = apply_event(state, offer_event)
    hosted = _resolve(
        offered_state,
        offer_event.offer,
        command_id=REQUESTED_COMMAND_ID,
        requested_by=GIVER_ID,
    )
    assert hosted.actor_id == RECIPIENT_ID
    assert hosted.requested_by == GIVER_ID
    assert "requested_by" in hosted.model_dump(mode="json")

    invalid = hosted.model_dump(mode="python") | {"requested_by": RECIPIENT_ID}
    with pytest.raises(ValueError, match="requested by the giver"):
        PrivateGiftChangedEvent.model_validate(invalid)


def test_stale_revision_or_depleted_source_cannot_create_a_historical_payment():
    state = _state()
    offer_event = _offer(state)
    offered_state = apply_event(state, offer_event)
    with pytest.raises(InvalidEvent, match="stale or no longer pending"):
        plan_private_gift_resolution(
            offered_state,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=ACCEPT_COMMAND_ID,
            actor_id=RECIPIENT_ID,
            ruleset_version=RULESET,
            gift_id=offer_event.offer.gift_id,
            expected_revision=2,
            operation="accept",
        )

    depleted_resources = dict(offered_state.resources)
    depleted_resources[GIVER_ACCOUNT_ID] = ResourceAccount(
        resource_id="core.food", balance="1", owner_id=GIVER_ID,
    )
    depleted = offered_state.model_copy(update={"resources": depleted_resources})
    with pytest.raises(InsufficientResources):
        _resolve(depleted, offer_event.offer)

    acceptance = _resolve(offered_state, offer_event.offer)
    changed_recipient = offered_state.entities[RECIPIENT_ID].model_copy(update={
        "components": {
            "inventory": InventoryComponent(revision=2).model_dump(mode="json"),
        }
    })
    changed_inventory_state = offered_state.model_copy(update={
        "entities": {**offered_state.entities, RECIPIENT_ID: changed_recipient},
    })
    with pytest.raises(InvalidEvent, match="destination inventory revision"):
        apply_event(changed_inventory_state, acceptance)

