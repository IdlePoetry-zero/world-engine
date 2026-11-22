"""Typed predicates and source-chain helpers for player investigations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


WorldTick = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
WEIGH_EVIDENCE_POLICY = "investigation.weigh_evidence.v1"
WEIGH_EVIDENCE_DURATION = 300
ASK_NPC_CLAIM_POLICY = "investigation.ask_npc_claim.v1"
FALLIBLE_CLAIM_KINDS = frozenset({"belief", "claim", "rumor"})


class InvestigationFactModel(BaseModel):
    """A factual predicate recorded by a source, without a natural-language parser."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    effective_tick: WorldTick


class EntityStatusFact(InvestigationFactModel):
    predicate: Literal["entity_status"] = "entity_status"
    value: str = Field(min_length=1, max_length=64)


class EntityLocationFact(InvestigationFactModel):
    predicate: Literal["entity_location"] = "entity_location"
    value: UUID


InvestigationFact = Annotated[
    EntityStatusFact | EntityLocationFact,
    Field(discriminator="predicate"),
]


class EvidenceAssessment(BaseModel):
    """A replay-checkable result about agreement between two typed sources."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    finding: Literal["corroborated", "conflict"]
    observation_id: UUID
    claim_id: UUID
    predicate: Literal["entity_status", "entity_location"]
    effective_tick: WorldTick


class EvidenceDisclosureReceipt(BaseModel):
    """Replay-verifiable receipt for an NPC's explicitly requested disclosure."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    source_record_id: UUID
    speaker_id: UUID
    recipient_id: UUID
    location_id: UUID
    disclosed_tick: WorldTick
    policy_version: Literal["investigation.ask_npc_claim.v1"] = ASK_NPC_CLAIM_POLICY


class EvidenceSourceRecord(Protocol):
    id: UUID
    source_refs: list[UUID]
    source_event_id: UUID | None
    provenance: list[object]
    assessment: EvidenceAssessment | None


class AssessmentKnowledgeRecord(EvidenceSourceRecord, Protocol):
    owner_id: UUID | None
    holder_id: UUID | None
    subject_id: UUID | None
    kind: str
    fact: object | None
    received_tick: int
    valid_from_tick: int | None
    visibility: object


class DisclosureKnowledgeRecord(EvidenceSourceRecord, Protocol):
    id: UUID
    owner_id: UUID | None
    holder_id: UUID | None
    subject_id: UUID | None
    claim: str
    summary: str | None
    confidence: object
    kind: str
    fact: object | None
    assessment: EvidenceAssessment | None
    disclosure_policy: str
    disclosure: EvidenceDisclosureReceipt | None
    received_tick: int
    valid_from_tick: int | None
    visibility: object


class DisclosureEntity(Protocol):
    id: UUID
    kind: str
    status: str
    location_id: UUID | None
    components: dict[str, object]


class DisclosureState(Protocol):
    current_tick: int
    entities: Mapping[UUID, DisclosureEntity]
    knowledge: list[DisclosureKnowledgeRecord]


class VisibilityEntity(Protocol):
    id: UUID
    kind: str
    location_id: UUID | None
    components: Mapping[str, object]


class VisibilityRecord(Protocol):
    owner_id: UUID | None
    holder_id: UUID | None
    subject_id: UUID | None
    kind: str
    received_tick: int
    valid_from_tick: int | None


class VisibilityState(Protocol):
    current_tick: int
    entities: Mapping[UUID, VisibilityEntity]
    knowledge: list[VisibilityRecord]


def entity_visible_to_actor(state: VisibilityState, actor_id: UUID, entity_id: UUID) -> bool:
    """Match the entity allowlist used by player projection for one actor."""
    actor = state.entities.get(actor_id)
    entity = state.entities.get(entity_id)
    if actor is None or entity is None or entity.kind == "shipment":
        return False
    known_places = {str(actor.location_id)} if actor.location_id is not None else set()
    raw_known_places = actor.components.get("known_places", [])
    if isinstance(raw_known_places, (list, tuple, set)):
        known_places.update(str(value) for value in raw_known_places)
    is_known_place = entity.kind in {"place", "location"} and str(entity.id) in known_places
    is_present = actor.location_id is not None and entity.location_id == actor.location_id
    if entity.id != actor.id and not is_known_place and not is_present:
        return False
    if entity.components.get("hidden") is True and not any(
        record.subject_id == entity.id
        and actor_id in {record.owner_id, record.holder_id}
        and record.kind == "observation"
        and record.received_tick <= state.current_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
        for record in state.knowledge
    ):
        return False
    return True


class _Provenance(Protocol):
    kind: str
    reference_id: str


def source_roots(
    record_id: UUID,
    records_by_id: Mapping[UUID, EvidenceSourceRecord],
) -> frozenset[tuple[str, str]]:
    """Resolve a knowledge record to terminal cited sources, rejecting cycles.

    A source reference that names another retained knowledge record is followed
    recursively. References that do not name a knowledge record remain opaque
    source identifiers; callers must not interpret them as evidence content.
    """

    def visit(current_id: UUID, visiting: frozenset[UUID]) -> set[tuple[str, str]]:
        if current_id in visiting:
            raise ValueError("knowledge source references contain a cycle")
        record = records_by_id.get(current_id)
        if record is None:
            # A source_refs entry may refer to an event or an external source.
            return {("source_ref", str(current_id))}

        if record.assessment is not None:
            raise ValueError("a prior investigation assessment cannot be reused as a source")

        next_visiting = visiting | {current_id}
        roots: set[tuple[str, str]] = set()
        if record.source_event_id is not None:
            roots.add(("event", str(record.source_event_id)))
        for source_ref in record.source_refs:
            if source_ref in records_by_id:
                roots.update(visit(source_ref, next_visiting))
            else:
                roots.add(("source_ref", str(source_ref)))
        for item in record.provenance:
            provenance = item  # Typed as object to keep this module independent of components.py.
            kind = getattr(provenance, "kind", None)
            reference_id = getattr(provenance, "reference_id", None)
            # Rule provenance identifies the method, not an evidentiary source.
            if isinstance(kind, str) and kind != "rule" and isinstance(reference_id, str):
                roots.add((f"provenance:{kind}", reference_id))
        if not roots:
            raise ValueError("knowledge record has no resolvable source provenance")
        return roots

    return frozenset(visit(record_id, frozenset()))


def compare_evidence_sources(
    observation_id: UUID,
    claim_id: UUID,
    records: list[AssessmentKnowledgeRecord],
    *,
    actor_id: UUID,
    at_tick: int,
) -> str:
    """Return corroborated/conflict for one authorized, independent source pair."""
    by_id = {record.id: record for record in records}
    observation = by_id.get(observation_id)
    claim = by_id.get(claim_id)
    if observation is None or claim is None or observation.id == claim.id:
        raise ValueError("evidence sources are missing or duplicated")
    for source in (observation, claim):
        if (
            source.received_tick > at_tick
            or source.valid_from_tick is not None and source.valid_from_tick > at_tick
            or not _knowledge_record_visible_to(source, actor_id)
        ):
            raise ValueError("evidence source is not available to its actor")
        if source.assessment is not None:
            raise ValueError("a derived assessment cannot be reused as primary evidence")
    if observation.kind != "observation" or claim.kind not in FALLIBLE_CLAIM_KINDS:
        raise ValueError("evidence requires one observation and one fallible claim")
    if observation.fact is None or claim.fact is None:
        raise ValueError("evidence sources require typed factual predicates")
    if observation.subject_id is None or observation.subject_id != claim.subject_id:
        raise ValueError("evidence sources must refer to the same subject")
    if (
        observation.fact.predicate != claim.fact.predicate
        or observation.fact.effective_tick != claim.fact.effective_tick
        or observation.fact.effective_tick > at_tick
    ):
        raise ValueError("evidence sources must compare the same non-future predicate at the same world tick")

    try:
        observation_roots = source_roots(observation.id, by_id)
        claim_roots = source_roots(claim.id, by_id)
    except ValueError as exc:
        raise ValueError("evidence source provenance is invalid") from exc
    if observation_roots & claim_roots:
        raise ValueError("evidence sources do not have independent provenance")
    return "corroborated" if facts_agree(observation.fact, claim.fact) else "conflict"


def validate_assessment_record(
    assessment_record: AssessmentKnowledgeRecord,
    records: list[AssessmentKnowledgeRecord],
    *,
    actor_id: UUID,
    at_tick: int,
) -> str:
    """Validate the typed provenance and result of an evidence comparison.

    This function is used both before committing an action and while replaying
    the corresponding knowledge event. It reads only record IDs and typed
    predicates; it never infers facts from claim text.
    """
    assessment = assessment_record.assessment
    if assessment is None:
        raise ValueError("knowledge record is not an evidence assessment")
    if (
        assessment_record.owner_id != actor_id
        or assessment_record.holder_id is not None
        or assessment_record.kind != "belief"
        or assessment_record.fact is not None
        or assessment_record.received_tick != at_tick
        or assessment_record.valid_from_tick not in (None, at_tick)
        or getattr(assessment_record.visibility, "policy", None) != "private"
        or getattr(assessment_record.visibility, "subject_ids", ())
    ):
        raise ValueError("evidence assessment must be a private actor-owned belief at the current tick")
    if len(assessment_record.provenance) != 1:
        raise ValueError("evidence assessment must cite its registered rule provenance")
    provenance = assessment_record.provenance[0]
    if (
        getattr(provenance, "kind", None) != "rule"
        or getattr(provenance, "reference_id", None) != WEIGH_EVIDENCE_POLICY
    ):
        raise ValueError("evidence assessment must cite its registered rule provenance")

    by_id = {record.id: record for record in records}
    observation = by_id.get(assessment.observation_id)
    claim = by_id.get(assessment.claim_id)
    if observation is None or claim is None or observation.id == claim.id:
        raise ValueError("evidence assessment sources are missing or duplicated")
    if assessment_record.source_refs != [observation.id, claim.id]:
        raise ValueError("evidence assessment source_refs must exactly identify its observation and claim")

    try:
        expected_finding = compare_evidence_sources(
            observation.id, claim.id, records, actor_id=actor_id, at_tick=at_tick,
        )
    except ValueError as exc:
        raise ValueError("evidence assessment source validation failed") from exc
    if (
        observation.subject_id is None
        or assessment_record.subject_id != observation.subject_id
        or assessment.predicate != observation.fact.predicate
        or assessment.effective_tick != observation.fact.effective_tick
        or assessment.finding != expected_finding
    ):
        raise ValueError("evidence assessment finding does not match its typed sources")
    expected_claim, expected_summary = assessment_text(observation.fact, expected_finding)
    if assessment_record.claim != expected_claim or assessment_record.summary != expected_summary:
        raise ValueError("evidence assessment text does not match its typed result")
    return expected_finding


def validate_disclosure_record(
    disclosed_record: DisclosureKnowledgeRecord,
    state: DisclosureState,
) -> UUID:
    """Validate an exact, co-located, actor-private copy of an NPC claim.

    The copied record retains the source's factual effective tick. Its received
    and receipt ticks are the current world tick, so asking an NPC cannot
    rewrite or backdate either piece of information.
    """
    receipt = disclosed_record.disclosure
    if receipt is None:
        raise ValueError("knowledge record is not an NPC claim disclosure")
    if receipt.policy_version != ASK_NPC_CLAIM_POLICY:
        raise ValueError("NPC claim disclosure policy version is unsupported")
    speaker = state.entities.get(receipt.speaker_id)
    recipient = state.entities.get(receipt.recipient_id)
    location = state.entities.get(receipt.location_id)
    source = next((item for item in state.knowledge if item.id == receipt.source_record_id), None)
    if (
        speaker is None
        or speaker.kind not in {"character", "person"}
        or speaker.status != "active"
        or recipient is None
        or recipient.kind not in {"character", "person"}
        or recipient.status != "active"
        or receipt.recipient_id != disclosed_record.owner_id
        or receipt.recipient_id == receipt.speaker_id
        or speaker.location_id is None
        or speaker.location_id != recipient.location_id
        or speaker.location_id != receipt.location_id
        or location is None
        or location.kind not in {"place", "location"}
        or location.status != "active"
        or state.current_tick != receipt.disclosed_tick
    ):
        raise ValueError("NPC claim disclosure participants must be active and co-located at the recorded tick")

    actor_knows_speaker = any(
        record.subject_id == speaker.id
        and record.kind == "observation"
        and receipt.recipient_id in {record.owner_id, record.holder_id}
        and record.received_tick <= state.current_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
        for record in state.knowledge
    )
    if speaker.components.get("hidden") is True and not actor_knows_speaker:
        raise ValueError("NPC claim speaker is not visible to the recipient")

    if source is None or source.id == disclosed_record.id:
        raise ValueError("NPC claim disclosure source record is missing")
    if (
        source.holder_id != speaker.id
        or source.disclosure_policy != "if_asked"
        or source.disclosure is not None
        or source.assessment is not None
        or source.kind not in FALLIBLE_CLAIM_KINDS
        or source.fact is None
        or getattr(source.fact, "predicate", None) not in {"entity_status", "entity_location"}
        or source.subject_id is None
        or source.received_tick > state.current_tick
        or source.valid_from_tick is not None and source.valid_from_tick > state.current_tick
        or getattr(source.visibility, "policy", None) != "private"
        or getattr(source.visibility, "subject_ids", ())
    ):
        raise ValueError("NPC claim source is not an eligible private fallible claim")

    if (
        disclosed_record.holder_id is not None
        or disclosed_record.subject_id != source.subject_id
        or disclosed_record.claim != source.claim
        or disclosed_record.summary != source.summary
        or disclosed_record.confidence != source.confidence
        or disclosed_record.source_refs != [source.id]
        or disclosed_record.source_event_id != source.source_event_id
        or disclosed_record.provenance != source.provenance
        or disclosed_record.received_tick != state.current_tick
        or disclosed_record.valid_from_tick != source.valid_from_tick
        or disclosed_record.kind != source.kind
        or disclosed_record.fact != source.fact
        or disclosed_record.assessment is not None
        or disclosed_record.disclosure_policy != "never"
        or getattr(disclosed_record.visibility, "policy", None) != "private"
        or getattr(disclosed_record.visibility, "subject_ids", ())
    ):
        raise ValueError("disclosed claim must exactly preserve the source record and remain private to the recipient")
    return source.id


def _knowledge_record_visible_to(record: AssessmentKnowledgeRecord, actor_id: UUID) -> bool:
    if actor_id in {record.owner_id, record.holder_id}:
        return True
    visibility = record.visibility
    policy = getattr(visibility, "policy", None)
    subject_ids = getattr(visibility, "subject_ids", ())
    return policy == "public" or (policy == "subject_ids" and actor_id in subject_ids)


def facts_agree(left: object, right: object) -> bool:
    """Whether two typed source assertions make the same time-bounded claim."""
    if not isinstance(left, BaseModel) or not isinstance(right, BaseModel):
        return False
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def fact_summary(fact: object) -> str:
    """Return a fixed, non-narrative label for a supported predicate."""
    if isinstance(fact, EntityStatusFact):
        return f"主体在世界时刻 {fact.effective_tick} 的状态"
    if isinstance(fact, EntityLocationFact):
        return f"主体在世界时刻 {fact.effective_tick} 的地点"
    raise ValueError("unsupported investigation predicate")


def assessment_text(fact: object, finding: str) -> tuple[str, str]:
    """Build the versioned, deterministic text stored for a typed assessment."""
    detail = fact_summary(fact)
    if finding == "corroborated":
        summary = f"两条来源独立的记录对{detail}相符；这只说明来源一致，不代表最终证实。"
    elif finding == "conflict":
        summary = f"两条来源独立的记录对{detail}存在冲突；尚不能判断哪一项正确。"
    else:
        raise ValueError("unsupported evidence finding")
    return f"证据复核：{summary}", summary


def fact_value_label(fact: object, *, subject_name: str | None, location_name: str | None = None) -> str:
    """Render only typed values for a safe evidence assessment record."""
    subject = subject_name or "该对象"
    if isinstance(fact, EntityStatusFact):
        return f"{subject}在世界时刻 {fact.effective_tick} 的状态为「{fact.value}」"
    if isinstance(fact, EntityLocationFact):
        location = location_name or "某地点"
        return f"{subject}在世界时刻 {fact.effective_tick} 位于「{location}」"
    raise ValueError("unsupported investigation predicate")
