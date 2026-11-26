"""Settle low-stakes NPC contact at an exact world-calendar boundary."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .ambient_events import ambient_world_seed
from .components import RelationRecord, ScheduledEventRecord, SignedRelationValue, VisibilityScope
from .relationship_policy import settle_relationship_change
from .social_contacts import SocialContactOpportunity, plan_social_contact_candidates

if TYPE_CHECKING:
    from .models import WorldState


SOCIAL_CONTACT_HANDLER = "world.social_contact"
SOCIAL_CONTACT_VERSION = "1.0.0"
SOCIAL_CONTACT_SETTLED = "core.social_contact_settled.v1"


class SocialContactChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opportunity: SocialContactOpportunity
    outcome: Literal["kept_to_self", "acknowledged", "exchanged_words"]


class SocialContactRelationEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_id: UUID
    to_id: UUID
    before: SignedRelationValue
    after: SignedRelationValue


class SocialContactSettledEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["core.social_contact_settled.v1"] = SOCIAL_CONTACT_SETTLED
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    schedule_id: UUID
    occurrence: StrictInt = Field(ge=0, le=999_999)
    checked_tick: StrictInt
    checked_actor_control_ids: tuple[UUID, ...] = Field(max_length=10_000)
    choices: tuple[SocialContactChoice, ...] = Field(max_length=64)
    relationship_effects: tuple[SocialContactRelationEffect, ...] = Field(default=(), max_length=128)
    visibility: dict[str, Literal["author_only"]] = Field(default_factory=lambda: {"policy": "author_only"})


def build_social_contact_schedules(state: WorldState, namespace: UUID) -> tuple[ScheduledEventRecord, ...]:
    day = state.calendar.day_seconds
    origin = state.current_tick - state.current_tick % day
    slots = sorted({day // 2, day * 4 // 5})
    schedules = []
    for slot in slots:
        first_tick = origin + slot
        if first_tick < state.current_tick:
            first_tick += day
        schedules.append(ScheduledEventRecord(
            id=uuid5(namespace, f"{SOCIAL_CONTACT_HANDLER}:{slot}"), due_tick=first_tick,
            phase="resolution", priority=-20, handler_id=SOCIAL_CONTACT_HANDLER,
            handler_version=SOCIAL_CONTACT_VERSION, repeat_ticks=day, max_runs=1_000_000,
        ))
    return tuple(schedules)


def plan_social_contact_settlement(
    state: WorldState, schedule: ScheduledEventRecord, world_id: UUID, branch_id: UUID,
    controlled: frozenset[UUID],
) -> SocialContactSettledEvent:
    event, _ = _settle_contact_slot(state, schedule, world_id, branch_id, controlled)
    return event


def _settle_contact_slot(
    state: WorldState, schedule: ScheduledEventRecord, world_id: UUID, branch_id: UUID,
    controlled: frozenset[UUID],
) -> tuple[SocialContactSettledEvent, list[RelationRecord] | None]:
    if (state.metadata.get("social_world_v1") is not True
        or (schedule.handler_id, schedule.handler_version, schedule.status, schedule.due_tick, schedule.repeat_ticks)
        != (SOCIAL_CONTACT_HANDLER, SOCIAL_CONTACT_VERSION, "queued", state.current_tick, state.calendar.day_seconds)):
        raise ValueError("Social contact requires its registered, due calendar slot")
    world_seed = ambient_world_seed(state, world_id)
    seed = sha256(f"{world_seed}:{branch_id}:{schedule.id}:{schedule.completed_runs}".encode()).hexdigest()
    candidates = plan_social_contact_candidates(state, stable_seed=seed, controlled_actor_ids=controlled)
    choices = []
    for candidate in candidates:
        pair = ":".join(map(str, candidate.participant_ids))
        draw = sha256(f"{seed}:{candidate.location_id}:{pair}".encode("ascii")).digest()[0]
        outcome = "kept_to_self" if draw < 56 else "acknowledged" if draw < 143 else "exchanged_words"
        choices.append(SocialContactChoice(opportunity=candidate, outcome=outcome))
    event_id = uuid5(schedule.id, f"{branch_id}:{schedule.completed_runs}")
    effects, relations = _settle_contact_relations(state, world_id, event_id, choices)
    event = SocialContactSettledEvent(
        event_id=event_id, world_id=world_id, branch_id=branch_id,
        schedule_id=schedule.id, occurrence=schedule.completed_runs, checked_tick=state.current_tick,
        checked_actor_control_ids=tuple(sorted(controlled, key=str)), choices=tuple(choices), relationship_effects=effects,
    )
    return event, relations


def resolve_social_contact_state(state: WorldState, event: SocialContactSettledEvent) -> dict[str, object]:
    schedule = next((row for row in state.scheduled_events if row.id == event.schedule_id), None)
    if schedule is None:
        raise ValueError("The social contact receipt has no registered schedule")
    expected, relations = _settle_contact_slot(
        state, schedule, event.world_id, event.branch_id, frozenset(event.checked_actor_control_ids),
    )
    if event != expected:
        raise ValueError("The social contact receipt does not match its calendar slot and current ties")

    completed = schedule.completed_runs + 1
    repeats = completed < schedule.max_runs
    replacement = ScheduledEventRecord.model_validate(schedule.model_dump(mode="python") | {
        "completed_runs": completed, "status": "queued" if repeats else "completed",
        "due_tick": state.current_tick + state.calendar.day_seconds if repeats else state.current_tick,
        "revision": schedule.revision + 1,
    })
    updates = {"scheduled_events": [replacement if row.id == schedule.id else row for row in state.scheduled_events]}
    if relations is not None:
        updates["relations"] = relations
    return updates


def _settle_contact_relations(
    state: WorldState, world_id: UUID, event_id: UUID, choices: list[SocialContactChoice],
) -> tuple[tuple[SocialContactRelationEffect, ...], list[RelationRecord] | None]:
    relations = {row.id: row for row in state.relations}
    keys: dict[tuple[UUID, UUID], list[UUID]] = {}
    for row in state.relations:
        keys.setdefault((row.from_id, row.to_id), []).append(row.id)
    effects = []
    for choice in choices:
        if choice.outcome != "exchanged_words":
            continue
        a, b = choice.opportunity.participant_ids
        for observer, other in ((a, b), (b, a)):
            ids = keys.get((observer, other), [])
            if len(ids) > 1:
                continue
            if ids:
                relation = relations[ids[0]]
                if (relation.visibility.policy not in {"private", "subject_ids"}
                    or not set(relation.visibility.subject_ids).issubset({a, b})):
                    continue
            else:
                relation = RelationRecord(
                    id=uuid5(world_id, f"relation:{observer}:{other}"), from_id=observer, to_id=other,
                    updated_tick=state.current_tick, visibility=VisibilityScope(policy="subject_ids", subject_ids=[a, b]),
                )
            count = min(relation.routine_social_contacts + 1, 29)
            settlement = settle_relationship_change(
                relation.dimensions.get("familiarity", Decimal(0)), Decimal("0.5"), routine_occurrence=count,
            )
            if (not settlement.applied_delta or len(relation.evidence_refs) >= 10_000
                or (relation.id in relations and not ids)):
                continue
            relations[relation.id] = RelationRecord.model_validate(relation.model_dump(mode="python") | {
                "updated_tick": state.current_tick, "routine_social_contacts": count,
                "revision": relation.revision + 1 if ids else relation.revision,
                "dimensions": {**relation.dimensions, "familiarity": settlement.resulting_score},
                "evidence_refs": [*relation.evidence_refs, event_id],
            })
            effects.append(SocialContactRelationEffect(
                from_id=observer, to_id=other, before=settlement.current_score, after=settlement.resulting_score,
            ))
    return tuple(effects), list(relations.values()) if effects else None
