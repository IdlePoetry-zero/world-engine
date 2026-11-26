"""Typed gameplay event payloads layered onto the core event envelope."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field, StrictInt, field_validator, model_validator

from .components import (
    AUTHOR_COMPONENT_NAME,
    COMPONENT_NAME,
    CapabilitiesComponent,
    ExactDecimal,
    GoalRecord,
    HealthComponent,
    KnowledgeRecord,
    PersonalityComponent,
    PersonalityLockViolation,
    ProvenanceReference,
    RelationRecord,
    SignedRelationValue,
    ResourceName,
    parse_component,
)
from .models import ContractModel, JsonObject, ResourceId, WorldEntity
from .json_types import JsonValue
from .investigation_cases import (
    AccusationRepairOffer,
    InvestigationCaseFile,
    PublicAccusationCorrection,
    RepairOperation,
)
from .adjudication import (
    IMPROVISED_EXAMINE_DURATION,
    IMPROVISED_EXAMINE_POLICY,
    IMPROVISED_EXAMINE_ROLL_LABEL,
    improvised_examine_chance,
    improvised_roll_digest,
)
from .combat import (
    COMBAT_ATTACK_POLICY,
    COMBAT_ATTACK_ROLL_LABEL,
    COMBAT_EXCHANGE_DURATION,
    combat_roll_digest,
    combat_roll_value,
)
from .treatment import (
    INJURY_TREATMENT_DURATION,
    INJURY_TREATMENT_HEALING,
    INJURY_TREATMENT_MIN_CARE,
    INJURY_TREATMENT_POLICY,
)
from .organization_recruitment import MembershipRecruitmentPlan
from .shipment import ShipmentAccountFlow, ShipmentLotManifest, ShipmentLotReceipt, ShipmentSpoiledLot


class ComponentReplacedEvent(ContractModel):
    """Replace one registered component after a revision check; never a JSON patch."""

    type: Literal["core.component_replaced.v1"] = "core.component_replaced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    component_name: COMPONENT_NAME
    expected_revision: int | None = Field(default=None, ge=1)
    replacement: JsonObject
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @field_validator("replacement")
    @classmethod
    def replacement_is_object(cls, value: JsonObject) -> JsonObject:
        if not value:
            raise ValueError("component replacement cannot be empty")
        return value


class AuthorComponentRevisedEvent(ContractModel):
    """Author an approved public definition field after a compare-and-swap check."""

    type: Literal["core.author_component_revised.v1"] = "core.author_component_revised.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    author_id: UUID
    component_name: AUTHOR_COMPONENT_NAME
    expected_previous_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    replacement: JsonValue
    revision_note: str = Field(min_length=1, max_length=1000)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

class KnowledgeAcquiredEvent(ContractModel):
    type: Literal["core.knowledge_acquired.v1"] = "core.knowledge_acquired.v1"
    event_id: UUID = Field(default_factory=uuid4)
    record: KnowledgeRecord
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def has_evidence_source(self) -> KnowledgeAcquiredEvent:
        if self.record.relationship_disclosure is not None:
            raise ValueError("relationship disclosure claims require their dedicated typed event")
        if not self.record.source_refs and self.record.source_event_id is None and not self.record.provenance:
            raise ValueError("acquired knowledge must cite at least one source event")
        return self


class MovementCompletedEvent(ContractModel):
    type: Literal["core.movement_completed.v1"] = "core.movement_completed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    started_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    arrived_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class EntityDiscoveredEvent(ContractModel):
    type: Literal["core.entity_discovered.v1"] = "core.entity_discovered.v1"
    event_id: UUID = Field(default_factory=uuid4)
    discoverer_id: UUID
    entity_id: UUID
    place_id: UUID
    difficulty: int = Field(default=0, ge=0, le=100)
    roll: int = Field(ge=1, le=100)
    chance_percent: int = Field(ge=1, le=100)
    reveal_scope: Literal["world", "discoverer"] = "world"
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class ImprovisedCheckResolvedEvent(ContractModel):
    """Auditable result for a bounded, rules-owned examination check."""

    type: Literal["core.improvised_check_resolved.v1"] = "core.improvised_check_resolved.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    target_id: UUID
    location_id: UUID
    skill_id: ResourceName
    expected_skills_revision: int = Field(ge=1)
    skill_level: ExactDecimal = Field(ge=Decimal("0"), le=Decimal("100"))
    policy_version: Literal["improvised.examine.v1"] = IMPROVISED_EXAMINE_POLICY
    chance_percent: int = Field(ge=1, le=95)
    roll: int = Field(ge=1, le=100)
    roll_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    passed: bool
    source_description_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    historical_evidence_id: UUID | None = None
    historical_evidence_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    started_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    resolved_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    observation_record_id: UUID | None = None
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def result_is_replayable_and_private(self) -> ImprovisedCheckResolvedEvent:
        if self.started_tick > 2**63 - 1 - IMPROVISED_EXAMINE_DURATION:
            raise ValueError("improvised check exceeds the signed world-clock range")
        if self.resolved_tick != self.started_tick + IMPROVISED_EXAMINE_DURATION:
            raise ValueError("improvised examination must use its fixed 60-second duration")
        if self.chance_percent != improvised_examine_chance(self.skill_level):
            raise ValueError("improvised check chance does not match the registered policy")
        expected_digest = improvised_roll_digest(
            world_id=self.world_id,
            branch_id=self.branch_id,
            command_id=self.command_id,
            ruleset_version=self.ruleset_version,
            label=IMPROVISED_EXAMINE_ROLL_LABEL,
        )
        if self.roll_digest != expected_digest:
            raise ValueError("improvised check roll digest does not match its command seed")
        if self.roll != int(expected_digest, 16) % 100 + 1:
            raise ValueError("improvised check roll does not match its deterministic digest")
        if self.passed != (self.roll <= self.chance_percent):
            raise ValueError("improvised check result does not match its roll and chance")
        if (self.observation_record_id is not None) != self.passed:
            raise ValueError("only a successful improvised examination may record an observation")
        if (self.historical_evidence_id is None) != (self.historical_evidence_hash is None):
            raise ValueError("historical evidence ID and hash must be present together")
        if self.historical_evidence_id is not None and not self.passed:
            raise ValueError("a failed examination cannot select historical evidence")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("improvised check audit data must remain author-only")
        return self


class DamageAppliedEvent(ContractModel):
    type: Literal["core.damage_applied.v1"] = "core.damage_applied.v1"
    event_id: UUID = Field(default_factory=uuid4)
    target_id: UUID
    source_id: UUID | None = None
    damage: int = Field(gt=0, le=1_000_000)
    injury_type: ResourceId = "core.injury.blunt"
    description: str = Field(default="", max_length=1000)
    expected_health_revision: int = Field(ge=1)
    expected_population_revision: int | None = Field(default=None, ge=1)
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class CombatExchangeResolvedEvent(ContractModel):
    """Persist the exact command-seeded result and the pair's encounter state."""

    type: Literal["core.combat_exchange_resolved.v1"] = "core.combat_exchange_resolved.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    policy_version: Literal["combat.attack.v1"] = COMBAT_ATTACK_POLICY
    encounter_id: UUID
    exchange_number: int = Field(ge=1)
    actor_id: UUID
    target_id: UUID
    location_id: UUID
    expected_actor_encounter_revision: int | None = Field(default=None, ge=1)
    expected_target_encounter_revision: int | None = Field(default=None, ge=1)
    started_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    resolved_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    chance_percent: int = Field(ge=5, le=95)
    roll: int = Field(ge=1, le=100)
    roll_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome: Literal["hit", "miss"]
    weapon_resource_id: ResourceName | None = None
    encounter_status: Literal["active", "resolved"] = "active"
    damage_event_id: UUID | None = None
    damage: int = Field(default=0, ge=0, le=1_000_000)
    expected_target_health_revision: int | None = Field(default=None, ge=1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def combat_result_is_replayable(self) -> CombatExchangeResolvedEvent:
        if self.actor_id == self.target_id:
            raise ValueError("a combat exchange needs two distinct participants")
        if self.started_tick > 2**63 - 1 - COMBAT_EXCHANGE_DURATION:
            raise ValueError("combat exchange exceeds the signed world-clock range")
        if self.resolved_tick != self.started_tick + COMBAT_EXCHANGE_DURATION:
            raise ValueError("combat exchange must use its registered ten-second duration")
        expected_digest = combat_roll_digest(
            world_id=self.world_id,
            branch_id=self.branch_id,
            command_id=self.command_id,
            ruleset_version=self.ruleset_version,
            label=COMBAT_ATTACK_ROLL_LABEL,
        )
        if self.roll_digest != expected_digest or self.roll != combat_roll_value(expected_digest, 100):
            raise ValueError("combat roll does not match the deterministic command seed")
        if (self.outcome == "hit") != (self.roll <= self.chance_percent):
            raise ValueError("combat outcome does not match the recorded chance and roll")
        if self.outcome == "hit":
            if (
                self.damage <= 0
                or self.damage_event_id != uuid5(self.command_id, f"{self.ruleset_version}:combat.damage")
                or self.expected_target_health_revision is None
            ):
                raise ValueError("a hit must name its registered damage event and health revision")
        elif (
            self.damage != 0
            or self.damage_event_id is not None
            or self.expected_target_health_revision is not None
            or self.encounter_status != "active"
        ):
            raise ValueError("a miss cannot carry damage or resolve the encounter")
        expected_visibility = {
            "policy": "subject_ids",
            "subject_ids": sorted((str(self.actor_id), str(self.target_id))),
        }
        if self.visibility != expected_visibility:
            raise ValueError("combat exchange details must be visible only to its participants")
        return self


class CombatRetreatResolvedEvent(ContractModel):
    """Close one active encounter as the actor retreats along a registered route."""

    type: Literal["core.combat_retreat_resolved.v1"] = "core.combat_retreat_resolved.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    policy_version: Literal["combat.retreat.v1"] = "combat.retreat.v1"
    encounter_id: UUID
    actor_id: UUID
    opponent_id: UUID
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    expected_actor_encounter_revision: int = Field(ge=1)
    expected_opponent_encounter_revision: int = Field(ge=1)
    started_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    arrived_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    outcome: Literal["escaped"] = "escaped"
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def retreat_is_a_valid_world_clock_interval(self) -> CombatRetreatResolvedEvent:
        if self.actor_id == self.opponent_id:
            raise ValueError("a character cannot retreat from themself")
        if self.from_place_id == self.to_place_id or self.arrived_tick <= self.started_tick:
            raise ValueError("retreat requires a distinct destination and positive travel time")
        expected_visibility = {
            "policy": "subject_ids",
            "subject_ids": sorted((str(self.actor_id), str(self.opponent_id))),
        }
        if self.visibility != expected_visibility:
            raise ValueError("retreat details must be visible only to its participants")
        return self


class HealthRestoredEvent(ContractModel):
    type: Literal["core.health_restored.v1"] = "core.health_restored.v1"
    event_id: UUID = Field(default_factory=uuid4)
    target_id: UUID
    amount: int = Field(gt=0, le=1_000_000)
    method: ResourceId = "core.rest"
    expected_health_revision: int = Field(ge=1)
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class InjuryTreatedEvent(ContractModel):
    """Record treatment of one existing injury with a finite, consumed dose."""

    type: Literal["core.injury_treated.v1"] = "core.injury_treated.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    policy_version: Literal["injury.treatment.v1"] = INJURY_TREATMENT_POLICY
    healer_id: UUID
    target_id: UUID
    injury_id: UUID
    location_id: UUID
    expected_health_revision: int = Field(ge=1)
    expected_skills_revision: int = Field(ge=1)
    skill_level: ExactDecimal = Field(ge=INJURY_TREATMENT_MIN_CARE, le=Decimal("100"))
    medicine_resource_id: ResourceName
    consumed_event_id: UUID
    started_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    resolved_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    healing_amount: int = Field(ge=0, le=INJURY_TREATMENT_HEALING)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def treatment_is_command_bound_and_auditable(self) -> InjuryTreatedEvent:
        if self.started_tick > 2**63 - 1 - INJURY_TREATMENT_DURATION:
            raise ValueError("injury treatment exceeds the signed world-clock range")
        if self.resolved_tick != self.started_tick + INJURY_TREATMENT_DURATION:
            raise ValueError("injury treatment must use its registered thirty-minute duration")
        if self.event_id != uuid5(self.command_id, f"{self.ruleset_version}:injury_treatment.applied"):
            raise ValueError("injury treatment event id must be bound to its command")
        if self.consumed_event_id != uuid5(self.command_id, f"{self.ruleset_version}:injury_treatment.supply"):
            raise ValueError("injury treatment supply reference must be bound to its command")
        if len(self.provenance) != 1 or (
            self.provenance[0].kind != "rule"
            or self.provenance[0].reference_id != INJURY_TREATMENT_POLICY
        ):
            raise ValueError("injury treatment must cite its registered rule provenance")
        expected_visibility = {"policy": "subject_ids"}
        allowed_subject_ids = {
            tuple(sorted({str(self.healer_id), str(self.target_id)})),
            # Earlier R10 runtime builds serialized self-treatment as a
            # two-entry duplicate list. Accept it for replay only.
            tuple(sorted((str(self.healer_id), str(self.target_id)))),
        }
        subject_ids = self.visibility.get("subject_ids")
        if (
            {key: value for key, value in self.visibility.items() if key != "subject_ids"} != expected_visibility
            or not isinstance(subject_ids, list)
            or tuple(subject_ids) not in allowed_subject_ids
        ):
            raise ValueError("injury treatment details must be visible only to its participants")
        return self


class LifeEndedEvent(ContractModel):
    """Resolve a character's fixed lifespan at its exact world-time boundary."""

    type: Literal["core.life_ended.v1"] = "core.life_ended.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    expected_life_revision: int = Field(ge=1)
    expected_population_revision: int | None = Field(default=None, ge=1)
    cause: Literal["lifespan"] = "lifespan"
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class PlayerCharacterContinuedEvent(ContractModel):
    """Change only the branch's controlled-character pointer after a death."""

    type: Literal["core.player_character_continued.v1"] = "core.player_character_continued.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    previous_player_id: UUID
    new_player_id: UUID
    source_death_event_id: UUID
    current_tick: StrictInt = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def continuation_is_command_bound(self) -> PlayerCharacterContinuedEvent:
        expected_id = uuid5(self.command_id, f"{self.ruleset_version}:player_character_continued")
        if self.event_id != expected_id:
            raise ValueError("player continuation event ID must be bound to its command")
        if (
            self.actor_id != self.previous_player_id
            or self.previous_player_id == self.new_player_id
            or self.source_death_event_id == self.event_id
        ):
            raise ValueError("player continuation actor, target, or source is inconsistent")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("player continuation details must remain private")
        return self


class CohortMemberPromotedEvent(ContractModel):
    """Replace one anonymous cohort member with an individually modeled person."""

    type: Literal["core.cohort_member_promoted.v1"] = "core.cohort_member_promoted.v1"
    event_id: UUID = Field(default_factory=uuid4)
    cohort_id: UUID
    expected_population_revision: int = Field(ge=1)
    age_years: int | None = Field(default=None, ge=0, le=200)
    character: WorldEntity
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def character_is_newborn_cohort_member(self) -> CohortMemberPromotedEvent:
        if self.character.kind not in {"character", "person"}:
            raise ValueError("a promoted cohort member must be a character")
        if self.character.status != "active":
            raise ValueError("a promoted cohort member must begin active")
        return self


class PopulationRevisionExpectation(ContractModel):
    cohort_id: UUID
    expected_revision: int = Field(ge=1)


class PopulationYearAdvancedEvent(ContractModel):
    """Advance every registered age-structured population at one year boundary."""

    type: Literal["core.population_year_advanced.v1"] = "core.population_year_advanced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    year_index: int = Field(ge=1, le=2**63 - 1)
    scheduled_event_id: UUID
    expected_populations: list[PopulationRevisionExpectation] = Field(min_length=1, max_length=100_000)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def has_unique_population_revisions(self) -> PopulationYearAdvancedEvent:
        cohort_ids = [item.cohort_id for item in self.expected_populations]
        if len(cohort_ids) != len(set(cohort_ids)):
            raise ValueError("population-year event may include each cohort exactly once")
        if cohort_ids != sorted(cohort_ids, key=str):
            raise ValueError("population-year cohort revisions must use stable UUID order")
        return self


class OrganizationMembershipReviewedEvent(ContractModel):
    """Audit one deterministic annual resident-enrollment review after census."""

    type: Literal["core.organization_membership_reviewed.v1"] = "core.organization_membership_reviewed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    year_index: int = Field(ge=1, le=2**63 - 1)
    scheduled_event_id: UUID
    census_event_id: UUID
    review: MembershipRecruitmentPlan
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "public"})

    @model_validator(mode="after")
    def annual_review_is_complete_and_registered(self) -> OrganizationMembershipReviewedEvent:
        if self.review.year_index != self.year_index:
            raise ValueError("membership review plan must match its event year")
        if not self.review.expected_organizations:
            raise ValueError("membership review event requires at least one opted-in organization")
        organization_ids = [item.organization_id for item in self.review.expected_organizations]
        if organization_ids != sorted(set(organization_ids), key=str):
            raise ValueError("membership review organizations must use stable UUID order")
        report_ids = [item.organization_id for item in self.review.organizations]
        if report_ids != organization_ids:
            raise ValueError("membership review must report every opted-in organization exactly once")
        enrollment_keys = [
            (item.organization_id, item.member_id)
            for item in self.review.enrollments
        ]
        if enrollment_keys != sorted(set(enrollment_keys), key=lambda item: (str(item[0]), str(item[1]))):
            raise ValueError("membership enrollments must be unique and use stable organization/member order")
        member_ids = [item.member_id for item in self.review.enrollments]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("one resident cannot be enrolled into multiple organizations in one annual review")
        if self.visibility.get("policy") != "public":
            raise ValueError("organization membership review is a public governance event")
        if not any(
            item.kind == "rule" and item.reference_id == "organization.membership_recruitment.v1"
            for item in self.provenance
        ):
            raise ValueError("membership review must cite its registered rule provenance")
        return self


class BackgroundConsumptionLot(ContractModel):
    lot_id: UUID
    account_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class BackgroundAccountExpectation(ContractModel):
    account_id: UUID
    expected_balance: ExactDecimal


class BackgroundNeedRevisionExpectation(ContractModel):
    entity_id: UUID
    expected_revision: int = Field(ge=1)
    action: Literal["continue", "eat", "drink", "rest"] = "continue"
    resource_id: ResourceName | None = None
    consumed_quantity: ExactDecimal | None = Field(default=None, gt=Decimal("0"))
    consumption_account_id: UUID | None = None
    expected_inventory_revision: int | None = Field(default=None, ge=1)
    consumption_sink_account_id: UUID | None = None
    consumption_lots: list[BackgroundConsumptionLot] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def action_fields_match(self) -> BackgroundNeedRevisionExpectation:
        consumes = self.action in {"eat", "drink"}
        if consumes and (
            self.resource_id is None
            or self.consumed_quantity is None
            or self.expected_inventory_revision is None
            or self.consumption_sink_account_id is None
            or not self.consumption_lots
        ):
            raise ValueError("background consumption requires a resource, quantity, sink, inventory revision and FIFO lots")
        if not consumes and (
            self.resource_id is not None
            or self.consumed_quantity is not None
            or self.consumption_account_id is not None
            or self.expected_inventory_revision is not None
            or self.consumption_sink_account_id is not None
            or self.consumption_lots
        ):
            raise ValueError("only background eating or drinking may include resource-consumption fields")
        lot_ids = [lot.lot_id for lot in self.consumption_lots]
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("background FIFO lot list cannot repeat a lot")
        if consumes:
            with localcontext() as context:
                context.prec = 80
                allocated = sum((lot.quantity for lot in self.consumption_lots), Decimal("0"))
            if allocated != self.consumed_quantity:
                raise ValueError("background FIFO lots must sum exactly to the consumed quantity")
        return self


class BackgroundNeedsAdvancedEvent(ContractModel):
    """Advance passive needs for all eligible characters, optionally including the focal actor."""

    type: Literal["core.background_needs_advanced.v1"] = "core.background_needs_advanced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    include_actor: bool = False
    from_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    to_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    expected_agents: list[BackgroundNeedRevisionExpectation] = Field(min_length=1, max_length=100_000)
    expected_accounts: list[BackgroundAccountExpectation] = Field(default_factory=list, max_length=100_000)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def has_valid_interval_and_stable_manifest(self) -> BackgroundNeedsAdvancedEvent:
        if self.to_tick <= self.from_tick:
            raise ValueError("background needs event requires a positive world-time interval")
        entity_ids = [item.entity_id for item in self.expected_agents]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("background needs manifest cannot repeat an entity")
        if entity_ids != sorted(entity_ids, key=str):
            raise ValueError("background needs manifest must use stable UUID order")
        if self.actor_id in entity_ids and not self.include_actor:
            raise ValueError("the acting character cannot also be advanced as a background agent")
        account_ids = [item.account_id for item in self.expected_accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("background account balance manifest cannot repeat an account")
        if account_ids != sorted(account_ids, key=str):
            raise ValueError("background account balance manifest must use stable UUID order")
        return self


class BackgroundRoutineNeedExpectation(ContractModel):
    """Aggregate one deterministic threshold-care resource over a window."""

    resource_id: ResourceName
    threshold: int = Field(ge=0, le=100)
    action_count: int = Field(ge=0, le=100_000)
    sink_account_id: UUID | None = None
    consumption_lots: list[BackgroundConsumptionLot] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def lot_quantities_match_whole_actions(self) -> BackgroundRoutineNeedExpectation:
        lot_ids = [item.lot_id for item in self.consumption_lots]
        if len(lot_ids) != len(set(lot_ids)):
            raise ValueError("routine care lots must be unique and FIFO aggregated")
        if self.action_count and self.sink_account_id is None:
            raise ValueError("routine care actions require a resource sink account")
        with localcontext() as context:
            context.prec = 80
            consumed = sum((item.quantity for item in self.consumption_lots), Decimal("0"))
        if consumed != Decimal(self.action_count):
            raise ValueError("routine care FIFO lots must cover exactly one unit per recorded action")
        return self


class BackgroundRoutineAgentExpectation(ContractModel):
    entity_id: UUID
    expected_needs_revision: int = Field(ge=1)
    expected_inventory_revision: int | None = Field(default=None, ge=1)
    expected_routine_revision: int | None = Field(default=None, ge=1)
    expected_location_id: UUID | None = None
    food: BackgroundRoutineNeedExpectation | None = None
    water: BackgroundRoutineNeedExpectation | None = None


class BackgroundRoutineAdvancedEvent(ContractModel):
    """Replayable all-agent daily-routine and threshold-care batch."""

    type: Literal["core.background_routine_advanced.v1"] = "core.background_routine_advanced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    include_actor: bool = False
    from_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    to_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    expected_agents: list[BackgroundRoutineAgentExpectation] = Field(min_length=1, max_length=100_000)
    expected_accounts: list[BackgroundAccountExpectation] = Field(default_factory=list, max_length=100_000)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def validate_interval_and_stable_manifest(self) -> BackgroundRoutineAdvancedEvent:
        if self.to_tick <= self.from_tick:
            raise ValueError("background routine event requires a positive world-time interval")
        agent_ids = [item.entity_id for item in self.expected_agents]
        if len(agent_ids) != len(set(agent_ids)) or agent_ids != sorted(agent_ids, key=str):
            raise ValueError("background routine agents must be unique and use stable UUID order")
        if self.actor_id in agent_ids and not self.include_actor:
            raise ValueError("the acting character cannot be advanced as a background routine agent")
        account_ids = [item.account_id for item in self.expected_accounts]
        if len(account_ids) != len(set(account_ids)) or account_ids != sorted(account_ids, key=str):
            raise ValueError("background routine accounts must be unique and use stable UUID order")
        return self


class BackgroundDeprivationSubject(ContractModel):
    entity_id: UUID
    boundary_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    expected_needs_revision: int = Field(ge=1)
    expected_health_revision: int | None = Field(default=None, ge=1)
    expected_population_revision: int | None = Field(default=None, ge=1)
    expected_damage: int = Field(ge=0, le=1_000_000)
    terminal_status: Literal["active", "dead"]


class BackgroundDeprivationResolvedEvent(ContractModel):
    """Resolve due deprivation consequences, optionally including the focal actor."""

    type: Literal["core.background_deprivation_resolved.v1"] = "core.background_deprivation_resolved.v1"
    event_id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    include_actor: bool = False
    resolved_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    subjects: list[BackgroundDeprivationSubject] = Field(min_length=1, max_length=100_000)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def has_unique_stable_subjects(self) -> BackgroundDeprivationResolvedEvent:
        subject_ids = [item.entity_id for item in self.subjects]
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError("deprivation batch cannot repeat a subject")
        if subject_ids != sorted(subject_ids, key=str):
            raise ValueError("deprivation batch subjects must use stable UUID order")
        if self.actor_id in subject_ids and not self.include_actor:
            raise ValueError("deprivation resolver cannot include the acting character")
        return self


class GovernanceSuccessionEvent(ContractModel):
    """Record a policy-bound choice to fill or vacate a deceased holder's role."""

    type: Literal["core.governance_succession.v1"] = "core.governance_succession.v1"
    event_id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    slot: Literal["leader", "office"]
    office_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    predecessor_id: UUID
    successor_id: UUID | None = None
    expected_governance_revision: int = Field(ge=1)
    decision_basis: Literal["election", "appointment", "inheritance", "council", "none"]
    decision_request_id: UUID
    decision_option_id: str = Field(min_length=1, max_length=120)
    source_death_event_id: UUID
    evidence_refs: list[UUID] = Field(min_length=1, max_length=10_000)
    rationale: str = Field(default="", max_length=2000)
    linked_leadership_office_ids: list[ResourceName] | None = Field(
        default=None, max_length=4096, exclude_if=lambda value: value is None,
    )
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def slot_and_choice_are_consistent(self) -> GovernanceSuccessionEvent:
        if (self.slot == "leader") != (self.office_id is None):
            raise ValueError("leader succession has no office_id; office succession requires one")
        if self.source_death_event_id not in self.evidence_refs:
            raise ValueError("succession evidence must include the predecessor's death event")
        if self.successor_id is None and self.decision_option_id != "leave_vacant":
            raise ValueError("a vacant slot must use the leave_vacant decision option")
        linked = self.linked_leadership_office_ids
        if linked is not None and (self.slot != "leader" or linked != sorted(set(linked))):
            raise ValueError("linked leadership offices must be unique, sorted, and recorded on a leader succession")
        return self


class OrganizationMemberResignedEvent(ContractModel):
    """Record an actor's voluntary exit and clear the offices they held."""

    type: Literal["core.organization_member_resigned.v1"] = "core.organization_member_resigned.v1"
    event_id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    actor_id: UUID
    expected_governance_revision: int = Field(ge=1)
    cleared_office_ids: list[ResourceName] = Field(default_factory=list, max_length=4096)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "public"})

    @model_validator(mode="after")
    def resignation_manifest_is_exact_and_auditable(self) -> OrganizationMemberResignedEvent:
        if self.cleared_office_ids != sorted(set(self.cleared_office_ids)):
            raise ValueError("resigned office ids must be unique and use stable order")
        if self.visibility.get("policy") != "public":
            raise ValueError("membership resignation is a public governance event")
        if not any(
            item.kind == "rule" and item.reference_id == "organization.membership_resignation.v1"
            for item in self.provenance
        ):
            raise ValueError("membership resignation must cite its registered rule provenance")
        return self


class GovernanceLeadershipHandedOverEvent(ContractModel):
    """Record an incumbent's appointment-based transfer to an active member."""

    type: Literal["core.governance_leadership_handed_over.v1"] = "core.governance_leadership_handed_over.v1"
    event_id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    predecessor_id: UUID
    successor_id: UUID
    expected_governance_revision: int = Field(ge=1)
    transferred_office_ids: list[ResourceName] = Field(default_factory=list, max_length=4096)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "public"})

    @model_validator(mode="after")
    def handover_manifest_is_exact_and_auditable(self) -> GovernanceLeadershipHandedOverEvent:
        if self.predecessor_id == self.successor_id:
            raise ValueError("leadership handover requires distinct predecessor and successor")
        if self.transferred_office_ids != sorted(set(self.transferred_office_ids)):
            raise ValueError("transferred office ids must be unique and use stable order")
        if self.visibility.get("policy") != "public":
            raise ValueError("leadership handover is a public governance event")
        if not any(
            item.kind == "rule" and item.reference_id == "organization.leadership_handover.v1"
            for item in self.provenance
        ):
            raise ValueError("leadership handover must cite its registered rule provenance")
        return self


class ProductionQuotaRenewedEvent(ContractModel):
    """Grant one scheduled finite quota slice from a registered policy."""

    type: Literal["core.production_quota_renewed.v1"] = "core.production_quota_renewed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    producer_id: UUID
    quota_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    expected_production_revision: int = Field(ge=1)
    period_index: int = Field(ge=1, le=2**63 - 1)
    scheduled_event_id: UUID
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class RenewableHarvestRecipientExpectation(ContractModel):
    entity_id: UUID
    account_id: UUID
    expected_balance: ExactDecimal
    expected_inventory_revision: int = Field(ge=1)
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class RenewableHarvestExpectation(ContractModel):
    region_id: UUID
    policy_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    resource_id: ResourceName
    source_account_id: UUID
    expected_place_revision: int = Field(ge=1)
    expected_policy_revision: int = Field(ge=1)
    source_expected_balance: ExactDecimal
    source_expected_credit_limit: ExactDecimal
    recipients: list[RenewableHarvestRecipientExpectation] = Field(max_length=100_000)

    @model_validator(mode="after")
    def stable_unique_recipients(self) -> RenewableHarvestExpectation:
        entity_ids = [item.entity_id for item in self.recipients]
        account_ids = [item.account_id for item in self.recipients]
        if entity_ids != sorted(entity_ids, key=str) or len(entity_ids) != len(set(entity_ids)):
            raise ValueError("renewable recipient expectations must use unique, stable entity UUID order")
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("renewable recipient expectations cannot repeat a holder account")
        return self


class PopulationSeatAccount(ContractModel):
    resource_id: ResourceName
    account_id: UUID


class PopulationSeatPromotion(ContractModel):
    """Replace one dead named seat with a member drawn from an exact age bucket."""

    cohort_id: UUID
    expected_population_revision: int = Field(ge=1)
    expected_policy_revision: int = Field(ge=1)
    vacant_member_id: UUID
    expected_vacant_life_revision: int = Field(ge=1)
    age_years: int = Field(ge=0, le=200)
    expected_age_cohort_revision: int = Field(ge=1)
    promotion_serial: int = Field(ge=0, le=2**63 - 1)
    character: WorldEntity
    holder_accounts: list[PopulationSeatAccount] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def stable_holder_accounts(self) -> PopulationSeatPromotion:
        if self.character.kind not in {"character", "person"} or self.character.status != "active":
            raise ValueError("a named seat promotion must create one active character")
        resources = [item.resource_id for item in self.holder_accounts]
        accounts = [item.account_id for item in self.holder_accounts]
        if resources != sorted(resources) or len(resources) != len(set(resources)):
            raise ValueError("population seat holder resources must be unique and sorted")
        if len(accounts) != len(set(accounts)):
            raise ValueError("population seat holder accounts must be unique")
        return self


class RenewableResourceHarvestedEvent(ContractModel):
    """Distribute one bounded period yield to named recipients' owned stock."""

    type: Literal["core.renewable_resource_harvested.v1"] = "core.renewable_resource_harvested.v1"
    event_id: UUID = Field(default_factory=uuid4)
    period_index: int = Field(ge=1, le=2**63 - 1)
    scheduled_event_id: UUID
    items: list[RenewableHarvestExpectation] = Field(min_length=1, max_length=100_000)
    promotions: list[PopulationSeatPromotion] = Field(default_factory=list, max_length=100_000)
    provenance: list[ProvenanceReference] = Field(min_length=1, max_length=1000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def unique_stable_items(self) -> RenewableResourceHarvestedEvent:
        keys = [(item.region_id, item.policy_id) for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("renewable harvest event cannot repeat a region policy")
        if keys != sorted(keys, key=lambda item: (str(item[0]), item[1])):
            raise ValueError("renewable harvest event items must use stable region/policy order")
        promotion_keys = [(item.cohort_id, item.vacant_member_id) for item in self.promotions]
        if len(promotion_keys) != len(set(promotion_keys)):
            raise ValueError("renewable harvest cannot refill one named seat more than once")
        if promotion_keys != sorted(promotion_keys, key=lambda item: (str(item[0]), str(item[1]))):
            raise ValueError("renewable seat promotions must use stable cohort and member UUID order")
        return self


class ScheduledEventCompletedEvent(ContractModel):
    type: Literal["core.scheduled_event_completed.v1"] = "core.scheduled_event_completed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    scheduled_event_id: UUID
    expected_revision: int = Field(ge=1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class ResourceAccountOpenedEvent(ContractModel):
    type: Literal["core.resource_account_opened.v1"] = "core.resource_account_opened.v1"
    event_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    owner_id: UUID
    resource_id: ResourceId
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class MarketTradeExecutedEvent(ContractModel):
    type: Literal["core.market_trade_executed.v1"] = "core.market_trade_executed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    buyer_id: UUID
    seller_id: UUID
    market_id: UUID
    listing_id: UUID
    resource_id: ResourceId
    currency_resource_id: ResourceId
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    unit_price_minor: int = Field(ge=0, le=10**30 - 1)
    stock_account_id: UUID
    buyer_resource_account_id: UUID
    buyer_currency_account_id: UUID
    seller_currency_account_id: UUID
    source_lot_ids: list[UUID] = Field(min_length=1, max_length=1000)
    buyer_lot_id: UUID
    expected_seller_inventory_revision: int | None = Field(default=None, ge=1)
    expected_buyer_inventory_revision: int | None = Field(default=None, ge=1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class RelationEstablishedEvent(ContractModel):
    type: Literal["core.relation_established.v1"] = "core.relation_established.v1"
    event_id: UUID = Field(default_factory=uuid4)
    relation: RelationRecord
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class RelationDimensionChangedEvent(ContractModel):
    type: Literal["core.relation_dimension_changed.v1"] = "core.relation_dimension_changed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    relation_id: UUID
    dimension: ResourceId
    expected_previous: SignedRelationValue | None = None
    value: SignedRelationValue
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})
    social_policy_id: Literal["social_world_v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    speech_memory_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    routine_occurrence: StrictInt | None = Field(
        default=None, ge=1, le=2**63 - 1, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def social_speech_receipt_is_coherent(self) -> RelationDimensionChangedEvent:
        fields = (self.social_policy_id, self.speech_memory_id, self.routine_occurrence)
        if all(value is None for value in fields):
            return self
        if any(value is None for value in fields):
            raise ValueError("social speech relationship receipt fields must be provided together")
        if self.dimension != "familiarity":
            raise ValueError("social speech receipts can only change familiarity")
        if self.speech_memory_id is None or self.speech_memory_id.int == 0:
            raise ValueError("social speech receipt requires a non-empty memory UUID")
        if self.evidence_refs != [self.speech_memory_id]:
            raise ValueError("social speech evidence must name exactly its source memory")
        return self


class GoalCreatedEvent(ContractModel):
    type: Literal["core.goal_created.v1"] = "core.goal_created.v1"
    event_id: UUID = Field(default_factory=uuid4)
    goal: GoalRecord
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class GoalUpdatedEvent(ContractModel):
    type: Literal["core.goal_updated.v1"] = "core.goal_updated.v1"
    event_id: UUID = Field(default_factory=uuid4)
    goal_id: UUID
    expected_revision: int = Field(ge=1)
    progress: int | None = Field(default=None, ge=0, le=100)
    status: Literal["active", "paused", "succeeded", "failed", "abandoned"] | None = None
    evidence_refs: list[UUID] | None = Field(default=None, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def has_update(self) -> GoalUpdatedEvent:
        if self.progress is None and self.status is None and self.evidence_refs is None:
            raise ValueError("goal update must change progress, status, or evidence")
        return self


class InvestigationCaseOpenedEvent(ContractModel):
    type: Literal["core.investigation_case_opened.v1"] = "core.investigation_case_opened.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    case_file: InvestigationCaseFile
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def file_belongs_to_actor_and_is_open(self) -> InvestigationCaseOpenedEvent:
        if self.case_file.owner_id != self.actor_id or self.case_file.status != "open":
            raise ValueError("opened investigation case file must be open and actor-owned")
        if self.event_id != uuid5(self.command_id, f"{self.ruleset_version}:investigation_case.opened"):
            raise ValueError("case-open event ID must be bound to its command")
        if self.case_file.id != uuid5(self.command_id, f"{self.ruleset_version}:investigation_case.file"):
            raise ValueError("case-file ID must be bound to its opening command")
        return self


class InvestigationCaseEvidenceAttachedEvent(ContractModel):
    type: Literal["core.investigation_case_evidence_attached.v1"] = "core.investigation_case_evidence_attached.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    case_file_id: UUID
    expected_revision: int = Field(ge=1)
    assessment_id: UUID
    updated_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def event_id_is_command_bound(self) -> InvestigationCaseEvidenceAttachedEvent:
        if self.event_id != uuid5(self.command_id, f"{self.ruleset_version}:investigation_case.evidence_attached"):
            raise ValueError("case-evidence event ID must be bound to its command")
        return self


class InvestigationCaseClosedEvent(ContractModel):
    type: Literal["core.investigation_case_closed.v1"] = "core.investigation_case_closed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    case_file_id: UUID
    expected_revision: int = Field(ge=1)
    suspect_id: UUID | None = None
    public_accusation: bool = False
    outcome: Literal["supported", "disconfirmed", "contested", "unresolved"]
    closed_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def closure_is_command_bound_and_private(self) -> InvestigationCaseClosedEvent:
        if self.event_id != uuid5(self.command_id, f"{self.ruleset_version}:investigation_case.closed"):
            raise ValueError("case-close event ID must be bound to its command")
        if self.public_accusation and self.suspect_id is None:
            raise ValueError("a public accusation must name a suspect")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("case closure details must remain private")
        return self


class PublicAccusationCorrectionRecordedEvent(ContractModel):
    """Record an accuser's public correction without rewriting the case outcome."""

    type: Literal["core.investigation_public_accusation_corrected.v1"] = (
        "core.investigation_public_accusation_corrected.v1"
    )
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    record: PublicAccusationCorrection
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def correction_is_command_bound_and_private(self) -> PublicAccusationCorrectionRecordedEvent:
        if self.event_id != uuid5(
            self.command_id, f"{self.ruleset_version}:investigation_public_accusation_corrected",
        ):
            raise ValueError("public-accusation correction event ID must be bound to its command")
        if self.record.event_id != self.event_id or self.record.actor_id != self.actor_id:
            raise ValueError("public-accusation correction record must match its event")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("public-accusation correction provenance must remain private")
        return self


class AccusationRepairTrustProof(ContractModel):
    """Storage-verified source facts for one negative directed trust effect."""

    case_file_id: UUID
    source_closure_event_id: UUID
    source_relation_effect_event_id: UUID
    accuser_id: UUID
    suspect_id: UUID
    baseline_trust: SignedRelationValue
    post_effect_trust: SignedRelationValue

    @model_validator(mode="after")
    def proves_a_negative_trust_loss(self) -> AccusationRepairTrustProof:
        if self.baseline_trust <= self.post_effect_trust:
            raise ValueError("accusation repair trust proof must show an applied negative trust effect")
        return self


class AccusationRepairKeyReview(ContractModel):
    reason: Literal["relationship_repair"] = "relationship_repair"
    evidence_event_ids: tuple[UUID, UUID]
    model_call_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")


class AccusationRepairTrustRecovery(ContractModel):
    proof: AccusationRepairTrustProof
    applied_delta: StrictInt = Field(ge=0, le=100)
    key_review: AccusationRepairKeyReview | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def bounded_to_a_fraction_of_the_proven_loss(self) -> AccusationRepairTrustRecovery:
        maximum_recovery = int((self.proof.baseline_trust - self.proof.post_effect_trust) / 3)
        if self.applied_delta > maximum_recovery:
            raise ValueError("accusation repair trust recovery exceeds one third of its proven loss")
        return self


class AccusationRepairOfferRecordedEvent(ContractModel):
    """Record one exact authored explanation offer after a public correction."""

    type: Literal["core.accusation_repair_offer_recorded.v1"] = "core.accusation_repair_offer_recorded.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    record: AccusationRepairOffer
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def offer_is_command_bound_and_private(self) -> AccusationRepairOfferRecordedEvent:
        expected_id = uuid5(self.command_id, f"{self.ruleset_version}:accusation_repair.offer")
        if self.event_id != expected_id or self.record.offer_id != self.event_id:
            raise ValueError("accusation repair offer ID must be bound to its command")
        if (
            self.record.revision != 1
            or self.record.status != "pending"
            or self.record.last_operation is not None
            or self.actor_id != self.record.accuser_id
        ):
            raise ValueError("a new accusation repair offer must be pending and authored by its accuser")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("accusation repair source details must remain private")
        return self


class AccusationRepairResolvedEvent(ContractModel):
    """Record the offended person's accept, decline, or defer transition."""

    type: Literal["core.accusation_repair_resolved.v1"] = "core.accusation_repair_resolved.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    operation: RepairOperation
    expected_revision: StrictInt = Field(ge=1)
    resulting_offer: AccusationRepairOffer
    trust_recovery: AccusationRepairTrustRecovery | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def response_is_command_bound_and_private(self) -> AccusationRepairResolvedEvent:
        offer = self.resulting_offer
        expected_id = uuid5(
            self.command_id,
            f"{self.ruleset_version}:accusation_repair.{self.operation}:{offer.offer_id}",
        )
        expected_status = {"accept": "accepted", "decline": "declined", "defer": "pending"}[self.operation]
        if self.event_id != expected_id:
            raise ValueError("accusation repair response event ID must be bound to its command")
        if (
            self.actor_id != offer.suspect_id
            or offer.revision != self.expected_revision + 1
            or offer.status != expected_status
            or offer.last_operation != self.operation
        ):
            raise ValueError("accusation repair response actor, status, or revision is inconsistent")
        if self.requested_by is not None and self.requested_by != offer.accuser_id:
            raise ValueError("a hosted accusation repair response must be requested by its accuser")
        if self.trust_recovery is not None:
            proof = self.trust_recovery.proof
            if self.operation != "accept" or (
                proof.case_file_id != offer.case_file_id
                or proof.source_closure_event_id != offer.source_closure_event_id
                or proof.accuser_id != offer.accuser_id
                or proof.suspect_id != offer.suspect_id
            ):
                raise ValueError("accusation repair trust recovery must match an accepted offer's source")
            key_review = self.trust_recovery.key_review
            if key_review is not None and (
                self.requested_by is None
                or key_review.evidence_event_ids != (offer.correction_event_id, offer.offer_id)
            ):
                raise ValueError("key review must bind the hosted response's correction and private offer")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("accusation repair response provenance must remain private")
        return self


class ShipmentDispatchedEvent(ContractModel):
    """Move one actor-owned resource manifest into shipment custody."""

    type: Literal["core.shipment_dispatched.v1"] = "core.shipment_dispatched.v1"
    event_id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    sender_id: UUID
    source_owner_id: UUID
    destination_owner_id: UUID
    origin_place_id: UUID
    destination_place_id: UUID
    route_id: UUID
    resource_id: ResourceId
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    source_account_id: UUID
    transit_account_id: UUID
    destination_account_id: UUID
    arrival_schedule_id: UUID
    departure_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    arrival_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    lifecycle_version: Literal["legacy_v1", "expiry_v2"] = "legacy_v1"
    spoilage_schedule_id: UUID | None = None
    spoilage_due_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    expected_source_inventory_revision: int = Field(ge=1)
    expected_source_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    lot_transfers: list[ShipmentLotManifest] = Field(min_length=1, max_length=100_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def exact_source_manifest(self) -> ShipmentDispatchedEvent:
        if self.sender_id != self.source_owner_id:
            raise ValueError("shipment source owner must be the local sender")
        if self.arrival_tick <= self.departure_tick:
            raise ValueError("shipment arrival must follow departure")
        if (self.spoilage_schedule_id is None) != (self.spoilage_due_tick is None):
            raise ValueError("shipment dispatch must supply both spoilage schedule ID and tick, or neither")
        if self.spoilage_due_tick is not None and self.spoilage_due_tick <= self.arrival_tick:
            raise ValueError("shipment spoilage schedule must follow the exact arrival tick")
        if self.lifecycle_version == "legacy_v1" and self.spoilage_schedule_id is not None:
            raise ValueError("legacy shipment dispatches cannot add expiry schedules during replay")
        if self.spoilage_schedule_id is not None and self.spoilage_due_tick is not None:
            expected_schedule = uuid5(
                self.event_id,
                f"shipment.spoilage-schedule:{self.shipment_id}:{self.spoilage_due_tick}",
            )
            if self.spoilage_schedule_id != expected_schedule:
                raise ValueError("shipment spoilage schedule ID must be bound to its dispatch event")
        if len({item.source_lot_id for item in self.lot_transfers}) != len(self.lot_transfers):
            raise ValueError("shipment dispatch cannot repeat a source FIFO lot")
        if len({item.transit_lot_id for item in self.lot_transfers}) != len(self.lot_transfers):
            raise ValueError("shipment dispatch cannot repeat a transit lot identity")
        with localcontext() as context:
            context.prec = 80
            total = sum((item.quantity for item in self.lot_transfers), Decimal("0"))
        if total != self.quantity:
            raise ValueError("shipment dispatch lot quantities must equal the exact requested quantity")
        return self


class ShipmentArrivalDeliveredEvent(ContractModel):
    """Deliver the manifest and complete its exact-due one-shot schedule."""

    type: Literal["core.shipment_arrival_delivered.v1"] = "core.shipment_arrival_delivered.v1"
    event_id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    expected_shipment_revision: int = Field(ge=1)
    scheduled_event_id: UUID
    expected_schedule_revision: int = Field(ge=1)
    expected_spoilage_schedule_revision: int | None = Field(default=None, ge=1)
    arrival_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    expected_transit_inventory_revision: int = Field(ge=1)
    expected_destination_inventory_revision: int = Field(ge=1)
    expected_destination_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    flows: list[ShipmentAccountFlow] = Field(min_length=1, max_length=1)
    receipts: list[ShipmentLotReceipt] = Field(min_length=1, max_length=100_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def unique_receipt_lots(self) -> ShipmentArrivalDeliveredEvent:
        if len({item.transit_lot_id for item in self.receipts}) != len(self.receipts):
            raise ValueError("shipment receipt cannot repeat a transit lot")
        if len({item.destination_lot_id for item in self.receipts}) != len(self.receipts):
            raise ValueError("shipment receipt cannot repeat a destination lot")
        with localcontext() as context:
            context.prec = 80
            receipt_total = sum((item.quantity for item in self.receipts), Decimal("0"))
        if receipt_total != self.flows[0].quantity:
            raise ValueError("shipment receipt lots must exactly match the transit flow quantity")
        return self


class ShipmentArrivalDeferredEvent(ContractModel):
    """Complete arrival processing while retaining shipment-owned cargo at destination."""

    type: Literal["core.shipment_arrival_deferred.v1"] = "core.shipment_arrival_deferred.v1"
    event_id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    expected_shipment_revision: int = Field(ge=1)
    scheduled_event_id: UUID
    expected_schedule_revision: int = Field(ge=1)
    arrival_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    reason: Literal["receiver_unavailable"] = "receiver_unavailable"
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class ShipmentReceiptAcceptedEvent(ContractModel):
    """Release deferred cargo to its designated receiver when physically present."""

    type: Literal["core.shipment_receipt_accepted.v1"] = "core.shipment_receipt_accepted.v1"
    event_id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    expected_shipment_revision: int = Field(ge=1)
    receiver_id: UUID
    received_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    expected_spoilage_schedule_revision: int | None = Field(default=None, ge=1)
    expected_transit_inventory_revision: int = Field(ge=1)
    expected_destination_inventory_revision: int = Field(ge=1)
    expected_destination_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    flows: list[ShipmentAccountFlow] = Field(min_length=1, max_length=1)
    receipts: list[ShipmentLotReceipt] = Field(min_length=1, max_length=100_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def unique_receipt_lots(self) -> ShipmentReceiptAcceptedEvent:
        if len({item.transit_lot_id for item in self.receipts}) != len(self.receipts):
            raise ValueError("shipment receipt cannot repeat a transit lot")
        if len({item.destination_lot_id for item in self.receipts}) != len(self.receipts):
            raise ValueError("shipment receipt cannot repeat a destination lot")
        with localcontext() as context:
            context.prec = 80
            receipt_total = sum((item.quantity for item in self.receipts), Decimal("0"))
        if receipt_total != self.flows[0].quantity:
            raise ValueError("shipment receipt lots must exactly match the transit flow quantity")
        return self


class ShipmentSpoilageResolvedEvent(ContractModel):
    """Dispose only lots reaching their registered expiry boundary in transit custody."""

    type: Literal["core.shipment_spoilage_resolved.v1"] = "core.shipment_spoilage_resolved.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    shipment_id: UUID
    expected_shipment_revision: int = Field(ge=1)
    scheduled_event_id: UUID
    expected_schedule_revision: int = Field(ge=1)
    spoiled_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    expected_transit_inventory_revision: int = Field(ge=1)
    expected_transit_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    resource_id: ResourceId
    transit_account_id: UUID
    destruction_sink_account_id: UUID
    expected_destruction_sink_balance: ExactDecimal | None = Field(default=None, ge=Decimal("0"))
    create_destruction_sink: bool = False
    spoiled_lots: list[ShipmentSpoiledLot] = Field(min_length=1, max_length=100_000)
    next_spoilage_schedule_id: UUID | None = None
    next_spoilage_due_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def exact_spoilage_shape(self) -> ShipmentSpoilageResolvedEvent:
        expected_event_id = uuid5(
            self.command_id,
            f"{self.ruleset_version}:shipment.spoilage:{self.shipment_id}:{self.scheduled_event_id}",
        )
        if self.event_id != expected_event_id:
            raise ValueError("shipment spoilage event ID must be bound to its due scheduled command")
        if (self.next_spoilage_schedule_id is None) != (self.next_spoilage_due_tick is None):
            raise ValueError("next shipment spoilage schedule ID and due tick must be supplied together")
        if self.next_spoilage_due_tick is not None and self.next_spoilage_due_tick <= self.spoiled_tick:
            raise ValueError("the next shipment spoilage boundary must be later than this one")
        lot_ids = [item.transit_lot_id for item in self.spoiled_lots]
        if lot_ids != sorted(lot_ids, key=str) or len(lot_ids) != len(set(lot_ids)):
            raise ValueError("shipment spoilage lots must be unique and ordered by transit-lot UUID")
        if any(
            item.spoiled_tick != self.spoiled_tick
            or item.event_id != self.event_id
            or item.destruction_sink_account_id != self.destruction_sink_account_id
            for item in self.spoiled_lots
        ):
            raise ValueError("shipment spoilage lot receipts must match the exact event boundary, ID and sink")
        if self.create_destruction_sink != (self.expected_destruction_sink_balance is None):
            raise ValueError("new destruction sink creation requires an absent opening balance; existing sinks require a CAS balance")
        if self.next_spoilage_schedule_id is not None and self.next_spoilage_due_tick is not None:
            expected_next_schedule = uuid5(
                self.command_id,
                f"{self.ruleset_version}:shipment.spoilage-schedule:{self.shipment_id}:{self.next_spoilage_due_tick}",
            )
            if self.next_spoilage_schedule_id != expected_next_schedule:
                raise ValueError("next shipment spoilage schedule ID must be bound to its due command")
        return self


class HistoricalEvidenceDestroyedEvent(ContractModel):
    """Record destruction of one active physical copy of authored evidence."""

    type: Literal["core.historical_evidence_destroyed.v1"] = "core.historical_evidence_destroyed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    policy_version: Literal["investigation.evidence_destruction.v1"] = "investigation.evidence_destruction.v1"
    actor_id: UUID
    target_id: UUID
    location_id: UUID
    historical_evidence_id: UUID
    historical_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    destroyed_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def destruction_is_command_bound_and_private(self) -> HistoricalEvidenceDestroyedEvent:
        if self.event_id != uuid5(self.command_id, f"{self.ruleset_version}:historical_evidence.destroyed"):
            raise ValueError("historical evidence destruction event ID must be bound to its command")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("historical evidence destruction details must remain private")
        return self


class ConsumedResourceFlow(ContractModel):
    holder_account_id: UUID
    sink_account_id: UUID
    resource_id: ResourceId
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ProducedResourceFlow(ContractModel):
    source_account_id: UUID
    holder_account_id: UUID
    resource_id: ResourceId
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ResourceConversion(ContractModel):
    step_id: ResourceId
    input_resource_id: ResourceId
    input_quantity: ExactDecimal = Field(ge=Decimal("0"))
    output_resource_id: ResourceId
    output_quantity: ExactDecimal = Field(ge=Decimal("0"))

    @model_validator(mode="after")
    def nonempty_flow(self) -> ResourceConversion:
        if self.input_quantity == 0 and self.output_quantity == 0:
            raise ValueError("a resource conversion must consume or produce a positive quantity")
        return self


class ResourceConsumedEvent(ContractModel):
    type: Literal["core.resource_consumed.v1"] = "core.resource_consumed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    consumer_id: UUID | None = None
    purpose: str = Field(min_length=1, max_length=240)
    flows: list[ConsumedResourceFlow] = Field(min_length=1, max_length=1000)
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class ResourceProducedEvent(ContractModel):
    type: Literal["core.resource_produced.v1"] = "core.resource_produced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    producer_id: UUID | None = None
    recipe_id: ResourceId
    batch_count: int = Field(default=1, ge=1, le=1_000_000)
    consumed_inputs: list[ConsumedResourceFlow] = Field(min_length=1, max_length=1000)
    produced_outputs: list[ProducedResourceFlow] = Field(min_length=1, max_length=1000)
    conversions: list[ResourceConversion] = Field(min_length=1, max_length=1000)
    source_event_ids: list[UUID] = Field(default_factory=list, max_length=10_000)
    provenance: list[ProvenanceReference] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def conversion_matches_flow_totals(self) -> ResourceProducedEvent:
        if self.producer_id is None and not self.source_event_ids:
            raise ValueError("legacy production events must cite at least one source event")
        if self.producer_id is not None and not self.source_event_ids and not self.provenance:
            raise ValueError("production must cite source events or typed rule/pack/genesis provenance")
        with localcontext() as context:
            context.prec = 40
            expected_inputs: dict[str, Decimal] = {}
            expected_outputs: dict[str, Decimal] = {}
            for conversion in self.conversions:
                expected_inputs[conversion.input_resource_id] = (
                    expected_inputs.get(conversion.input_resource_id, Decimal("0")) + conversion.input_quantity
                )
                expected_outputs[conversion.output_resource_id] = (
                    expected_outputs.get(conversion.output_resource_id, Decimal("0")) + conversion.output_quantity
                )
            input_totals: dict[str, Decimal] = {}
            output_totals: dict[str, Decimal] = {}
            for flow in self.consumed_inputs:
                input_totals[flow.resource_id] = input_totals.get(flow.resource_id, Decimal("0")) + flow.quantity
            for flow in self.produced_outputs:
                output_totals[flow.resource_id] = output_totals.get(flow.resource_id, Decimal("0")) + flow.quantity
        if input_totals != expected_inputs:
            raise ValueError("conversion input totals must exactly match consumed resource flows")
        if output_totals != expected_outputs:
            raise ValueError("conversion output totals must exactly match produced resource flows")
        return self


GameplayEvent = (
    MovementCompletedEvent
    | EntityDiscoveredEvent
    | CombatExchangeResolvedEvent
    | CombatRetreatResolvedEvent
    | DamageAppliedEvent
    | HealthRestoredEvent
    | InjuryTreatedEvent
    | ResourceAccountOpenedEvent
    | ComponentReplacedEvent
    | AuthorComponentRevisedEvent
    | KnowledgeAcquiredEvent
    | RelationEstablishedEvent
    | RelationDimensionChangedEvent
    | GoalCreatedEvent
    | GoalUpdatedEvent
    | ResourceConsumedEvent
    | ResourceProducedEvent
    | ShipmentDispatchedEvent
    | ShipmentArrivalDeliveredEvent
    | ShipmentArrivalDeferredEvent
    | ShipmentReceiptAcceptedEvent
    | ShipmentSpoilageResolvedEvent
    | HistoricalEvidenceDestroyedEvent
)


def validate_replacement(entity: WorldEntity, event: ComponentReplacedEvent) -> object:
    """Validate a replacement payload and ensure it advances exactly one revision."""
    parsed = parse_component(entity.kind, event.component_name, event.replacement)
    revision = getattr(parsed, "revision", None)
    current_value = entity.components.get(event.component_name)
    current = None
    if current_value is None:
        if event.expected_revision is not None or revision != 1:
            raise ValueError("a new component must expect no previous revision and start at revision 1")
    else:
        current = parse_component(entity.kind, event.component_name, current_value)
        current_revision = getattr(current, "revision", None)
        if current_revision is None or event.expected_revision != current_revision:
            raise ValueError("component expected_revision does not match stored revision")
        if revision != current_revision + 1:
            raise ValueError("replacement revision must increase exactly by one")
    if isinstance(parsed, PersonalityComponent):
        return _validate_personality_replacement(current, parsed, allow_lock_changes=False)
    if isinstance(parsed, CapabilitiesComponent):
        return _validate_capabilities_replacement(current, parsed, allow_protection_changes=False)
    if isinstance(parsed, HealthComponent):
        return _validate_health_replacement(current, parsed)
    return parsed


def validate_author_replacement(entity: WorldEntity, event: AuthorComponentRevisedEvent) -> object:
    """Validate a full registered component replacement and its revision sequence."""
    parsed = parse_component(entity.kind, event.component_name, event.replacement)
    revision = getattr(parsed, "revision", None)
    current_value = entity.components.get(event.component_name)
    if revision is None:
        return parsed
    current = None
    if current_value is None:
        if revision != 1:
            raise ValueError("a new revisioned component must start at revision 1")
    else:
        current = parse_component(entity.kind, event.component_name, current_value)
        current_revision = getattr(current, "revision", None)
        if current_revision is None:
            raise ValueError("stored component has no revision to compare")
        if revision != current_revision + 1:
            raise ValueError("replacement revision must increase exactly by one")
    if isinstance(parsed, PersonalityComponent):
        return _validate_personality_replacement(current, parsed, allow_lock_changes=True)
    if isinstance(parsed, CapabilitiesComponent):
        return _validate_capabilities_replacement(current, parsed, allow_protection_changes=True)
    if isinstance(parsed, HealthComponent):
        return _validate_health_replacement(current, parsed)
    return parsed


def _validate_capabilities_replacement(
    current: object, replacement: CapabilitiesComponent, *, allow_protection_changes: bool,
) -> CapabilitiesComponent:
    current_protection = isinstance(current, CapabilitiesComponent) and current.combat_death_protection
    if "combat_death_protection" not in replacement.model_fields_set:
        return replacement.model_copy(update={"combat_death_protection": current_protection})
    if not allow_protection_changes and replacement.combat_death_protection != current_protection:
        raise ValueError("Combat death protection can only be changed through an author revision.")
    return replacement


def _validate_health_replacement(current: object, replacement: HealthComponent) -> HealthComponent:
    injury_id = current.incapacitating_injury_id if isinstance(current, HealthComponent) else None
    if "incapacitating_injury_id" in replacement.model_fields_set:
        if replacement.incapacitating_injury_id != injury_id:
            raise ValueError("The incapacitating injury cannot be changed by a component replacement.")
        return replacement
    if injury_id is None:
        return replacement
    return HealthComponent.model_validate({**replacement.model_dump(), "incapacitating_injury_id": injury_id})


def _validate_personality_replacement(
    current: object, replacement: PersonalityComponent, *, allow_lock_changes: bool,
) -> PersonalityComponent:
    current_locks = current.locked_fields if isinstance(current, PersonalityComponent) else ()
    locked_values = {field: getattr(current, field) for field in current_locks}
    changed_fields = tuple(field for field, value in locked_values.items() if getattr(replacement, field) != value)
    if changed_fields:
        raise PersonalityLockViolation(changed_fields)

    locks = replacement.locked_fields if "locked_fields" in replacement.model_fields_set else current_locks
    if not allow_lock_changes and locks != current_locks:
        raise ValueError("Personality locks can only be changed through an author revision.")
    return replacement.model_copy(update={**locked_values, "locked_fields": locks})
