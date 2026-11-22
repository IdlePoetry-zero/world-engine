"""Recompute employment events from the authoritative world state."""

from typing import TYPE_CHECKING
from uuid import UUID

from .components import SkillsComponent
from .errors import InvalidEvent
from .occupation_events import OccupationAppointedEvent, OccupationResignedEvent
from .occupations import OccupationActorContext, OccupationError, OccupationSource, WorkAssignment, appoint_to_posting, resign_occupation

if TYPE_CHECKING:
    from .models import WorldState


def plan_occupation_change(
    state: "WorldState", *, actor_id: UUID, event_id: UUID,
    subject_id: UUID, expected_revision: int, resign: bool,
) -> WorkAssignment:
    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise InvalidEvent("Occupation change requires an active character.")
    source = OccupationSource(kind="event", reference_id=str(event_id))
    context = OccupationActorContext(actor_id=actor.id, active=True, co_located=False)
    try:
        if resign:
            assignment = next((item for item in state.work_assignments if item.assignment_id == subject_id), None)
            if assignment is None:
                raise InvalidEvent("Work assignment does not exist.")
            return resign_occupation(assignment, actor=context, expected_assignment_revision=expected_revision,
                                    effective_tick=state.current_tick, source=source)
        posting = next((item for item in state.work_postings if item.posting_id == subject_id), None)
        if posting is None:
            raise InvalidEvent("Work posting is unavailable.")
        employer = state.entities.get(posting.employer_id)
        workplace = state.entities.get(posting.location_id)
        if employer is None or workplace is None or employer.status != "active" or workplace.status != "active":
            raise InvalidEvent("Work posting is unavailable.")
        hidden = {entity.id for entity in (employer, workplace) if entity.components.get("hidden") is True}
        if hidden:
            observed = {record.subject_id for record in state.knowledge if record.kind == "observation"
                        and actor.id in {record.owner_id, record.holder_id} and record.received_tick <= state.current_tick
                        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)}
            if not hidden.issubset(observed):
                raise InvalidEvent("Work posting is unavailable.")
        context = OccupationActorContext(
            actor_id=actor.id, active=True, co_located=(actor.location_id == posting.location_id),
            skills=SkillsComponent.model_validate(actor.components.get("skills", {})),
        )
        return appoint_to_posting(posting, state.work_assignments, actor=context,
                                  expected_posting_revision=expected_revision,
                                  effective_tick=state.current_tick, source=source)
    except OccupationError as exc:
        raise InvalidEvent(str(exc)) from exc


def apply_occupation_change(
    state: "WorldState", event: OccupationAppointedEvent | OccupationResignedEvent,
) -> list[WorkAssignment]:
    if event.effective_tick != state.current_tick:
        raise InvalidEvent("Occupation event must use the current world tick.")
    resign = isinstance(event, OccupationResignedEvent)
    subject = event.assignment_id if resign else event.posting_id
    revision = event.expected_assignment_revision if resign else event.expected_posting_revision
    proposed = plan_occupation_change(state, actor_id=event.actor_id, event_id=event.event_id,
                                      subject_id=subject, expected_revision=revision, resign=resign)
    if proposed != event.resulting_assignment:
        raise InvalidEvent("Occupation event does not match its authoritative assignment.")
    if not resign:
        return [*state.work_assignments, proposed]
    return [proposed if assignment.assignment_id == subject else assignment for assignment in state.work_assignments]
