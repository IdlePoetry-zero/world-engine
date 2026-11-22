"""Authored investigation-case definitions and deterministic case evaluation."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from .investigation import (
    AssessmentKnowledgeRecord,
    source_roots,
    validate_assessment_record,
    WorldTick,
)


CaseName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")]
CaseReference = Annotated[str, Field(min_length=1, max_length=160)]


class CaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class CaseSourceReference(CaseModel):
    """A stable authored source root allowed by one case route."""

    kind: Literal["event", "genesis", "pack", "observation"]
    reference_id: CaseReference


class CaseEvidenceRequirement(CaseModel):
    """One typed comparison required by a route."""

    subject_id: UUID
    predicate: Literal["entity_status", "entity_location"]
    effective_tick: WorldTick
    finding: Literal["corroborated", "conflict"]
    observation_provenance: list[CaseSourceReference] = Field(min_length=1, max_length=16)
    claim_provenance: list[CaseSourceReference] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def source_roots_are_independent(self) -> CaseEvidenceRequirement:
        observation = {(item.kind, item.reference_id) for item in self.observation_provenance}
        claim = {(item.kind, item.reference_id) for item in self.claim_provenance}
        if len(observation) != len(self.observation_provenance) or len(claim) != len(self.claim_provenance):
            raise ValueError("case evidence provenance roots must be unique on each side")
        if observation & claim:
            raise ValueError("case evidence provenance roots must be independent")
        return self


class InvestigationCaseRoute(CaseModel):
    """An alternative authored evidence route supporting or ruling out one suspect."""

    route_id: CaseName
    suspect_id: UUID
    finding: Literal["supports", "rules_out"]
    requirements: list[CaseEvidenceRequirement] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_requirements(self) -> InvestigationCaseRoute:
        keys = [
            (item.subject_id, item.predicate, item.effective_tick, item.finding,
             tuple((ref.kind, ref.reference_id) for ref in item.observation_provenance),
             tuple((ref.kind, ref.reference_id) for ref in item.claim_provenance))
            for item in self.requirements
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("case route requirements must be unique")
        return self


class WrongSuspectRelationEffect(CaseModel):
    """An authored, bounded directed relation change for a public false accusation."""

    dimension: Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]
    delta: StrictInt = Field(ge=-100, le=100)

    @model_validator(mode="after")
    def nonzero_effect(self) -> WrongSuspectRelationEffect:
        if self.delta == 0:
            raise ValueError("wrong-suspect relation effect must be nonzero")
        return self


class InvestigationCaseDefinition(CaseModel):
    """Author-only case policy stored on a local item anchor."""

    case_id: UUID
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(default="", max_length=1000)
    routes: list[InvestigationCaseRoute] = Field(min_length=2, max_length=128)
    wrong_suspect_effect: WrongSuspectRelationEffect | None = None

    @model_validator(mode="after")
    def unique_route_ids(self) -> InvestigationCaseDefinition:
        ids = [route.route_id for route in self.routes]
        if len(ids) != len(set(ids)):
            raise ValueError("investigation case route IDs must be unique")
        return self


CaseFileStatus = Literal["open", "closed"]
CaseFileOutcome = Literal["supported", "disconfirmed", "contested", "unresolved"]


class InvestigationCaseFile(CaseModel):
    """Private player-owned tracking record for one authored case."""

    id: UUID
    case_id: UUID
    anchor_id: UUID
    owner_id: UUID
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(default="", max_length=1000)
    status: CaseFileStatus = "open"
    revision: int = Field(default=1, ge=1)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=10_000)
    opened_tick: WorldTick
    updated_tick: WorldTick
    closed_tick: WorldTick | None = None
    outcome: CaseFileOutcome | None = None
    suspect_id: UUID | None = None
    public_accusation: bool = False

    @model_validator(mode="after")
    def lifecycle_fields_match_status(self) -> InvestigationCaseFile:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("case file evidence references must be unique")
        if self.updated_tick < self.opened_tick:
            raise ValueError("case file updated_tick cannot precede opened_tick")
        if self.status == "open":
            if (self.closed_tick is not None or self.outcome is not None or self.suspect_id is not None
                    or self.public_accusation):
                raise ValueError("an open case file cannot contain closure fields")
        else:
            if self.closed_tick is None or self.outcome is None or self.closed_tick != self.updated_tick:
                raise ValueError("a closed case file requires a matching close tick and outcome")
            if self.closed_tick < self.opened_tick:
                raise ValueError("case file cannot close before it opens")
            if self.public_accusation and self.suspect_id is None:
                raise ValueError("a public accusation must name a suspect")
        return self


class PublicAccusationCorrection(CaseModel):
    """Append-only provenance that an accuser publicly corrected one false accusation."""

    event_id: UUID
    case_file_id: UUID
    expected_revision: int = Field(ge=1)
    source_closure_event_id: UUID
    actor_id: UUID
    suspect_id: UUID
    corrected_tick: WorldTick
    location_id: UUID


RepairOperation = Literal["accept", "decline", "defer"]
RepairStatement = Annotated[
    str,
    StringConstraints(min_length=1, max_length=500, strip_whitespace=False),
]


class AccusationRepairOffer(CaseModel):
    """One immutable explanation offered after a public accusation correction."""

    offer_id: UUID
    revision: StrictInt = Field(ge=1)
    correction_event_id: UUID
    source_closure_event_id: UUID
    case_file_id: UUID
    case_revision: StrictInt = Field(ge=1)
    accuser_id: UUID
    suspect_id: UUID
    statement: RepairStatement
    offered_tick: WorldTick
    updated_tick: WorldTick
    status: Literal["pending", "accepted", "declined"] = "pending"
    last_operation: RepairOperation | None = None

    @model_validator(mode="after")
    def valid_offer_lifecycle(self) -> AccusationRepairOffer:
        if not self.statement.strip():
            raise ValueError("an accusation repair offer requires nonempty authored text")
        if self.accuser_id == self.suspect_id or self.updated_tick < self.offered_tick:
            raise ValueError("an accusation repair offer has invalid participants or times")
        if self.status == "pending":
            if self.last_operation is None and (self.revision != 1 or self.updated_tick != self.offered_tick):
                raise ValueError("a new pending repair offer must start at revision one")
            if self.last_operation not in {None, "defer"}:
                raise ValueError("a pending repair offer may only record a defer operation")
        elif self.status == "accepted":
            if self.last_operation != "accept" or self.revision < 2:
                raise ValueError("an accepted repair offer must record an accept transition")
        elif self.status == "declined":
            if self.last_operation != "decline" or self.revision < 2:
                raise ValueError("a declined repair offer must record a decline transition")
        return self


class CaseEntity(Protocol):
    id: UUID
    kind: str
    status: str
    location_id: UUID | None
    components: Mapping[str, object]


class CaseState(Protocol):
    current_tick: int
    entities: Mapping[UUID, CaseEntity]
    knowledge: list[AssessmentKnowledgeRecord]
    investigation_cases: list[InvestigationCaseFile]


def case_definition_from_anchor(anchor: CaseEntity) -> InvestigationCaseDefinition:
    raw = anchor.components.get("investigation_case")
    return InvestigationCaseDefinition.model_validate(raw)


def case_definition_digest(definition: InvestigationCaseDefinition) -> str:
    encoded = json.dumps(
        definition.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def case_anchor_is_local_and_available(state: CaseState, actor_id: UUID, anchor_id: UUID) -> bool:
    actor = state.entities.get(actor_id)
    anchor = state.entities.get(anchor_id)
    if (
        actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
        or actor.location_id is None or anchor is None or anchor.kind != "item" or anchor.status != "active"
    ):
        return False
    raw_place = anchor.components.get("place")
    nested_parent = raw_place.get("parent_id") if isinstance(raw_place, Mapping) else None
    raw_parent = anchor.components.get("parent_id")
    if anchor.location_id != actor.location_id and str(raw_parent or nested_parent) != str(actor.location_id):
        return False
    if anchor.components.get("hidden") is True and not any(
        record.subject_id == anchor.id
        and actor_id in {record.owner_id, record.holder_id}
        and record.kind == "observation"
        and record.received_tick <= state.current_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
        for record in state.knowledge
    ):
        return False
    return True


def _reference_roots(references: list[CaseSourceReference]) -> frozenset[tuple[str, str]]:
    return frozenset(
        ("event", ref.reference_id) if ref.kind == "event"
        else (f"provenance:{ref.kind}", ref.reference_id)
        for ref in references
    )


def matched_case_routes(
    definition: InvestigationCaseDefinition,
    file: InvestigationCaseFile,
    records: list[AssessmentKnowledgeRecord],
    *,
    actor_id: UUID,
    at_tick: int,
) -> tuple[InvestigationCaseRoute, ...]:
    """Return routes whose every typed requirement is backed by attached assessments."""
    by_id = {record.id: record for record in records}
    assessments = [by_id[record_id] for record_id in file.evidence_refs if record_id in by_id]
    results: list[InvestigationCaseRoute] = []
    for route in definition.routes:
        route_complete = True
        used_assessments: set[UUID] = set()
        for requirement in route.requirements:
            satisfied = False
            for record in assessments:
                if record.id in used_assessments:
                    continue
                assessment = record.assessment
                if (
                    assessment is None
                    or record.subject_id != requirement.subject_id
                    or assessment.predicate != requirement.predicate
                    or assessment.effective_tick != requirement.effective_tick
                    or assessment.finding != requirement.finding
                ):
                    continue
                try:
                    validate_case_assessment_access(record, records, actor_id=actor_id, at_tick=at_tick)
                    observation = by_id[assessment.observation_id]
                    claim = by_id[assessment.claim_id]
                    roots_by_id = by_id
                    observation_roots = source_roots(observation.id, roots_by_id)
                    claim_roots = source_roots(claim.id, roots_by_id)
                except (KeyError, ValueError):
                    continue
                if (
                    observation_roots & _reference_roots(requirement.observation_provenance)
                    and claim_roots & _reference_roots(requirement.claim_provenance)
                    and not observation_roots & claim_roots
                ):
                    satisfied = True
                    used_assessments.add(record.id)
                    break
            if not satisfied:
                route_complete = False
                break
        if route_complete:
            results.append(route)
    return tuple(results)


def validate_case_assessment_access(
    record: AssessmentKnowledgeRecord,
    records: list[AssessmentKnowledgeRecord],
    *,
    actor_id: UUID,
    at_tick: int,
) -> None:
    """Validate source agreement at the assessment's creation tick and access now."""
    if (
        record.received_tick > at_tick
        or record.valid_from_tick is not None and record.valid_from_tick > at_tick
    ):
        raise ValueError("case assessment is not available at the current tick")
    validate_assessment_record(
        record, records, actor_id=actor_id, at_tick=record.received_tick,
    )


def case_closure_outcome(
    suspect_id: UUID | None,
    routes: tuple[InvestigationCaseRoute, ...],
) -> str:
    """Classify the player's explicit conclusion using only completed authored routes."""
    if suspect_id is None:
        return "unresolved"
    supports = any(route.suspect_id == suspect_id and route.finding == "supports" for route in routes)
    rules_out = any(route.suspect_id == suspect_id and route.finding == "rules_out" for route in routes)
    if supports and rules_out:
        return "contested"
    if supports:
        return "supported"
    if rules_out:
        return "disconfirmed"
    return "unresolved"
