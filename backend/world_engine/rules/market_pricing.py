"""Rule-layer entry point for the typed market pricing schedule."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from world_engine.domain.market_pricing import plan_market_price_review

if TYPE_CHECKING:
    from world_engine.domain.market_pricing_events import MarketPriceReviewResolvedEvent
    from world_engine.domain.models import ScheduledEventRecord, WorldState


def resolve_market_price_review(
    state: WorldState,
    scheduled_event: ScheduledEventRecord,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
) -> MarketPriceReviewResolvedEvent:
    """Build the one canonical audit/update event for a due world review."""

    return plan_market_price_review(
        state,
        scheduled_event,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
    )
