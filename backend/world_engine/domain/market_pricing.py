"""Dependency-light market pricing policies and deterministic review planner.

This module deliberately does not import ``components`` or ``models`` at import
time. Its configuration DTOs can therefore be used by those modules to validate
world metadata without a circular import. State adapters import component
schemas only when an observation is requested.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import TYPE_CHECKING, Annotated, Literal, Mapping
from uuid import UUID, uuid5

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from .market_pricing_events import MarketPriceReviewResolvedEvent
    from .models import ScheduledEventRecord, WorldState

MAX_PRICE_MINOR = 10**30 - 1
PRICE_REVIEW_HANDLER_ID = "market.price_review"
PRICE_REVIEW_HANDLER_VERSION = "1.0.0"
PRICE_REVIEW_EVENT_TYPE = "core.market_price_review_resolved.v1"
PRICE_REVIEW_CONFIG_KEY = "market_pricing"
PRICE_REVIEW_CONFIG_VERSION = "world.market_pricing.v1"
PRICE_REVIEW_DAYS = 30
_BASIS = Decimal(10_000)
_RESOURCE_NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]*$"


def _parse_exact_decimal(value: object) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("decimal values must use decimal strings or integers, not binary floats")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("value is not a valid decimal") from exc
    if not number.is_finite():
        raise ValueError("decimal values must be finite numbers")
    digits = number.as_tuple().digits
    exponent = number.as_tuple().exponent
    digit_count = len(digits)
    while digit_count and digits[digit_count - 1] == 0:
        digit_count -= 1
        exponent += 1
    if max(0, -exponent) > 6:
        raise ValueError("decimal values support at most 6 fractional digits")
    if max(1, digit_count + exponent) > 30:
        raise ValueError("decimal values support at most 30 integer digits")
    return number


ExactDecimal = Annotated[Decimal, BeforeValidator(_parse_exact_decimal)]
ResourceName = Annotated[str, Field(min_length=1, max_length=160, pattern=_RESOURCE_NAME_PATTERN)]
Tick = Annotated[int, Field(ge=-(2**63), le=2**63 - 1, strict=True)]


class _Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class MarketPriceInputError(ValueError):
    """Raised when an observation or review does not match registered state."""

    def __init__(self, message: str, *, code: str = "invalid_market_pricing_state") -> None:
        super().__init__(message)
        self.code = code


class MarketPriceUnavailable(MarketPriceInputError):
    """A configured listing is currently unavailable and must be audit-skipped."""

    def __init__(self, message: str, *, code: str, snapshot_digest: str) -> None:
        super().__init__(message, code=code)
        self.snapshot_digest = snapshot_digest


class StockPricePolicy(_Contract):
    """Stable reference price and bounded stock-pressure curve for one offer."""

    policy_id: ResourceName
    revision: int = Field(ge=1, strict=True)
    reference_price_minor: int = Field(ge=1, le=MAX_PRICE_MINOR, strict=True)
    target_stock_quantity: ExactDecimal = Field(gt=Decimal("0"))
    sensitivity_bps: int = Field(default=10_000, ge=0, le=100_000, strict=True)
    max_markup_bps: int = Field(default=5_000, ge=0, le=100_000, strict=True)
    max_discount_bps: int = Field(default=5_000, ge=0, le=9_999, strict=True)
    price_floor_minor: int = Field(default=1, ge=1, le=MAX_PRICE_MINOR, strict=True)
    price_ceiling_minor: int = Field(default=MAX_PRICE_MINOR, ge=1, le=MAX_PRICE_MINOR, strict=True)

    @model_validator(mode="after")
    def floor_not_above_ceiling(self) -> StockPricePolicy:
        if self.price_floor_minor > self.price_ceiling_minor:
            raise ValueError("price floor cannot exceed the price ceiling")
        return self


class MarketListingPricePolicy(StockPricePolicy):
    """A stable price policy bound to one market and listing UUID."""

    market_id: UUID
    listing_id: UUID


class MarketPricingConfig(_Contract):
    """Versioned world metadata for the bounded monthly stock-price review."""

    schema_version: Literal["world.market_pricing.v1"]
    review_period_days: Literal[30]
    policies: list[MarketListingPricePolicy] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def unique_policy_bindings(self) -> MarketPricingConfig:
        listing_keys = [(policy.market_id, policy.listing_id) for policy in self.policies]
        policy_ids = [policy.policy_id for policy in self.policies]
        if len(listing_keys) != len(set(listing_keys)):
            raise ValueError("market pricing config cannot repeat a market/listing binding")
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("market pricing policy IDs must be unique")
        return self


class MarketStockObservation(_Contract):
    """Compact observation and compare-and-swap inputs for an active offer."""

    market_id: UUID
    listing_id: UUID
    seller_id: UUID
    resource_id: ResourceName
    stock_account_id: UUID
    observed_tick: Tick
    tradable_quantity: ExactDecimal = Field(ge=Decimal("0"))
    stock_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    market_revision: int = Field(ge=1, strict=True)
    seller_inventory_revision: int = Field(ge=1, strict=True)
    current_unit_price_minor: int = Field(ge=0, le=MAX_PRICE_MINOR, strict=True)
    eligible_lot_count: int = Field(ge=0, strict=True)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class MarketPriceProposal(_Contract):
    """One calculated quote, ready to embed in a batched review event."""

    assessment_id: UUID
    schedule_id: UUID
    period_index: int = Field(ge=1, strict=True)
    due_tick: Tick
    observed_tick: Tick
    policy_id: ResourceName
    policy_revision: int = Field(ge=1, strict=True)
    market_id: UUID
    listing_id: UUID
    seller_id: UUID
    resource_id: ResourceName
    stock_account_id: UUID
    reference_price_minor: int = Field(ge=1, le=MAX_PRICE_MINOR, strict=True)
    target_stock_quantity: ExactDecimal = Field(gt=Decimal("0"))
    observed_stock_quantity: ExactDecimal = Field(ge=Decimal("0"))
    pressure_bps: int = Field(ge=-9_999, le=100_000, strict=True)
    unit_price_minor: int = Field(ge=1, le=MAX_PRICE_MINOR, strict=True)
    eligible_lot_count: int = Field(ge=0, strict=True)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expected_market_revision: int = Field(ge=1, strict=True)
    expected_seller_inventory_revision: int = Field(ge=1, strict=True)
    expected_stock_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    expected_unit_price_minor: int = Field(ge=0, le=MAX_PRICE_MINOR, strict=True)


def parse_market_pricing_config(metadata: Mapping[str, object]) -> MarketPricingConfig | None:
    """Validate the optional config stored under ``metadata.market_pricing``."""

    raw = metadata.get(PRICE_REVIEW_CONFIG_KEY)
    if raw is None:
        return None
    return MarketPricingConfig.model_validate(raw)


def validate_market_pricing_config(state: WorldState) -> MarketPricingConfig | None:
    """Validate configured market/listing references against a loaded world."""

    from .components import MarketComponent

    config = parse_market_pricing_config(state.metadata)
    review_schedules = [
        scheduled for scheduled in state.scheduled_events
        if scheduled.handler_id == PRICE_REVIEW_HANDLER_ID
    ]
    if config is None:
        if review_schedules:
            raise MarketPriceInputError("market price review schedule exists without a versioned world config")
        return None
    if len(review_schedules) != 1:
        raise MarketPriceInputError("market pricing config requires exactly one world review schedule")
    schedule = review_schedules[0]
    expected_period = config.review_period_days * state.calendar.day_seconds
    if (
        schedule.handler_version != PRICE_REVIEW_HANDLER_VERSION
        or schedule.phase != "maintenance"
        or schedule.payload != {}
        or schedule.repeat_ticks != expected_period
    ):
        raise MarketPriceInputError("market price review schedule does not match the versioned config")
    if schedule.status == "queued":
        if schedule.completed_runs >= schedule.max_runs:
            raise MarketPriceInputError("terminal market price schedule cannot remain queued")
    elif schedule.status == "completed":
        if schedule.completed_runs != schedule.max_runs:
            raise MarketPriceInputError("completed market price schedule must be at its finite run cap")
    else:
        raise MarketPriceInputError("configured market price review schedule must be queued or terminal")
    for policy in config.policies:
        market_entity = state.entities.get(policy.market_id)
        if market_entity is None:
            raise MarketPriceInputError(f"configured market {policy.market_id} does not exist")
        try:
            market = MarketComponent.model_validate(market_entity.components.get("market"))
        except Exception as exc:
            raise MarketPriceInputError(f"configured market {policy.market_id} has no valid market component") from exc
        offer = next((item for item in market.offers if item.listing_id == policy.listing_id), None)
        if offer is None:
            raise MarketPriceInputError(
                f"configured listing {policy.listing_id} does not exist in market {policy.market_id}"
            )
        if offer.resource_id == market.currency_resource_id:
            raise MarketPriceInputError("configured market listing cannot sell the market's own currency")
    return config


def observe_market_offer(
    state: WorldState,
    market_id: UUID,
    listing_id: UUID,
    *,
    at_tick: Tick,
) -> MarketStockObservation:
    """Read a seller's unexpired, FIFO-ordered inventory for one active offer."""

    from .components import InventoryComponent, MarketComponent

    if at_tick != state.current_tick:
        raise MarketPriceInputError("market stock must be observed at the world's current tick")
    market_entity = state.entities.get(market_id)
    if market_entity is None:
        raise MarketPriceInputError("market entity does not exist")
    try:
        market = MarketComponent.model_validate(market_entity.components.get("market"))
    except Exception as exc:
        raise MarketPriceInputError("market has no valid registered market component") from exc
    offer = next((item for item in market.offers if item.listing_id == listing_id), None)
    if offer is None:
        raise MarketPriceInputError("configured listing does not exist", code="listing_missing")

    if not offer.active:
        raise _unavailable(
            "listing is inactive",
            code="listing_inactive",
            market=market,
            market_id=market_id,
            offer=offer,
            at_tick=at_tick,
            seller_status=None,
            seller_location=None,
        )
    if market_entity.status != "active":
        raise _unavailable(
            "market is inactive",
            code="market_inactive",
            market=market,
            market_id=market_id,
            offer=offer,
            at_tick=at_tick,
            seller_status=None,
            seller_location=None,
        )

    seller = state.entities.get(offer.seller_id)
    if seller is None:
        raise MarketPriceInputError("configured listing seller does not exist", code="seller_missing")
    if seller.status != "active":
        raise _unavailable(
            "seller is inactive",
            code="seller_inactive",
            market=market,
            market_id=market_id,
            offer=offer,
            at_tick=at_tick,
            seller_status=seller.status,
            seller_location=seller.location_id,
        )
    if seller.location_id != market_id:
        raise _unavailable(
            "seller is not present at the market",
            code="seller_not_present",
            market=market,
            market_id=market_id,
            offer=offer,
            at_tick=at_tick,
            seller_status=seller.status,
            seller_location=seller.location_id,
        )

    stock = state.resources.get(offer.stock_account_id)
    if (
        stock is None
        or stock.account_type != "holder"
        or stock.owner_id != seller.id
        or stock.resource_id != offer.resource_id
    ):
        raise MarketPriceInputError("listing stock account does not match its active seller and resource")
    if offer.resource_id == market.currency_resource_id:
        raise MarketPriceInputError("listing cannot trade the market currency for itself")
    try:
        inventory = InventoryComponent.model_validate(seller.components.get("inventory"))
    except Exception as exc:
        raise MarketPriceInputError("listed seller has no valid inventory component") from exc

    eligible = [
        lot
        for lot in inventory.lots
        if lot.account_id == offer.stock_account_id
        and lot.resource_id == offer.resource_id
        and (lot.expiry_tick is None or lot.expiry_tick > at_tick)
    ]
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        tradable_quantity = sum((lot.quantity for lot in eligible), Decimal("0"))
    if tradable_quantity > stock.balance:
        raise MarketPriceInputError("eligible inventory lots exceed the seller stock account balance")

    digest = _snapshot_digest(
        market_id=market_id,
        market_revision=market.revision,
        listing_id=offer.listing_id,
        seller_id=seller.id,
        resource_id=offer.resource_id,
        currency_resource_id=market.currency_resource_id,
        stock_account_id=offer.stock_account_id,
        minimum_quantity=offer.minimum_quantity,
        observed_tick=at_tick,
        current_unit_price_minor=offer.unit_price_minor,
        stock_account_balance=stock.balance,
        seller_inventory_revision=inventory.revision,
        eligible_lots=eligible,
    )
    return MarketStockObservation(
        market_id=market_id,
        listing_id=offer.listing_id,
        seller_id=seller.id,
        resource_id=offer.resource_id,
        stock_account_id=offer.stock_account_id,
        observed_tick=at_tick,
        tradable_quantity=tradable_quantity,
        stock_account_balance=stock.balance,
        market_revision=market.revision,
        seller_inventory_revision=inventory.revision,
        current_unit_price_minor=offer.unit_price_minor,
        eligible_lot_count=len(eligible),
        snapshot_digest=digest,
    )


def plan_market_price_update(
    observation: MarketStockObservation,
    policy: StockPricePolicy,
    *,
    schedule_id: UUID,
    period_index: int,
    due_tick: Tick,
) -> MarketPriceProposal:
    """Return a deterministic bounded quote without changing world state."""

    if period_index < 1:
        raise MarketPriceInputError("price review period index must be positive")
    if observation.observed_tick != due_tick:
        raise MarketPriceInputError("price review must use an observation at the exact due tick")
    pressure = _stock_pressure_bps(observation.tradable_quantity, policy)
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        raw_price = Decimal(policy.reference_price_minor) * Decimal(10_000 + pressure) / _BASIS
        rounded_price = int(raw_price.to_integral_value(rounding=ROUND_HALF_UP))
    final_price = min(policy.price_ceiling_minor, max(policy.price_floor_minor, rounded_price))
    assessment_id = uuid5(
        schedule_id,
        f"market.price_review.v1:{observation.listing_id.hex}:{period_index}",
    )
    return MarketPriceProposal(
        assessment_id=assessment_id,
        schedule_id=schedule_id,
        period_index=period_index,
        due_tick=due_tick,
        observed_tick=observation.observed_tick,
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        market_id=observation.market_id,
        listing_id=observation.listing_id,
        seller_id=observation.seller_id,
        resource_id=observation.resource_id,
        stock_account_id=observation.stock_account_id,
        reference_price_minor=policy.reference_price_minor,
        target_stock_quantity=policy.target_stock_quantity,
        observed_stock_quantity=observation.tradable_quantity,
        pressure_bps=pressure,
        unit_price_minor=final_price,
        eligible_lot_count=observation.eligible_lot_count,
        snapshot_digest=observation.snapshot_digest,
        expected_market_revision=observation.market_revision,
        expected_seller_inventory_revision=observation.seller_inventory_revision,
        expected_stock_account_balance=observation.stock_account_balance,
        expected_unit_price_minor=observation.current_unit_price_minor,
    )


def plan_market_price_review(
    state: WorldState,
    scheduled_event: ScheduledEventRecord,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
) -> MarketPriceReviewResolvedEvent:
    """Batch all configured listings into one compact deterministic event."""

    from .market_pricing_events import MarketPriceChange, MarketPriceReviewResolvedEvent

    config = validate_market_pricing_config(state)
    if config is None:
        raise MarketPriceInputError("scheduled market price review has no world pricing config")
    stored = next((item for item in state.scheduled_events if item.id == scheduled_event.id), None)
    if stored is None or stored.model_dump(mode="json") != scheduled_event.model_dump(mode="json"):
        raise MarketPriceInputError("price review schedule does not match the stored queued schedule")
    if (
        stored.handler_id != PRICE_REVIEW_HANDLER_ID
        or stored.handler_version != PRICE_REVIEW_HANDLER_VERSION
        or stored.phase != "maintenance"
        or stored.status != "queued"
        or stored.due_tick != state.current_tick
        or stored.repeat_ticks != config.review_period_days * state.calendar.day_seconds
        or stored.completed_runs >= stored.max_runs
    ):
        raise MarketPriceInputError("market price review schedule is invalid or not due at the exact world tick")
    by_id = {item.id: item for item in state.scheduled_events}
    if any(
        dependency not in by_id or by_id[dependency].status != "completed"
        for dependency in stored.dependencies
    ):
        raise MarketPriceInputError("market price review dependencies are not complete")

    period_index = stored.completed_runs + 1
    rows: list[dict[str, object]] = []
    changes: list[MarketPriceChange] = []
    unchanged_count = 0
    skipped_count = 0
    policies = sorted(config.policies, key=lambda row: (row.market_id.hex, row.listing_id.hex))
    for assignment in policies:
        try:
            observation = observe_market_offer(
                state,
                assignment.market_id,
                assignment.listing_id,
                at_tick=state.current_tick,
            )
        except MarketPriceUnavailable as unavailable:
            skipped_count += 1
            rows.append({
                "policy_id": assignment.policy_id,
                "policy_revision": assignment.revision,
                "market_id": str(assignment.market_id),
                "listing_id": str(assignment.listing_id),
                "status": "skipped",
                "reason": unavailable.code,
                "snapshot_digest": unavailable.snapshot_digest,
            })
            continue
        proposal = plan_market_price_update(
            observation,
            assignment,
            schedule_id=stored.id,
            period_index=period_index,
            due_tick=stored.due_tick,
        )
        status = "changed" if proposal.unit_price_minor != observation.current_unit_price_minor else "unchanged"
        rows.append({
            "policy_id": assignment.policy_id,
            "policy_revision": assignment.revision,
            "market_id": str(assignment.market_id),
            "listing_id": str(assignment.listing_id),
            "status": status,
            "snapshot_digest": proposal.snapshot_digest,
            "observed_stock": _canonical_decimal(proposal.observed_stock_quantity),
            "pressure_bps": proposal.pressure_bps,
            "previous_price_minor": proposal.expected_unit_price_minor,
            "proposed_price_minor": proposal.unit_price_minor,
        })
        if status == "unchanged":
            unchanged_count += 1
        else:
            changes.append(MarketPriceChange(
                market_id=proposal.market_id,
                listing_id=proposal.listing_id,
                seller_id=proposal.seller_id,
                stock_account_id=proposal.stock_account_id,
                policy_id=proposal.policy_id,
                policy_revision=proposal.policy_revision,
                expected_market_revision=proposal.expected_market_revision,
                expected_seller_inventory_revision=proposal.expected_seller_inventory_revision,
                expected_stock_account_balance=proposal.expected_stock_account_balance,
                previous_unit_price_minor=proposal.expected_unit_price_minor,
                new_unit_price_minor=proposal.unit_price_minor,
                snapshot_digest=proposal.snapshot_digest,
            ))

    changes.sort(key=lambda row: (row.market_id.hex, row.listing_id.hex))
    event_id = uuid5(
        command_id,
        f"{ruleset_version}:market.price_review:{stored.id}:{period_index}",
    )
    return MarketPriceReviewResolvedEvent(
        event_id=event_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        schedule_id=stored.id,
        expected_schedule_revision=stored.revision,
        expected_completed_runs=stored.completed_runs,
        period_index=period_index,
        due_tick=stored.due_tick,
        config_digest=_config_digest(config),
        assessment_count=len(policies),
        changed_count=len(changes),
        unchanged_count=unchanged_count,
        skipped_count=skipped_count,
        assessment_digest=_digest_payload(rows),
        changes=changes,
    )


def _stock_pressure_bps(observed: Decimal, policy: StockPricePolicy) -> int:
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        pressure = (policy.target_stock_quantity - observed) * Decimal(policy.sensitivity_bps) / policy.target_stock_quantity
        rounded = int(pressure.to_integral_value(rounding=ROUND_HALF_UP))
    return min(policy.max_markup_bps, max(-policy.max_discount_bps, rounded))


def _unavailable(
    message: str,
    *,
    code: str,
    market,
    market_id: UUID,
    offer,
    at_tick: int,
    seller_status: str | None,
    seller_location: UUID | None,
) -> MarketPriceUnavailable:
    digest = _digest_payload([{
        "market_id": str(market_id),
        "market_revision": market.revision,
        "listing_id": str(offer.listing_id),
        "seller_id": str(offer.seller_id),
        "resource_id": offer.resource_id,
        "stock_account_id": str(offer.stock_account_id),
        "currency_resource_id": market.currency_resource_id,
        "minimum_quantity": _canonical_decimal(offer.minimum_quantity),
        "active": offer.active,
        "current_unit_price_minor": offer.unit_price_minor,
        "observed_tick": at_tick,
        "seller_status": seller_status,
        "seller_location": str(seller_location) if seller_location is not None else None,
        "reason": code,
    }])
    return MarketPriceUnavailable(message, code=code, snapshot_digest=digest)


def _snapshot_digest(
    *,
    market_id: UUID,
    market_revision: int,
    listing_id: UUID,
    seller_id: UUID,
    resource_id: ResourceName,
    currency_resource_id: ResourceName,
    stock_account_id: UUID,
    minimum_quantity: Decimal,
    observed_tick: int,
    current_unit_price_minor: int,
    stock_account_balance: Decimal,
    seller_inventory_revision: int,
    eligible_lots: list[object],
) -> str:
    lot_rows = [
        {
            "lot_id": str(lot.lot_id),
            "account_id": str(lot.account_id),
            "resource_id": lot.resource_id,
            "quantity": _canonical_decimal(lot.quantity),
            "quality": lot.quality,
            "expiry_tick": lot.expiry_tick,
            "origin_event_id": str(lot.origin_event_id) if lot.origin_event_id else None,
        }
        for lot in eligible_lots
    ]
    snapshot = {
        "market_id": str(market_id),
        "market_revision": market_revision,
        "listing_id": str(listing_id),
        "seller_id": str(seller_id),
        "resource_id": resource_id,
        "currency_resource_id": currency_resource_id,
        "stock_account_id": str(stock_account_id),
        "minimum_quantity": _canonical_decimal(minimum_quantity),
        "observed_tick": observed_tick,
        "current_unit_price_minor": current_unit_price_minor,
        "stock_account_balance": _canonical_decimal(stock_account_balance),
        "seller_inventory_revision": seller_inventory_revision,
        "eligible_lots_fifo": lot_rows,
    }
    return _digest_payload(snapshot)


def _config_digest(config: MarketPricingConfig) -> str:
    payload = config.model_dump(mode="json")
    payload["policies"] = sorted(
        payload["policies"],
        key=lambda row: (UUID(row["market_id"]).hex, UUID(row["listing_id"]).hex),
    )
    return _digest_payload(payload)


def _digest_payload(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
