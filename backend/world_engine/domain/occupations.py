"""Pure DTOs and checks for posted work and employment assignments.

Occupation records keep the assigned job separate from ``SkillsComponent``:
an assignment records where and for whom a character works, while skill levels
remain in the existing progression model. This module has no world-state,
storage, money, or time-settlement dependency. The application host supplies an
authoritative actor snapshot and records appointment/resignation source events.

Work posting validity uses a half-open interval: ``valid_from_tick`` is
inclusive and ``valid_until_tick`` is exclusive. A resignation may be submitted
remotely; only appointment requires the trusted host to assert co-location.
Live appointment and resignation operations require an event provenance source.
Pack/genesis sources are retained for authored initial assignment snapshots.
"""

from __future__ import annotations

from typing import Iterable, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from .components import ExactDecimal, ResourceName, SkillsComponent

OccupationTick = StrictInt
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
_MAX_REVISION = 2**63 - 1


class OccupationError(ValueError):
    """A stable, user-actionable occupation eligibility or concurrency error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OccupationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class OccupationSkillRequirement(OccupationModel):
    """Minimum level in an existing skill, as read from SkillsComponent."""

    skill_id: ResourceName
    minimum_level: ExactDecimal = Field(ge=0, le=100)


class WorkPosting(OccupationModel):
    """A versioned employer posting with a finite number of active slots."""

    posting_id: UUID
    employer_id: UUID
    location_id: UUID
    title: str = Field(min_length=1, max_length=120)
    required_skill: OccupationSkillRequirement | None = None
    slots: StrictInt = Field(ge=1, le=_MAX_REVISION)
    valid_from_tick: OccupationTick = Field(ge=_MIN_TICK, le=_MAX_TICK)
    valid_until_tick: OccupationTick | None = Field(default=None, ge=_MIN_TICK, le=_MAX_TICK)
    status: Literal["open", "closed"] = "open"
    revision: StrictInt = Field(default=1, ge=1, le=_MAX_REVISION)

    @model_validator(mode="after")
    def validity_interval_is_ordered(self) -> WorkPosting:
        if self.valid_until_tick is not None and self.valid_until_tick <= self.valid_from_tick:
            raise ValueError("work posting validity must have a positive half-open interval")
        return self


class OccupationSource(OccupationModel):
    """Provenance for an authored occupation snapshot or lifecycle event.

    Event references must be canonical UUID strings. ``pack`` references name
    an authored catalog source and ``genesis`` references an initial world
    record; those source kinds cannot authorize live lifecycle changes.
    """

    kind: Literal["event", "genesis", "pack"]
    reference_id: str = Field(min_length=1, max_length=160)

    @field_validator("reference_id")
    @classmethod
    def event_source_is_a_canonical_uuid(cls, value: str, info):
        if info.data.get("kind") == "event":
            try:
                parsed = UUID(value)
            except (ValueError, AttributeError) as exc:
                raise ValueError("event occupation provenance must be a UUID") from exc
            if str(parsed) != value:
                raise ValueError("event occupation provenance must use a canonical UUID")
        return value


class WorkAssignment(OccupationModel):
    """An immutable employment snapshot; ending preserves identity and origin."""

    assignment_id: UUID
    actor_id: UUID
    posting_id: UUID
    employer_id: UUID
    location_id: UUID
    title: str = Field(min_length=1, max_length=120)
    required_skill: OccupationSkillRequirement | None = None
    started_tick: OccupationTick = Field(ge=_MIN_TICK, le=_MAX_TICK)
    ended_tick: OccupationTick | None = Field(default=None, ge=_MIN_TICK, le=_MAX_TICK)
    revision: StrictInt = Field(default=1, ge=1, le=_MAX_REVISION)
    source: OccupationSource
    end_source: OccupationSource | None = None

    @model_validator(mode="after")
    def identity_and_lifecycle_are_consistent(self) -> WorkAssignment:
        if self.assignment_id != occupation_assignment_id(self.source, self.actor_id, self.posting_id):
            raise ValueError("occupation assignment ID must be derived from its source, actor, and posting")
        if (self.ended_tick is None) != (self.end_source is None):
            raise ValueError("ended occupation assignments require both an end tick and end source")
        if self.ended_tick is not None and self.ended_tick < self.started_tick:
            raise ValueError("an occupation cannot end before it starts")
        return self

    @property
    def status(self) -> Literal["active", "ended"]:
        return "ended" if self.ended_tick is not None else "active"


class OccupationActorContext(OccupationModel):
    """Trusted host facts used by the pure appointment/resignation checks.

    The host resolves actor life state and co-location from authoritative world
    records. Skill values come from the actor's existing SkillsComponent.
    """

    actor_id: UUID
    active: StrictBool
    co_located: StrictBool
    skills: SkillsComponent = Field(default_factory=SkillsComponent)


def occupation_assignment_id(source: OccupationSource, actor_id: UUID, posting_id: UUID) -> UUID:
    """Derive a stable assignment ID from its unique source and endpoints."""

    source = OccupationSource.model_validate(source.model_dump(mode="python"))
    actor_id = UUID(str(actor_id))
    posting_id = UUID(str(posting_id))
    if source.kind == "event":
        namespace = UUID(source.reference_id)
    else:
        namespace = uuid5(NAMESPACE_URL, f"world-engine:occupation:{source.kind}:{source.reference_id}")
    return uuid5(namespace, f"assignment:{actor_id}:{posting_id}")


def appoint_to_posting(
    posting: WorkPosting,
    assignments: Iterable[WorkAssignment],
    *,
    actor: OccupationActorContext,
    expected_posting_revision: int,
    effective_tick: int,
    source: OccupationSource,
) -> WorkAssignment:
    """Check eligibility and return a new assignment without mutating inputs.

    The trusted host supplies the active/co-located actor snapshot and current
    posting revision. The initial policy allows one active assignment per
    actor. A separate committed event/reducer owns posting capacity and payment
    settlement; this function performs neither.
    """

    posting = WorkPosting.model_validate(posting.model_dump(mode="python"))
    actor = OccupationActorContext.model_validate(actor.model_dump(mode="python"))
    source = _live_event_source(source)
    tick = _checked_tick(effective_tick)
    assignment_rows = _checked_assignments(assignments)

    if type(expected_posting_revision) is not int or expected_posting_revision != posting.revision:
        raise OccupationError("stale_posting_revision", "The work posting changed; refresh before applying.")
    if not actor.active:
        raise OccupationError("actor_inactive", "Only an active character can take a job.")
    if not actor.co_located:
        raise OccupationError("actor_not_at_workplace", "The character must be present at the posting location.")
    if posting.status != "open":
        raise OccupationError("posting_closed", "This work posting is closed.")
    if tick < posting.valid_from_tick or (
        posting.valid_until_tick is not None and tick >= posting.valid_until_tick
    ):
        raise OccupationError("posting_out_of_date", "This work posting is not valid at the current time.")
    if posting.required_skill is not None:
        skill = actor.skills.skills.get(posting.required_skill.skill_id)
        if skill is None or skill.level < posting.required_skill.minimum_level:
            raise OccupationError("required_skill_missing", "The required skill level has not been met.")

    if any(item.actor_id == actor.actor_id and item.ended_tick is None for item in assignment_rows):
        raise OccupationError("actor_already_employed", "The character already has an active job.")
    active_at_posting = sum(
        item.posting_id == posting.posting_id and item.ended_tick is None for item in assignment_rows
    )
    if active_at_posting >= posting.slots:
        raise OccupationError("posting_full", "This work posting has no remaining slots.")

    assignment_id = occupation_assignment_id(source, actor.actor_id, posting.posting_id)
    if any(item.assignment_id == assignment_id for item in assignment_rows):
        raise OccupationError("assignment_id_conflict", "This appointment event already identifies an assignment.")
    return WorkAssignment(
        assignment_id=assignment_id,
        actor_id=actor.actor_id,
        posting_id=posting.posting_id,
        employer_id=posting.employer_id,
        location_id=posting.location_id,
        title=posting.title,
        required_skill=posting.required_skill,
        started_tick=tick,
        revision=1,
        source=source,
    )


def resign_occupation(
    assignment: WorkAssignment,
    *,
    actor: OccupationActorContext,
    expected_assignment_revision: int,
    effective_tick: int,
    source: OccupationSource,
) -> WorkAssignment:
    """End the actor's assignment while retaining its ID and appointment source.

    Remote resignation is allowed; the trusted host still has to establish the
    actor's active status and bind the request to the assignment holder.
    """

    assignment = WorkAssignment.model_validate(assignment.model_dump(mode="python"))
    actor = OccupationActorContext.model_validate(actor.model_dump(mode="python"))
    source = _live_event_source(source)
    tick = _checked_tick(effective_tick)

    if type(expected_assignment_revision) is not int or expected_assignment_revision != assignment.revision:
        raise OccupationError("stale_assignment_revision", "The assignment changed; refresh before resigning.")
    if not actor.active:
        raise OccupationError("actor_inactive", "Only an active character can resign from a job.")
    if actor.actor_id != assignment.actor_id:
        raise OccupationError("not_assignment_holder", "A character can only resign from their own job.")
    if assignment.ended_tick is not None:
        raise OccupationError("assignment_not_active", "This job has already ended.")
    if tick < assignment.started_tick:
        raise OccupationError("tick_precedes_assignment", "A job cannot end before it starts.")
    if assignment.revision >= _MAX_REVISION:
        raise OccupationError("assignment_revision_exhausted", "The assignment revision cannot be advanced.")
    if assignment.source.kind == "event" and source.reference_id == assignment.source.reference_id:
        raise OccupationError("source_event_reused", "Resignation must use a new source event.")

    values = assignment.model_dump(mode="python")
    values.update(ended_tick=tick, revision=assignment.revision + 1, end_source=source)
    return WorkAssignment.model_validate(values)


def _live_event_source(source: OccupationSource) -> OccupationSource:
    source = OccupationSource.model_validate(source.model_dump(mode="python"))
    if source.kind != "event":
        raise OccupationError("event_source_required", "Live occupation changes require a committed source event.")
    return source


def _checked_tick(value: int) -> int:
    if type(value) is not int or not _MIN_TICK <= value <= _MAX_TICK:
        raise OccupationError("invalid_effective_tick", "The effective world tick is invalid.")
    return value


def _checked_assignments(assignments: Iterable[WorkAssignment]) -> tuple[WorkAssignment, ...]:
    rows = tuple(WorkAssignment.model_validate(item.model_dump(mode="python")) for item in assignments)
    ids = [item.assignment_id for item in rows]
    if len(ids) != len(set(ids)):
        raise OccupationError("duplicate_assignment_id", "Occupation assignment IDs must be unique.")
    return rows
