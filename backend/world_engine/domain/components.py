"""Closed schemas for authoritative entity components and world records."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Literal, get_args
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .json_types import JsonObject, JsonValue
from .investigation import EvidenceAssessment, EvidenceDisclosureReceipt, InvestigationFact
from .investigation_cases import InvestigationCaseDefinition


def _parse_exact_decimal(value: object) -> Decimal:
    if isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("decimal values must use decimal strings or integers, not binary floats")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("value is not a valid decimal") from exc
    if not number.is_finite():
        raise ValueError("decimal values must be finite numbers")
    parts = number.as_tuple()
    digits = parts.digits
    exponent = parts.exponent
    digit_count = len(digits)
    while digit_count and digits[digit_count - 1] == 0:
        digit_count -= 1
        exponent += 1
    scale = max(0, -exponent)
    integer_digits = max(1, digit_count + exponent)
    if scale > 6:
        raise ValueError("decimal values support at most 6 fractional digits")
    if integer_digits > 30:
        raise ValueError("decimal values support at most 30 integer digits")
    return number


ExactDecimal = Annotated[Decimal, BeforeValidator(_parse_exact_decimal)]
UnitInterval = Annotated[ExactDecimal, Field(ge=Decimal("0"), le=Decimal("1"))]
SignedRelationValue = Annotated[ExactDecimal, Field(ge=Decimal("-100"), le=Decimal("100"))]
BoundedName = Annotated[str, Field(min_length=1, max_length=160)]
ResourceName = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]
Tick = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
StrictTick = Annotated[StrictInt, Field(ge=-(2**63), le=2**63 - 1)]


class ComponentModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    revision: int = Field(default=1, ge=1)


class PopulationOrigin(BaseModel):
    """Reducer-authored source of a named person promoted from a real cohort."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    source_event_id: UUID
    source_event_type: Literal[
        "core.cohort_member_promoted.v1",
        "core.renewable_resource_harvested.v1",
    ]
    population_group_id: UUID


class LifeComponent(ComponentModel):
    species_id: BoundedName = "core.human"
    birth_tick: Tick
    lifespan_ticks: int | None = Field(default=None, gt=0)
    maturity_ticks: int = Field(default=0, ge=0)
    population_group_id: UUID | None = None
    population_origin: PopulationOrigin | None = None
    is_immortal: bool = False
    life_stage: Literal["child", "adult", "elder", "unknown"] = "unknown"

    @model_validator(mode="after")
    def immortal_lifespan_consistency(self) -> LifeComponent:
        if self.is_immortal and self.lifespan_ticks is not None:
            raise ValueError("immortal characters cannot have a fixed lifespan")
        return self


class InjuryRecord(ComponentModel):
    injury_id: UUID
    injury_type: BoundedName
    severity: int = Field(ge=1, le=100)
    started_tick: Tick
    cause_event_id: UUID | None = None
    treated: bool = False
    treated_event_id: UUID | None = None
    treated_tick: Tick | None = None
    description: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def treatment_provenance_is_consistent(self) -> InjuryRecord:
        if self.treated_event_id is not None and (not self.treated or self.treated_tick is None):
            raise ValueError("treated injury provenance requires a treated injury and treatment tick")
        if self.treated_tick is not None and (not self.treated or self.treated_event_id is None):
            raise ValueError("treated injury tick requires its treatment event provenance")
        return self


class HealthComponent(ComponentModel):
    current: int = Field(ge=0)
    maximum: int = Field(gt=0)
    injuries: list[InjuryRecord] = Field(default_factory=list, max_length=10_000)
    conditions: list[str] = Field(default_factory=list, max_length=100)
    incapacitating_injury_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def health_within_maximum(self) -> HealthComponent:
        if self.current > self.maximum:
            raise ValueError("current health cannot exceed maximum")
        if len({item.injury_id for item in self.injuries}) != len(self.injuries):
            raise ValueError("injury ids must be unique")
        if self.incapacitating_injury_id is not None and not any(
            injury.injury_id == self.incapacitating_injury_id and not injury.treated for injury in self.injuries
        ):
            raise ValueError("an incapacitating injury must reference an existing untreated injury")
        return self


class CombatEncounterComponent(ComponentModel):
    """One participant's replayable view of a pairwise combat encounter."""

    encounter_id: UUID
    opponent_id: UUID
    location_id: UUID
    started_tick: Tick
    status: Literal["active", "retreated", "resolved"] = "active"
    exchange_count: int = Field(ge=1)
    last_event_id: UUID
    last_damage_event_id: UUID | None = None
    last_outcome: Literal["hit", "miss", "retreated", "truce"]
    resolved_tick: Tick | None = None

    @model_validator(mode="after")
    def terminal_status_has_resolution_tick(self) -> CombatEncounterComponent:
        if self.status == "active" and self.resolved_tick is not None:
            raise ValueError("an active combat encounter cannot have a resolution tick")
        if self.status != "active" and self.resolved_tick is None:
            raise ValueError("a closed combat encounter requires its resolution tick")
        if self.status == "retreated" and self.last_outcome != "retreated":
            raise ValueError("a retreated encounter must record a retreat outcome")
        if self.last_outcome == "truce" and self.status != "resolved":
            raise ValueError("a truce must resolve its encounter")
        if self.status == "resolved" and self.last_outcome == "retreated":
            raise ValueError("a resolved encounter cannot use a retreat outcome")
        return self


class NeedValue(ComponentModel):
    value: int = Field(ge=0, le=100)
    desired: int = Field(default=80, ge=0, le=100)
    critical_below: int = Field(default=20, ge=0, le=100)
    last_satisfied_tick: Tick | None = None
    # Remainder of deterministic rate-based need change in world seconds.
    # Keeping it prevents short actions from losing fractional day progress.
    decay_remainder: int = Field(default=0, ge=-(2**31) + 1, le=2**31 - 1)
    last_critical_damage_tick: Tick | None = None


class NeedsComponent(ComponentModel):
    food: NeedValue | None = None
    water: NeedValue | None = None
    rest: NeedValue | None = None
    shelter: NeedValue | None = None
    safety: NeedValue | None = None
    health: NeedValue | None = None
    social: NeedValue | None = None
    belonging: NeedValue | None = None
    custom: dict[BoundedName, NeedValue] = Field(default_factory=dict, max_length=64)


class SkillValue(ComponentModel):
    level: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("100"))
    experience: int = Field(default=0, ge=0)
    trained_ticks: int = Field(default=0, ge=0)
    last_practiced_tick: Tick | None = None


class SkillsComponent(ComponentModel):
    skills: dict[ResourceName, SkillValue] = Field(default_factory=dict, max_length=256)


PersonalityField = Literal[
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
    "traits",
    "values",
]
_PERSONALITY_FIELD_ORDER = get_args(PersonalityField)


class PersonalityLockViolation(ValueError):
    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__(f"Locked personality fields cannot change until unlocked: {', '.join(fields)}.")


class PersonalityComponent(ComponentModel):
    openness: UnitInterval = Decimal("0.5")
    conscientiousness: UnitInterval = Decimal("0.5")
    extraversion: UnitInterval = Decimal("0.5")
    agreeableness: UnitInterval = Decimal("0.5")
    neuroticism: UnitInterval = Decimal("0.5")
    traits: list[BoundedName] = Field(default_factory=list, max_length=64)
    values: list[BoundedName] = Field(default_factory=list, max_length=64)
    locked_fields: tuple[PersonalityField, ...] = Field(
        default=(), max_length=len(_PERSONALITY_FIELD_ORDER), exclude_if=lambda value: not value,
    )

    @field_validator("locked_fields")
    @classmethod
    def canonicalize_locked_fields(cls, fields: tuple[PersonalityField, ...]) -> tuple[PersonalityField, ...]:
        distinct_fields = set(fields)
        if len(distinct_fields) != len(fields):
            raise ValueError("personality locked_fields must be unique")
        return tuple(field for field in _PERSONALITY_FIELD_ORDER if field in distinct_fields)


class DailyRoutineWindow(ComponentModel):
    """One non-overlapping daily activity window with a typed place reference."""

    window_id: UUID
    start_second: int = Field(ge=0, le=2**31 - 1)
    duration_seconds: int = Field(gt=0, le=2**31 - 1)
    activity: Literal["rest", "work", "study", "socialize"]
    place_id: UUID


class DailyRoutineComponent(ComponentModel):
    windows: list[DailyRoutineWindow] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unique_non_overlapping_windows(self) -> DailyRoutineComponent:
        ids = [window.window_id for window in self.windows]
        if len(ids) != len(set(ids)):
            raise ValueError("daily routine window IDs must be unique")
        by_start = sorted(self.windows, key=lambda window: window.start_second)
        for previous, current in zip(by_start, by_start[1:]):
            if previous.start_second + previous.duration_seconds > current.start_second:
                raise ValueError("daily routine windows cannot overlap")
        return self


class AttributesComponent(ComponentModel):
    values: dict[ResourceName, ExactDecimal | str | int | bool] = Field(default_factory=dict, max_length=256)

    @field_validator("values", mode="before")
    @classmethod
    def reject_attribute_floats(cls, value: object) -> object:
        if isinstance(value, dict) and any(isinstance(item, float) for item in value.values()):
            raise ValueError("attribute numeric values must not use binary floats")
        return value


class CapabilitiesComponent(ComponentModel):
    grants: list[ResourceName] = Field(default_factory=list, max_length=256)
    restrictions: list[ResourceName] = Field(default_factory=list, max_length=256)
    combat_death_protection: StrictBool = Field(default=False, exclude_if=lambda value: not value)


class ProgressionComponent(ComponentModel):
    experience: int = Field(default=0, ge=0)
    level: int = Field(default=1, ge=1)
    milestones: list[BoundedName] = Field(default_factory=list, max_length=256)
    next_threshold: int | None = Field(default=None, ge=0)


class PlaceRoute(ComponentModel):
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    travel_ticks: int = Field(gt=0, le=2**63 - 1)
    capacity: int | None = Field(default=None, gt=0)
    risk: UnitInterval = Decimal("0")
    access_tags: list[ResourceName] = Field(default_factory=list, max_length=128)
    travel_modes: list[ResourceName] = Field(default_factory=list, max_length=64)
    is_open: bool = True

    @model_validator(mode="after")
    def distinct_endpoints(self) -> PlaceRoute:
        if self.from_place_id == self.to_place_id:
            raise ValueError("a route must connect two different places")
        return self


class WaypointOpportunity(ComponentModel):
    """An optional stop a traveler may choose to explore along an itinerary.

    This record advertises a possible interaction; it never grants consent or
    performs the interaction by itself.
    """

    opportunity_id: UUID
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1000)
    opens_tick: Tick | None = None
    closes_tick: Tick | None = None
    minimum_itinerary_elapsed_ticks: int = Field(default=0, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def valid_availability_window(self) -> WaypointOpportunity:
        if (
            self.opens_tick is not None
            and self.closes_tick is not None
            and self.opens_tick >= self.closes_tick
        ):
            raise ValueError("waypoint opportunity availability must use a non-empty half-open tick range")
        return self

    def is_available(self, current_tick: int, itinerary_elapsed_ticks: int) -> bool:
        """Return whether this opportunity is open at the supplied travel boundary."""
        if itinerary_elapsed_ticks < self.minimum_itinerary_elapsed_ticks:
            return False
        if self.opens_tick is not None and current_tick < self.opens_tick:
            return False
        if self.closes_tick is not None and current_tick >= self.closes_tick:
            return False
        return True


class RenewableHarvestPolicy(ComponentModel):
    """Finite seasonal yield issued from one region's registered environment."""

    policy_id: ResourceName
    resource_id: ResourceName
    source_account_id: UUID
    period_ticks: int = Field(gt=0, le=2**63 - 1)
    per_period: ExactDecimal = Field(gt=Decimal("0"))
    maximum_total: ExactDecimal = Field(gt=Decimal("0"))
    issued_total: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"))
    last_issued_period: int = Field(default=0, ge=0, le=2**63 - 1)
    recipients: list[RenewableHarvestRecipient] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def finite_yield_bound(self) -> RenewableHarvestPolicy:
        if self.issued_total > self.maximum_total:
            raise ValueError("renewable issuance cannot exceed the policy's finite maximum_total")
        recipient_ids = [recipient.entity_id for recipient in self.recipients]
        account_ids = [recipient.account_id for recipient in self.recipients]
        if len(recipient_ids) != len(set(recipient_ids)) or recipient_ids != sorted(recipient_ids, key=str):
            raise ValueError("renewable recipients must be unique and use stable entity UUID order")
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("renewable recipients cannot share holder accounts")
        with localcontext() as context:
            context.prec = 80
            recipient_total = sum((recipient.per_period for recipient in self.recipients), Decimal("0"))
        if recipient_total != self.per_period:
            raise ValueError("renewable policy per_period must equal the sum of named recipient rations")
        return self


class RenewableHarvestRecipient(ComponentModel):
    """One named character's finite-period ration and owned holder account."""

    entity_id: UUID
    account_id: UUID
    per_period: ExactDecimal = Field(gt=Decimal("0"))


class PlaceComponent(ComponentModel):
    place_type: Literal["region", "settlement", "building", "wilderness", "site", "other"] = "site"
    region_id: UUID | None = None
    parent_id: UUID | None = None
    capacity: int | None = Field(default=None, gt=0)
    access_policy: Literal["open", "restricted", "private"] = "open"
    terrain: ResourceName | None = None
    environment: dict[ResourceName, str | int | bool] = Field(default_factory=dict, max_length=128)
    routes: list[PlaceRoute] = Field(default_factory=list, max_length=2048)
    renewable_harvests: list[RenewableHarvestPolicy] = Field(default_factory=list, max_length=4096)
    waypoint_opportunities: list[WaypointOpportunity] = Field(
        default_factory=list,
        max_length=64,
        exclude_if=lambda value: not value,
    )

    @field_validator("waypoint_opportunities")
    @classmethod
    def stable_waypoint_opportunity_order(
        cls,
        opportunities: list[WaypointOpportunity],
    ) -> list[WaypointOpportunity]:
        return sorted(opportunities, key=lambda opportunity: str(opportunity.opportunity_id))

    @model_validator(mode="after")
    def unique_routes(self) -> PlaceComponent:
        route_ids = [route.route_id for route in self.routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("route ids must be unique within a place")
        policy_ids = [policy.policy_id for policy in self.renewable_harvests]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("renewable harvest policy ids must be unique within a place")
        resources = [policy.resource_id for policy in self.renewable_harvests]
        if len(resources) != len(set(resources)):
            raise ValueError("renewable harvest resources must be unique within a place")
        source_ids = [policy.source_account_id for policy in self.renewable_harvests]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("renewable harvest source accounts must be unique within a place")
        opportunity_ids = [opportunity.opportunity_id for opportunity in self.waypoint_opportunities]
        if len(opportunity_ids) != len(set(opportunity_ids)):
            raise ValueError("waypoint opportunity ids must be unique within a place")
        return self


class MarketOffer(ComponentModel):
    listing_id: UUID
    resource_id: ResourceName
    stock_account_id: UUID
    seller_id: UUID
    unit_price_minor: int = Field(ge=0)
    minimum_quantity: ExactDecimal = Field(default=Decimal("1"), gt=Decimal("0"))
    active: bool = True


class MarketComponent(ComponentModel):
    currency_resource_id: ResourceName
    offers: list[MarketOffer] = Field(default_factory=list, max_length=4096)

    @model_validator(mode="after")
    def unique_listings(self) -> MarketComponent:
        listing_ids = [offer.listing_id for offer in self.offers]
        if len(listing_ids) != len(set(listing_ids)):
            raise ValueError("market listing ids must be unique")
        return self


class RecipeFlow(ComponentModel):
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ProductionRecipe(ComponentModel):
    recipe_id: ResourceName
    inputs: list[RecipeFlow] = Field(min_length=1, max_length=128)
    outputs: list[RecipeFlow] = Field(min_length=1, max_length=128)
    duration_ticks: int = Field(gt=0, le=2**63 - 1)
    required_skill: ResourceName | None = None
    required_skill_level: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("100"))


class ProductionQuotaPolicy(ComponentModel):
    """A bounded periodic allowance for a registered producer source account."""

    quota_id: ResourceName
    resource_id: ResourceName
    source_account_id: UUID
    period_ticks: int = Field(gt=0, le=2**63 - 1)
    per_period: ExactDecimal = Field(gt=Decimal("0"))
    maximum_renewal: ExactDecimal = Field(gt=Decimal("0"))
    base_credit_limit: ExactDecimal = Field(ge=Decimal("0"))
    issued_total: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"))
    # Period zero is represented by the initial/base quota. Renewals begin in 1.
    last_issued_period: int = Field(default=0, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def finite_issuance_bound(self) -> ProductionQuotaPolicy:
        if self.issued_total > self.maximum_renewal:
            raise ValueError("issued quota cannot exceed the policy's finite maximum_renewal")
        return self


class ProductionComponent(ComponentModel):
    recipes: list[ProductionRecipe] = Field(default_factory=list, max_length=2048)
    quotas: list[ProductionQuotaPolicy] = Field(default_factory=list, max_length=2048)

    @model_validator(mode="after")
    def unique_recipe_ids(self) -> ProductionComponent:
        recipe_ids = [recipe.recipe_id for recipe in self.recipes]
        if len(recipe_ids) != len(set(recipe_ids)):
            raise ValueError("production recipe ids must be unique")
        quota_ids = [quota.quota_id for quota in self.quotas]
        if len(quota_ids) != len(set(quota_ids)):
            raise ValueError("production quota ids must be unique")
        return self


class OfficeRecord(ComponentModel):
    office_id: ResourceName
    title: BoundedName
    holder_id: UUID | None = None
    authority: list[ResourceName] = Field(default_factory=list, max_length=256)
    term_end_tick: Tick | None = None


LEADERSHIP_OFFICE_ID = "office.leader"
LEADERSHIP_AUTHORITY_TAG = "organization.lead"


def is_leadership_office(office: OfficeRecord) -> bool:
    """Recognize the registered leader office or an explicitly tagged equivalent."""
    return office.office_id == LEADERSHIP_OFFICE_ID or LEADERSHIP_AUTHORITY_TAG in office.authority


def leadership_office_ids_held_by(offices: list[OfficeRecord], member_id: UUID) -> list[str]:
    """Return the registered leadership offices tied to this holder."""
    return sorted(office.office_id for office in offices if office.holder_id == member_id and is_leadership_office(office))


class MembershipRecruitmentPolicy(BaseModel):
    """Finite, explicit authority to enroll local adult residents as members."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    policy_id: ResourceName
    charter_clause_id: ResourceName
    admission_rule_id: Literal["organization.local_role_match.v1"]
    authority_tag: ResourceName
    region_id: UUID
    eligible_roles: list[BoundedName] = Field(min_length=1, max_length=1024)
    minimum_age_years: int = Field(default=18, ge=0, le=200)
    maximum_age_years: int = Field(default=64, ge=0, le=200)
    active_member_capacity: int = Field(ge=1, le=100_000)
    max_recruits_per_period: int = Field(ge=1, le=1000)
    selection_seed_id: UUID

    @model_validator(mode="after")
    def finite_recruitment_policy(self) -> MembershipRecruitmentPolicy:
        roles = [role.casefold() for role in self.eligible_roles]
        if len(roles) != len(set(roles)):
            raise ValueError("recruitment roles must be unique ignoring case")
        if self.minimum_age_years > self.maximum_age_years:
            raise ValueError("recruitment age range is reversed")
        if self.max_recruits_per_period > self.active_member_capacity:
            raise ValueError("period recruitment cannot exceed finite active-member capacity")
        return self


class GovernanceComponent(ComponentModel):
    charter: str = Field(default="", max_length=8000)
    members: list[UUID] = Field(default_factory=list, max_length=100_000)
    leader_id: UUID | None = None
    offices: list[OfficeRecord] = Field(default_factory=list, max_length=4096)
    succession_policy: Literal["election", "appointment", "inheritance", "council", "none"] = "none"
    decision_threshold: UnitInterval = Decimal("0.5")
    authority_tags: list[ResourceName] = Field(default_factory=list, max_length=256)
    charter_clause_ids: list[ResourceName] = Field(default_factory=list, max_length=4096)
    membership_recruitment: list[MembershipRecruitmentPolicy] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def unique_members_offices(self) -> GovernanceComponent:
        if len(self.members) != len(set(self.members)):
            raise ValueError("organization members must be unique")
        if len(self.authority_tags) != len(set(self.authority_tags)):
            raise ValueError("organization authority tags must be unique")
        if len(self.charter_clause_ids) != len(set(self.charter_clause_ids)):
            raise ValueError("organization charter clause ids must be unique")
        policy_ids = [policy.policy_id for policy in self.membership_recruitment]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("organization membership recruitment policy ids must be unique")
        if len(self.membership_recruitment) > 1:
            raise ValueError("only one local role-matching membership policy per organization is currently supported")
        office_ids = [office.office_id for office in self.offices]
        if len(office_ids) != len(set(office_ids)):
            raise ValueError("organization office ids must be unique")
        if self.leader_id is not None and self.leader_id not in self.members:
            raise ValueError("organization leader must be listed as a member")
        for office in self.offices:
            if office.holder_id is not None and office.holder_id not in self.members:
                raise ValueError("office holder must be listed as an organization member")
        return self


class TreasuryComponent(ComponentModel):
    accounts: dict[ResourceName, UUID] = Field(default_factory=dict, max_length=128)


class InventoryLot(ComponentModel):
    lot_id: UUID
    account_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    quality: int = Field(default=100, ge=0, le=100)
    expiry_tick: Tick | None = None
    origin_event_id: UUID | None = None


class InventoryComponent(ComponentModel):
    lots: list[InventoryLot] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def unique_lot_ids(self) -> InventoryComponent:
        lot_ids = [lot.lot_id for lot in self.lots]
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("inventory lot ids must be unique")
        return self


class PopulationAgeRate(ComponentModel):
    """Annual per-member fertility and mortality rates for one age in years."""

    age_years: int = Field(ge=0, le=200)
    annual_birth_rate: UnitInterval = Decimal("0")
    annual_death_rate: UnitInterval = Decimal("0")


class PopulationAgeCohort(ComponentModel):
    """Anonymous residents at an exact age; 200 denotes age 200 and above.

    Fractional annual rates can merge from ages 199 and 200 into one 200+
    bucket. Remainders therefore allow values below two until the next annual
    calculation consumes their integral part.
    """

    age_years: int = Field(ge=0, le=200)
    headcount: int = Field(ge=0, le=2**63 - 1)
    birth_remainder: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), lt=Decimal("2"))
    death_remainder: ExactDecimal = Field(default=Decimal("0"), ge=Decimal("0"), lt=Decimal("2"))


PopulationRoleWeight = Annotated[StrictInt, Field(gt=0, le=1000)]
PopulationSkillLevel = Annotated[ExactDecimal, Field(ge=Decimal("0"), le=Decimal("100"))]
PopulationRoleSkills = Annotated[dict[ResourceName, PopulationSkillLevel], Field(max_length=32)]


class PopulationOccupationProfile(ComponentModel):
    """Optional authored weights and skills for regional seat promotions."""

    role_weights: dict[BoundedName, PopulationRoleWeight] = Field(
        default_factory=dict,
        max_length=10_000,
        exclude_if=lambda value: not value,
    )
    role_skill_levels: dict[BoundedName, PopulationRoleSkills] = Field(
        default_factory=dict,
        max_length=64,
        exclude_if=lambda value: not value,
    )


class PopulationSeatPolicy(ComponentModel):
    """Finite roster and authored naming inputs for anonymous-to-named promotion."""

    non_player_seat_capacity: int = Field(ge=0, le=100_000)
    home_place_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    extra_given_names: list[BoundedName] = Field(min_length=1, max_length=10_000)
    extra_family_names: list[BoundedName] = Field(min_length=1, max_length=10_000)
    extra_roles: list[BoundedName] = Field(min_length=1, max_length=10_000)
    occupation_profile: PopulationOccupationProfile | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    maintain_civic_occupations: bool = Field(default=False, exclude_if=lambda value: not value)
    maintain_civic_recruitment_pool: bool = Field(default=False, exclude_if=lambda value: not value)
    eligible_min_age_years: int = Field(default=18, ge=0, le=200)
    eligible_max_age_years: int = Field(default=64, ge=0, le=200)
    lifespan_min_years: int = Field(ge=1, le=200)
    lifespan_max_years: int = Field(ge=1, le=200)
    seed_id: UUID
    promotion_serial: int = Field(default=0, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def finite_unique_policy_inputs(self) -> PopulationSeatPolicy:
        if len(set(self.home_place_ids)) != len(self.home_place_ids):
            raise ValueError("population seat home places must be unique")
        for label, values in (
            ("given names", self.extra_given_names),
            ("family names", self.extra_family_names),
            ("roles", self.extra_roles),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"population seat {label} must be unique")
        if self.occupation_profile is not None:
            authored_roles = set(self.extra_roles)
            profiled_roles = (
                set(self.occupation_profile.role_weights)
                | set(self.occupation_profile.role_skill_levels)
            )
            if not profiled_roles <= authored_roles:
                raise ValueError("population occupation profiles must use authored roles")
        if self.maintain_civic_occupations and (
            self.occupation_profile is None or not self.occupation_profile.role_weights
        ):
            raise ValueError("civic occupation priority requires authored regional role weights")
        if self.maintain_civic_recruitment_pool and not self.maintain_civic_occupations:
            raise ValueError("civic recruitment-pool maintenance requires civic occupation maintenance")
        if self.maintain_civic_recruitment_pool and (
            self.occupation_profile is None or not self.occupation_profile.role_weights
        ):
            raise ValueError("civic recruitment-pool maintenance requires authored regional role weights")
        if self.eligible_min_age_years > self.eligible_max_age_years:
            raise ValueError("population seat eligible age range is reversed")
        if self.eligible_max_age_years >= self.lifespan_min_years:
            raise ValueError("eligible seat ages must be younger than every permitted lifespan")
        if self.lifespan_min_years > self.lifespan_max_years:
            raise ValueError("population seat lifespan range is reversed")
        return self


class PopulationComponent(ComponentModel):
    species_id: BoundedName
    # Total living members represented by this regional cohort, including
    # individually modeled people linked through LifeComponent.population_group_id.
    headcount: int = Field(ge=0, le=2**63 - 1)
    # Anonymous members available for promotion into individually modeled records.
    # None preserves legacy worlds whose headcount did not distinguish the pool.
    unmodeled_headcount: int | None = Field(default=None, ge=0, le=2**63 - 1)
    carrying_capacity: int | None = Field(default=None, ge=0)
    annual_birth_rate: UnitInterval = Decimal("0")
    annual_death_rate: UnitInterval = Decimal("0")
    annual_migration_rate: UnitInterval = Decimal("0")
    age_rates: list[PopulationAgeRate] = Field(default_factory=list, max_length=201)
    age_cohorts: list[PopulationAgeCohort] = Field(default_factory=list, max_length=201)
    last_advanced_year: int | None = Field(default=None, ge=0, le=2**63 - 1)
    seat_policy: PopulationSeatPolicy | None = None

    @model_validator(mode="after")
    def anonymous_pool_within_total(self) -> PopulationComponent:
        if self.unmodeled_headcount is not None and self.unmodeled_headcount > self.headcount:
            raise ValueError("unmodeled_headcount cannot exceed total headcount")
        rate_ages = [item.age_years for item in self.age_rates]
        cohort_ages = [item.age_years for item in self.age_cohorts]
        if len(rate_ages) != len(set(rate_ages)) or len(cohort_ages) != len(set(cohort_ages)):
            raise ValueError("population age rates and cohort ages must be unique")
        if self.age_rates or self.age_cohorts:
            expected_ages = list(range(201))
            if rate_ages != expected_ages or cohort_ages != expected_ages:
                raise ValueError("age-structured populations require one sorted row for every age from 0 through 200+")
            if self.unmodeled_headcount is None or self.last_advanced_year is None:
                raise ValueError("age-structured populations require an explicit unmodeled count and last year")
            if sum(item.headcount for item in self.age_cohorts) != self.unmodeled_headcount:
                raise ValueError("age-cohort headcounts must sum to unmodeled_headcount")
        elif self.last_advanced_year is not None:
            raise ValueError("last_advanced_year requires a complete age-structured population")
        return self

class VisibilityScope(ComponentModel):
    policy: Literal["public", "private", "subject_ids", "participants_and_observers", "author_only"] = "private"
    subject_ids: list[UUID] = Field(default_factory=list, max_length=100_000)


class RelationObligation(ComponentModel):
    obligation_id: UUID
    kind: ResourceName
    description: str = Field(default="", max_length=2000)
    beneficiary_id: UUID | None = None
    due_tick: Tick | None = None
    status: Literal["active", "fulfilled", "breached", "released"] = "active"
    source_event_id: UUID | None = None


class RelationRecord(ComponentModel):
    id: UUID
    from_id: UUID
    to_id: UUID
    dimensions: dict[ResourceName, SignedRelationValue] = Field(default_factory=dict, max_length=64)
    label: str = Field(default="", max_length=240)
    obligations: list[RelationObligation] = Field(default_factory=list, max_length=1024)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=10_000)
    updated_tick: Tick = 0
    visibility: VisibilityScope = Field(default_factory=VisibilityScope)

    @model_validator(mode="after")
    def relation_endpoints_differ(self) -> RelationRecord:
        if self.from_id == self.to_id:
            raise ValueError("a relation must connect two different entities")
        return self


class ProvenanceReference(ComponentModel):
    kind: Literal["event", "genesis", "rule", "pack", "observation"]
    reference_id: BoundedName
    notes: str = Field(default="", max_length=1000)


class HistoricalEvidenceArtifact(ComponentModel):
    """An author-defined physical source that can be read through an inspection check."""

    evidence_id: UUID
    subject_id: UUID
    claim: str = Field(min_length=1, max_length=4000)
    fact: InvestigationFact
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validates_authored_provenance(self) -> HistoricalEvidenceArtifact:
        references = [(source.kind, source.reference_id) for source in self.provenance]
        if any(kind not in {"pack", "genesis"} for kind, _ in references):
            raise ValueError("historical evidence must cite authored pack or genesis provenance")
        if len(references) != len(set(references)):
            raise ValueError("historical evidence provenance references must be unique")
        return self


class RelationshipDisclosureReceipt(BaseModel):
    """Immutable provenance for one recipient's report of a partner's words."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    source_partnership_id: UUID
    source_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    discloser_id: UUID
    opposite_participant_id: UUID
    recipient_id: UUID
    location_id: UUID
    disclosed_tick: StrictTick

    @model_validator(mode="after")
    def has_distinct_participants(self) -> RelationshipDisclosureReceipt:
        if len({self.discloser_id, self.opposite_participant_id, self.recipient_id}) != 3:
            raise ValueError("a relationship disclosure requires two partners and a distinct recipient")
        return self


class KnowledgeRecord(ComponentModel):
    id: UUID
    owner_id: UUID | None = None
    holder_id: UUID | None = None
    subject_id: UUID | None = None
    claim: str = Field(min_length=1, max_length=4000)
    summary: str | None = Field(default=None, max_length=4000)
    confidence: UnitInterval = Decimal("0.5")
    source_refs: list[UUID] = Field(default_factory=list, max_length=10_000)
    source_event_id: UUID | None = None
    provenance: list[ProvenanceReference] = Field(default_factory=list, max_length=10_000)
    received_tick: Tick = 0
    valid_from_tick: Tick | None = None
    kind: Literal["fact", "observation", "belief", "rumor", "memory", "claim"] = "belief"
    fact: InvestigationFact | None = None
    assessment: EvidenceAssessment | None = None
    disclosure_policy: Literal["never", "if_asked"] = "never"
    disclosure: EvidenceDisclosureReceipt | None = None
    relationship_disclosure: RelationshipDisclosureReceipt | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    visibility: VisibilityScope = Field(default_factory=VisibilityScope)

    @model_validator(mode="after")
    def has_knowledge_owner(self) -> KnowledgeRecord:
        if self.owner_id is None and self.holder_id is None:
            raise ValueError("knowledge requires an owner_id or holder_id")
        if self.disclosure_policy == "if_asked" and (
            self.holder_id is None
            or self.kind not in {"belief", "claim", "rumor"}
            or self.fact is None
            or self.assessment is not None
            or self.disclosure is not None
        ):
            raise ValueError("on-request disclosure is only valid for a held, typed fallible claim")
        if self.disclosure is not None and (
            self.owner_id is None
            or self.holder_id is not None
            or self.disclosure_policy != "never"
            or self.kind not in {"belief", "claim", "rumor"}
            or self.fact is None
            or self.assessment is not None
        ):
            raise ValueError("an NPC claim disclosure must be an actor-owned typed fallible claim")
        if self.relationship_disclosure is not None and (
            self.owner_id is not None
            or self.holder_id != self.relationship_disclosure.recipient_id
            or self.subject_id != self.relationship_disclosure.discloser_id
            or self.kind != "claim"
            or self.fact is not None
            or self.assessment is not None
            or self.disclosure_policy != "never"
            or self.disclosure is not None
            or self.source_event_id is None
            or self.received_tick != self.relationship_disclosure.disclosed_tick
            or self.valid_from_tick != self.relationship_disclosure.disclosed_tick
            or self.visibility.policy != "subject_ids"
            or self.visibility.subject_ids != [self.relationship_disclosure.recipient_id]
        ):
            raise ValueError("a relationship disclosure claim must be held privately by its recipient")
        return self


class GoalCondition(ComponentModel):
    kind: Literal[
        "reach_location",
        "resource_balance_minimum",
        "relationship_dimension_minimum",
        "entity_status",
        "goal_completed",
    ]
    subject_id: UUID | None = None
    target_id: UUID | None = None
    resource_id: ResourceName | None = None
    quantity: ExactDecimal | None = None
    dimension: ResourceName | None = None
    threshold: SignedRelationValue | None = None
    status: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def condition_has_required_fields(self) -> GoalCondition:
        if self.kind == "reach_location" and (self.subject_id is None or self.target_id is None):
            raise ValueError("reach_location requires subject_id and target_id")
        if self.kind == "resource_balance_minimum" and (
            self.subject_id is None or self.resource_id is None or self.quantity is None
        ):
            raise ValueError("resource_balance_minimum requires subject_id, resource_id, and quantity")
        if self.kind == "relationship_dimension_minimum" and (
            self.subject_id is None or self.target_id is None or self.dimension is None or self.threshold is None
        ):
            raise ValueError("relationship_dimension_minimum requires subject_id, target_id, dimension, and threshold")
        if self.kind == "entity_status" and (self.subject_id is None or self.status is None):
            raise ValueError("entity_status requires subject_id and status")
        if self.kind == "goal_completed" and self.target_id is None:
            raise ValueError("goal_completed requires target_id")
        return self


class GoalRecord(ComponentModel):
    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4000)
    status: Literal["active", "paused", "succeeded", "failed", "abandoned"] = "active"
    priority: int = Field(default=50, ge=0, le=100)
    deadline: Tick | None = None
    progress: int = Field(default=0, ge=0, le=100)
    conditions: list[GoalCondition] = Field(default_factory=list, max_length=256)
    auto_complete: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=10_000)
    created_tick: Tick = 0
    updated_tick: Tick = 0

    @model_validator(mode="after")
    def completed_goal_has_evidence(self) -> GoalRecord:
        if self.auto_complete and not self.conditions:
            raise ValueError("an automatically completed goal requires explicit conditions")
        if self.status == "succeeded" and not self.evidence_refs:
            raise ValueError("a succeeded goal requires evidence references")
        return self


class ScheduledEventRecord(ComponentModel):
    id: UUID
    due_tick: Tick
    phase: Literal["environment", "delivery", "decision", "resolution", "observation", "maintenance"]
    priority: int = 0
    dependencies: list[UUID] = Field(default_factory=list, max_length=10_000)
    handler_id: ResourceName
    handler_version: str = Field(min_length=1, max_length=100)
    status: Literal["queued", "running", "completed", "cancelled", "failed"] = "queued"
    repeat_ticks: int | None = Field(default=None, gt=0, le=2**63 - 1)
    max_runs: int = Field(default=1, ge=1, le=1_000_000)
    completed_runs: int = Field(default=0, ge=0, le=1_000_000)
    payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_repeat_schedule(self) -> ScheduledEventRecord:
        if self.max_runs > 1 and self.repeat_ticks is None:
            raise ValueError("a recurring schedule requires a positive repeat_ticks interval")
        if self.completed_runs > self.max_runs:
            raise ValueError("completed_runs cannot exceed the finite max_runs cap")
        if self.status == "queued" and self.completed_runs >= self.max_runs:
            raise ValueError("a schedule at its max_runs cap cannot remain queued")
        return self


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    schema: Any
    entity_kinds: frozenset[str]
    replaceable: bool = True


_CHARACTERS = frozenset({"character", "person"})
_PLACES = frozenset({"place", "location"})
_ORGANIZATIONS = frozenset({"organization", "polity", "faction", "company", "government"})
_GROUPS = frozenset({"group", "population", "population_group", "cohort"})
_SHIPMENTS = frozenset({"shipment"})
_ALL_ENTITIES = _CHARACTERS | _PLACES | _ORGANIZATIONS | _GROUPS | _SHIPMENTS | frozenset({"item", "market"})

# Imported after the component model definitions to avoid making the domain
# shipment contract depend on a partially initialized registry.
from .shipment import ShipmentComponent

COMPONENT_REGISTRY: dict[str, ComponentSpec] = {
    "life": ComponentSpec(LifeComponent, _CHARACTERS),
    "health": ComponentSpec(HealthComponent, _CHARACTERS),
    # Combat state is reducer-owned; generic author/component replacement must
    # never create or rewrite an encounter.
    "combat_encounter": ComponentSpec(CombatEncounterComponent, _CHARACTERS, replaceable=False),
    "needs": ComponentSpec(NeedsComponent, _CHARACTERS),
    "skills": ComponentSpec(SkillsComponent, _CHARACTERS),
    "personality": ComponentSpec(PersonalityComponent, _CHARACTERS),
    "attributes": ComponentSpec(AttributesComponent, _CHARACTERS),
    "capabilities": ComponentSpec(CapabilitiesComponent, _CHARACTERS),
    "progression": ComponentSpec(ProgressionComponent, _CHARACTERS),
    "daily_routine": ComponentSpec(DailyRoutineComponent, _CHARACTERS),
    "known_places": ComponentSpec(list[UUID], _CHARACTERS, replaceable=False),
    "place": ComponentSpec(PlaceComponent, _PLACES),
    "market": ComponentSpec(MarketComponent, _PLACES | _ORGANIZATIONS | frozenset({"market"})),
    "shipment": ComponentSpec(ShipmentComponent, _SHIPMENTS, replaceable=False),
    "production": ComponentSpec(ProductionComponent, _PLACES | _ORGANIZATIONS | _CHARACTERS),
    "governance": ComponentSpec(GovernanceComponent, _ORGANIZATIONS),
    "treasury": ComponentSpec(TreasuryComponent, _ORGANIZATIONS),
    "inventory": ComponentSpec(InventoryComponent, _ALL_ENTITIES),
    "population": ComponentSpec(PopulationComponent, _GROUPS),
    "public_description": ComponentSpec(Annotated[str, Field(max_length=4000)], _ALL_ENTITIES, replaceable=False),
    "appearance": ComponentSpec(Annotated[str, Field(max_length=4000)], _CHARACTERS, replaceable=False),
    "public_role": ComponentSpec(Annotated[str, Field(max_length=1000)], _CHARACTERS | _ORGANIZATIONS, replaceable=False),
    "hidden": ComponentSpec(StrictBool, _ALL_ENTITIES, replaceable=False),
    "parent_id": ComponentSpec(UUID, _ALL_ENTITIES, replaceable=False),
    "historical_evidence": ComponentSpec(HistoricalEvidenceArtifact, frozenset({"item"}), replaceable=False),
    "investigation_case": ComponentSpec(InvestigationCaseDefinition, frozenset({"item"}), replaceable=False),
}

_COMPONENT_ADAPTERS = {name: TypeAdapter(spec.schema) for name, spec in COMPONENT_REGISTRY.items()}


REPLACEABLE_COMPONENT_NAMES = frozenset(
    name for name, spec in COMPONENT_REGISTRY.items() if spec.replaceable
)
AUTHOR_COMPONENT_NAME = Literal[
    "life",
    "health",
    "needs",
    "skills",
    "personality",
    "attributes",
    "capabilities",
    "progression",
    "daily_routine",
    "known_places",
    "place",
    "market",
    "production",
    "governance",
    "treasury",
    "inventory",
    "population",
    "public_description",
    "appearance",
    "public_role",
    "hidden",
    "parent_id",
    "historical_evidence",
    "investigation_case",
]
COMPONENT_NAME = Literal[
    "life",
    "health",
    "needs",
    "skills",
    "personality",
    "attributes",
    "capabilities",
    "progression",
    "place",
    "market",
    "production",
    "governance",
    "treasury",
    "inventory",
    "population",
]


def validate_component(entity_kind: str, component_name: str, value: object) -> object:
    """Validate a component against its registered schema and owner entity kind."""
    parsed = parse_component(entity_kind, component_name, value)
    if isinstance(parsed, BaseModel):
        return parsed.model_dump(mode="json")
    return _COMPONENT_ADAPTERS[component_name].dump_python(parsed, mode="json")


def parse_component(entity_kind: str, component_name: str, value: object) -> object:
    """Return a validated component model/value for cross-reference checks."""
    spec = COMPONENT_REGISTRY.get(component_name)
    if spec is None:
        raise ValueError(f"unregistered component {component_name!r}")
    if entity_kind not in spec.entity_kinds:
        raise ValueError(f"component {component_name!r} is not valid for entity kind {entity_kind!r}")
    return _COMPONENT_ADAPTERS[component_name].validate_python(value)


def validate_components(entity_kind: str, components: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Reject unknown component names and canonicalize registered values."""
    validated: dict[str, JsonValue] = {}
    for name, value in components.items():
        validated[name] = validate_component(entity_kind, name, value)  # type: ignore[assignment]
    return validated
