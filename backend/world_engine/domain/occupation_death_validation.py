"""Bind automatic posting vacancies to authoritative death transitions."""

from collections.abc import Sequence
from uuid import UUID

from .errors import InvalidEvent
from .gameplay_events import BackgroundDeprivationResolvedEvent, DamageAppliedEvent, LifeEndedEvent
from .models import DomainEvent, EntityFieldChangedEvent, WorldState
from .occupation_death_vacancies import (
    OccupationDeathVacanciesResolvedEvent,
    OccupationDeathVacancyError,
    plan_occupation_death_vacancies,
)
from .occupation_legacy_vacancies import OccupationLegacyVacanciesResolvedEvent
from .reducers import apply_event


def derive_death_vacancies(
    state: WorldState, events: Sequence[DomainEvent], *, world_id: UUID, branch_id: UUID,
    command_id: UUID, actor_id: UUID, ruleset_version: str,
) -> OccupationDeathVacanciesResolvedEvent | None:
    """Derive employment effects from actual transitions, including computed damage."""
    workers = {row.actor_id for row in state.work_assignments if row.ended_tick is None}
    if not workers:
        return None
    candidates = [_possible_deaths(event) & workers for event in events]
    if not any(candidates):
        return None

    causes: dict[UUID, UUID] = {}
    for event, subjects in zip(events, candidates, strict=True):
        before = state
        state = apply_event(state, event)
        for subject in subjects:
            if before.entities[subject].status != "dead" and state.entities[subject].status == "dead":
                causes[subject] = event.event_id

    try:
        return plan_occupation_death_vacancies(
            state.work_assignments, terminal_transition_event_by_actor=causes,
            world_id=world_id, branch_id=branch_id, command_id=command_id,
            actor_id=actor_id, ruleset_version=ruleset_version, effective_tick=state.current_tick,
        )
    except OccupationDeathVacancyError as exc:
        raise InvalidEvent(str(exc)) from exc


def validate_persisted_death_vacancies(
    state: WorldState, events: list[DomainEvent], *, world_id: UUID, branch_id: UUID,
    command_id: UUID, actor_id: UUID, ruleset_version: str,
) -> list[DomainEvent]:
    """Verify a stored suffix before exposing its base events to other validators.

    Historical batches may lack a vacancy suffix. Replaying or importing them
    preserves their original state hashes. Every new live commit derives it.
    """
    maintenance = [event for event in events if isinstance(event, OccupationLegacyVacanciesResolvedEvent)]
    if maintenance:
        event = maintenance[0]
        if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
            world_id, branch_id, command_id, actor_id, ruleset_version,
        ):
            raise InvalidEvent("Stored employment maintenance must be a sole event scoped to its command.")
    vacancies = [event for event in events if isinstance(event, OccupationDeathVacanciesResolvedEvent)]
    if not vacancies:
        return events
    if len(vacancies) != 1 or events[-1] is not vacancies[0]:
        raise InvalidEvent("A death-vacancy event must be the final event of its command.")
    base = events[:-1]
    expected = derive_death_vacancies(
        state, base, world_id=world_id, branch_id=branch_id, command_id=command_id,
        actor_id=actor_id, ruleset_version=ruleset_version,
    )
    if vacancies[0] != expected:
        raise InvalidEvent("Employment closure must match deaths and assignments in the same command.")
    return base


def _possible_deaths(event: DomainEvent) -> set[UUID]:
    if isinstance(event, LifeEndedEvent):
        return {event.entity_id}
    if isinstance(event, EntityFieldChangedEvent) and event.field_name == "status" and event.value == "dead":
        return {event.entity_id}
    if isinstance(event, DamageAppliedEvent):
        return {event.target_id}
    if isinstance(event, BackgroundDeprivationResolvedEvent):
        return {subject.entity_id for subject in event.subjects if subject.terminal_status == "dead"}
    return set()
