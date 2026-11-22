"""Bind private gift transitions to their authoritative command envelopes."""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from uuid import UUID

from .errors import InvalidEvent
from .private_gifts import PrivateGiftChangedEvent


GIFT_OFFER_COMMAND = "action.offer_private_gift"
GIFT_RESOLVE_COMMAND = "action.resolve_private_gift"
NPC_GIFT_REPLY_COMMAND = "action.request_private_gift_reply"
GIFT_COMMANDS = frozenset({GIFT_OFFER_COMMAND, GIFT_RESOLVE_COMMAND, NPC_GIFT_REPLY_COMMAND})


def validate_private_gift_batch(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
    day_seconds: int,
) -> None:
    gifts = [event for event in events if isinstance(event, PrivateGiftChangedEvent)]
    if not gifts:
        if command_type in GIFT_COMMANDS:
            raise InvalidEvent("A private gift command must commit its gift transition.")
        return
    if len(gifts) != 1 or len(events) != 1:
        raise InvalidEvent("A private gift command commits exactly one private transition.")
    event = gifts[0]
    if (
        (event.world_id, event.branch_id, event.command_id, event.ruleset_version)
        != (world_id, branch_id, command_id, ruleset_version)
        or (event.requested_by or event.actor_id) != actor_id
    ):
        raise InvalidEvent("Private gift event does not match its command scope or actor.")
    if command_type is None and command_payload is None:
        return
    if command_payload is None:
        raise InvalidEvent("Private gift command payload is missing.")

    offer = event.offer
    if event.operation == "offer":
        expected = {
            "kind": "offer_private_gift",
            "recipient_id": str(offer.recipient_id),
            "resource_id": offer.resource_id,
            "quantity": command_payload.get("quantity"),
        }
        if "expires_at_tick" in command_payload:
            expires_at = command_payload["expires_at_tick"]
            expected["expires_at_tick"] = expires_at
        else:
            expires_at = min(2**63 - 1, offer.offered_tick + 7 * day_seconds)
        try:
            amount = expected["quantity"]
            amount_matches = isinstance(amount, str) and Decimal(amount) == offer.quantity
        except InvalidOperation:
            amount_matches = False
        if (
            command_type != GIFT_OFFER_COMMAND or command_payload != expected
            or type(expires_at) is not int or expires_at != offer.expires_at_tick or not amount_matches
        ):
            raise InvalidEvent("Private gift must match the exact proposed recipient, stock and deadline.")
        return

    expected = {
        "gift_id": str(offer.gift_id),
        "expected_revision": event.expected_revision,
    }
    if event.requested_by is None:
        required_command = GIFT_RESOLVE_COMMAND
        expected.update(kind="resolve_private_gift", operation=event.operation)
    else:
        required_command = NPC_GIFT_REPLY_COMMAND
        expected.update(kind="request_private_gift_reply")
    if (
        command_type != required_command or command_payload != expected
        or type(command_payload.get("expected_revision")) is not int
    ):
        raise InvalidEvent("Private gift resolution must match its prior revision and authorized decision.")
