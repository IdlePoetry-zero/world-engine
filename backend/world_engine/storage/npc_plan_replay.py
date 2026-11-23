"""Reconstruct trusted NPC plan validation context from committed source rows.

This module is pure: storage hosts supply the branch-cutoff-filtered travel
receipts, and the helper performs no repository reads or writes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.models import EventRecord, WorldState
from world_engine.domain.npc_plan_events import NpcPlanRevisedEvent
from world_engine.domain.npc_plan_review_events import NpcPlanReviewResolvedEvent
from world_engine.domain.promise_validation import requires_promise_validation, validate_promise_batch
from world_engine.domain.story_nodes import StoryMilestoneResolvedEvent, story_reaction_event_id
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.rules.engine import RuleEngine
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.simulation.npc_capabilities import NpcCapabilityRegistry
from world_engine.simulation.npc_plan_validation import (
    NpcPlanSimulationBatchValidation,
    validate_npc_plan_simulation_batch,
)
from world_engine.simulation.contracts import SimulationContext


_NPC_PLAN_EVENT_TYPES = frozenset({
    "core.npc_plan_created.v1",
    "core.npc_plan_revised.v1",
    "core.npc_plan_review_resolved.v1",
})
_BACKUP_COMMAND_TYPE = "backup.restored_event_batch"
_SIMULATION_COMMAND_TYPE = "simulation.window"


def replay_npc_plan_batch(
    before_state: WorldState,
    source_batch: Sequence[EventRecord],
    *,
    journey_resolution_receipts: Mapping[UUID, TravelJourneyChangedEvent] | None = None,
    source_command_type: str | None = None,
    source_command_payload: Mapping[str, object] | None = None,
    registry: NpcCapabilityRegistry | None = None,
    rule_engine: RuleEngine | None = None,
) -> NpcPlanSimulationBatchValidation | None:
    """Recompute one committed NPC plan batch from its original event context.

    ``None`` means the source batch contains no NPC plan lifecycle event. The
    state version and simulation identity are taken from the source EventRecord
    envelope; restored CommandRows are used only to verify their source mapping.
    """
    if not source_batch:
        return None
    first = source_batch[0]
    if not any(record.type in _NPC_PLAN_EVENT_TYPES for record in source_batch):
        return None
    _validate_source_batch_envelope(source_batch)
    _validate_source_command_mapping(
        first, len(source_batch), before_state, source_command_type, source_command_payload,
    )

    parsed = tuple(_parse_source_event(record) for record in source_batch)
    reaction_ids = {reaction.reaction_id for reaction in before_state.story_reactions}
    plan_event_count = len(parsed)
    for outcome in reversed(parsed):
        if (not isinstance(outcome, StoryMilestoneResolvedEvent)
                or outcome.milestone.schedule_id not in reaction_ids
                or outcome.event_id != story_reaction_event_id(first.command_id, outcome.milestone.schedule_id)):
            break
        plan_event_count -= 1
    parsed = parsed[:plan_event_count]
    input_version = first.state_version_after - 1
    if input_version < 0:
        raise InvalidEvent("NPC plan source batch has no preceding branch version.")
    if any(
        getattr(event, "world_id", first.world_id) != first.world_id
        or getattr(event, "branch_id", first.branch_id) != first.branch_id
        or getattr(event, "command_id", first.command_id) != first.command_id
        or getattr(event, "ruleset_version", first.ruleset_version) != first.ruleset_version
        for event in parsed
    ):
        raise InvalidEvent("NPC plan events differ from their source event-record context.")

    reviews = [event for event in parsed if isinstance(event, NpcPlanReviewResolvedEvent)]
    historically_controlled = {
        review.actor_id for review in reviews
        if review.source == "policy"
        and review.outcome == "skipped"
        and review.reason == "npc_review_actor_user_controlled"
    }
    control_interruptions = [
        event for event in parsed
        if isinstance(event, NpcPlanRevisedEvent)
        and event.control_interruption == "account_control"
    ]
    if control_interruptions:
        if len(parsed) != 1 or len(control_interruptions) != 1 or parsed[0] is not control_interruptions[0]:
            raise InvalidEvent("An account-control plan interruption must be its own single-event batch.")
        historically_controlled.add(control_interruptions[0].actor_id)
    historically_controlled = frozenset(historically_controlled)
    receipts = dict(journey_resolution_receipts or {})
    _validate_receipt_map(receipts)
    if before_state.current_tick >= 2**63 - 1:
        raise InvalidEvent("NPC plan replay cannot construct a zero-tick plan at the maximum world clock.")
    plan = WorldAdvancePlan(
        started_tick=before_state.current_tick,
        target_tick=before_state.current_tick + 1,
    )
    world_seed = before_state.metadata.get("generation_seed")
    if world_seed is None:
        world_seed = before_state.metadata.get("seed")
    if world_seed is None or world_seed == "":
        world_seed = first.world_id
    context = SimulationContext(
        world_id=first.world_id,
        branch_id=first.branch_id,
        command_id=first.command_id,
        command_actor_id=first.actor_id,
        externally_controlled_actor_ids=historically_controlled,
        world_seed=str(world_seed),
        state_version=input_version,
        ruleset_version=first.ruleset_version,
        journey_resolution_receipts=receipts,
        journey_receipt_branch_ids=frozenset(event.branch_id for event in receipts.values()),
    )
    return validate_npc_plan_simulation_batch(
        before_state,
        parsed,
        plan,
        context,
        journey_resolution_receipts=receipts,
        registry=registry,
        rule_engine=rule_engine,
    )


def validate_archived_promise_batch(
    state: WorldState,
    source_batch: Sequence[EventRecord],
    events: Sequence[object],
    *,
    npc_validation: NpcPlanSimulationBatchValidation | None = None,
) -> None:
    """Validate a promise against the recorded actor, or the recomputed NPC command."""
    if not requires_promise_validation(None, events):
        return
    if npc_validation is None and any(record.type in _NPC_PLAN_EVENT_TYPES for record in source_batch):
        npc_validation = replay_npc_plan_batch(state, source_batch)
    first = source_batch[0]
    validate_promise_batch(
        events if npc_validation is None else npc_validation.core_rule_events,
        world_id=first.world_id, branch_id=first.branch_id, command_id=first.command_id,
        actor_id=first.actor_id if npc_validation is None else npc_validation.actual_npc_actor_id,
        ruleset_version=first.ruleset_version,
        command_type=None if npc_validation is None else npc_validation.action_command_type,
        command_payload=None if npc_validation is None else npc_validation.action_command_payload,
        causation_ids=first.causation_ids, committed_reactions=state.story_reactions,
    )


def _validate_source_batch_envelope(source_batch: Sequence[EventRecord]) -> None:
    first = source_batch[0]
    last = source_batch[-1]
    if (
        not last.commit_boundary
        or any(record.commit_boundary for record in source_batch[:-1])
        or [record.sequence for record in source_batch]
        != list(range(first.sequence, first.sequence + len(source_batch)))
        or any(
            (record.world_id, record.branch_id, record.command_id, record.actor_id,
             record.ruleset_version, record.state_version_after, record.causation_ids,
             record.correlation_id)
            != (first.world_id, first.branch_id, first.command_id, first.actor_id,
                first.ruleset_version, first.state_version_after, first.causation_ids,
                first.correlation_id)
            for record in source_batch
        )
    ):
        raise InvalidEvent("NPC plan source event batch has inconsistent committed metadata.")


def _validate_source_command_mapping(
    first: EventRecord,
    source_event_count: int,
    before_state: WorldState,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    if command_type is None:
        if command_payload is not None:
            raise InvalidEvent("NPC plan source payload has no source command type.")
        return
    if not isinstance(command_payload, Mapping):
        raise InvalidEvent("NPC plan source command metadata is missing its payload.")
    if command_type == _SIMULATION_COMMAND_TYPE:
        if set(command_payload) != {"job_id", "next_tick"}:
            raise InvalidEvent("NPC plan simulation command metadata has an unexpected shape.")
        try:
            UUID(str(command_payload["job_id"]))
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("NPC plan source simulation job ID is malformed.") from exc
        if command_payload["next_tick"] != str(before_state.current_tick):
            raise InvalidEvent("NPC plan source simulation command did not commit at this zero-tick boundary.")
        return
    if command_type == _BACKUP_COMMAND_TYPE:
        if (
            command_payload.get("kind") != "verified_backup_event_batch"
            or command_payload.get("source_request_payload_available") is not False
            or command_payload.get("source_command_id") != str(first.command_id)
            or type(command_payload.get("source_event_count")) is not int
            or command_payload.get("source_event_count") != source_event_count
            or not isinstance(command_payload.get("source_archive_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", command_payload["source_archive_sha256"]) is None
        ):
            raise InvalidEvent("Restored NPC plan command does not map to its exact archived source batch.")
        return
    raise InvalidEvent("NPC plan batch is not bound to a simulation or verified restore command.")


def _parse_source_event(record: EventRecord):
    try:
        return parse_event({
            "type": record.type,
            "event_id": str(record.event_id),
            "visibility": record.visibility,
            **record.payload,
        })
    except Exception as exc:
        raise InvalidEvent("NPC plan source batch contains an invalid event record.") from exc


def _validate_receipt_map(receipts: Mapping[UUID, TravelJourneyChangedEvent]) -> None:
    if len(receipts) > 1000:
        raise InvalidEvent("NPC plan replay accepts at most 1000 prior journey receipts.")
    for journey_id, event in receipts.items():
        if (
            not isinstance(journey_id, UUID)
            or not isinstance(event, TravelJourneyChangedEvent)
            or journey_id != event.journey.journey_id
            or event.operation not in {"arrive", "interrupt"}
        ):
            raise InvalidEvent("NPC plan journey receipts must be actual arrival or interruption events.")

