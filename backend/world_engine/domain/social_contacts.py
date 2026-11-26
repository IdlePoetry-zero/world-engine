"""Pure planning of ordinary social-contact opportunities.

This module discovers only possible contact between active, uncontrolled NPCs
who occupy the same actual place. Every opportunity carries one typed proof of
a preexisting connection. It does not schedule an action or imply agreement.
"""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import heapq
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import GovernanceComponent, RelationRecord
from .occupations import WorkAssignment, WorkPosting

if TYPE_CHECKING:
    from .models import WorldEntity, WorldState


MAX_SOCIAL_CONTACT_CANDIDATES = 64
MAX_SOCIAL_CONTACTS_PER_TIE = 16
MAX_SOCIAL_CONTACT_GROUP_ACTORS = MAX_SOCIAL_CONTACTS_PER_TIE
MAX_SOCIAL_CONTACT_STABLE_SEED_LENGTH = 256

_PEOPLE = frozenset({"character", "person"})
_PLACES = frozenset({"place", "location"})
_ORGANIZATIONS = frozenset({"organization", "polity", "faction", "company", "government"})
_TICK_MIN = -(2**63)
_TICK_MAX = 2**63 - 1


class _SocialContactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SharedInstitutionProof(_SocialContactModel):
    """Both NPCs are currently registered members of this institution."""

    kind: Literal["shared_institution"] = "shared_institution"
    institution_id: UUID


class SharedDutyProof(_SocialContactModel):
    """Both NPCs hold active assignments for the same registered posting."""

    kind: Literal["shared_duty"] = "shared_duty"
    posting_id: UUID
    employer_id: UUID
    location_id: UUID
    assignment_ids: tuple[UUID, UUID]
    title: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def assignments_are_distinct(self) -> SharedDutyProof:
        if self.assignment_ids[0] == self.assignment_ids[1]:
            raise ValueError("a shared duty requires two distinct work assignments")
        return self


class EstablishedRelationProof(_SocialContactModel):
    """An existing directed relationship record links the pair."""

    kind: Literal["established_relation"] = "established_relation"
    relation_id: UUID
    from_id: UUID
    to_id: UUID

    @model_validator(mode="after")
    def relation_endpoints_are_distinct(self) -> EstablishedRelationProof:
        if self.from_id == self.to_id:
            raise ValueError("a relation proof must connect two different NPCs")
        return self


SocialContactProof = Annotated[
    SharedInstitutionProof | SharedDutyProof | EstablishedRelationProof,
    Field(discriminator="kind"),
]


class SocialContactOpportunity(_SocialContactModel):
    """A raw, co-located contact candidate with an auditable tie proof."""

    participant_ids: tuple[UUID, UUID]
    location_id: UUID
    slot_tick: StrictInt = Field(ge=_TICK_MIN, le=_TICK_MAX)
    proof: SocialContactProof

    @model_validator(mode="after")
    def participants_are_canonical(self) -> SocialContactOpportunity:
        if self.participant_ids[0] == self.participant_ids[1]:
            raise ValueError("a social contact needs two distinct participants")
        if tuple(sorted(self.participant_ids, key=str)) != self.participant_ids:
            raise ValueError("social contact participant IDs must be in canonical order")
        if isinstance(self.proof, SharedDutyProof) and self.proof.location_id != self.location_id:
            raise ValueError("the shared duty location must match the contact location")
        return self


def plan_social_contact_candidates(
    state: WorldState,
    *,
    stable_seed: str,
    controlled_actor_ids: frozenset[UUID] = frozenset(),
    slot_tick: int | None = None,
    scope_place_id: UUID | None = None,
    max_candidates: int = MAX_SOCIAL_CONTACT_CANDIDATES,
) -> tuple[SocialContactOpportunity, ...]:
    """Return bounded social-contact opportunities for the current state.

    Candidates require two active, named, non-player, non-controlled NPCs at
    the same active location and one preexisting typed tie: shared membership
    in an active governance component, two active assignments for the same
    work posting, or an existing directed ``RelationRecord``. Actors in transit
    are excluded even if their last stored location is still populated.

    ``slot_tick`` defaults to the state's current world tick and, when
    supplied, must equal it: the planner never predicts future co-location.
    ``scope_place_id`` optionally limits the candidates to a place and its
    nested locations. Stable hashes only order bounded candidates; there is no
    stochastic draw, affinity score, or forced interaction.
    """
    tick = _validate_inputs(state, stable_seed, controlled_actor_ids, slot_tick, scope_place_id, max_candidates)
    if max_candidates == 0:
        return ()

    in_transit = {
        journey.actor_id for journey in state.travel_journeys
        if journey.status == "in_transit"
    }
    actors_by_id: dict[UUID, WorldEntity] = {}
    locations_by_actor: dict[UUID, UUID] = {}
    for actor in state.entities.values():
        if (
            actor.kind not in _PEOPLE
            or actor.status != "active"
            or not actor.name
            or actor.id == state.player_id
            or actor.id in controlled_actor_ids
            or actor.id in in_transit
            or actor.location_id is None
        ):
            continue
        location = state.entities.get(actor.location_id)
        if location is None or location.kind not in _PLACES or location.status != "active":
            continue
        if scope_place_id is not None and not _is_within_scope(state, location.id, scope_place_id):
            continue
        actors_by_id[actor.id] = actor
        locations_by_actor[actor.id] = location.id

    # Map a co-located pair to its single canonical proof. A fixed proof
    # priority prefers a concrete shared duty, then institutional membership,
    # then an existing directed relation.
    proofs_by_pair: dict[tuple[UUID, UUID, UUID], SocialContactProof] = {}

    def add_pair(
        actor_a: UUID,
        actor_b: UUID,
        location_id: UUID,
        proof: SocialContactProof,
    ) -> None:
        if actor_a == actor_b or locations_by_actor.get(actor_a) != location_id or locations_by_actor.get(actor_b) != location_id:
            return
        participants = tuple(sorted((actor_a, actor_b), key=str))
        key = (participants[0], participants[1], location_id)
        previous = proofs_by_pair.get(key)
        if previous is not None:
            if _proof_order(proof) < _proof_order(previous):
                proofs_by_pair[key] = proof
            return
        if len(proofs_by_pair) < max_candidates:
            proofs_by_pair[key] = proof
            return
        # Keep only the best bounded candidate set while scanning tie sources.
        # Candidate rank does not depend on its proof, so a discarded pair can
        # never re-enter after the rank threshold gets stricter.
        worst_key = max(
            proofs_by_pair,
            key=lambda candidate_key: _candidate_order_key(stable_seed, tick, candidate_key),
        )
        if _candidate_order_key(stable_seed, tick, key) < _candidate_order_key(stable_seed, tick, worst_key):
            del proofs_by_pair[worst_key]
            proofs_by_pair[key] = proof

    # Shared governance membership. Groups are split by actual location, so a
    # remote member is never considered present for this contact.
    for institution in state.entities.values():
        if institution.kind not in _ORGANIZATIONS or institution.status != "active":
            continue
        raw_governance = institution.components.get("governance")
        if raw_governance is None:
            continue
        try:
            governance = GovernanceComponent.model_validate(raw_governance)
        except (TypeError, ValueError):
            continue
        members_by_location: dict[UUID, list[UUID]] = defaultdict(list)
        for member_id in governance.members:
            location_id = locations_by_actor.get(member_id)
            if location_id is not None:
                members_by_location[location_id].append(member_id)
        proof = SharedInstitutionProof(institution_id=institution.id)
        for location_id, member_ids in members_by_location.items():
            for pair in _bounded_ring_pairs(
                member_ids, stable_seed, tick,
                f"institution:{institution.id}:{location_id}",
            ):
                add_pair(pair[0], pair[1], location_id, proof)

    # A registered active work assignment is the typed duty proof. The pair
    # must hold assignments for the same posting and both must be at the
    # assignment's actual workplace now.
    postings: dict[UUID, WorkPosting] = {}
    for raw_posting in state.work_postings:
        try:
            posting = WorkPosting.model_validate(raw_posting.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError):
            continue
        postings[posting.posting_id] = posting

    assignments_by_posting: dict[tuple[UUID, UUID], dict[UUID, WorkAssignment]] = defaultdict(dict)
    for raw_assignment in state.work_assignments:
        try:
            assignment = WorkAssignment.model_validate(raw_assignment.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            assignment.ended_tick is not None
            or assignment.started_tick > tick
            or assignment.actor_id not in actors_by_id
            or locations_by_actor.get(assignment.actor_id) != assignment.location_id
        ):
            continue
        posting = postings.get(assignment.posting_id)
        if (
            posting is None
            or posting.employer_id != assignment.employer_id
            or posting.location_id != assignment.location_id
            or posting.valid_from_tick > tick
            or posting.valid_until_tick is not None and posting.valid_until_tick <= tick
        ):
            continue
        assignments_by_posting[(assignment.posting_id, assignment.location_id)][assignment.actor_id] = assignment

    for (posting_id, location_id), actor_assignments in assignments_by_posting.items():
        actor_ids = tuple(actor_assignments)
        for pair in _bounded_ring_pairs(
            actor_ids, stable_seed, tick, f"duty:{posting_id}:{location_id}",
        ):
            posting = postings[posting_id]
            assignment_ids = tuple(actor_assignments[actor_id].assignment_id for actor_id in pair)
            add_pair(
                pair[0], pair[1], location_id,
                SharedDutyProof(
                    posting_id=posting_id,
                    employer_id=posting.employer_id,
                    location_id=location_id,
                    assignment_ids=assignment_ids,
                    title=posting.title,
                ),
            )

    # Existing relations are scanned once. The proof retains direction but
    # deliberately excludes private dimensions, obligations, and free text.
    for raw_relation in state.relations:
        try:
            relation = RelationRecord.model_validate(raw_relation.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError):
            continue
        if relation.updated_tick > tick:
            continue
        from_location = locations_by_actor.get(relation.from_id)
        to_location = locations_by_actor.get(relation.to_id)
        if from_location is None or from_location != to_location:
            continue
        add_pair(
            relation.from_id,
            relation.to_id,
            from_location,
            EstablishedRelationProof(
                relation_id=relation.id, from_id=relation.from_id, to_id=relation.to_id,
            ),
        )

    ranked = sorted(
        proofs_by_pair.items(),
        key=lambda row: _candidate_order_key(stable_seed, tick, row[0]),
    )
    return tuple(
        SocialContactOpportunity(
            participant_ids=(actor_a, actor_b),
            location_id=location_id,
            slot_tick=tick,
            proof=proof,
        )
        for (actor_a, actor_b, location_id), proof in ranked
    )


def _validate_inputs(
    state: WorldState,
    stable_seed: str,
    controlled_actor_ids: frozenset[UUID],
    slot_tick: int | None,
    scope_place_id: UUID | None,
    max_candidates: int,
) -> int:
    # Keep WorldState out of the module import graph: the canonical event
    # contract imports this module while models.py builds the event union.
    from .models import WorldState

    if not isinstance(state, WorldState):
        raise TypeError("state must be a WorldState")
    if not isinstance(stable_seed, str) or not stable_seed.strip() or len(stable_seed) > MAX_SOCIAL_CONTACT_STABLE_SEED_LENGTH:
        raise ValueError("stable_seed must be non-empty text within the configured bound")
    if not isinstance(controlled_actor_ids, frozenset) or any(not isinstance(actor_id, UUID) for actor_id in controlled_actor_ids):
        raise ValueError("controlled_actor_ids must be a frozenset of UUIDs")
    tick = state.current_tick if slot_tick is None else slot_tick
    if type(tick) is not int or not _TICK_MIN <= tick <= _TICK_MAX:
        raise ValueError("slot_tick must be a signed 64-bit integer")
    if tick != state.current_tick:
        raise ValueError("slot_tick must match the current world tick")
    if scope_place_id is not None:
        if not isinstance(scope_place_id, UUID):
            raise ValueError("scope_place_id must be a UUID")
        scope = state.entities.get(scope_place_id)
        if scope is None or scope.kind not in _PLACES or scope.status != "active":
            raise ValueError("scope_place_id must identify an active place")
    if type(max_candidates) is not int or not 0 <= max_candidates <= MAX_SOCIAL_CONTACT_CANDIDATES:
        raise ValueError(f"max_candidates must be between 0 and {MAX_SOCIAL_CONTACT_CANDIDATES}")
    return tick


def _is_within_scope(state: WorldState, location_id: UUID, scope_place_id: UUID) -> bool:
    seen: set[UUID] = set()
    current_id: UUID | None = location_id
    while current_id is not None and current_id not in seen:
        if current_id == scope_place_id:
            return True
        seen.add(current_id)
        current = state.entities.get(current_id)
        current_id = current.location_id if current is not None and current.kind in _PLACES else None
    return False


def _bounded_ring_pairs(
    actor_ids: tuple[UUID, ...] | list[UUID],
    stable_seed: str,
    slot_tick: int,
    tie_key: str,
) -> tuple[tuple[UUID, UUID], ...]:
    unique_ids = set(actor_ids)
    if len(unique_ids) < 2:
        return ()
    selected = heapq.nsmallest(
        MAX_SOCIAL_CONTACT_GROUP_ACTORS,
        unique_ids,
        key=lambda actor_id: (
            _stable_rank(stable_seed, slot_tick, f"group:{tie_key}:{actor_id}"),
            str(actor_id),
        ),
    )
    pairs: set[tuple[UUID, UUID]] = set()
    for index, actor_a in enumerate(selected):
        actor_b = selected[(index + 1) % len(selected)]
        if actor_a == actor_b:
            continue
        pairs.add(tuple(sorted((actor_a, actor_b), key=str)))
    return tuple(sorted(pairs, key=lambda pair: (str(pair[0]), str(pair[1]))))


def _proof_order(proof: SocialContactProof) -> tuple[int, str, str, str]:
    if isinstance(proof, SharedDutyProof):
        return (0, str(proof.posting_id), str(proof.assignment_ids[0]), str(proof.assignment_ids[1]))
    if isinstance(proof, SharedInstitutionProof):
        return (1, str(proof.institution_id), "", "")
    return (2, str(proof.relation_id), str(proof.from_id), str(proof.to_id))


def _stable_rank(stable_seed: str, slot_tick: int, identity: str) -> bytes:
    payload = f"social_contact_v1\0{stable_seed}\0{slot_tick}\0{identity}".encode("utf-8")
    return sha256(payload).digest()


def _candidate_order_key(
    stable_seed: str,
    slot_tick: int,
    pair_key: tuple[UUID, UUID, UUID],
) -> tuple[bytes, tuple[str, str, str]]:
    actor_a, actor_b, location_id = pair_key
    return (
        _stable_rank(stable_seed, slot_tick, f"candidate:{actor_a}:{actor_b}:{location_id}"),
        (str(actor_a), str(actor_b), str(location_id)),
    )
