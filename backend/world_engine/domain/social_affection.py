"""Private, provenance-bound affection effects from gifts and partnerships.

These helpers accept a validated event and the world state immediately before
that event is applied. They return the complete relation list only when a
social-world effect is safe to settle; callers can leave legacy worlds and
unsupported relationship rows unchanged by handling ``None`` as no update.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .components import LifeComponent, RelationRecord, VisibilityScope
from .private_gifts import PrivateGiftChangedEvent
from .relationship_partnership_events import RelationshipPartnershipChangedEvent
from .relationship_policy import INITIAL_RELATIONSHIP_SCORE, settle_relationship_change

if TYPE_CHECKING:
    from .models import WorldState


SOCIAL_WORLD_POLICY_ID = "social_world_v1"
AFFECTION_DIMENSION = "affection"
_PRIVATE_GIFT_AFFECTION_DELTA = 2
_PARTNERSHIP_AFFECTION_DELTA = 8


def private_gift_affection_update(
    state: WorldState,
    event: PrivateGiftChangedEvent,
) -> list[RelationRecord] | None:
    """Return relations after one accepted private gift raises recipient affection.

    The routine class is every prior accepted gift in the same recipient-to-
    giver direction, regardless of wording, resource, or quantity. Gift
    contents remain private through private or participant-scoped relations.
    """
    if state.metadata.get(SOCIAL_WORLD_POLICY_ID) is not True:
        return None
    if not isinstance(event, PrivateGiftChangedEvent) or event.operation != "accept":
        return None
    if event.offer.status != "accepted" or event.transfer is None or not _matches_pending_gift(state, event):
        return None

    recipient_id = event.offer.recipient_id
    giver_id = event.offer.giver_id
    prior_accepts = {
        gift.gift_id
        for gift in state.private_gifts
        if gift.gift_id != event.offer.gift_id
        and gift.status == "accepted"
        and gift.recipient_id == recipient_id
        and gift.giver_id == giver_id
    }
    return _settle_affection_directions(
        state,
        event.world_id,
        event.event_id,
        ((recipient_id, giver_id, _PRIVATE_GIFT_AFFECTION_DELTA, False, len(prior_accepts) + 1),),
    )


def partnership_affection_update(
    state: WorldState,
    event: RelationshipPartnershipChangedEvent,
) -> list[RelationRecord] | None:
    """Return relations after a consented adult partnership acceptance.

    The first accepted partnership for an unordered pair is a verified key
    event in both directions. Later acceptances for that pair receive ordinary
    routine weight with geometric decay. Declines, withdrawals, proposals, and
    endings never create a negative or positive affection effect.
    """
    if state.metadata.get(SOCIAL_WORLD_POLICY_ID) is not True:
        return None
    if (
        not isinstance(event, RelationshipPartnershipChangedEvent)
        or event.operation != "accept"
        or event.resulting_partnership.status != "active"
        or not _matches_pending_partnership(state, event)
    ):
        return None

    partnership = event.resulting_partnership
    if not _is_active_adult(state, partnership.proposer_id) or not _is_active_adult(state, partnership.partner_id):
        return None

    pair = frozenset((partnership.proposer_id, partnership.partner_id))
    prior_acceptances = sum(
        1
        for previous in state.relationship_partnerships
        if previous.partnership_id != partnership.partnership_id
        and frozenset((previous.proposer_id, previous.partner_id)) == pair
        and previous.status in {"active", "ended"}
    )
    first_acceptance = prior_acceptances == 0
    routine_occurrence = None if first_acceptance else prior_acceptances + 1
    return _settle_affection_directions(
        state,
        event.world_id,
        event.event_id,
        (
            (partnership.proposer_id, partnership.partner_id, _PARTNERSHIP_AFFECTION_DELTA,
             first_acceptance, routine_occurrence),
            (partnership.partner_id, partnership.proposer_id, _PARTNERSHIP_AFFECTION_DELTA,
             first_acceptance, routine_occurrence),
        ),
    )


def _matches_pending_gift(state: WorldState, event: PrivateGiftChangedEvent) -> bool:
    matches = [gift for gift in state.private_gifts if gift.gift_id == event.offer.gift_id]
    if len(matches) != 1:
        return False
    current = matches[0]
    if current.status != "pending" or current.revision != event.expected_revision:
        return False
    expected = current.model_copy(update={
        "status": "accepted",
        "revision": current.revision + 1,
        "resolved_tick": state.current_tick,
    })
    return event.offer == expected and event.offer.resolved_tick == state.current_tick


def _matches_pending_partnership(state: WorldState, event: RelationshipPartnershipChangedEvent) -> bool:
    result = event.resulting_partnership
    matches = [
        partnership for partnership in state.relationship_partnerships
        if partnership.partnership_id == result.partnership_id
    ]
    if len(matches) != 1:
        return False
    current = matches[0]
    if (
        current.status != "pending"
        or current.revision != event.expected_revision
        or current.current_proposal_recipient_id != event.actor_id
    ):
        return False
    expected = current.model_copy(update={
        "status": "active",
        "revision": current.revision + 1,
        "updated_tick": state.current_tick,
    })
    return result == expected


def _is_active_adult(state: WorldState, actor_id: UUID) -> bool:
    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        return False
    try:
        life = LifeComponent.model_validate(actor.components.get("life"))
        year_ticks = state.calendar.day_seconds * state.calendar.year_days
        age_years = (state.current_tick - life.birth_tick) // year_ticks
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return life.life_stage in {"adult", "elder"} and age_years >= 18


def _settle_affection_directions(
    state: WorldState,
    identity_scope: UUID,
    source_event_id: UUID,
    effects: tuple[tuple[UUID, UUID, int, bool, int | None], ...],
) -> list[RelationRecord] | None:
    """Merge safe directional changes while retaining every existing baseline.

    Preserve public and incompatible rows as authored. Applying a private
    adjustment to an externally visible score would disclose the interaction,
    while creating a second direction would make existing consumers ambiguous.
    New rows are pair-private and have deterministic IDs; no conflicting row
    is ever duplicated.
    """
    current_relations = list(state.relations)
    planned: list[tuple[tuple[UUID, UUID], RelationRecord | None, RelationRecord | None]] = []
    requested_edges = {(from_id, to_id) for from_id, to_id, *_ in effects}
    if len(requested_edges) != len(effects):
        return None

    for from_id, to_id, proposed_delta, verified_key, occurrence in effects:
        edge = from_id, to_id
        matches = [row for row in current_relations if (row.from_id, row.to_id) == edge]
        if len(matches) > 1:
            return None
        existing = matches[0] if matches else None
        relation_id = _relation_id(identity_scope, from_id, to_id)
        if existing is not None:
            if not _supports_private_adjustment(existing, from_id, to_id):
                return None
            if source_event_id in existing.evidence_refs or len(existing.evidence_refs) >= 10_000:
                return None
            before = existing.dimensions.get(AFFECTION_DIMENSION, INITIAL_RELATIONSHIP_SCORE)
        else:
            if any(row.id == relation_id for row in current_relations):
                return None
            before = INITIAL_RELATIONSHIP_SCORE

        result = settle_relationship_change(
            before,
            proposed_delta,
            verified_key_evidence=verified_key,
            routine_occurrence=occurrence,
        )
        if result.applied_delta == 0:
            planned.append((edge, existing, None))
            continue

        if existing is None:
            changed = RelationRecord(
                id=relation_id,
                from_id=from_id,
                to_id=to_id,
                dimensions={AFFECTION_DIMENSION: result.resulting_score},
                evidence_refs=[source_event_id],
                updated_tick=state.current_tick,
                visibility=VisibilityScope(
                    policy="subject_ids",
                    subject_ids=sorted((from_id, to_id), key=str),
                ),
            )
        else:
            changed = existing.model_copy(update={
                "dimensions": {**existing.dimensions, AFFECTION_DIMENSION: result.resulting_score},
                "evidence_refs": [*existing.evidence_refs, source_event_id],
                "updated_tick": state.current_tick,
                "revision": existing.revision + 1,
            })
        planned.append((edge, existing, changed))

    updated = list(current_relations)
    changed_any = False
    for edge, existing, changed in planned:
        if changed is None:
            continue
        changed_any = True
        if existing is None:
            updated.append(changed)
        else:
            index = next(i for i, row in enumerate(updated) if row.id == existing.id)
            updated[index] = changed
    return updated if changed_any else None


def _supports_private_adjustment(relation: RelationRecord, from_id: UUID, to_id: UUID) -> bool:
    """Reject unknown/broader scoped audiences while retaining legacy baselines."""
    if relation.visibility.policy not in {"private", "subject_ids"}:
        return False
    participants = {from_id, to_id}
    return set(relation.visibility.subject_ids).issubset(participants)


def _relation_id(identity_scope: UUID, from_id: UUID, to_id: UUID) -> UUID:
    return uuid5(identity_scope, f"relation:{from_id}:{to_id}")
