"""Pure rules for private, non-adjudicative speech between co-located people."""

from __future__ import annotations

from uuid import UUID, uuid5

from world_engine.domain.components import KnowledgeRecord, ProvenanceReference, RelationRecord
from world_engine.domain.gameplay_events import (
    KnowledgeAcquiredEvent,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
)
from world_engine.domain.investigation import entity_visible_to_actor
from world_engine.domain.models import WorldEntity, WorldState

from .contracts import RuleContext, SpeakIntent


class SpeechActionError(ValueError):
    """A bounded rule rejection for speech that cannot happen in this scene."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def private_reply_available(
    parent: KnowledgeRecord | None, *, speaker_id: UUID, recipient_id: UUID, tick: int, answered: set[UUID],
) -> bool:
    return (
        parent is not None and parent.kind == "memory" and parent.owner_id == recipient_id
        and parent.holder_id == speaker_id and parent.subject_id == recipient_id
        and parent.visibility.policy == "subject_ids"
        and set(parent.visibility.subject_ids) == {recipient_id, speaker_id}
        and parent.received_tick <= tick and parent.id not in answered
    )


def build_private_speech_events(
    state: WorldState,
    speaker: WorldEntity,
    intent: SpeakIntent,
    context: RuleContext,
) -> tuple[KnowledgeAcquiredEvent | RelationEstablishedEvent | RelationDimensionChangedEvent, ...]:
    """Create one pair-private memory for each recipient, with stable IDs.

    Unprompted speech just records an utterance. An explicit reply to a real
    private utterance records reciprocal familiarity for the same pair.
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

    reply_to = intent.reply_to_memory_id
    if reply_to is not None:
        parent = next((item for item in state.knowledge if item.id == reply_to), None)
        answered = {item.speech_reply_to for item in state.knowledge if item.speech_reply_to is not None}
        if not private_reply_available(
            parent, speaker_id=speaker.id, recipient_id=targets[0].id, tick=state.current_tick, answered=answered,
        ):
            raise SpeechActionError("speech_reply_unavailable", "That private utterance is unavailable for a first reply.")

    events: list[KnowledgeAcquiredEvent | RelationEstablishedEvent | RelationDimensionChangedEvent] = []
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
            speech_reply_to=reply_to,
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
        if reply_to is not None:
            events.extend(_reciprocal_familiarity(state, speaker.id, target.id, event_id, context.world_id))
    return tuple(events)


def _reciprocal_familiarity(state: WorldState, speaker: UUID, recipient: UUID, evidence: UUID, world_id: UUID):
    pair = {speaker, recipient}
    for observer, person in ((speaker, recipient), (recipient, speaker)):
        relations = [row for row in state.relations if (row.from_id, row.to_id) == (observer, person)]
        if len(relations) > 1:
            raise SpeechActionError("speech_relation_ambiguous", "This pair has more than one matching relationship.")
        change_id = uuid5(evidence, f"familiarity:{observer}:{person}")
        visibility = {"policy": "subject_ids", "subject_ids": sorted(map(str, pair))}
        if not relations:
            yield RelationEstablishedEvent(
                event_id=change_id,
                relation=RelationRecord(
                    id=uuid5(world_id, f"relation:{observer}:{person}"), from_id=observer, to_id=person,
                    dimensions={"familiarity": "1"}, updated_tick=state.current_tick, evidence_refs=[evidence],
                ), visibility=visibility,
            )
            continue
        relation = relations[0]
        before = relation.dimensions.get("familiarity")
        if before is None or before < 100:
            yield RelationDimensionChangedEvent(
                event_id=change_id, relation_id=relation.id, dimension="familiarity", expected_previous=before,
                value=min(100, (before or 0) + 1), evidence_refs=[evidence], visibility=visibility,
            )
