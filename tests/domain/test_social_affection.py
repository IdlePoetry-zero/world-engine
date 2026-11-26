from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from world_engine.domain.components import (
    LifeComponent,
    RelationRecord,
    VisibilityScope,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.private_gifts import (
    PrivateGiftChangedEvent,
    PrivateGiftLotTransfer,
    PrivateGiftOffer,
    PrivateGiftTransferEvidence,
    private_gift_event_id,
    private_gift_visibility,
)
from world_engine.domain.relationship_partnership_events import (
    RelationshipPartnershipChangedEvent,
    partnership_event_id,
    partnership_visibility,
)
from world_engine.domain.relationship_partnerships import RelationshipPartnership
from world_engine.domain.social_affection import (
    AFFECTION_DIMENSION,
    partnership_affection_update,
    private_gift_affection_update,
)


WORLD_ID = UUID(int=100)
BRANCH_ID = UUID(int=101)
PLACE_ID = UUID(int=102)
GIVER_ID = UUID(int=103)
RECIPIENT_ID = UUID(int=104)
RULESET = "social-affection-test.v1"


def uid(value: int) -> UUID:
    return UUID(int=value)


def _world(
    *,
    social_world: bool = True,
    relations: list[RelationRecord] | None = None,
    gifts: list[PrivateGiftOffer] | None = None,
    partnerships: list[RelationshipPartnership] | None = None,
    age_years: int = 20,
) -> WorldState:
    state = WorldState(
        current_tick=0,
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Workshop"),
            GIVER_ID: WorldEntity(
                id=GIVER_ID,
                kind="character",
                name="Giver",
                status="active",
                location_id=PLACE_ID,
                components={"life": LifeComponent(
                    birth_tick=-age_years * 360 * 86_400,
                    life_stage="adult",
                ).model_dump(mode="json")},
            ),
            RECIPIENT_ID: WorldEntity(
                id=RECIPIENT_ID,
                kind="character",
                name="Recipient",
                status="active",
                location_id=PLACE_ID,
                components={"life": LifeComponent(
                    birth_tick=-age_years * 360 * 86_400,
                    life_stage="adult",
                ).model_dump(mode="json")},
            ),
        },
        relations=relations or [],
        private_gifts=gifts or [],
        relationship_partnerships=partnerships or [],
        metadata={"social_world_v1": True} if social_world else {},
    )
    return state


def _gift_offer(
    gift_id: UUID,
    *,
    giver_id: UUID = GIVER_ID,
    recipient_id: UUID = RECIPIENT_ID,
    quantity: str = "1",
    resource_id: str = "core.herb",
    status: str = "pending",
    revision: int = 1,
    offered_tick: int = 0,
    resolved_tick: int | None = None,
) -> PrivateGiftOffer:
    return PrivateGiftOffer(
        gift_id=gift_id,
        giver_id=giver_id,
        recipient_id=recipient_id,
        place_id=PLACE_ID,
        resource_id=resource_id,
        quantity=quantity,
        offered_tick=offered_tick,
        expires_at_tick=max(10, offered_tick + 1),
        revision=revision,
        status=status,
        resolved_tick=resolved_tick,
        source_account_id=uid(200 + gift_id.int % 100),
    )


def _accepted_gift_event(offer: PrivateGiftOffer, *, command_id: UUID | None = None) -> PrivateGiftChangedEvent:
    command = command_id or uid(300)
    accepted = offer.model_copy(update={
        "status": "accepted",
        "revision": offer.revision + 1,
        "resolved_tick": 0,
    })
    transfer = PrivateGiftTransferEvidence(
        from_account_id=offer.source_account_id,
        to_account_id=uid(500),
        resource_id=offer.resource_id,
        quantity=offer.quantity,
        source_balance_before=offer.quantity,
        source_inventory_revision=1,
        destination_balance_before=Decimal("0"),
        destination_account_created=True,
        lots=(PrivateGiftLotTransfer(
            source_lot_id=uid(501),
            received_lot_id=uid(502),
            quantity=offer.quantity,
        ),),
    )
    return PrivateGiftChangedEvent(
        event_id=private_gift_event_id(command, RULESET, "accept", offer.gift_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command,
        actor_id=offer.recipient_id,
        ruleset_version=RULESET,
        operation="accept",
        expected_revision=offer.revision,
        offer=accepted,
        transfer=transfer,
        visibility=private_gift_visibility(offer.giver_id, offer.recipient_id),
    )


def _relation(
    relation_id: UUID,
    from_id: UUID,
    to_id: UUID,
    *,
    affection: str | None = None,
    visibility: str = "subject_ids",
    evidence_refs: list[UUID] | None = None,
    **dimensions: str,
) -> RelationRecord:
    values = dict(dimensions)
    if affection is not None:
        values[AFFECTION_DIMENSION] = affection
    scope = (
        VisibilityScope(policy="subject_ids", subject_ids=sorted((from_id, to_id), key=str))
        if visibility == "subject_ids"
        else VisibilityScope(policy=visibility)
    )
    return RelationRecord(
        id=relation_id,
        from_id=from_id,
        to_id=to_id,
        dimensions=values,
        label="Existing relationship view",
        evidence_refs=evidence_refs or [],
        visibility=scope,
    )


def _pending_partnership(partnership_id: UUID = uid(600)) -> RelationshipPartnership:
    return RelationshipPartnership(
        partnership_id=partnership_id,
        proposer_id=GIVER_ID,
        partner_id=RECIPIENT_ID,
        status="pending",
        proposed_tick=0,
        updated_tick=0,
        revision=1,
    )


def _accepted_partnership_event(
    current: RelationshipPartnership,
    *,
    command_id: UUID = uid(601),
) -> RelationshipPartnershipChangedEvent:
    accepted = current.model_copy(update={
        "status": "active",
        "revision": current.revision + 1,
        "updated_tick": 0,
    })
    return RelationshipPartnershipChangedEvent(
        event_id=partnership_event_id(command_id, RULESET, "accept", current.partnership_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=current.current_proposal_recipient_id,
        ruleset_version=RULESET,
        operation="accept",
        expected_revision=current.revision,
        resulting_partnership=accepted,
        visibility=partnership_visibility(current.proposer_id, current.partner_id),
    )


def test_accepted_gift_uses_same_directed_pair_history_and_merges_old_dimensions() -> None:
    current = _gift_offer(uid(610), quantity="2.5")
    event = _accepted_gift_event(current)
    prior_gifts = [
        _gift_offer(uid(611), quantity="8", resource_id="core.silk", status="accepted", revision=2,
                    offered_tick=-2, resolved_tick=0),
        _gift_offer(uid(612), giver_id=RECIPIENT_ID, recipient_id=GIVER_ID,
                    status="accepted", revision=2, offered_tick=-2, resolved_tick=0),
        _gift_offer(uid(613), status="declined", revision=2, offered_tick=-2, resolved_tick=0),
        current,
    ]
    source = _relation(
        uid(614), RECIPIENT_ID, GIVER_ID, affection="10", familiarity="7", trust="35",
        evidence_refs=[uid(615)],
    )
    state = _world(relations=[source], gifts=prior_gifts)

    updated = private_gift_affection_update(state, event)

    assert updated is not None
    result = next(row for row in updated if row.id == source.id)
    assert result.dimensions == {
        "affection": Decimal("11.000000"),
        "familiarity": Decimal("7"),
        "trust": Decimal("35"),
    }
    assert result.label == source.label
    assert result.visibility == source.visibility
    assert result.evidence_refs == [uid(615), event.event_id]
    assert result.revision == source.revision + 1


def test_new_gift_direction_is_private_and_uses_deterministic_identity() -> None:
    current = _gift_offer(uid(620))
    event = _accepted_gift_event(current)
    updated = private_gift_affection_update(_world(gifts=[current]), event)

    assert updated is not None and len(updated) == 1
    relation, = updated
    assert relation.id == uuid5(WORLD_ID, f"relation:{RECIPIENT_ID}:{GIVER_ID}")
    assert (relation.from_id, relation.to_id) == (RECIPIENT_ID, GIVER_ID)
    assert relation.dimensions[AFFECTION_DIMENSION] == Decimal("2.000000")
    assert relation.visibility.policy == "subject_ids"
    assert relation.visibility.subject_ids == sorted((RECIPIENT_ID, GIVER_ID), key=str)
    assert relation.evidence_refs == [event.event_id]


def test_public_baseline_cannot_disclose_a_private_gift_effect() -> None:
    current = _gift_offer(uid(630))
    event = _accepted_gift_event(current)
    public = _relation(uid(631), RECIPIENT_ID, GIVER_ID, affection="12", visibility="public")
    public_state = _world(relations=[public], gifts=[current])
    assert private_gift_affection_update(public_state, event) is None
    assert public_state.relations == [public]

    duplicate = _relation(uid(632), RECIPIENT_ID, GIVER_ID, affection="5")
    duplicate_state = _world(relations=[duplicate, duplicate.model_copy(update={"id": uid(633)})], gifts=[current])
    assert private_gift_affection_update(duplicate_state, event) is None


def test_absent_social_flag_keeps_legacy_gift_behavior() -> None:
    current = _gift_offer(uid(634))
    event = _accepted_gift_event(current)
    baseline = _relation(uid(635), RECIPIENT_ID, GIVER_ID, affection="10")
    state = _world(social_world=False, relations=[baseline], gifts=[current])

    assert private_gift_affection_update(state, event) is None
    assert state.relations == [baseline]


def test_first_adult_partnership_is_a_two_direction_key_event_and_preserves_baselines() -> None:
    current = _pending_partnership()
    event = _accepted_partnership_event(current)
    forward = _relation(uid(640), GIVER_ID, RECIPIENT_ID, affection="79.5", trust="17")
    reverse = _relation(uid(641), RECIPIENT_ID, GIVER_ID, affection="79.5", respect="22")
    state = _world(relations=[forward, reverse], partnerships=[current])

    updated = partnership_affection_update(state, event)

    assert updated is not None
    scores = {(row.from_id, row.to_id): row.dimensions[AFFECTION_DIMENSION] for row in updated}
    assert scores[(GIVER_ID, RECIPIENT_ID)] == Decimal("87.500000")
    assert scores[(RECIPIENT_ID, GIVER_ID)] == Decimal("87.500000")
    assert next(row for row in updated if row.id == forward.id).dimensions["trust"] == Decimal("17")
    assert next(row for row in updated if row.id == reverse.id).dimensions["respect"] == Decimal("22")
    assert next(row for row in updated if row.id == forward.id).evidence_refs == [event.event_id]
    assert next(row for row in updated if row.id == reverse.id).evidence_refs == [event.event_id]


def test_repeated_pair_acceptance_is_ordinary_and_endings_have_no_penalty() -> None:
    current = _pending_partnership(uid(650))
    event = _accepted_partnership_event(current, command_id=uid(651))
    previous = RelationshipPartnership(
        partnership_id=uid(652),
        proposer_id=RECIPIENT_ID,
        partner_id=GIVER_ID,
        status="ended",
        proposed_tick=0,
        updated_tick=0,
        revision=3,
    )
    state = _world(partnerships=[previous, current])

    updated = partnership_affection_update(state, event)

    assert updated is not None
    assert len(updated) == 2
    assert all(row.dimensions[AFFECTION_DIMENSION] == Decimal("4.000000") for row in updated)

    active = current.model_copy(update={"status": "active", "revision": 2})
    ended = active.model_copy(update={"status": "ended", "revision": 3})
    end_command = uid(653)
    end_event = RelationshipPartnershipChangedEvent(
        event_id=partnership_event_id(end_command, RULESET, "end", active.partnership_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=end_command,
        actor_id=GIVER_ID,
        ruleset_version=RULESET,
        operation="end",
        expected_revision=active.revision,
        resulting_partnership=ended,
        visibility=partnership_visibility(active.proposer_id, active.partner_id),
    )
    ending_state = _world(partnerships=[active])
    assert partnership_affection_update(ending_state, end_event) is None


def test_partnership_requires_current_formal_acceptance_and_adulthood() -> None:
    current = _pending_partnership(uid(660))
    event = _accepted_partnership_event(current, command_id=uid(661))
    assert partnership_affection_update(_world(age_years=17, partnerships=[current]), event) is None
    assert partnership_affection_update(_world(partnerships=[]), event) is None

    state_without_opt_in = _world(social_world=False, partnerships=[current])
    assert partnership_affection_update(state_without_opt_in, event) is None
