"""Pure rules for private, non-adjudicative speech between co-located people."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid5

from world_engine.domain.components import KnowledgeRecord, ProvenanceReference, RelationRecord
from world_engine.domain.gameplay_events import (
    KnowledgeAcquiredEvent,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
)
from world_engine.domain.investigation import entity_visible_to_actor
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.relationship_policy import (
    INITIAL_RELATIONSHIP_SCORE,
    RelationshipCurveResult,
    settle_relationship_change,
)

from .contracts import RuleContext, SpeakIntent


SOCIAL_WORLD_POLICY_ID = "social_world_v1"
FAMILIARITY_DIMENSION = "familiarity"
_FAMILIARITY_SPEECH_DELTA = Decimal("1")


class SpeechActionError(ValueError):
    """A bounded rule rejection for speech that cannot happen in this scene."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SpeechRelationEvidenceError(ValueError):
    """A private reply receipt cannot support the requested relation effect."""


@dataclass(frozen=True, slots=True)
class SpeechRelationEvidence:
    """Replay-recomputable evidence and numeric plan for one directed relation."""

    relation_id: UUID
    observer_id: UUID
    counterpart_id: UUID
    speech_memory_id: UUID
    expected_previous: Decimal | None
    routine_occurrence: int
    settlement: RelationshipCurveResult


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
    topic_id = intent.topic_record_id
    source_refs = []
    if topic_id is not None:
        source_refs = _topic_speech_sources(state, speaker.id, targets[0].id, topic_id)
        if not source_refs:
            raise SpeechActionError("speech_topic_unavailable", "That topic is unavailable for this private exchange.")

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
            source_refs=source_refs,
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
            if state.metadata.get(SOCIAL_WORLD_POLICY_ID) is True:
                events.extend(build_social_familiarity_events(state, record, context.world_id))
            else:
                events.extend(_reciprocal_familiarity(state, speaker.id, target.id, event_id, context.world_id))
    return tuple(events)


def _topic_speech_sources(state: WorldState, speaker_id: UUID, target_id: UUID, topic_id: UUID) -> list[UUID]:
    requested = next((item for item in state.knowledge if item.id == topic_id), None)
    if requested is None or requested.received_tick > state.current_tick:
        return []

    reporting = requested.topic_inspection in {"discovered", "nothing_hidden_found"}
    original_id = requested.source_refs[0] if reporting else topic_id
    topic = next((item for item in state.knowledge if item.id == original_id), None) if reporting else requested
    if (
        (reporting and requested.owner_id != speaker_id)
        or topic is None or topic.holder_id != speaker_id or topic.kind != "observation"
        or topic.source_event_id is None or topic.received_tick > state.current_tick
        or not any(
            row.holder_id == target_id and row.source_event_id == topic.source_event_id
            and row.kind == "observation" and row.received_tick <= state.current_tick
            for row in state.knowledge
        )
    ):
        return []
    condition_kind = "report_topic_inspection" if reporting else "discuss_known_topic"
    completed = {goal.id for goal in state.goals if goal.owner_id == speaker_id and goal.status == "succeeded"}
    if not any(
        goal.status == "active" and goal.owner_id == speaker_id
        and (goal.deadline is None or state.current_tick <= goal.deadline)
        and all(condition.target_id in completed for condition in goal.conditions if condition.kind == "goal_completed")
        and any(
            condition.kind == condition_kind and condition.subject_id == speaker_id
            and condition.target_id == target_id and condition.evidence_id == original_id
            for condition in goal.conditions
        )
        for goal in state.goals
    ):
        return []
    return [topic_id, original_id] if reporting else [topic_id]


def speech_relation_evidence(
    state: WorldState,
    *,
    memory_id: UUID,
    observer_id: UUID,
    counterpart_id: UUID,
) -> SpeechRelationEvidence | None:
    """Recompute a social-world familiarity receipt from committed speech state.

    A qualifying memory is a pair-private reply to an extant pair-private
    utterance. Each direction is evaluated against its own relation record.
    The routine class is all such replies for that directed pair and the
    ``familiarity`` dimension, so changes in room or wording do not reset the
    ordinal. Prior ordinals are derived only from receipt IDs already recorded
    on that directed relation.

    ``None`` means the world has not opted into ``social_world_v1``. Invalid or
    unbound receipt data raises ``SpeechRelationEvidenceError`` so a reducer can
    reject a forged opt-in event instead of silently applying it.
    """
    if state.metadata.get(SOCIAL_WORLD_POLICY_ID) is not True:
        return None
    if not all(isinstance(value, UUID) and value.int != 0 for value in (memory_id, observer_id, counterpart_id)):
        raise SpeechRelationEvidenceError("A speech relation receipt requires real participant and memory UUIDs.")
    if observer_id == counterpart_id:
        raise SpeechRelationEvidenceError("A speech relation receipt must involve two different people.")

    memories = [record for record in state.knowledge if record.id == memory_id]
    if len(memories) != 1:
        raise SpeechRelationEvidenceError("The cited private speech memory is not present exactly once.")
    memory = memories[0]
    records_by_id = {record.id: record for record in state.knowledge}
    replies_by_parent: dict[UUID, list[KnowledgeRecord]] = {}
    for record in records_by_id.values():
        if record.speech_reply_to is not None:
            replies_by_parent.setdefault(record.speech_reply_to, []).append(record)
    if not _is_reply_memory(memory, records_by_id, replies_by_parent):
        raise SpeechRelationEvidenceError("The cited memory is not a verified private speech reply receipt.")
    if {observer_id, counterpart_id} != {memory.owner_id, memory.holder_id}:
        raise SpeechRelationEvidenceError("The requested relation direction does not match the speech participants.")

    relations = [
        relation for relation in state.relations
        if relation.from_id == observer_id and relation.to_id == counterpart_id
    ]
    if len(relations) != 1:
        raise SpeechRelationEvidenceError("The directed speech relationship must exist exactly once before settlement.")
    relation = relations[0]
    if memory_id in relation.evidence_refs:
        raise SpeechRelationEvidenceError("The speech memory has already settled for this directed relationship.")

    prior_receipts = {
        reference_id
        for reference_id in relation.evidence_refs
        if reference_id in records_by_id
        and _is_reply_memory(records_by_id[reference_id], records_by_id, replies_by_parent)
        and {records_by_id[reference_id].owner_id, records_by_id[reference_id].holder_id}
        == {observer_id, counterpart_id}
    }
    occurrence = len(prior_receipts) + 1
    before = relation.dimensions.get(FAMILIARITY_DIMENSION)
    result = settle_relationship_change(
        before if before is not None else INITIAL_RELATIONSHIP_SCORE,
        _FAMILIARITY_SPEECH_DELTA,
        routine_occurrence=occurrence,
    )
    return SpeechRelationEvidence(
        relation_id=relation.id,
        observer_id=observer_id,
        counterpart_id=counterpart_id,
        speech_memory_id=memory_id,
        expected_previous=before,
        routine_occurrence=occurrence,
        settlement=result,
    )


def _is_reply_memory(
    memory: KnowledgeRecord,
    records_by_id: dict[UUID, KnowledgeRecord],
    replies_by_parent: dict[UUID, list[KnowledgeRecord]],
) -> bool:
    if (
        memory.kind != "memory"
        or memory.owner_id is None
        or memory.holder_id is None
        or memory.owner_id == memory.holder_id
        or memory.subject_id != memory.owner_id
        or memory.fact is not None
        or memory.assessment is not None
        or memory.speech_reply_to is None
        or memory.visibility.policy != "subject_ids"
        or set(memory.visibility.subject_ids) != {memory.owner_id, memory.holder_id}
    ):
        return False
    parent = records_by_id.get(memory.speech_reply_to)
    if (
        parent is None
        or parent.id == memory.id
        or parent.kind != "memory"
        or parent.owner_id != memory.holder_id
        or parent.holder_id != memory.owner_id
        or parent.subject_id != parent.owner_id
        or parent.fact is not None
        or parent.assessment is not None
        or parent.received_tick > memory.received_tick
        or parent.visibility.policy != "subject_ids"
        or set(parent.visibility.subject_ids) != {memory.owner_id, memory.holder_id}
    ):
        return False
    replies = replies_by_parent.get(parent.id, [])
    return len(replies) == 1 and replies[0].id == memory.id


def build_social_familiarity_events(
    state: WorldState,
    memory: KnowledgeRecord,
    world_id: UUID,
) -> tuple[RelationEstablishedEvent | RelationDimensionChangedEvent, ...]:
    if memory.owner_id is None or memory.holder_id is None:
        raise SpeechActionError("speech_relation_unavailable", "The private speech receipt has no valid participants.")

    pair_ids = {memory.owner_id, memory.holder_id}
    visibility = {"policy": "subject_ids", "subject_ids": sorted(map(str, pair_ids))}
    planning_state = state.model_copy(update={"knowledge": [*state.knowledge, memory]})
    events: list[RelationEstablishedEvent | RelationDimensionChangedEvent] = []

    # A response is an exchange receipt for both directed views. Each relation
    # has its own score, evidence history, and repetition ordinal.
    for observer, counterpart in (
        (memory.holder_id, memory.owner_id),
        (memory.owner_id, memory.holder_id),
    ):
        relations = [
            row for row in planning_state.relations
            if (row.from_id, row.to_id) == (observer, counterpart)
        ]
        if len(relations) > 1:
            raise SpeechActionError("speech_relation_ambiguous", "This pair has more than one matching relationship.")
        if not relations:
            relation_id = uuid5(world_id, f"relation:{observer}:{counterpart}")
            if any(row.id == relation_id for row in planning_state.relations):
                raise SpeechActionError("speech_relation_ambiguous", "This pair has a conflicting relationship identity.")
            relation = RelationRecord(
                id=relation_id,
                from_id=observer,
                to_id=counterpart,
                dimensions={FAMILIARITY_DIMENSION: INITIAL_RELATIONSHIP_SCORE},
                updated_tick=state.current_tick,
                visibility=visibility,
            )
            events.append(RelationEstablishedEvent(
                event_id=uuid5(memory.id, f"{SOCIAL_WORLD_POLICY_ID}:relation:{observer}:{counterpart}"),
                relation=relation,
                visibility=visibility,
            ))
            planning_state = planning_state.model_copy(update={
                "relations": [*planning_state.relations, relation],
            })

        try:
            evidence = speech_relation_evidence(
                planning_state,
                memory_id=memory.id,
                observer_id=observer,
                counterpart_id=counterpart,
            )
        except SpeechRelationEvidenceError as exc:
            raise SpeechActionError("speech_relation_unavailable", "The private reply cannot update familiarity safely.") from exc
        if evidence is None:
            raise SpeechActionError("speech_relation_unavailable", "The social relationship policy is not enabled.")

        events.append(RelationDimensionChangedEvent(
            event_id=uuid5(memory.id, f"{SOCIAL_WORLD_POLICY_ID}:familiarity:{observer}:{counterpart}"),
            relation_id=evidence.relation_id,
            dimension=FAMILIARITY_DIMENSION,
            expected_previous=evidence.expected_previous,
            value=evidence.settlement.resulting_score,
            evidence_refs=[memory.id],
            visibility=visibility,
            social_policy_id=SOCIAL_WORLD_POLICY_ID,
            speech_memory_id=memory.id,
            routine_occurrence=evidence.routine_occurrence,
        ))
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
