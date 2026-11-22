"""Deterministic authorization checks for destroying authored physical evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from .adjudication import historical_evidence_digest
from .components import HistoricalEvidenceArtifact
from .models import WorldEntity


HISTORICAL_EVIDENCE_DESTRUCTION_POLICY = "investigation.evidence_destruction.v1"


class DestructionKnowledge(Protocol):
    owner_id: UUID | None
    holder_id: UUID | None
    subject_id: UUID | None
    kind: str
    received_tick: int
    valid_from_tick: int | None


class DestructionState(Protocol):
    current_tick: int
    entities: Mapping[UUID, WorldEntity]
    knowledge: list[DestructionKnowledge]


def validate_destroyable_historical_item(
    state: DestructionState,
    *,
    actor_id: UUID,
    target_id: UUID,
    expected_evidence_id: UUID | None = None,
    expected_evidence_hash: str | None = None,
) -> tuple[WorldEntity, HistoricalEvidenceArtifact, UUID]:
    """Return the checked local item, artifact and location without exposing its claim."""
    actor = state.entities.get(actor_id)
    target = state.entities.get(target_id)
    if (
        actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
        or actor.location_id is None or target is None or target.kind != "item" or target.status != "active"
    ):
        raise ValueError("historical evidence item or acting character is unavailable")
    location_id = actor.location_id
    location = state.entities.get(location_id)
    if location is None or location.kind not in {"place", "location"} or location.status != "active":
        raise ValueError("historical evidence destruction requires an active local place")

    raw_place = target.components.get("place")
    nested_parent = raw_place.get("parent_id") if isinstance(raw_place, Mapping) else None
    raw_parent = target.components.get("parent_id")
    parent_id = raw_parent or nested_parent
    is_directly_local = target.location_id == location_id
    is_nested_local = target.location_id is None and str(parent_id) == str(location_id)
    if not (is_directly_local or is_nested_local):
        raise ValueError("historical evidence item is not local to the actor")
    if target.components.get("hidden") is True and not any(
        record.subject_id == target.id
        and actor_id in {record.owner_id, record.holder_id}
        and record.kind == "observation"
        and record.received_tick <= state.current_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
        for record in state.knowledge
    ):
        raise ValueError("historical evidence item is not visible to the actor")
    try:
        artifact = HistoricalEvidenceArtifact.model_validate(target.components.get("historical_evidence"))
    except Exception as exc:
        raise ValueError("item does not contain a valid registered historical artifact") from exc
    if expected_evidence_id is not None and artifact.evidence_id != expected_evidence_id:
        raise ValueError("historical evidence artifact ID changed")
    if expected_evidence_hash is not None and historical_evidence_digest(artifact) != expected_evidence_hash:
        raise ValueError("historical evidence artifact digest changed")
    return target, artifact, location_id
