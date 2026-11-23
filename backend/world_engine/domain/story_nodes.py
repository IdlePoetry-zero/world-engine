"""Declarative milestones evaluated against committed events and world state."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from decimal import Decimal, localcontext
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, StringConstraints, model_validator

from .components import (
    ExactDecimal, GoalCondition, KnowledgeRecord, ProvenanceReference, ResourceName,
    ScheduledEventRecord, SignedRelationValue, SkillsComponent, Tick, VisibilityScope,
)
from .errors import InvalidEvent
from .contests import contest_score
from .json_types import JsonObject
from .npc_goal_completion import goal_condition_met
from .promise_trust_state import PromiseTrustProjection
from .world_books import WorldBookCitation

if TYPE_CHECKING:
    from .models import StoryArrival, WorldState

STORY_NODE_HANDLER = "story.milestone"
STORY_NODE_VERSION = "1.0.0"
STORY_NODE_RESOLVED = "core.story_milestone_resolved.v1"
STORY_OPENING_METADATA = "world_pack_story_opening_id"
MAX_TRACED_STORY_NODES = 128
MAX_STORY_DEPTH = 16
MAX_STORY_FANOUT = 8

_Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")]
_Summary = Annotated[str, StringConstraints(min_length=1, max_length=600)]
_Effect = Literal["occurred", "mitigated", "prevented", "inapplicable"]


class _StoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, revalidate_instances="always")


class StoryGoalGate(_StoryModel):
    kind: Literal["world_condition"]
    owner_id: UUID
    condition: GoalCondition


class StoryCapabilityGate(_StoryModel):
    kind: Literal["capability"]
    actor_id: UUID
    capability: ResourceName


class StorySkillGate(_StoryModel):
    kind: Literal["skill"]
    actor_id: UUID
    skill: ResourceName
    minimum: Annotated[int, Field(strict=True, ge=1, le=100)]


class StoryKnowledgeGate(_StoryModel):
    kind: Literal["knowledge"]
    actor_id: UUID
    record_id: UUID


class StoryPriorOutcomeGate(_StoryModel):
    kind: Literal["prior_outcome"]
    schedule_id: UUID
    outcome: _Key


class StoryEnvironmentGate(_StoryModel):
    kind: Literal["environment"]
    place_id: UUID
    field: ResourceName
    equals: StrictStr | StrictInt | StrictBool


class StoryChallengeGate(_StoryModel):
    kind: Literal["challenge_passed"]
    actor_id: UUID
    challenge_id: UUID


class StoryContestGate(_StoryModel):
    kind: Literal["contest_result"]
    actor_id: UUID
    definition_id: UUID
    result: Literal["win", "loss", "draw", "settled"]


class StoryPromiseTrustGate(_StoryModel):
    kind: Literal["promise_trust"]
    observer_id: UUID
    counterpart_id: UUID
    comparison: Literal["at_least", "at_most"]
    threshold: SignedRelationValue


StoryGate = Annotated[
    StoryGoalGate | StoryCapabilityGate | StorySkillGate | StoryKnowledgeGate | StoryPriorOutcomeGate
    | StoryEnvironmentGate | StoryChallengeGate | StoryContestGate | StoryPromiseTrustGate,
    Field(discriminator="kind"),
]


class StoryTransfer(_StoryModel):
    from_account_id: UUID
    to_account_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))

    @model_validator(mode="after")
    def distinct_accounts(self) -> StoryTransfer:
        if self.from_account_id == self.to_account_id:
            raise ValueError("A story transfer requires two different accounts.")
        return self


class StoryCapabilityAward(_StoryModel):
    actor_id: UUID
    capability: ResourceName


class StoryOutcome(_StoryModel):
    key: _Key
    effect: _Effect
    summary: _Summary
    transfer: StoryTransfer | None = Field(default=None, exclude_if=lambda value: value is None)
    awards: tuple[StoryCapabilityAward, ...] = Field(default=(), max_length=8, exclude_if=lambda rows: not rows)


class StoryVariant(_StoryModel):
    outcome: StoryOutcome
    when: tuple[StoryGate, ...] = Field(min_length=1, max_length=12)


class StoryEnrollment(_StoryModel):
    organization_id: UUID
    actor_id: UUID
    leader: StrictBool = False


class StoryOpportunity(_StoryModel):
    """An explicitly public lead shown before a milestone resolves."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    detail: _Summary
    place_id: UUID
    contact_id: UUID | None = None
    lead_ticks: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
    known_cost: _Summary | None = Field(default=None, exclude_if=lambda value: value is None)
    possible_impact: _Summary | None = Field(default=None, exclude_if=lambda value: value is None)


class StoryNode(_StoryModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    visibility: Literal["public", "author_only"]
    references: tuple[WorldBookCitation, ...] = Field(default=(), max_length=4)
    participant_ids: tuple[UUID, ...] = Field(default=(), max_length=24)
    opportunity: StoryOpportunity | None = Field(default=None, exclude_if=lambda value: value is None)
    opens_place_ids: tuple[UUID, ...] = Field(default=(), max_length=32, exclude_if=lambda ids: not ids)
    opens_organization_ids: tuple[UUID, ...] = Field(default=(), max_length=16, exclude_if=lambda ids: not ids)
    reveals_entry_ids: tuple[UUID, ...] = Field(default=(), max_length=64, exclude_if=lambda ids: not ids)
    enrollments: tuple[StoryEnrollment, ...] = Field(default=(), max_length=32, exclude_if=lambda rows: not rows)
    prevention_possible: StrictBool = False
    variants: tuple[StoryVariant, ...] = Field(default=(), max_length=8)
    fallback: StoryOutcome

    @model_validator(mode="after")
    def validate_outcomes(self) -> StoryNode:
        outcomes = [self.fallback, *(variant.outcome for variant in self.variants)]
        if self.opportunity is not None and self.visibility != "public":
            raise ValueError("Only public milestones may declare a visible opportunity.")
        if len({outcome.key for outcome in outcomes}) != len(outcomes):
            raise ValueError("Story outcome keys must be unique within a milestone.")
        for identifiers in (self.opens_place_ids, self.opens_organization_ids, self.reveals_entry_ids):
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("A story milestone must reference distinct places, organizations, and entries.")
        if len({(row.organization_id, row.actor_id) for row in self.enrollments}) != len(self.enrollments):
            raise ValueError("Story enrollments must be distinct.")
        leaders = [row.organization_id for row in self.enrollments if row.leader]
        if len(set(leaders)) != len(leaders):
            raise ValueError("A single story milestone cannot appoint multiple leaders for one organization.")
        if self.fallback.effect not in {"occurred", "inapplicable"} or self.fallback.transfer is not None:
            raise ValueError("Intervention outcomes require explicit conditions.")
        if not self.prevention_possible and any(variant.outcome.effect in {"prevented", "inapplicable"}
                                                for variant in self.variants):
            raise ValueError("An immutable milestone cannot be stopped by an intervention variant.")
        for outcome in outcomes:
            if (len({(award.actor_id, award.capability) for award in outcome.awards}) != len(outcome.awards)
                    or outcome.awards and outcome.effect not in {"occurred", "mitigated"}):
                raise ValueError("Capability awards require distinct recipients and a successful story outcome.")
        return self


class StoryReaction(_StoryModel):
    version: Literal["1.0.0"] = "1.0.0"
    reaction_id: UUID
    priority: Annotated[int, Field(strict=True, ge=-100, le=100)] = 0
    when: tuple[StoryGate, ...] = Field(min_length=1, max_length=12)
    node: StoryNode


class StoryDispatch(_StoryModel):
    """An author's preapproved bulletin to a finite roster at another place."""

    version: Literal["1.0.0"] = "1.0.0"
    from_place_id: UUID
    to_place_id: UUID
    recipient_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    message: _Summary

    @model_validator(mode="after")
    def validate_route_and_roster(self) -> StoryDispatch:
        if self.from_place_id == self.to_place_id or len(set(self.recipient_ids)) != len(self.recipient_ids):
            raise ValueError("Story dispatches require distinct places and unique recipients.")
        return self


class StoryFollowup(_StoryModel):
    followup_id: UUID
    after_id: UUID
    after_outcome: _Key | None = None
    delay_ticks: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
    priority: Annotated[int, Field(strict=True, ge=-100, le=100)] = 0
    node: StoryNode
    dispatch: StoryDispatch | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def dispatch_requires_confirmed_outcome(self) -> StoryFollowup:
        if self.dispatch is not None and (self.after_outcome is None or self.node.visibility != "author_only"):
            raise ValueError("A dispatch requires a specific confirmed outcome and an author-only delivery node.")
        return self

    def schedule_after(self, parent: StoryMilestone | None) -> ScheduledEventRecord | None:
        if parent is None or (self.after_outcome is not None and parent.outcome != self.after_outcome):
            return None
        due_tick = parent.resolved_tick + self.delay_ticks
        if due_tick > 2**63 - 1:
            return None  # This clock cannot represent an appointment beyond its final tick.
        return ScheduledEventRecord(id=self.followup_id, due_tick=due_tick, phase="observation", priority=self.priority,
                                    handler_id=STORY_NODE_HANDLER, handler_version=STORY_NODE_VERSION,
                                    payload=self.node.model_dump(mode="json"))


class StoryMilestone(_StoryModel):
    schedule_id: UUID
    resolved_tick: Tick
    outcome: _Key
    arrival_actor_ids: tuple[UUID, ...] = Field(default=(), max_length=256, exclude_if=lambda ids: not ids)
    opened_place_ids: tuple[UUID, ...] = Field(default=(), max_length=32, exclude_if=lambda ids: not ids)
    opened_organization_ids: tuple[UUID, ...] = Field(default=(), max_length=16, exclude_if=lambda ids: not ids)
    revealed_entry_ids: tuple[UUID, ...] = Field(default=(), max_length=64, exclude_if=lambda ids: not ids)
    settlement_event_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)


class StoryMilestoneResolvedEvent(_StoryModel):
    type: Literal[STORY_NODE_RESOLVED] = STORY_NODE_RESOLVED
    event_id: UUID = Field(default_factory=uuid4)
    milestone: StoryMilestone
    arrival_world_id: UUID | None = Field(default=None, exclude_if=lambda ident: ident is None)
    transfer: StoryTransfer | None = Field(default=None, exclude_if=lambda value: value is None)
    capability_awards: tuple[StoryCapabilityAward, ...] = Field(default=(), max_length=8, exclude_if=lambda rows: not rows)
    organization_enrollments: tuple[StoryEnrollment, ...] = Field(
        default=(), max_length=32, exclude_if=lambda rows: not rows,
    )
    trigger_event_ids: tuple[UUID, ...] = Field(default=(), max_length=25, exclude_if=lambda ids: not ids)
    delivered_knowledge: tuple[KnowledgeRecord, ...] = Field(default=(), max_length=8, exclude_if=lambda rows: not rows)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


def read_story_node(schedule: ScheduledEventRecord) -> StoryNode:
    if (schedule.handler_version != STORY_NODE_VERSION or schedule.phase != "observation"
            or schedule.dependencies or schedule.repeat_ticks is not None or schedule.max_runs != 1):
        raise ValueError("Story milestones must be one-shot observation schedules with versioned conditions.")
    return StoryNode.model_validate(schedule.payload)


def _gate_passes(state: WorldState, gate: StoryGate) -> bool:
    if isinstance(gate, StoryPriorOutcomeGate):
        return any(record.schedule_id == gate.schedule_id and record.outcome == gate.outcome
                   for record in state.story_milestones)
    if isinstance(gate, StoryGoalGate):
        return goal_condition_met(state, gate.owner_id, gate.condition)
    if isinstance(gate, StoryEnvironmentGate):
        place = state.entities.get(gate.place_id)
        if place is None or place.kind not in {"place", "location"} or place.status != "active":
            return False
        value = place.components.get("place", {}).get("environment", {}).get(gate.field)
        return type(value) is type(gate.equals) and value == gate.equals
    if isinstance(gate, StoryPromiseTrustGate):
        if not state.promise_trust:
            return False
        projection = PromiseTrustProjection.from_payload(state.promise_trust)
        return any(
            bond.from_id == gate.observer_id and bond.to_id == gate.counterpart_id
            and (bond.trust >= gate.threshold if gate.comparison == "at_least" else bond.trust <= gate.threshold)
            for bond in projection.bonds.values()
        )
    actor = state.entities.get(gate.actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        return False
    if isinstance(gate, StoryChallengeGate):
        return any(attempt.actor_id == gate.actor_id and attempt.challenge_id == gate.challenge_id and attempt.passed
                   for attempt in state.challenge_attempts)
    if isinstance(gate, StoryContestGate):
        for row in state.contests:
            if row.status != "settled" or row.definition_id != gate.definition_id or gate.actor_id not in {row.proposer_id, row.recipient_id}:
                continue
            left, right = contest_score(row)
            own, other = (left, right) if gate.actor_id == row.proposer_id else (right, left)
            if gate.result in {"settled", "win" if own > other else "loss" if own < other else "draw"}:
                return True
        return False
    if isinstance(gate, StoryCapabilityGate):
        capabilities = actor.components.get("capabilities", {})
        return gate.capability in capabilities.get("grants", []) and gate.capability not in capabilities.get("restrictions", [])
    if isinstance(gate, StorySkillGate):
        skills = SkillsComponent.model_validate(actor.components.get("skills", {})).skills
        return gate.skill in skills and skills[gate.skill].level >= Decimal(gate.minimum)
    return any(record.id == gate.record_id and (record.holder_id or record.owner_id) == gate.actor_id
               and record.visibility.policy != "author_only" and record.received_tick <= state.current_tick
               and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
               for record in state.knowledge)


def _transfer_accounts_match(state: WorldState, transfer: StoryTransfer) -> bool:
    return all((account := state.resources.get(ident)) is not None and account.account_type == "holder"
               and account.resource_id == transfer.resource_id for ident in (transfer.from_account_id, transfer.to_account_id))


def transfer_is_affordable(state: WorldState, transfer: StoryTransfer | None) -> bool:
    if transfer is None:
        return True
    if not _transfer_accounts_match(state, transfer):
        return False
    with localcontext() as context:
        context.prec = 40
        return (state.resources[transfer.from_account_id].balance >= transfer.quantity
                and state.resources[transfer.to_account_id].balance + transfer.quantity < Decimal("1e30"))


def story_outcome(state: WorldState, node: StoryNode) -> StoryOutcome:
    return next((variant.outcome for variant in node.variants
                 if all(_gate_passes(state, gate) for gate in variant.when)
                 and transfer_is_affordable(state, variant.outcome.transfer)), node.fallback)


def _arrival_matches(arrival: StoryArrival, ident: UUID, outcome: str) -> bool:
    return arrival.trigger_id == ident and (arrival.outcome is None or arrival.outcome == outcome)


def story_dispatch_knowledge_id(followup_id: UUID, recipient_id: UUID) -> UUID:
    return uuid5(followup_id, f"story.dispatch:{recipient_id}")


def _dispatch_knowledge(state: WorldState, followup: StoryFollowup, parent: StoryMilestone, event_id: UUID) -> tuple[KnowledgeRecord, ...]:
    dispatch = followup.dispatch
    assert dispatch is not None and parent.settlement_event_id is not None
    if any(state.entities[place].status != "active" for place in (dispatch.from_place_id, dispatch.to_place_id)):
        return ()
    return tuple(
        KnowledgeRecord(
            id=story_dispatch_knowledge_id(followup.followup_id, recipient_id), holder_id=recipient_id,
            claim=dispatch.message, kind="claim", received_tick=state.current_tick, valid_from_tick=state.current_tick,
            source_event_id=parent.settlement_event_id, source_refs=[parent.settlement_event_id, event_id],
            provenance=[ProvenanceReference(kind="event", reference_id=str(parent.settlement_event_id))],
            visibility=VisibilityScope(policy="subject_ids", subject_ids=[recipient_id]),
        )
        for recipient_id in dispatch.recipient_ids
        if (recipient := state.entities.get(recipient_id)) is not None and recipient.status == "active"
        and recipient.location_id == dispatch.to_place_id
    )


def _resolved_story_event(
    state: WorldState, ident: UUID, node: StoryNode, event_id: UUID, *, world_id: UUID | None,
    activation_gates: tuple[StoryGate, ...] = (),
) -> StoryMilestoneResolvedEvent:
    outcome = story_outcome(state, node)
    activates = outcome.effect in {"occurred", "mitigated"}
    opened_places = node.opens_place_ids if activates else ()
    awards = tuple(
        award for award in outcome.awards
        if (actor := state.entities.get(award.actor_id)) is not None and actor.status == "active"
        and award.capability not in actor.components.get("capabilities", {}).get("grants", ())
        and award.capability not in actor.components.get("capabilities", {}).get("restrictions", ())
    )
    arrivals = [
        arrival for arrival in state.story_arrivals
        if _arrival_matches(arrival, ident, outcome.key)
        and (state.entities[arrival.actor.location_id].status == "active" or arrival.actor.location_id in opened_places)
    ]
    arrived_ids = {arrival.actor.id for arrival in arrivals}
    enrollments = tuple(
        row for row in node.enrollments
        if activates
        and ((actor := state.entities.get(row.actor_id)) is not None and actor.status == "active"
             or row.actor_id in arrived_ids)
        and ((organization := state.entities[row.organization_id]).status == "active"
             or row.organization_id in node.opens_organization_ids)
        and (str(row.actor_id) not in organization.components.get("governance", {}).get("members", ())
             or row.leader and not organization.components.get("governance", {}).get("leader_id"))
    )
    requires_review = any(arrival.review_delay_ticks is not None for arrival in arrivals)
    if requires_review and world_id is None:
        raise InvalidEvent("A story arrival with an NPC review must identify its world.")
    trace = any(followup.dispatch is not None for followup in state.story_followups)
    trigger_event_ids = ()
    knowledge = ()
    if trace:
        followup = next((row for row in state.story_followups if row.followup_id == ident), None)
        parent_id = followup.after_id if followup is not None else None
        outcome_gates = next((variant.when for variant in node.variants if variant.outcome.key == outcome.key), ())
        preceding_ids = {gate.schedule_id for gate in (*activation_gates, *outcome_gates)
                         if isinstance(gate, StoryPriorOutcomeGate)}
        known_story_events = {record.settlement_event_id for record in state.story_milestones if record.settlement_event_id}
        cited_knowledge = {gate.record_id for gate in (*activation_gates, *outcome_gates)
                           if isinstance(gate, StoryKnowledgeGate)}
        evidence = {record.source_refs[-1] for record in state.knowledge if record.id in cited_knowledge
                    and record.source_refs and record.source_refs[-1] in known_story_events}
        trigger_event_ids = tuple(
            record.settlement_event_id for record in state.story_milestones
            if record.settlement_event_id is not None and (
                record.schedule_id == parent_id or record.schedule_id in preceding_ids or record.settlement_event_id in evidence
            )
        )
        if followup is not None and followup.dispatch is not None:
            parent = next((record for record in state.story_milestones if record.schedule_id == parent_id), None)
            if parent is None or parent.settlement_event_id is None or parent.outcome != followup.after_outcome:
                raise InvalidEvent("A dispatch must cite its committed source outcome.")
            knowledge = _dispatch_knowledge(state, followup, parent, event_id)
    return StoryMilestoneResolvedEvent(
        event_id=event_id, transfer=outcome.transfer, capability_awards=awards,
        organization_enrollments=enrollments,
        arrival_world_id=world_id if requires_review else None,
        trigger_event_ids=trigger_event_ids, delivered_knowledge=knowledge,
        milestone=StoryMilestone(schedule_id=ident, resolved_tick=state.current_tick, outcome=outcome.key,
                                 arrival_actor_ids=tuple(arrival.actor.id for arrival in arrivals),
                                 opened_place_ids=opened_places,
                                 opened_organization_ids=node.opens_organization_ids if activates else (),
                                 revealed_entry_ids=node.reveals_entry_ids if activates else (),
                                 settlement_event_id=event_id if trace else None),
    )


def settle_story_milestone(
    state: WorldState, schedule: ScheduledEventRecord, event_id: UUID, *, world_id: UUID | None = None,
) -> StoryMilestoneResolvedEvent:
    if (schedule.handler_id != STORY_NODE_HANDLER or schedule.status != "queued"
            or schedule.due_tick != state.current_tick):
        raise InvalidEvent("A story milestone must resolve when its registered schedule is due.")
    return _resolved_story_event(state, schedule.id, read_story_node(schedule), event_id, world_id=world_id)


def ready_story_reaction(state: WorldState) -> StoryReaction | None:
    completed = {record.schedule_id for record in state.story_milestones}
    return min((reaction for reaction in state.story_reactions if reaction.reaction_id not in completed
                and all(_gate_passes(state, gate) for gate in reaction.when)),
               key=lambda reaction: (-reaction.priority, str(reaction.reaction_id)), default=None)


def story_reaction_event_id(command_id: UUID, reaction_id: UUID) -> UUID:
    return uuid5(command_id, f"story.reaction:{reaction_id}")


def settle_story_reaction(
    state: WorldState, reaction: StoryReaction, event_id: UUID, *, world_id: UUID | None = None,
) -> StoryMilestoneResolvedEvent:
    return _resolved_story_event(state, reaction.reaction_id, reaction.node, event_id, world_id=world_id,
                                 activation_gates=reaction.when)


def validate_story_arrival_world(events: Iterable[object], world_id: UUID) -> None:
    if any(isinstance(event, StoryMilestoneResolvedEvent)
           and event.arrival_world_id is not None and event.arrival_world_id != world_id for event in events):
        raise InvalidEvent("An arrival review must belong to the story event's world.")


def story_followup_schedules(state: WorldState, milestone: StoryMilestone) -> list[ScheduledEventRecord]:
    return [schedule for item in state.story_followups if item.after_id == milestone.schedule_id
            if (schedule := item.schedule_after(milestone)) is not None]


def validate_story_state(state: WorldState) -> None:
    schedules = {schedule.id: schedule for schedule in state.scheduled_events if schedule.handler_id == STORY_NODE_HANDLER}
    if (not schedules and not state.story_milestones and not state.story_reactions
            and not state.story_followups and not state.story_arrivals):
        return
    reactions = {reaction.reaction_id: reaction for reaction in state.story_reactions}
    followups = {followup.followup_id: followup for followup in state.story_followups}
    if (len(reactions) != len(state.story_reactions) or len(followups) != len(state.story_followups)
            or set(schedules).intersection(reactions) or set(followups).intersection(reactions)):
        raise ValueError("Story nodes must have distinct identifiers.")
    nodes = {schedule_id: read_story_node(schedule) for schedule_id, schedule in schedules.items()}
    nodes.update({key: reaction.node for key, reaction in reactions.items()})
    nodes.update({key: followup.node for key, followup in followups.items()})
    allowed_outcomes = {key: {node.fallback.key, *(variant.outcome.key for variant in node.variants)}
                        for key, node in nodes.items()}
    records = {record.schedule_id: record for record in state.story_milestones}
    if len(records) != len(state.story_milestones) or set(records).difference(reactions) != {
        key for key, schedule in schedules.items() if schedule.status == "completed"
    }:
        raise ValueError("Completed story schedules must have exactly one milestone receipt.")
    if any(schedule.status == "queued" and schedule.due_tick < state.current_tick for schedule in schedules.values()):
        raise ValueError("A queued story milestone cannot precede the world clock.")
    traced = any(followup.dispatch is not None for followup in followups.values())
    if traced:
        if len(nodes) > MAX_TRACED_STORY_NODES:
            raise ValueError("A story with dispatches exceeds the node budget.")
        settlement_ids = {record.settlement_event_id for record in records.values()}
        if None in settlement_ids or len(settlement_ids) != len(records):
            raise ValueError("Traced story milestones require distinct settlement event IDs.")
    for key, record in records.items():
        if ((key in schedules and record.resolved_tick != schedules[key].due_tick) or record.resolved_tick > state.current_tick
                or record.outcome not in allowed_outcomes[key]):
            raise ValueError("A story milestone receipt conflicts with its schedule.")
    reserved_entities = set()
    reserved_entries = set()
    entries = {entry.entry_id: entry for entry in state.world_book_entries}
    for key, node in nodes.items():
        released_entities = (*node.opens_place_ids, *node.opens_organization_ids)
        if reserved_entities.intersection(released_entities) or reserved_entries.intersection(node.reveals_entry_ids):
            raise ValueError("A place, organization, or reference entry can only have one registered story opening.")
        reserved_entities.update(released_entities)
        reserved_entries.update(node.reveals_entry_ids)
        receipt = records.get(key)
        outcome = next((item for item in (node.fallback, *(variant.outcome for variant in node.variants))
                        if receipt is not None and item.key == receipt.outcome), None)
        activates = outcome is not None and outcome.effect in {"occurred", "mitigated"}
        if receipt is not None and (
            receipt.opened_place_ids != (node.opens_place_ids if activates else ())
            or receipt.opened_organization_ids != (node.opens_organization_ids if activates else ())
            or receipt.revealed_entry_ids != (node.reveals_entry_ids if activates else ())
        ):
            raise ValueError("A story opening receipt must match its registered story outcome.")
        for identifiers, kinds in (
            (node.opens_place_ids, {"place", "location"}),
            (node.opens_organization_ids, {"organization", "polity", "faction", "company", "government"}),
        ):
            for ident in identifiers:
                entity = state.entities.get(ident)
                if entity is None or entity.kind not in kinds:
                    raise ValueError("A story opening must reference a registered place or organization of the expected type.")
                if not activates and (entity.status != "closed" or entity.components.get("hidden") is not True):
                    raise ValueError("A staged place or organization must be closed and hidden until its story opening.")
        for entry_id in node.reveals_entry_ids:
            entry = entries.get(entry_id)
            if entry is None or entry.visibility != ("public" if activates else "author_only"):
                raise ValueError("A staged reference must stay private until its story opening.")
    arrivals = {arrival.actor.id: arrival for arrival in state.story_arrivals}
    if len(arrivals) != len(state.story_arrivals):
        raise ValueError("A character must have a single story arrival.")
    known_entities = {ident: arrival.actor for ident, arrival in arrivals.items()} | state.entities
    for ident, arrival in arrivals.items():
        if (arrival.trigger_id not in nodes or arrival.outcome is not None
                and arrival.outcome not in allowed_outcomes[arrival.trigger_id]):
            raise ValueError("An arrival must reference a registered story node and outcome.")
        place = state.entities.get(arrival.actor.location_id)
        if place is None or place.kind not in {"place", "location"}:
            raise ValueError("A story entry place must already be registered.")
        receipt = records.get(arrival.trigger_id)
        admitted = receipt is not None and ident in receipt.arrival_actor_ids
        if admitted != (ident in state.entities) or ident == state.player_id:
            raise ValueError("A staged role only becomes a world entity through its own arrival receipt.")
    for record in records.values():
        if len(set(record.arrival_actor_ids)) != len(record.arrival_actor_ids) or any(
            ident not in arrivals or not _arrival_matches(arrivals[ident], record.schedule_id, record.outcome)
            for ident in record.arrival_actor_ids
        ):
            raise ValueError("An arrival receipt must match the registered story node and outcome.")
    relation_ids = set()
    goal_ids = set()
    arrival_knowledge_ids = set()
    current_relations = {relation.id: relation for relation in state.relations} if arrivals else {}
    current_goals = {goal.id: goal for goal in state.goals} if arrivals else {}
    regular_entities = state.entities.keys() - arrivals.keys() if arrivals else set()
    unborn_ids = {str(ident) for ident in arrivals if ident not in state.entities}
    if unborn_ids and any(schedule.handler_id == "npc.goal_review" and str(schedule.payload.get("actor_id")) in unborn_ids
                          for schedule in state.scheduled_events):
        raise ValueError("A staged character's NPC review is registered only on arrival.")
    for arrival in state.story_arrivals:
        expected_knowledge = {record.id for record in arrival.knowledge}
        if len(expected_knowledge) != len(arrival.knowledge) or arrival_knowledge_ids & expected_knowledge:
            raise ValueError("Arrival knowledge must have distinct reserved IDs.")
        arrival_knowledge_ids.update(expected_knowledge)
        for record in arrival.knowledge:
            if record.subject_id is not None and record.subject_id not in known_entities:
                raise ValueError("An arrival knowledge subject must be a registered entity.")
    for record in state.knowledge:
        if record.id in arrival_knowledge_ids:
            arrival = arrivals.get(record.holder_id)
            if (arrival is None or arrival.actor.id not in state.entities or record.source_event_id is None
                    or not any(item.id == record.id for item in arrival.knowledge)):
                raise ValueError("Arrival knowledge can only be created for an admitted actor.")
    for arrival in state.story_arrivals:
        for relation in arrival.relations:
            current = current_relations.get(relation.id)
            if (relation.id in relation_ids or relation.from_id not in known_entities or relation.to_id not in known_entities
                    or current is not None and (current.from_id, current.to_id) != (relation.from_id, relation.to_id)):
                raise ValueError("An arrival relationship requires a distinct ID and two registered roles or entities.")
            relation_ids.add(relation.id)
        if not arrival.goals:
            continue
        goal_scope = regular_entities | {arrival.actor.id}
        goal_scope.update(goal.id for goal in arrival.goals)
        goal_scope.update(goal.id for goal in state.goals if goal.owner_id == arrival.actor.id)
        for goal in arrival.goals:
            current_goal = current_goals.get(goal.id)
            if goal.id in goal_ids or (current_goal is not None and current_goal.owner_id != arrival.actor.id):
                raise ValueError("An arrival goal ID must be reserved for its actor.")
            if any(reference is not None and reference not in goal_scope for condition in goal.conditions
                   for reference in (condition.subject_id, condition.target_id)):
                raise ValueError("An arrival goal must reference an existing entity, its actor, or another goal for that actor.")
            goal_ids.add(goal.id)
    parents: dict[UUID, set[UUID]] = {key: set() for key in nodes}
    dispatch_knowledge: dict[UUID, tuple[StoryFollowup, UUID]] = {}
    challenge_ids = {challenge.challenge_id for challenge in state.challenges}
    contest_ids = {contest.definition_id for contest in state.contest_definitions}
    all_schedules = {schedule.id: schedule for schedule in state.scheduled_events}
    for key, followup in followups.items():
        if followup.after_id not in nodes or (
            followup.after_outcome is not None and followup.after_outcome not in allowed_outcomes[followup.after_id]
        ):
            raise ValueError("A story follow-up must reference an existing milestone or outcome.")
        parents[key].add(followup.after_id)
        dispatch = followup.dispatch
        if dispatch is not None:
            if any((place := state.entities.get(ident)) is None or place.kind not in {"place", "location"}
                   for ident in (dispatch.from_place_id, dispatch.to_place_id)) or any(
                (actor := known_entities.get(ident)) is None or actor.kind not in {"character", "person"}
                for ident in dispatch.recipient_ids
            ):
                raise ValueError("A dispatch must reference registered places and character recipients.")
            for recipient_id in dispatch.recipient_ids:
                dispatch_knowledge[story_dispatch_knowledge_id(key, recipient_id)] = (followup, recipient_id)
        scheduled = all_schedules.get(key)
        expected = followup.schedule_after(records.get(followup.after_id))
        if scheduled is not None and scheduled.status == "completed":
            scheduled = scheduled.model_copy(update={"status": "queued", "completed_runs": 0, "revision": scheduled.revision - 1})
        if expected != scheduled:
            raise ValueError("A story follow-up schedule must match the committed parent milestone and delay.")
    for key, node in nodes.items():
        if node.opportunity is not None:
            opportunity = node.opportunity
            place = state.entities.get(opportunity.place_id)
            contact = known_entities.get(opportunity.contact_id) if opportunity.contact_id is not None else None
            if (place is None or place.kind not in {"place", "location"}
                    or opportunity.contact_id is not None and (contact is None or contact.kind not in {"character", "person"})):
                raise ValueError("A story opportunity must reference a place and an optional character.")
        if len(set(node.participant_ids)) != len(node.participant_ids) or any(
            entity not in known_entities for entity in node.participant_ids
        ):
            raise ValueError("Story participants must reference distinct existing entities.")
        if any((recipient := known_entities.get(award.actor_id)) is None or recipient.kind not in {"character", "person"}
               for outcome in (node.fallback, *(variant.outcome for variant in node.variants)) for award in outcome.awards):
            raise ValueError("A story capability award must reference a registered character.")
        if any((organization := state.entities.get(row.organization_id)) is None
               or "governance" not in organization.components
               or (member := known_entities.get(row.actor_id)) is None or member.kind not in {"character", "person"}
               for row in node.enrollments):
            raise ValueError("A story enrollment must reference a governed organization and a registered character.")
        gates = [gate for variant in node.variants for gate in variant.when]
        if key in reactions:
            gates.extend(reactions[key].when)
        for gate in gates:
            if isinstance(gate, StoryKnowledgeGate) and gate.record_id in dispatch_knowledge:
                producer, recipient_id = dispatch_knowledge[gate.record_id]
                if gate.actor_id != recipient_id:
                    raise ValueError("A dispatch knowledge gate must name its authorized recipient.")
                parents[key].add(producer.followup_id)
            if isinstance(gate, StoryChallengeGate) and gate.challenge_id not in challenge_ids:
                raise ValueError("Story gates may only reference a registered challenge.")
            if isinstance(gate, StoryContestGate) and gate.definition_id not in contest_ids:
                raise ValueError("Story gates may only reference a registered contest.")
            if isinstance(gate, StoryPromiseTrustGate):
                endpoints = (gate.observer_id, gate.counterpart_id)
                if endpoints[0] == endpoints[1] or any(
                    endpoint not in known_entities or known_entities[endpoint].kind not in {"character", "person"}
                    for endpoint in endpoints
                ):
                    raise ValueError("Promise trust story gates require two different characters.")
            if not isinstance(gate, StoryPriorOutcomeGate):
                continue
            prior = gate.schedule_id
            if gate.outcome not in allowed_outcomes.get(prior, set()) or (
                prior in schedules and key in schedules and prior not in followups and key not in followups
                and schedules[prior].due_tick >= schedules[key].due_tick
            ):
                raise ValueError("Story prerequisites must reference an earlier valid outcome.")
            parents[key].add(prior)
        for variant in node.variants:
            transfer = variant.outcome.transfer
            if transfer is not None and not _transfer_accounts_match(state, transfer):
                raise ValueError("Story transfers must reference holder accounts for the same resource.")
    for knowledge in state.knowledge if dispatch_knowledge else ():
        link = dispatch_knowledge.get(knowledge.id)
        if link is None:
            continue
        followup, recipient_id = link
        delivery = records.get(followup.followup_id)
        source = records.get(followup.after_id)
        if delivery is None or source is None or (
            knowledge.holder_id != recipient_id or knowledge.owner_id is not None
            or knowledge.visibility != VisibilityScope(policy="subject_ids", subject_ids=[recipient_id])
            or knowledge.received_tick != delivery.resolved_tick or knowledge.valid_from_tick != delivery.resolved_tick
            or knowledge.source_refs != [source.settlement_event_id, delivery.settlement_event_id]
            or knowledge.source_event_id != source.settlement_event_id
            or knowledge.claim != followup.dispatch.message or knowledge.kind != "claim"
        ):
            raise ValueError("Dispatch knowledge requires a matching private delivery receipt and committed source.")
    try:
        dependencies = TopologicalSorter(parents)
        if not traced:
            dependencies.prepare()
            return
        fanout = Counter(parent_id for edges in parents.values() for parent_id in edges)
        if any(count > MAX_STORY_FANOUT for count in fanout.values()):
            raise ValueError("A story dependency exceeds the fan-out budget.")
        depths: dict[UUID, int] = {}
        for key in dependencies.static_order():
            depths[key] = 1 + max((depths[parent] for parent in parents[key]), default=0)
            if depths[key] > MAX_STORY_DEPTH:
                raise ValueError("A story dependency exceeds the depth budget.")
    except CycleError as error:
        raise ValueError("Story prerequisites cannot form a cycle.") from error
