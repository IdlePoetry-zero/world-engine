"""Explicit public-accusation repair offers and target-owned responses."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence
from uuid import UUID, uuid5

from .errors import InvalidEvent
from .gameplay_events import (
    AccusationRepairKeyReview,
    AccusationRepairOfferRecordedEvent,
    AccusationRepairResolvedEvent,
    AccusationRepairTrustProof,
    AccusationRepairTrustRecovery,
    RelationDimensionChangedEvent,
)
from .investigation import entity_visible_to_actor
from .investigation_cases import AccusationRepairOffer, RepairOperation
from .models import WorldState
from .relationship_policy import settle_relationship_change


_UNAVAILABLE = "Accusation repair is unavailable."


def accusation_recovery_delta(state: WorldState, current: Decimal, proof: AccusationRepairTrustProof,
                              key_review: AccusationRepairKeyReview | None) -> int:
    from_loss = int((proof.baseline_trust - proof.post_effect_trust) / 3)
    to_baseline = max(0, int(proof.baseline_trust - current))
    proposed = min(from_loss, to_baseline)
    if state.metadata.get("social_world_v1") is not True:
        if key_review is not None:
            raise InvalidEvent(_UNAVAILABLE)
        return proposed
    if key_review is not None and (from_loss < 3 or current.copy_abs() < 76 or proposed == 0):
        raise InvalidEvent(_UNAVAILABLE)
    return int(settle_relationship_change(current, proposed, verified_key_evidence=key_review is not None).applied_delta)


def plan_accusation_repair_offer(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID,
    correction_event_id: UUID,
    statement: str,
) -> AccusationRepairOfferRecordedEvent:
    """Plan one immutable authored explanation, with no case or relation change."""
    correction = next((
        item for item in state.public_accusation_corrections
        if item.event_id == correction_event_id
    ), None)
    if correction is None or correction.actor_id != actor_id:
        raise InvalidEvent(_UNAVAILABLE)
    case_file = next((item for item in state.investigation_cases if item.id == correction.case_file_id), None)
    actor = state.entities.get(actor_id)
    suspect = state.entities.get(correction.suspect_id)
    location = state.entities.get(actor.location_id) if actor is not None and actor.location_id is not None else None
    if (
        case_file is None
        or case_file.owner_id != actor_id
        or case_file.status != "closed"
        or case_file.outcome != "disconfirmed"
        or not case_file.public_accusation
        or case_file.suspect_id != correction.suspect_id
        or case_file.revision != correction.expected_revision
        or correction.corrected_tick > state.current_tick
        or any(item.correction_event_id == correction_event_id for item in state.accusation_repair_offers)
        or actor is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or suspect is None
        or suspect.kind not in {"character", "person"}
        or suspect.status != "active"
        or actor.location_id is None
        or actor.location_id != suspect.location_id
        or location is None
        or location.kind not in {"place", "location"}
        or location.status != "active"
        or not entity_visible_to_actor(state, actor_id, suspect.id)
    ):
        raise InvalidEvent(_UNAVAILABLE)

    event_id = uuid5(command_id, f"{ruleset_version}:accusation_repair.offer")
    offer = AccusationRepairOffer(
        offer_id=event_id,
        revision=1,
        correction_event_id=correction.event_id,
        source_closure_event_id=correction.source_closure_event_id,
        case_file_id=correction.case_file_id,
        case_revision=correction.expected_revision,
        accuser_id=actor_id,
        suspect_id=suspect.id,
        statement=statement,
        offered_tick=state.current_tick,
        updated_tick=state.current_tick,
        status="pending",
        last_operation=None,
    )
    return AccusationRepairOfferRecordedEvent(
        event_id=event_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        record=offer,
    )


def plan_accusation_repair_resolution(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID,
    offer_id: UUID,
    expected_revision: int,
    operation: RepairOperation,
    requested_by: UUID | None = None,
    trust_proof: AccusationRepairTrustProof | None = None,
    key_review: AccusationRepairKeyReview | None = None,
) -> tuple[AccusationRepairResolvedEvent, RelationDimensionChangedEvent | None]:
    """Plan the target's explicit response and, on acceptance only, capped trust recovery."""
    if operation not in {"accept", "decline", "defer"}:
        raise InvalidEvent(_UNAVAILABLE)
    offer = next((item for item in state.accusation_repair_offers if item.offer_id == offer_id), None)
    if (
        offer is None
        or offer.status != "pending"
        or offer.revision != expected_revision
        or actor_id != offer.suspect_id
        or requested_by is not None and requested_by != offer.accuser_id
    ):
        raise InvalidEvent(_UNAVAILABLE)
    accuser = state.entities.get(offer.accuser_id)
    suspect = state.entities.get(offer.suspect_id)
    if (
        accuser is None or accuser.kind not in {"character", "person"} or accuser.status != "active"
        or suspect is None or suspect.kind not in {"character", "person"} or suspect.status != "active"
    ):
        raise InvalidEvent(_UNAVAILABLE)
    if trust_proof is not None and (
        operation != "accept"
        or trust_proof.case_file_id != offer.case_file_id
        or trust_proof.source_closure_event_id != offer.source_closure_event_id
        or trust_proof.accuser_id != offer.accuser_id
        or trust_proof.suspect_id != offer.suspect_id
    ):
        raise InvalidEvent(_UNAVAILABLE)

    event_id = uuid5(
        command_id,
        f"{ruleset_version}:accusation_repair.{operation}:{offer.offer_id}",
    )
    status = {"accept": "accepted", "decline": "declined", "defer": "pending"}[operation]
    resulting_offer = offer.model_copy(update={
        "revision": offer.revision + 1,
        "updated_tick": state.current_tick,
        "status": status,
        "last_operation": operation,
    })

    if key_review is not None and (requested_by is None or trust_proof is None):
        raise InvalidEvent(_UNAVAILABLE)
    relation_event = None
    recovery = None
    if operation == "accept" and trust_proof is not None:
        directed = [
            item for item in state.relations
            if item.from_id == offer.suspect_id and item.to_id == offer.accuser_id
        ]
        applied_delta = 0
        if len(directed) == 1:
            relation = directed[0]
            expected_previous = relation.dimensions.get("trust")
            current_trust = expected_previous if expected_previous is not None else Decimal("0")
            applied_delta = accusation_recovery_delta(state, current_trust, trust_proof, key_review)
            if applied_delta > 0:
                next_trust = current_trust + Decimal(applied_delta)
                relation_event = RelationDimensionChangedEvent(
                    event_id=uuid5(
                        command_id,
                        f"{ruleset_version}:accusation_repair.trust_recovery:{offer.offer_id}",
                    ),
                    relation_id=relation.id,
                    dimension="trust",
                    expected_previous=expected_previous,
                    value=next_trust,
                    evidence_refs=[event_id],
                    visibility={
                        "policy": "subject_ids",
                        "subject_ids": sorted((str(offer.accuser_id), str(offer.suspect_id))),
                    },
                )
        recovery = AccusationRepairTrustRecovery(
            proof=trust_proof,
            applied_delta=applied_delta,
            key_review=key_review,
        )

    response = AccusationRepairResolvedEvent(
        event_id=event_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        requested_by=requested_by,
        operation=operation,
        expected_revision=expected_revision,
        resulting_offer=resulting_offer,
        trust_recovery=recovery,
    )
    return response, relation_event
