"""Bind market price review events to their persisted simulation command."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid5

from .errors import InvalidEvent
from .market_pricing_events import MarketPriceReviewResolvedEvent


def validate_market_price_review_batch(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    """Require a review to be the sole event bound to its host command.

    Archive validation may omit the original command type/payload, but still
    supplies persisted command identity and verifies the event's deterministic
    ID and world/branch/ruleset provenance. Live commits must be a simulation
    window with the exact job and due tick payload.
    """

    review_events = [event for event in events if isinstance(event, MarketPriceReviewResolvedEvent)]
    if not review_events:
        return
    if len(events) != 1 or len(review_events) != 1:
        raise InvalidEvent("A market price review must be one atomic standalone event.")
    event = review_events[0]
    if (event.world_id, event.branch_id, event.command_id, event.ruleset_version) != (
        world_id,
        branch_id,
        command_id,
        ruleset_version,
    ):
        raise InvalidEvent("Market price review provenance does not match its persisted command.")
    expected_id = uuid5(
        command_id,
        f"{ruleset_version}:market.price_review:{event.schedule_id}:{event.period_index}",
    )
    if event.event_id != expected_id:
        raise InvalidEvent("Market price review event ID is not bound to its persisted command and period.")

    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("Market price review command type and payload must both be present or both absent.")
    if command_type is None:
        return
    if command_type != "simulation.window" or command_payload is None:
        raise InvalidEvent("Market price review may be committed only by a simulation window.")
    if set(command_payload) != {"job_id", "next_tick"}:
        raise InvalidEvent("Market price review worker command must carry only its job ID and exact next tick.")
    raw_job_id = command_payload.get("job_id")
    raw_next_tick = command_payload.get("next_tick")
    if not isinstance(raw_job_id, str) or not isinstance(raw_next_tick, str):
        raise InvalidEvent("Market price review worker payload values must be canonical strings.")
    try:
        job_id = UUID(raw_job_id)
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Market price review worker payload has an invalid job ID.") from exc
    if str(job_id) != raw_job_id or raw_next_tick != str(event.due_tick):
        raise InvalidEvent("Market price review worker payload does not match its exact due tick.")
