"""Pure rules for private, non-adjudicative speech between co-located people."""

from __future__ import annotations

from uuid import UUID, uuid5

from world_engine.domain.components import KnowledgeRecord, ProvenanceReference
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.investigation import entity_visible_to_actor
from world_engine.domain.models import WorldEntity, WorldState

from .contracts import RuleContext, SpeakIntent


class SpeechActionError(ValueError):
    """A bounded rule rejection for speech that cannot happen in this scene."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_private_speech_events(
    state: WorldState,
    speaker: WorldEntity,
    intent: SpeakIntent,
    context: RuleContext,
) -> tuple[KnowledgeAcquiredEvent, ...]:
    """Create one pair-private memory for each recipient, with stable IDs.

    The record is a memory that an utterance happened. It has no typed fact,
    assessment, relationship effect, or response attached to it.
    """
    if speaker.id != context.actor_id or speaker.status != "active":
        raise SpeechActionError("speech_actor_unavailable", "The speaker is not active in the current scene.")
    if speaker.id not in state.entities or state.entities[speaker.id] != speaker:
        raise SpeechActionError("speech_actor_unavailable", "The speaker is not active in the current scene.")

    place = state.entities.get(speaker.location_id) if speaker.location_id is not None else None
    if (
        place is None
        or place.kind not in {"place", "location"}
        or place.status != "active"
        or not entity_visible_to_actor(state, speaker.id, place.id)
    ):
        raise SpeechActionError("speech_scene_unavailable", "Speech needs an active, visible shared scene.")

    targets: list[WorldEntity] = []
    for target_id in intent.target_ids:
        target = state.entities.get(target_id)
        if (
            target is None
            or target.id == speaker.id
            or target.kind not in {"character", "person"}
            or target.status != "active"
            or target.location_id != speaker.location_id
            or not entity_visible_to_actor(state, speaker.id, target.id)
            or not entity_visible_to_actor(state, target.id, speaker.id)
        ):
            # Keep unknown, hidden, and out-of-scene references indistinguishable.
            raise SpeechActionError(
                "speech_recipient_unavailable",
                "Every recipient must be another active person perceptible in the current scene.",
            )
        targets.append(target)

    events: list[KnowledgeAcquiredEvent] = []
    for target in sorted(targets, key=lambda item: str(item.id)):
        audience = sorted((str(speaker.id), str(target.id)))
        event_id = uuid5(context.command_id, f"speech.v1:{speaker.id}:{target.id}")
        record_id = uuid5(event_id, "private-memory")
        speaker_name = speaker.name or "A person"
        target_name = target.name or "another person"
        record = KnowledgeRecord(
            id=record_id,
            owner_id=speaker.id,
            holder_id=target.id,
            subject_id=speaker.id,
            claim=f'{speaker_name} privately said to {target_name}: 「{intent.utterance}」',
            received_tick=state.current_tick,
            kind="memory",
            fact=None,
            assessment=None,
            provenance=[ProvenanceReference(
                kind="observation",
                reference_id=str(context.command_id),
                notes="A private utterance memory; the words are not a verified world fact.",
            )],
            visibility={"policy": "subject_ids", "subject_ids": audience},
        )
        events.append(KnowledgeAcquiredEvent(
            event_id=event_id,
            record=record,
            visibility={"policy": "subject_ids", "subject_ids": audience},
        ))
    return tuple(events)
