"""Pure, consent-preserving transitions for explicit romantic partnerships."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, TypeAdapter, model_validator

from .errors import InvalidEvent
from .relationship_invitations import _require_active_adult_pair

if TYPE_CHECKING:
    from .models import WorldState


_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
Tick = Annotated[int, Field(strict=True, ge=_MIN_TICK, le=_MAX_TICK)]
_MAX_REVISION = 2**63 - 1

RelationshipPartnershipStatus = Literal["pending", "active", "declined", "withdrawn", "ended"]
RelationshipPartnershipOperation = Literal["propose", "revise", "counter", "accept", "decline", "withdraw", "end"]
PartnershipTerm = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400, strict=True)]
PartnershipTerms = Annotated[tuple[PartnershipTerm, ...], Field(max_length=6)]
_PARTNERSHIP_TERMS_ADAPTER = TypeAdapter(PartnershipTerms)


class RelationshipPartnership(BaseModel):
    """One explicit proposal or mutual acceptance between two adults.

    Active status represents only the continuing, mutually accepted
    relationship. It does not imply consent to any particular interaction.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    partnership_id: UUID
    proposer_id: UUID
    partner_id: UUID
    status: RelationshipPartnershipStatus = "pending"
    proposed_tick: Tick
    updated_tick: Tick
    revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    proposal_terms: PartnershipTerms = Field(default=(), exclude_if=lambda terms: not terms)
    proposal_author_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def current_proposal_author_id(self) -> UUID:
        """Current author of the latest terms, with legacy records defaulting to proposer."""

        return self.proposal_author_id or self.proposer_id

    @property
    def current_proposal_recipient_id(self) -> UUID:
        """The other participant who can accept or decline the latest terms."""

        return self.partner_id if self.current_proposal_author_id == self.proposer_id else self.proposer_id

    @model_validator(mode="after")
    def valid_identity_and_lifecycle(self) -> RelationshipPartnership:
        if self.proposer_id == self.partner_id:
            raise ValueError("a relationship partnership needs two distinct participants")
        if self.proposal_author_id is not None and self.proposal_author_id not in {self.proposer_id, self.partner_id}:
            raise ValueError("the current proposal author must be one of the original participants")
        if self.updated_tick < self.proposed_tick:
            raise ValueError("a partnership cannot be updated before it was proposed")
        if self.status == "pending":
            if self.revision == 1 and self.updated_tick != self.proposed_tick:
                raise ValueError("an initial pending partnership must retain its original proposal tick")
            if self.revision == 1 and self.proposal_author_id is not None:
                raise ValueError("an original pending partnership must use the legacy proposal-author marker")
            if self.revision > 1 and (self.proposal_author_id is None or not self.proposal_terms):
                raise ValueError("a revised pending partnership must identify its latest author and full terms")
        if self.status in {"active", "declined", "withdrawn"} and self.revision < 2:
            raise ValueError("a resolved partnership proposal must have at least one transition revision")
        if self.status == "ended" and self.revision < 3:
            raise ValueError("an ended partnership must have been activated first")
        return self


def eligible_partnership_proposal_ids(
    state: WorldState, actor_id: UUID, visible_ids: Iterable[UUID],
) -> list[UUID]:
    """Find valid proposal targets inside an already authorized scene."""
    open_partners = {
        row.partner_id if row.proposer_id == actor_id else row.proposer_id
        for row in state.relationship_partnerships
        if row.status in {"pending", "active"} and actor_id in {row.proposer_id, row.partner_id}
    }
    eligible = []
    for other_id in visible_ids:
        if other_id == actor_id or other_id in open_partners:
            continue
        other = state.entities.get(other_id)
        if other is None or other.kind not in {"character", "person"}:
            continue
        try:
            _require_active_adult_pair(state, actor_id, other_id)
        except InvalidEvent:
            continue
        eligible.append(other_id)
    return eligible


def plan_relationship_partnership(
    state: WorldState,
    *,
    operation: RelationshipPartnershipOperation,
    partnership_id: UUID,
    actor_id: UUID,
    partner_id: UUID | None = None,
    expected_revision: int = 0,
    proposal_terms: PartnershipTerms = (),
) -> RelationshipPartnership:
    """Plan one explicit partnership transition without mutating world state."""

    _require_state(state)
    if not isinstance(operation, str) or operation not in {
        "propose", "revise", "counter", "accept", "decline", "withdraw", "end",
    }:
        raise InvalidEvent("Partnership operation is invalid.")
    if not isinstance(partnership_id, UUID):
        raise InvalidEvent("Partnership ID is invalid.")
    if not isinstance(actor_id, UUID):
        raise InvalidEvent("Partnership actor ID is invalid.")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or not 0 <= expected_revision <= _MAX_REVISION:
        raise InvalidEvent("Partnership expected_revision is invalid.")
    try:
        terms = _PARTNERSHIP_TERMS_ADAPTER.validate_python(proposal_terms)
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Partnership proposal terms are invalid.") from exc

    records = _validated_records(state.relationship_partnerships)
    if operation == "propose":
        if expected_revision != 0:
            raise InvalidEvent("A new partnership proposal must use expected_revision zero.")
        if not isinstance(partner_id, UUID):
            raise InvalidEvent("A partnership proposal requires a partner ID.")
        if partner_id == actor_id:
            raise InvalidEvent("A partnership requires two distinct participants.")
        if any(record.partnership_id == partnership_id for record in records):
            raise InvalidEvent("Partnership ID already exists.")
        if any(
            _same_pair(record, actor_id, partner_id) and record.status in {"pending", "active"}
            for record in records
        ):
            raise InvalidEvent("This pair already has a pending or active partnership.")
        _require_active_adult_pair(state, actor_id, partner_id)
        return RelationshipPartnership(
            partnership_id=partnership_id,
            proposer_id=actor_id,
            partner_id=partner_id,
            status="pending",
            proposed_tick=state.current_tick,
            updated_tick=state.current_tick,
            revision=1,
            proposal_terms=terms,
        )

    if partner_id is not None:
        raise InvalidEvent("Partner ID is only supplied when proposing a partnership.")
    if operation in {"revise", "counter"}:
        if not terms:
            raise InvalidEvent("A partnership proposal update must supply the full nonempty terms list.")
    elif terms:
        raise InvalidEvent("Only a proposal, revision, or counterproposal can supply partnership terms.")
    partnership = _current_partnership(state, records, partnership_id)
    _require_transition_state(state, partnership, actor_id, expected_revision)
    _require_active_actor(state, actor_id)

    if operation in {"revise", "counter"}:
        if partnership.status != "pending":
            raise InvalidEvent("Only a pending partnership proposal can be revised or countered.")
        if terms == partnership.proposal_terms:
            raise InvalidEvent("A partnership proposal update must change the current terms.")
        _require_active_adult_pair(state, partnership.proposer_id, partnership.partner_id)
        if operation == "revise":
            if actor_id != partnership.current_proposal_author_id:
                raise InvalidEvent("Only the current proposal author can revise the partnership terms.")
        elif actor_id != partnership.current_proposal_recipient_id:
            raise InvalidEvent("Only the current proposal recipient can counter the partnership terms.")
        return _proposal_revision(partnership, actor_id, terms, state.current_tick)

    if operation == "accept":
        if partnership.status != "pending":
            raise InvalidEvent("Only a pending partnership proposal can be accepted.")
        if actor_id != partnership.current_proposal_recipient_id:
            raise InvalidEvent("Only the current proposal recipient can accept this partnership.")
        _require_active_adult_pair(state, partnership.proposer_id, partnership.partner_id)
        return _transition(partnership, "active", state.current_tick)

    if operation == "decline":
        if partnership.status != "pending":
            raise InvalidEvent("Only a pending partnership proposal can be declined.")
        if actor_id != partnership.current_proposal_recipient_id:
            raise InvalidEvent("Only the current proposal recipient can decline this partnership.")
        return _transition(partnership, "declined", state.current_tick)

    if operation == "withdraw":
        if partnership.status != "pending":
            raise InvalidEvent("Only a pending partnership proposal can be withdrawn.")
        if actor_id != partnership.current_proposal_author_id:
            raise InvalidEvent("Only the current proposal author can withdraw this partnership proposal.")
        return _transition(partnership, "withdrawn", state.current_tick)

    if operation == "end":
        if partnership.status != "active":
            raise InvalidEvent("Only an active partnership can be ended.")
        if actor_id not in {partnership.proposer_id, partnership.partner_id}:
            raise InvalidEvent("Only a partnership participant can end it.")
        return _transition(partnership, "ended", state.current_tick)

    raise InvalidEvent("Partnership operation is invalid.")


def _transition(
    partnership: RelationshipPartnership,
    status: Literal["active", "declined", "withdrawn", "ended"],
    updated_tick: int,
) -> RelationshipPartnership:
    if partnership.revision >= _MAX_REVISION:
        raise InvalidEvent("Partnership revision has reached its supported limit.")
    return RelationshipPartnership(
        partnership_id=partnership.partnership_id,
        proposer_id=partnership.proposer_id,
        partner_id=partnership.partner_id,
        status=status,
        proposed_tick=partnership.proposed_tick,
        updated_tick=updated_tick,
        revision=partnership.revision + 1,
        proposal_terms=partnership.proposal_terms,
        proposal_author_id=partnership.proposal_author_id,
    )


def _proposal_revision(
    partnership: RelationshipPartnership,
    new_author_id: UUID,
    proposal_terms: PartnershipTerms,
    updated_tick: int,
) -> RelationshipPartnership:
    if partnership.revision >= _MAX_REVISION:
        raise InvalidEvent("Partnership revision has reached its supported limit.")
    return RelationshipPartnership(
        partnership_id=partnership.partnership_id,
        proposer_id=partnership.proposer_id,
        partner_id=partnership.partner_id,
        status="pending",
        proposed_tick=partnership.proposed_tick,
        updated_tick=updated_tick,
        revision=partnership.revision + 1,
        proposal_terms=proposal_terms,
        proposal_author_id=new_author_id,
    )


def _require_transition_state(
    state: WorldState,
    partnership: RelationshipPartnership,
    actor_id: UUID,
    expected_revision: int,
) -> None:
    if expected_revision < 1 or partnership.revision != expected_revision:
        raise InvalidEvent("Partnership expected_revision does not match the current record.")
    if partnership.updated_tick > state.current_tick or partnership.proposed_tick > state.current_tick:
        raise InvalidEvent("Partnership record is from the future relative to this world state.")
    if actor_id not in {partnership.proposer_id, partnership.partner_id}:
        raise InvalidEvent("Only a partnership participant can respond to it.")


def _current_partnership(
    state: WorldState,
    records: Sequence[RelationshipPartnership],
    partnership_id: UUID,
) -> RelationshipPartnership:
    matches = [item for item in records if item.partnership_id == partnership_id]
    if len(matches) != 1:
        raise InvalidEvent("Partnership does not exist exactly once in the current record set.")
    partnership = matches[0]
    if partnership.updated_tick > state.current_tick or partnership.proposed_tick > state.current_tick:
        raise InvalidEvent("Partnership record is from the future relative to this world state.")
    return partnership


def _validated_records(records: object) -> list[RelationshipPartnership]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise InvalidEvent("Partnership records must be a sequence.")
    parsed: list[RelationshipPartnership] = []
    try:
        for item in records:
            raw = item.model_dump(mode="python") if isinstance(item, RelationshipPartnership) else item
            parsed.append(RelationshipPartnership.model_validate(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Partnership record is invalid.") from exc
    ids = [item.partnership_id for item in parsed]
    if len(ids) != len(set(ids)):
        raise InvalidEvent("Partnership IDs must be unique.")
    live_pairs = [
        frozenset((item.proposer_id, item.partner_id))
        for item in parsed if item.status in {"pending", "active"}
    ]
    if len(live_pairs) != len(set(live_pairs)):
        raise InvalidEvent("A pair may have only one pending or active partnership.")
    return parsed


def _same_pair(partnership: RelationshipPartnership, first_id: UUID, second_id: UUID) -> bool:
    return {partnership.proposer_id, partnership.partner_id} == {first_id, second_id}


def _require_active_actor(state: WorldState, actor_id: UUID) -> None:
    entity = state.entities.get(actor_id)
    if entity is None or entity.kind not in {"character", "person"} or entity.status != "active":
        raise InvalidEvent("Only an active character can make this partnership transition.")


def _require_state(state: WorldState) -> None:
    from .models import WorldState

    if not isinstance(state, WorldState):
        raise InvalidEvent("A validated WorldState is required.")
