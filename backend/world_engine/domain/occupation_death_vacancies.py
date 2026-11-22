"""Typed, pure employment closure contracts for newly terminal workers.

The host owns death-event causality. It supplies the exact actor-to-terminal-
event map derived from the authoritative preceding command batch; this module
only closes matching active assignments and never hires a replacement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StrictInt, model_validator

from .json_types import JsonObject
from .occupations import OccupationModel, OccupationSource, WorkAssignment

OCCUPATION_DEATH_VACANCIES_EVENT_TYPE = "core.occupation_death_vacancies_resolved.v1"
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
_MAX_REVISION = 2**63 - 1
OccupationTick = Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]


class OccupationDeathVacancyError(ValueError):
    """Stable validation and concurrency error for employment closure plans."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OccupationDeathVacancy(OccupationModel):
    """CAS evidence for closing one active assignment after its worker dies."""

    assignment_id: UUID
    actor_id: UUID
    terminal_event_id: UUID
    expected_assignment_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)


class OccupationDeathVacanciesResolvedEvent(OccupationModel):
    """Close every active assignment held by workers newly terminal in a batch.

    Rows are sorted by assignment UUID string and contain the terminal cause
    event directly. The resulting WorkAssignment records use that death event
    as ``end_source`` while this event remains the auditable closure operation.
    """

    type: Literal["core.occupation_death_vacancies_resolved.v1"] = OCCUPATION_DEATH_VACANCIES_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    effective_tick: OccupationTick
    closures: list[OccupationDeathVacancy] = Field(min_length=1, max_length=100_000)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def event_is_command_bound_and_manifest_canonical(self) -> OccupationDeathVacanciesResolvedEvent:
        if not self.ruleset_version or self.ruleset_version.strip() != self.ruleset_version:
            raise ValueError("occupation death-vacancy event requires a canonical ruleset version")
        if self.event_id != occupation_death_vacancies_event_id(self.command_id, self.ruleset_version):
            raise ValueError("occupation death-vacancy event ID must be bound to its command and ruleset")
        assignment_ids = [row.assignment_id for row in self.closures]
        actor_ids = [row.actor_id for row in self.closures]
        if assignment_ids != sorted(assignment_ids, key=str) or len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("occupation death-vacancy rows must be sorted by unique assignment ID")
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("one newly terminal actor cannot hold multiple active assignments")
        if any(row.terminal_event_id == self.event_id for row in self.closures):
            raise ValueError("occupation death-vacancy event cannot cite itself as its terminal cause")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("occupation death-vacancy audit visibility is fixed to author_only")
        return self


def occupation_death_vacancies_event_id(command_id: UUID, ruleset_version: str) -> UUID:
    """Derive the stable closure event ID for this command and ruleset."""

    if not isinstance(command_id, UUID):
        raise ValueError("occupation death-vacancy event IDs require a UUID command ID")
    if (
        not isinstance(ruleset_version, str)
        or not ruleset_version.strip()
        or ruleset_version.strip() != ruleset_version
    ):
        raise ValueError("occupation death-vacancy event IDs require a canonical ruleset version")
    return uuid5(command_id, f"{ruleset_version}:occupation.death_vacancies")


def plan_occupation_death_vacancies(
    assignments: Iterable[WorkAssignment],
    *,
    terminal_transition_event_by_actor: Mapping[UUID, UUID],
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    effective_tick: int,
) -> OccupationDeathVacanciesResolvedEvent | None:
    """Build a sorted exact manifest from active assignments and trusted causes.

    The supplied map must be derived from validated terminal transitions in
    this command's preceding event batch. No event is returned if none of the
    newly terminal actors holds an active assignment.
    """

    tick = _checked_tick(effective_tick)
    causes = _checked_terminal_map(terminal_transition_event_by_actor)
    rows = _checked_assignments(assignments)
    closures: list[OccupationDeathVacancy] = []
    for assignment in rows:
        if assignment.ended_tick is not None:
            continue
        death_event_id = causes.get(assignment.actor_id)
        if death_event_id is None:
            continue
        if assignment.started_tick > tick:
            raise OccupationDeathVacancyError(
                "assignment_not_started", "An employment assignment cannot end before it begins.",
            )
        if assignment.revision >= _MAX_REVISION:
            raise OccupationDeathVacancyError(
                "assignment_revision_exhausted", "The work assignment revision cannot be advanced.",
            )
        closures.append(OccupationDeathVacancy(
            assignment_id=assignment.assignment_id,
            actor_id=assignment.actor_id,
            terminal_event_id=death_event_id,
            expected_assignment_revision=assignment.revision,
        ))
    closures.sort(key=lambda row: str(row.assignment_id))
    if not closures:
        return None
    try:
        return OccupationDeathVacanciesResolvedEvent(
            event_id=occupation_death_vacancies_event_id(command_id, ruleset_version),
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=actor_id,
            ruleset_version=ruleset_version,
            effective_tick=tick,
            closures=closures,
        )
    except Exception as exc:
        raise OccupationDeathVacancyError("invalid_closure_context", "The death-vacancy event context is invalid.") from exc


def apply_occupation_death_vacancies(
    assignments: Iterable[WorkAssignment],
    event: OccupationDeathVacanciesResolvedEvent,
    *,
    terminal_transition_event_by_actor: Mapping[UUID, UUID],
    current_tick: int,
) -> list[WorkAssignment]:
    """Recompute the manifest and return assignments closed at the event tick."""

    tick = _checked_tick(current_tick)
    rows = _checked_assignments(assignments)
    try:
        event = OccupationDeathVacanciesResolvedEvent.model_validate(event.model_dump(mode="python"))
    except Exception as exc:
        raise OccupationDeathVacancyError("invalid_event", "The occupation death-vacancy event is invalid.") from exc
    if event.effective_tick != tick:
        raise OccupationDeathVacancyError("stale_tick", "Employment vacancies must close at the authoritative current tick.")
    expected = plan_occupation_death_vacancies(
        rows,
        terminal_transition_event_by_actor=terminal_transition_event_by_actor,
        world_id=event.world_id,
        branch_id=event.branch_id,
        command_id=event.command_id,
        actor_id=event.actor_id,
        ruleset_version=event.ruleset_version,
        effective_tick=tick,
    )
    if expected is None or expected != event:
        raise OccupationDeathVacancyError(
            "closure_manifest_mismatch", "Death-vacancy rows do not match current assignments and terminal causes.",
        )

    closures = {row.assignment_id: row for row in event.closures}
    result: list[WorkAssignment] = []
    for assignment in rows:
        closure = closures.get(assignment.assignment_id)
        if closure is None:
            result.append(assignment)
            continue
        values = assignment.model_dump(mode="python")
        values.update(
            ended_tick=tick,
            revision=assignment.revision + 1,
            end_source=OccupationSource(kind="event", reference_id=str(closure.terminal_event_id)),
        )
        try:
            result.append(WorkAssignment.model_validate(values))
        except Exception as exc:
            raise OccupationDeathVacancyError(
                "assignment_closure_invalid", "The resulting ended work assignment is invalid.",
            ) from exc
    return result


def _checked_assignments(assignments: Iterable[WorkAssignment]) -> tuple[WorkAssignment, ...]:
    try:
        rows = tuple(WorkAssignment.model_validate(row.model_dump(mode="python")) for row in assignments)
    except Exception as exc:
        raise OccupationDeathVacancyError("assignment_invalid", "An employment assignment is invalid.") from exc
    assignment_ids = [row.assignment_id for row in rows]
    active_actors = [row.actor_id for row in rows if row.ended_tick is None]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise OccupationDeathVacancyError("duplicate_assignment_id", "Assignment IDs must be unique.")
    if len(active_actors) != len(set(active_actors)):
        raise OccupationDeathVacancyError("duplicate_active_actor", "An actor cannot hold multiple active assignments.")
    return rows


def _checked_terminal_map(terminal_events: Mapping[UUID, UUID]) -> dict[UUID, UUID]:
    if not isinstance(terminal_events, Mapping):
        raise OccupationDeathVacancyError("invalid_terminal_map", "A trusted actor-to-terminal-event map is required.")
    result: dict[UUID, UUID] = {}
    for actor_id, event_id in terminal_events.items():
        if not isinstance(actor_id, UUID) or not isinstance(event_id, UUID):
            raise OccupationDeathVacancyError("invalid_terminal_map", "Terminal actor and event IDs must be UUIDs.")
        result[actor_id] = event_id
    return result


def _checked_tick(value: int) -> int:
    if type(value) is not int or not _MIN_TICK <= value <= _MAX_TICK:
        raise OccupationDeathVacancyError("invalid_tick", "The authoritative employment closure tick is invalid.")
    return value
