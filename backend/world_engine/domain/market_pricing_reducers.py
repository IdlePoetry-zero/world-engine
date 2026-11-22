"""Pure reducer helpers for batched scheduled market price reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from .errors import InvalidEvent
from .market_pricing import plan_market_price_review
from .market_pricing_events import MarketPriceReviewResolvedEvent

if TYPE_CHECKING:
    from .models import WorldState


def validate_market_price_review_event(
    state: WorldState,
    event: MarketPriceReviewResolvedEvent,
) -> None:
    """Recompute all observations and require the exact canonical event body."""

    schedule = next((item for item in state.scheduled_events if item.id == event.schedule_id), None)
    if schedule is None:
        raise InvalidEvent("Market price review schedule does not exist.")
    try:
        expected = plan_market_price_review(
            state,
            schedule,
            world_id=event.world_id,
            branch_id=event.branch_id,
            command_id=event.command_id,
            ruleset_version=event.ruleset_version,
        )
    except Exception as exc:
        raise InvalidEvent(f"Market price review is not valid for this state: {exc}") from exc
    if expected.model_dump(mode="json") != event.model_dump(mode="json"):
        raise InvalidEvent("Market price review event does not match the canonical stock snapshot and price plan.")


def apply_market_price_review(
    state: WorldState,
    event: MarketPriceReviewResolvedEvent,
) -> WorldState:
    """Apply changed prices and advance the review recurrence atomically."""

    from .components import MarketComponent, ScheduledEventRecord

    validate_market_price_review_event(state, event)
    scheduled_index = next(i for i, item in enumerate(state.scheduled_events) if item.id == event.schedule_id)
    scheduled = state.scheduled_events[scheduled_index]

    changes_by_market: dict[UUID, dict[UUID, int]] = {}
    expected_revisions: dict[UUID, int] = {}
    for change in event.changes:
        changes_by_market.setdefault(change.market_id, {})[change.listing_id] = change.new_unit_price_minor
        expected_revisions[change.market_id] = change.expected_market_revision

    entities = dict(state.entities)
    for market_id in sorted(changes_by_market, key=lambda item: item.hex):
        entity = entities[market_id]
        market = MarketComponent.model_validate(entity.components.get("market"))
        if market.revision != expected_revisions[market_id]:
            raise InvalidEvent("Market component revision changed before the price review could apply.")
        new_offers = [
            offer.model_copy(update={"unit_price_minor": changes_by_market[market_id].get(offer.listing_id, offer.unit_price_minor)})
            for offer in market.offers
        ]
        updated_market = MarketComponent.model_validate(
            market.model_dump(mode="python")
            | {
                "revision": market.revision + 1,
                "offers": [offer.model_dump(mode="python") for offer in new_offers],
            }
        )
        components = dict(entity.components)
        components["market"] = updated_market.model_dump(mode="json")
        entities[market_id] = entity.model_copy(update={"components": components})

    completed_runs = scheduled.completed_runs + 1
    repeats = scheduled.repeat_ticks is not None and completed_runs < scheduled.max_runs
    next_due_tick = scheduled.due_tick + scheduled.repeat_ticks if repeats else scheduled.due_tick
    if next_due_tick > 2**63 - 1:
        raise InvalidEvent("Market price review recurrence exceeds the signed world-clock range.")
    replacement_data = scheduled.model_dump(mode="python") | {
        "status": "queued" if repeats else "completed",
        "due_tick": next_due_tick,
        "completed_runs": completed_runs,
        "revision": scheduled.revision + 1,
    }
    try:
        replacement = ScheduledEventRecord.model_validate(replacement_data)
    except Exception as exc:
        raise InvalidEvent(f"Market price review recurrence is invalid: {exc}") from exc
    if replacement.max_runs != scheduled.max_runs or replacement.repeat_ticks != scheduled.repeat_ticks:
        raise InvalidEvent("Market price review changed its immutable recurrence policy.")

    scheduled_events = list(state.scheduled_events)
    scheduled_events[scheduled_index] = replacement
    # The changed market components and schedule were individually validated.
    # No ownership, balance, lot, or cross-entity references change here.
    return state.model_copy(update={"entities": entities, "scheduled_events": scheduled_events})
