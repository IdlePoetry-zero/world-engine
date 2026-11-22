"""Author-scoped maintenance for stale assignments of already-dead workers.

This versioned event is intentionally separate from same-command death
vacancy derivation. It handles historical saves whose terminal transition
predates automatic vacancy events, without claiming or rewriting the original
death causation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StrictInt, model_validator

from .json_types import JsonObject
from .occupations import OccupationModel, OccupationSource, WorkAssignment

if TYPE_CHECKING:
    from .models import WorldState

OCCUPATION_LEGACY_VACANCIES_EVENT_TYPE = "core.occupation_legacy_vacancies_resolved.v1"
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
_MAX_REVISION = 2**63 - 1
OccupationTick = Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]


class OccupationLegacyVacancyError(ValueError):
    """Stable validation error for legacy assignment cleanup plans."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OccupationLegacyVacancy(OccupationModel):
    """CAS row for one stale active assignment held by an already-dead worker."""

    assignment_id: UUID
    actor_id: UUID
    expected_assignment_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)


class OccupationLegacyVacanciesResolvedEvent(OccupationModel):
    """Command-scoped author-only cleanup of known historical dead-worker jobs."""

    type: Literal["core.occupation_legacy_vacancies_resolved.v1"] = OCCUPATION_LEGACY_VACANCIES_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    effective_tick: OccupationTick
    vacancies: list[OccupationLegacyVacancy] = Field(min_length=1, max_length=100_000)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def event_is_bound_and_manifest_canonical(self) -> OccupationLegacyVacanciesResolvedEvent:
        if not self.ruleset_version or self.ruleset_version.strip() != self.ruleset_version:
            raise ValueError("legacy vacancy event requires a canonical ruleset version")
        if self.event_id != occupation_legacy_vacancies_event_id(self.command_id, self.ruleset_version):
            raise ValueError("legacy vacancy event ID must be bound to its command and ruleset")
        assignment_ids = [row.assignment_id for row in self.vacancies]
        actor_ids = [row.actor_id for row in self.vacancies]
        if assignment_ids != sorted(assignment_ids, key=str) or len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("legacy vacancy rows must be sorted by unique assignment ID")
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("a dead worker cannot hold multiple active assignments")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("legacy vacancy details are fixed to author_only visibility")
        return self


def occupation_legacy_vacancies_event_id(command_id: UUID, ruleset_version: str) -> UUID:
    """Derive the single stable maintenance event ID for this command/ruleset."""

    if not isinstance(command_id, UUID):
        raise ValueError("legacy vacancy event IDs require a UUID command ID")
    if (
        not isinstance(ruleset_version, str)
        or not ruleset_version.strip()
        or ruleset_version.strip() != ruleset_version
    ):
        raise ValueError("legacy vacancy event IDs require a canonical ruleset version")
    return uuid5(command_id, f"{ruleset_version}:occupation.legacy_vacancies")


def plan_legacy_occupation_vacancies(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
) -> OccupationLegacyVacanciesResolvedEvent | None:
    """Select all active assignments held by currently dead workers, exactly.

    Retired, living, already-ended, and missing workers are not auto-closed.
    This planner makes no assertion about when or why a historical worker died.
    """

    from .models import WorldState

    if not isinstance(state, WorldState):
        raise OccupationLegacyVacancyError("invalid_world_state", "A validated world state is required.")
    try:
        state = WorldState.model_validate(state.model_dump(mode="python"))
    except Exception as exc:
        raise OccupationLegacyVacancyError("invalid_world_state", "The world state is invalid for vacancy cleanup.") from exc
    assignments = _checked_assignments(state.work_assignments)
    vacancies: list[OccupationLegacyVacancy] = []
    for assignment in assignments:
        if assignment.ended_tick is not None:
            continue
        worker = state.entities.get(assignment.actor_id)
        if worker is None or worker.kind not in {"character", "person"} or worker.status != "dead":
            continue
        if assignment.started_tick > state.current_tick:
            raise OccupationLegacyVacancyError(
                "assignment_not_started", "A work assignment cannot end before it begins.",
            )
        if assignment.revision >= _MAX_REVISION:
            raise OccupationLegacyVacancyError(
                "assignment_revision_exhausted", "The work assignment revision cannot be advanced.",
            )
        vacancies.append(OccupationLegacyVacancy(
            assignment_id=assignment.assignment_id,
            actor_id=assignment.actor_id,
            expected_assignment_revision=assignment.revision,
        ))
    vacancies.sort(key=lambda row: str(row.assignment_id))
    if not vacancies:
        return None
    try:
        return OccupationLegacyVacanciesResolvedEvent(
            event_id=occupation_legacy_vacancies_event_id(command_id, ruleset_version),
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=actor_id,
            ruleset_version=ruleset_version,
            effective_tick=state.current_tick,
            vacancies=vacancies,
        )
    except Exception as exc:
        raise OccupationLegacyVacancyError("invalid_event_context", "The legacy vacancy event context is invalid.") from exc


def apply_legacy_occupation_vacancies(
    state: WorldState,
    event: OccupationLegacyVacanciesResolvedEvent,
) -> list[WorkAssignment]:
    """Recompute the exact live-state manifest and return closed assignments."""

    from .models import WorldState

    if not isinstance(state, WorldState):
        raise OccupationLegacyVacancyError("invalid_world_state", "A validated world state is required.")
    try:
        state = WorldState.model_validate(state.model_dump(mode="python"))
        event = OccupationLegacyVacanciesResolvedEvent.model_validate(event.model_dump(mode="python"))
    except Exception as exc:
        raise OccupationLegacyVacancyError("invalid_input", "The state or legacy vacancy event is invalid.") from exc
    if event.effective_tick != state.current_tick:
        raise OccupationLegacyVacancyError("stale_tick", "Legacy vacancy cleanup must use the current world tick.")
    expected = plan_legacy_occupation_vacancies(
        state,
        world_id=event.world_id,
        branch_id=event.branch_id,
        command_id=event.command_id,
        actor_id=event.actor_id,
        ruleset_version=event.ruleset_version,
    )
    if expected is None or expected != event:
        raise OccupationLegacyVacancyError(
            "vacancy_manifest_mismatch", "Legacy vacancy rows do not match all currently dead assignment holders.",
        )

    closures = {row.assignment_id: row for row in event.vacancies}
    result: list[WorkAssignment] = []
    for assignment in _checked_assignments(state.work_assignments):
        vacancy = closures.get(assignment.assignment_id)
        if vacancy is None:
            result.append(assignment)
            continue
        values = assignment.model_dump(mode="python")
        values.update(
            ended_tick=event.effective_tick,
            revision=assignment.revision + 1,
            end_source=OccupationSource(kind="event", reference_id=str(event.event_id)),
        )
        try:
            result.append(WorkAssignment.model_validate(values))
        except Exception as exc:
            raise OccupationLegacyVacancyError(
                "assignment_closure_invalid", "The resulting ended work assignment is invalid.",
            ) from exc
    return result


def _checked_assignments(assignments: Iterable[WorkAssignment]) -> tuple[WorkAssignment, ...]:
    try:
        rows = tuple(WorkAssignment.model_validate(row.model_dump(mode="python")) for row in assignments)
    except Exception as exc:
        raise OccupationLegacyVacancyError("assignment_invalid", "An employment assignment is invalid.") from exc
    assignment_ids = [row.assignment_id for row in rows]
    active_actors = [row.actor_id for row in rows if row.ended_tick is None]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise OccupationLegacyVacancyError("duplicate_assignment_id", "Assignment IDs must be unique.")
    if len(active_actors) != len(set(active_actors)):
        raise OccupationLegacyVacancyError("duplicate_active_actor", "An actor cannot hold multiple active assignments.")
    return rows
