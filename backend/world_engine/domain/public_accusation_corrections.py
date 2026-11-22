"""Deterministic planning for public corrections of disconfirmed accusations."""

from __future__ import annotations

from uuid import UUID, uuid5

from .errors import InvalidEvent
from .gameplay_events import PublicAccusationCorrectionRecordedEvent
from .investigation import entity_visible_to_actor
from .investigation_cases import PublicAccusationCorrection
from .models import WorldState


_UNAVAILABLE = "Public accusation correction is unavailable."


def plan_public_accusation_correction(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID,
    case_file_id: UUID,
    expected_revision: int,
    source_closure_event_id: UUID,
) -> PublicAccusationCorrectionRecordedEvent:
    """Plan a zero-time public correction without changing the case or relation."""
    actor = state.entities.get(actor_id)
    case_file = next((item for item in state.investigation_cases if item.id == case_file_id), None)
    if (
        actor is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or case_file is None
        or case_file.owner_id != actor_id
        or case_file.status != "closed"
        or case_file.outcome != "disconfirmed"
        or not case_file.public_accusation
        or case_file.suspect_id is None
        or case_file.revision != expected_revision
        or case_file.closed_tick is None
        or case_file.closed_tick > state.current_tick
        or any(item.case_file_id == case_file_id for item in state.public_accusation_corrections)
        or any(item.source_closure_event_id == source_closure_event_id
               for item in state.public_accusation_corrections)
    ):
        raise InvalidEvent(_UNAVAILABLE)

    suspect = state.entities.get(case_file.suspect_id)
    location = state.entities.get(actor.location_id) if actor.location_id is not None else None
    if (
        suspect is None
        or suspect.kind not in {"character", "person"}
        or suspect.status != "active"
        or actor.location_id is None
        or suspect.location_id != actor.location_id
        or location is None
        or location.kind not in {"place", "location"}
        or location.status != "active"
        or not entity_visible_to_actor(state, actor_id, suspect.id)
    ):
        raise InvalidEvent(_UNAVAILABLE)

    event_id = uuid5(command_id, f"{ruleset_version}:investigation_public_accusation_corrected")
    if source_closure_event_id == event_id:
        raise InvalidEvent(_UNAVAILABLE)
    record = PublicAccusationCorrection(
        event_id=event_id,
        case_file_id=case_file.id,
        expected_revision=case_file.revision,
        source_closure_event_id=source_closure_event_id,
        actor_id=actor_id,
        suspect_id=suspect.id,
        corrected_tick=state.current_tick,
        location_id=actor.location_id,
    )
    return PublicAccusationCorrectionRecordedEvent(
        event_id=event_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        record=record,
    )
