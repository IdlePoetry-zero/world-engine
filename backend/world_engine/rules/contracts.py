"""Closed semantic action contracts and pure rule evaluation envelopes."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    Field,
    PrivateAttr,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    WithJsonSchema,
    field_serializer,
    model_validator,
)

from world_engine.domain.components import ExactDecimal, ResourceName
from world_engine.domain.gameplay_events import AccusationRepairKeyReview, AccusationRepairTrustProof
from world_engine.domain.investigation import WorldTick
from world_engine.domain.institutional_credit import MAX_INSTITUTIONAL_CREDIT_MINOR
from world_engine.domain.json_types import JsonObject
from world_engine.domain.models import DomainEvent, ContractModel
from world_engine.domain.market_pricing import MAX_PRICE_MINOR
from world_engine.domain.relationship_invitations import InvitationMeeting
from world_engine.domain.relationship_partnerships import PartnershipTerms


class IntentModel(ContractModel):
    """Base for compiled actions. Extra model-supplied fields are always rejected."""


class MoveIntent(IntentModel):
    kind: Literal["move"] = "move"
    destination_id: UUID
    route_id: UUID | None = None


class RetreatIntent(IntentModel):
    """Leave an active co-located encounter over an explicitly registered route."""

    kind: Literal["retreat"] = "retreat"
    destination_id: UUID
    route_id: UUID | None = None


class ExploreIntent(IntentModel):
    kind: Literal["explore"] = "explore"
    place_id: UUID
    focus: Literal["general", "people", "routes", "supplies", "history"] = "general"


class SpeakIntent(IntentModel):
    """Say one private utterance to a bounded, explicitly named audience."""

    kind: Literal["speak"] = "speak"
    target_ids: list[UUID] = Field(
        min_length=1,
        max_length=8,
        json_schema_extra={"uniqueItems": True},
    )
    utterance: str = Field(min_length=1, max_length=1200)
    reply_to_memory_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="A known private utterance from the named recipient. A first reciprocal reply increases familiarity.",
    )
    topic_record_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="An active known topic explicitly included in the speaker's goals and already known to both participants.",
    )

    @model_validator(mode="after")
    def recipients_are_unique(self) -> SpeakIntent:
        if len(self.target_ids) != len(set(self.target_ids)):
            raise ValueError("speech recipients must be unique")
        if (self.reply_to_memory_id is not None or self.topic_record_id is not None) and len(self.target_ids) != 1:
            raise ValueError("a private reply or topic must name exactly one recipient")
        return self


class ImprovisedCheckIntent(IntentModel):
    """One limited check against a registered competency and visible object."""

    kind: Literal["improvised_check"] = "improvised_check"
    target_id: UUID
    skill_id: Literal["skill.investigation", "skill.local_knowledge"]


class WeighEvidenceIntent(IntentModel):
    """Compare one actor-known observation with one fallible typed claim."""

    kind: Literal["weigh_evidence"] = "weigh_evidence"
    observation_id: UUID
    claim_id: UUID


class AskNpcClaimIntent(IntentModel):
    """Ask one visible, co-located character to disclose one typed claim."""

    kind: Literal["ask_npc_claim"] = "ask_npc_claim"
    target_id: UUID
    subject_id: UUID
    predicate: Literal["entity_status", "entity_location"] | None = None
    effective_tick: WorldTick | None = None


class OpenInvestigationCaseIntent(IntentModel):
    kind: Literal["open_investigation_case"] = "open_investigation_case"
    anchor_id: UUID


class AttachCaseEvidenceIntent(IntentModel):
    kind: Literal["attach_case_evidence"] = "attach_case_evidence"
    case_file_id: UUID
    assessment_id: UUID


class CloseInvestigationCaseIntent(IntentModel):
    kind: Literal["close_investigation_case"] = "close_investigation_case"
    case_file_id: UUID
    suspect_id: UUID | None = None
    public_accusation: StrictBool = False

    @model_validator(mode="after")
    def public_accusation_names_suspect(self) -> CloseInvestigationCaseIntent:
        if self.public_accusation and self.suspect_id is None:
            raise ValueError("a public accusation requires an explicitly selected suspect")
        return self


class CorrectPublicAccusationIntent(IntentModel):
    kind: Literal["correct_public_accusation"] = "correct_public_accusation"
    case_file_id: UUID
    expected_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    source_closure_event_id: UUID


class OfferAccusationRepairIntent(IntentModel):
    kind: Literal["offer_accusation_repair"] = "offer_accusation_repair"
    correction_event_id: UUID
    statement: str = Field(min_length=1, max_length=500)


class ResolveAccusationRepairIntent(IntentModel):
    kind: Literal["resolve_accusation_repair"] = "resolve_accusation_repair"
    offer_id: UUID
    expected_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    operation: Literal["accept", "decline", "defer"]


class RequestAccusationRepairResponseIntent(IntentModel):
    kind: Literal["request_accusation_repair_response"] = "request_accusation_repair_response"
    offer_id: UUID
    expected_revision: StrictInt = Field(ge=1, le=2**63 - 1)


class DestroyHistoricalEvidenceIntent(IntentModel):
    """Destroy one currently visible local physical copy of historical evidence."""

    kind: Literal["destroy_historical_evidence"] = "destroy_historical_evidence"
    target_id: UUID


class DispatchShipmentIntent(IntentModel):
    """Dispatch exact FIFO cargo over one registered route to a named receiver."""

    kind: Literal["dispatch_shipment"] = "dispatch_shipment"
    route_id: UUID
    source_owner_id: UUID
    destination_owner_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    source_account_id: UUID
    destination_account_id: UUID


class ReceiveShipmentIntent(IntentModel):
    """Accept deferred shipment custody when its designated receiver is present."""

    kind: Literal["receive_shipment"] = "receive_shipment"
    shipment_id: UUID


class DistributeShipmentReliefIntent(IntentModel):
    """Authorize one organization's fixed-quantity shipment relief distribution."""

    kind: Literal["distribute_shipment_relief"] = "distribute_shipment_relief"
    organization_id: UUID
    shipment_id: UUID
    recipient_id: UUID
    quantity: Annotated[ExactDecimal, WithJsonSchema({"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]+)?$"})] = Field(gt=Decimal("0"))

    @field_serializer("quantity")
    def serialize_canonical_quantity(self, value: Decimal) -> str:
        normalized = format(value, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return normalized


class ConsumeIntent(IntentModel):
    kind: Literal["consume"] = "consume"
    need: Literal["food", "water", "health"]
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))

    @property
    def duration_ticks(self) -> int:
        return 300 if self.need == "food" else 60


class TradePriceCeiling(IntentModel):
    """Optional whole-transaction price guard in one exact minor currency."""

    max_total_minor: StrictInt = Field(ge=0, le=MAX_PRICE_MINOR)
    currency_resource_id: ResourceName


class TradeIntent(IntentModel):
    kind: Literal["trade"] = "trade"
    listing_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    price_ceiling: TradePriceCeiling | None = Field(default=None, exclude_if=lambda value: value is None)


class OfferInstitutionalCreditIntent(IntentModel):
    kind: Literal["offer_institutional_credit"] = "offer_institutional_credit"
    lender_organization_id: UUID
    borrower_organization_id: UUID
    currency_resource_id: ResourceName
    principal_minor: StrictInt = Field(gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR)
    total_repayable_minor: StrictInt | None = Field(
        default=None, gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR, exclude_if=lambda value: value is None,
    )
    accepted_through_tick: WorldTick
    maturity_tick: WorldTick


class ResolveInstitutionalCreditIntent(IntentModel):
    kind: Literal["resolve_institutional_credit"] = "resolve_institutional_credit"
    credit_id: UUID
    operation: Literal["accept", "decline", "withdraw", "pay"]
    expected_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    amount_minor: StrictInt | None = Field(
        default=None, gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def only_payment_includes_an_amount(self) -> ResolveInstitutionalCreditIntent:
        if (self.operation == "pay") != (self.amount_minor is not None):
            raise ValueError("Institutional credit payments require exactly one amount.")
        return self


class ProposeCreditExtensionIntent(IntentModel):
    kind: Literal["propose_credit_extension"] = "propose_credit_extension"
    credit_id: UUID
    organization_id: UUID
    expected_revision: StrictInt = Field(ge=1, lt=2**63 - 1)
    new_maturity_tick: WorldTick
    accept_through_tick: WorldTick


class ResolveCreditExtensionIntent(IntentModel):
    kind: Literal["resolve_credit_extension"] = "resolve_credit_extension"
    credit_id: UUID
    extension_id: UUID
    operation: Literal["accept", "decline", "withdraw"]
    expected_revision: StrictInt = Field(ge=1, lt=2**63 - 1)


class MobilizeTreasuryReserveIntent(IntentModel):
    kind: Literal["mobilize_treasury_reserve"] = "mobilize_treasury_reserve"
    organization_id: UUID
    currency_resource_id: ResourceName
    amount_minor: StrictInt = Field(gt=0, le=MAX_INSTITUTIONAL_CREDIT_MINOR)


class ProduceIntent(IntentModel):
    kind: Literal["produce"] = "produce"
    producer_id: UUID
    recipe_id: ResourceName
    batch_count: int = Field(default=1, ge=1, le=1_000_000)


class AttackIntent(IntentModel):
    kind: Literal["attack"] = "attack"
    target_id: UUID
    weapon_resource_id: ResourceName | None = None


class OfferCombatTruceIntent(IntentModel):
    """Offer an unconditional pause in the actor's current combat encounter."""

    kind: Literal["offer_combat_truce"] = "offer_combat_truce"
    expected_last_event_id: UUID


class ResolveCombatTruceIntent(IntentModel):
    """Accept or decline one recorded combat truce offer."""

    kind: Literal["resolve_combat_truce"] = "resolve_combat_truce"
    offer_id: UUID
    operation: Literal["accept", "decline"]


class RequestCombatTruceResponseIntent(IntentModel):
    """Ask the world host to solicit an NPC's voluntary truce response."""

    kind: Literal["request_combat_truce_response"] = "request_combat_truce_response"
    offer_id: UUID


class TreatInjuryIntent(IntentModel):
    """Treat one actor-known injury of a visible co-located character."""

    kind: Literal["treat_injury"] = "treat_injury"
    target_id: UUID
    injury_id: UUID
    medicine_resource_id: ResourceName


class TrainIntent(IntentModel):
    kind: Literal["train"] = "train"
    skill_id: ResourceName
    duration_ticks: int = Field(gt=0, le=8 * 60 * 60)


class AttemptCultivationBreakthroughIntent(IntentModel):
    """Attempt one server-registered cultivation realm transition."""

    kind: Literal["attempt_cultivation_breakthrough"] = "attempt_cultivation_breakthrough"
    policy_id: ResourceName


class LeaveOrganizationIntent(IntentModel):
    """Leave one organization the requesting actor currently belongs to."""

    kind: Literal["leave_organization"] = "leave_organization"
    organization_id: UUID


class HandoverLeadershipIntent(IntentModel):
    """Transfer the requesting leader's appointment-based office to a member."""

    kind: Literal["handover_leadership"] = "handover_leadership"
    organization_id: UUID
    successor_id: UUID


class OpenLeadershipBallotIntent(IntentModel):
    kind: Literal["open_leadership_ballot"] = "open_leadership_ballot"
    organization_id: UUID
    nominee_id: UUID


class OpenVacantLeadershipBallotIntent(IntentModel):
    kind: Literal["open_vacant_leadership_ballot"] = "open_vacant_leadership_ballot"
    organization_id: UUID
    nominee_id: UUID


class CastLeadershipBallotVoteIntent(IntentModel):
    kind: Literal["cast_leadership_ballot_vote"] = "cast_leadership_ballot_vote"
    ballot_id: UUID
    expected_ballot_revision: StrictInt = Field(ge=1)
    choice: Literal["yes", "no", "abstain"]


class CloseLeadershipBallotIntent(IntentModel):
    kind: Literal["close_leadership_ballot"] = "close_leadership_ballot"
    ballot_id: UUID
    expected_ballot_revision: StrictInt = Field(ge=1)


class RequestLeadershipBallotVoteIntent(IntentModel):
    kind: Literal["request_leadership_ballot_vote"] = "request_leadership_ballot_vote"
    ballot_id: UUID
    voter_id: UUID
    expected_ballot_revision: StrictInt = Field(ge=1)


class ProposeSafeConductIntent(IntentModel):
    kind: Literal["propose_safe_conduct"] = "propose_safe_conduct"
    proposer_organization_id: UUID
    recipient_organization_id: UUID
    from_place_id: UUID
    route_id: UUID
    expires_at_tick: WorldTick


class ResolveSafeConductIntent(IntentModel):
    kind: Literal["resolve_safe_conduct"] = "resolve_safe_conduct"
    treaty_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    operation: Literal["ratify", "decline", "withdraw", "revoke", "counter", "accept_counter"]
    counter_expires_at_tick: WorldTick | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def counter_requires_only_a_new_expiry(self) -> ResolveSafeConductIntent:
        if (self.operation == "counter") != (self.counter_expires_at_tick is not None):
            raise ValueError("only a counteroffer must specify a new expiry")
        return self


class RequestSafeConductResponseIntent(IntentModel):
    kind: Literal["request_safe_conduct_response"] = "request_safe_conduct_response"
    treaty_id: UUID
    expected_revision: StrictInt = Field(ge=1)


class RecordReturnPromiseIntent(IntentModel):
    kind: Literal["record_return_promise"] = "record_return_promise"
    promisee_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    deadline_tick: WorldTick
    private_text: str = Field(min_length=1, max_length=1000)


class FulfillReturnPromiseIntent(IntentModel):
    kind: Literal["fulfill_return_promise"] = "fulfill_return_promise"
    promise_id: UUID


class LateReturnPromiseIntent(IntentModel):
    kind: Literal["late_return_promise"] = "late_return_promise"
    promise_id: UUID


class ReleaseReturnPromiseIntent(IntentModel):
    kind: Literal["release_return_promise"] = "release_return_promise"
    promise_id: UUID


class OfferPrivateGiftIntent(IntentModel):
    """Offer a co-located character an explicitly sized private gift."""

    kind: Literal["offer_private_gift"] = "offer_private_gift"
    recipient_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    expires_at_tick: WorldTick | None = Field(default=None, exclude_if=lambda value: value is None)


class ResolvePrivateGiftIntent(IntentModel):
    """Accept or decline a gift, or withdraw its pending offer."""

    kind: Literal["resolve_private_gift"] = "resolve_private_gift"
    gift_id: UUID
    expected_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    operation: Literal["accept", "decline", "withdraw"]


class ProposeRelationshipInvitationIntent(IntentModel):
    """Ask one visible adult to consider an explicitly modeled relationship."""

    kind: Literal["propose_relationship_invitation"] = "propose_relationship_invitation"
    target_id: UUID
    meeting: InvitationMeeting | None = Field(default=None, exclude_if=lambda value: value is None)


class ResolveRelationshipInvitationIntent(IntentModel):
    """Accept, decline, withdraw, or end one persisted invitation."""

    kind: Literal["resolve_relationship_invitation"] = "resolve_relationship_invitation"
    invitation_id: UUID
    operation: Literal["accept", "decline", "withdraw", "end"]


class ProposeRelationshipPartnershipIntent(IntentModel):
    """Propose a private relationship partnership to one other character."""

    kind: Literal["propose_relationship_partnership"] = "propose_relationship_partnership"
    partner_id: UUID
    proposal_terms: PartnershipTerms = Field(default=(), exclude_if=lambda terms: not terms)


class ResolveRelationshipPartnershipIntent(IntentModel):
    """Resolve or end a pending relationship partnership proposal."""

    kind: Literal["resolve_relationship_partnership"] = "resolve_relationship_partnership"
    partnership_id: UUID
    operation: Literal["accept", "decline", "withdraw", "end"]
    expected_revision: StrictInt = Field(ge=1)


class UpdateRelationshipPartnershipProposalIntent(IntentModel):
    """Replace the complete terms of a pending proposal or issue a counterproposal."""

    kind: Literal["update_relationship_partnership_proposal"] = "update_relationship_partnership_proposal"
    partnership_id: UUID
    expected_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    operation: Literal["revise", "counter"]
    proposal_terms: PartnershipTerms = Field(min_length=1, max_length=6)


class DiscloseRelationshipPartnershipIntent(IntentModel):
    """Tell one visible character about the acting participant's active relationship."""

    kind: Literal["disclose_relationship_partnership"] = "disclose_relationship_partnership"
    partnership_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    recipient_id: UUID


class RequestRelationshipPartnershipResponseIntent(IntentModel):
    """Ask the world host to solicit the other participant's response."""

    kind: Literal["request_relationship_partnership_response"] = "request_relationship_partnership_response"
    partnership_id: UUID
    expected_revision: StrictInt = Field(ge=1)


class AttendRelationshipMeetingIntent(IntentModel):
    """Record the acting participant's attendance at a scheduled meeting."""

    kind: Literal["attend_relationship_meeting"] = "attend_relationship_meeting"
    invitation_id: UUID


class RecordMeetingReflectionIntent(IntentModel):
    """Record the actor's private outlook after attending an invitation meeting."""

    kind: Literal["record_meeting_reflection"] = "record_meeting_reflection"
    invitation_id: UUID
    outlook: Literal["closer", "unchanged", "uneasy"]


class RequestMeetingReflectionIntent(IntentModel):
    """Ask the world host to solicit the other participant's own reflection."""

    kind: Literal["request_meeting_reflection"] = "request_meeting_reflection"
    invitation_id: UUID


class RequestRelationshipMeetingAttendanceIntent(IntentModel):
    """Ask the world host to solicit one NPC participant's meeting attendance."""

    kind: Literal["request_relationship_meeting_attendance"] = "request_relationship_meeting_attendance"
    invitation_id: UUID


class RequestRelationshipReplyIntent(IntentModel):
    """Ask the world host to solicit one NPC's response to the player's pending invitation."""

    kind: Literal["request_relationship_reply"] = "request_relationship_reply"
    invitation_id: UUID


class TakeJobIntent(IntentModel):
    kind: Literal["take_job"] = "take_job"
    posting_id: UUID


class ResignJobIntent(IntentModel):
    kind: Literal["resign_job"] = "resign_job"
    assignment_id: UUID


class WorkShiftIntent(IntentModel):
    kind: Literal["work_shift"] = "work_shift"
    assignment_id: UUID


class RestIntent(IntentModel):
    kind: Literal["rest"] = "rest"
    duration_ticks: int = Field(ge=60, le=2**63 - 1)


class WaitIntent(IntentModel):
    kind: Literal["wait", "idle"] = "wait"
    duration_ticks: int = Field(gt=0, le=2**63 - 1)


ActionIntent = Annotated[
    MoveIntent
    | RetreatIntent
    | ExploreIntent
    | SpeakIntent
    | ImprovisedCheckIntent
    | WeighEvidenceIntent
    | AskNpcClaimIntent
    | OpenInvestigationCaseIntent
    | AttachCaseEvidenceIntent
    | CloseInvestigationCaseIntent
    | CorrectPublicAccusationIntent
    | OfferAccusationRepairIntent
    | ResolveAccusationRepairIntent
    | RequestAccusationRepairResponseIntent
    | DestroyHistoricalEvidenceIntent
    | DispatchShipmentIntent
    | ReceiveShipmentIntent
    | DistributeShipmentReliefIntent
    | ConsumeIntent
    | TradeIntent
    | OfferInstitutionalCreditIntent
    | ResolveInstitutionalCreditIntent
    | MobilizeTreasuryReserveIntent
    | ProposeCreditExtensionIntent
    | ResolveCreditExtensionIntent
    | ProduceIntent
    | LeaveOrganizationIntent
    | HandoverLeadershipIntent
    | OpenLeadershipBallotIntent
    | OpenVacantLeadershipBallotIntent
    | CastLeadershipBallotVoteIntent
    | CloseLeadershipBallotIntent
    | RequestLeadershipBallotVoteIntent
    | ProposeSafeConductIntent
    | ResolveSafeConductIntent
    | RequestSafeConductResponseIntent
    | RecordReturnPromiseIntent
    | FulfillReturnPromiseIntent
    | LateReturnPromiseIntent
    | ReleaseReturnPromiseIntent
    | OfferPrivateGiftIntent
    | ResolvePrivateGiftIntent
    | ProposeRelationshipInvitationIntent
    | ResolveRelationshipInvitationIntent
    | ProposeRelationshipPartnershipIntent
    | ResolveRelationshipPartnershipIntent
    | UpdateRelationshipPartnershipProposalIntent
    | DiscloseRelationshipPartnershipIntent
    | RequestRelationshipPartnershipResponseIntent
    | AttendRelationshipMeetingIntent
    | RecordMeetingReflectionIntent
    | RequestMeetingReflectionIntent
    | RequestRelationshipMeetingAttendanceIntent
    | RequestRelationshipReplyIntent
    | TakeJobIntent
    | ResignJobIntent
    | WorkShiftIntent
    | AttackIntent
    | OfferCombatTruceIntent
    | ResolveCombatTruceIntent
    | RequestCombatTruceResponseIntent
    | TreatInjuryIntent
    | TrainIntent
    | AttemptCultivationBreakthroughIntent
    | RestIntent
    | WaitIntent,
    Field(discriminator="kind"),
]
ACTION_INTENT_ADAPTER = TypeAdapter(ActionIntent)


def parse_action(value: object) -> ActionIntent:
    """Validate an untrusted semantic action against the closed registry."""
    try:
        return ACTION_INTENT_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"Action intent does not match a registered schema: {exc}") from exc


class BackgroundAgentChoice(ContractModel):
    """Closed background self-care choice made by the trusted agent policy."""

    entity_id: UUID
    action: Literal["continue", "eat", "drink", "rest"]
    resource_id: ResourceName | None = None
    quantity: ExactDecimal | None = Field(default=None, gt=Decimal("0"))
    account_id: UUID | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def choice_matches_resource_fields(self) -> BackgroundAgentChoice:
        consumes = self.action in {"eat", "drink"}
        has_resource = self.resource_id is not None or self.quantity is not None
        if consumes != has_resource or consumes and (self.resource_id is None or self.quantity is None):
            raise ValueError("only eat/drink choices require a resource id and positive quantity")
        if not consumes and self.account_id is not None:
            raise ValueError("only eat/drink choices may select a stock account")
        return self


class RuleContext(ContractModel):
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    include_actor_in_background: bool = False
    background_routine_mode: bool = False
    background_choices: list[BackgroundAgentChoice] = Field(default_factory=list, max_length=100_000)
    accusation_repair_requested_by: UUID | None = None
    accusation_repair_trust_proof: AccusationRepairTrustProof | None = None
    accusation_repair_key_review: AccusationRepairKeyReview | None = None
    simulation_command_id: UUID | None = None
    travelling_window: bool = False
    continuous_training: bool = False
    explicit_training_window: tuple[int, int] | None = None

    @model_validator(mode="after")
    def sorted_unique_background_choices(self) -> RuleContext:
        entity_ids = [choice.entity_id for choice in self.background_choices]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("background choices cannot repeat an entity")
        if entity_ids != sorted(entity_ids, key=str):
            raise ValueError("background choices must use stable UUID order")
        if self.actor_id in entity_ids and not self.include_actor_in_background:
            raise ValueError("background choice cannot target the acting character")
        return self


class RuleOutcome(ContractModel):
    status: Literal["succeeded", "partial", "failed", "rejected"]
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(max_length=1000)
    details: JsonObject = Field(default_factory=dict)


class RuleInterruption(ContractModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)
    at_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)


class RuleRoll(ContractModel):
    label: str = Field(min_length=1, max_length=120)
    value: int = Field(ge=1)
    maximum: int = Field(gt=0)
    outcome: str = Field(min_length=1, max_length=120)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RuleResolution(ContractModel):
    events: list[DomainEvent] = Field(default_factory=list)
    outcome: RuleOutcome
    elapsed_ticks: int = Field(default=0, ge=0, le=2**63 - 1)
    interruptions: list[RuleInterruption] = Field(default_factory=list, max_length=1000)
    rolls: list[RuleRoll] = Field(default_factory=list, max_length=1000)
    ruleset_version: str = Field(min_length=1, max_length=100)
    _validated_projection: object | None = PrivateAttr(default=None)

