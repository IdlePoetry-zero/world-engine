"""Declarative milestones evaluated against committed events and world state."""

from __future__ import annotations

from decimal import Decimal, localcontext
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, StringConstraints, model_validator

from .components import ExactDecimal, GoalCondition, ResourceName, ScheduledEventRecord, SkillsComponent, Tick
from .errors import InvalidEvent
from .json_types import JsonObject
from .npc_goal_completion import goal_condition_met
from .world_books import WorldBookCitation

if TYPE_CHECKING:
    from .models import WorldState

STORY_NODE_HANDLER = "story.milestone"
STORY_NODE_VERSION = "1.0.0"
STORY_NODE_RESOLVED = "core.story_milestone_resolved.v1"

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


StoryGate = Annotated[
    StoryGoalGate | StoryCapabilityGate | StorySkillGate | StoryKnowledgeGate | StoryPriorOutcomeGate | StoryEnvironmentGate,
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


class StoryOutcome(_StoryModel):
    key: _Key
    effect: _Effect
    summary: _Summary
    transfer: StoryTransfer | None = Field(default=None, exclude_if=lambda value: value is None)


class StoryVariant(_StoryModel):
    outcome: StoryOutcome
    when: tuple[StoryGate, ...] = Field(min_length=1, max_length=12)


class StoryNode(_StoryModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    visibility: Literal["public", "author_only"]
    references: tuple[WorldBookCitation, ...] = Field(default=(), max_length=4)
    participant_ids: tuple[UUID, ...] = Field(default=(), max_length=24)
    prevention_possible: StrictBool = False
    variants: tuple[StoryVariant, ...] = Field(default=(), max_length=8)
    fallback: StoryOutcome

    @model_validator(mode="after")
    def validate_outcomes(self) -> StoryNode:
        outcomes = [self.fallback, *(variant.outcome for variant in self.variants)]
        if len({outcome.key for outcome in outcomes}) != len(outcomes):
            raise ValueError("Story outcome keys must be unique within a milestone.")
        if self.fallback.effect not in {"occurred", "inapplicable"} or self.fallback.transfer is not None:
            raise ValueError("Intervention outcomes require explicit conditions.")
        if not self.prevention_possible and any(variant.outcome.effect in {"prevented", "inapplicable"}
                                                for variant in self.variants):
            raise ValueError("An immutable milestone cannot be stopped by an intervention variant.")
        return self


class StoryReaction(_StoryModel):
    version: Literal["1.0.0"] = "1.0.0"
    reaction_id: UUID
    priority: Annotated[int, Field(strict=True, ge=-100, le=100)] = 0
    when: tuple[StoryGate, ...] = Field(min_length=1, max_length=12)
    node: StoryNode


class StoryMilestone(_StoryModel):
    schedule_id: UUID
    resolved_tick: Tick
    outcome: _Key


class StoryMilestoneResolvedEvent(_StoryModel):
    type: Literal[STORY_NODE_RESOLVED] = STORY_NODE_RESOLVED
    event_id: UUID = Field(default_factory=uuid4)
    milestone: StoryMilestone
    transfer: StoryTransfer | None = Field(default=None, exclude_if=lambda value: value is None)
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
    actor = state.entities.get(gate.actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
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


def _transfer_is_affordable(state: WorldState, transfer: StoryTransfer | None) -> bool:
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
                 and _transfer_is_affordable(state, variant.outcome.transfer)), node.fallback)


def _resolved_story_event(state: WorldState, ident: UUID, node: StoryNode, event_id: UUID) -> StoryMilestoneResolvedEvent:
    outcome = story_outcome(state, node)
    return StoryMilestoneResolvedEvent(
        event_id=event_id, transfer=outcome.transfer,
        milestone=StoryMilestone(schedule_id=ident, resolved_tick=state.current_tick, outcome=outcome.key),
    )


def settle_story_milestone(
    state: WorldState, schedule: ScheduledEventRecord, event_id: UUID,
) -> StoryMilestoneResolvedEvent:
    if (schedule.handler_id != STORY_NODE_HANDLER or schedule.status != "queued"
            or schedule.due_tick != state.current_tick):
        raise InvalidEvent("A story milestone must resolve when its registered schedule is due.")
    return _resolved_story_event(state, schedule.id, read_story_node(schedule), event_id)


def ready_story_reaction(state: WorldState) -> StoryReaction | None:
    completed = {record.schedule_id for record in state.story_milestones}
    return min((reaction for reaction in state.story_reactions if reaction.reaction_id not in completed
                and all(_gate_passes(state, gate) for gate in reaction.when)),
               key=lambda reaction: (-reaction.priority, str(reaction.reaction_id)), default=None)


def settle_story_reaction(state: WorldState, reaction: StoryReaction, event_id: UUID) -> StoryMilestoneResolvedEvent:
    return _resolved_story_event(state, reaction.reaction_id, reaction.node, event_id)


def validate_story_state(state: WorldState) -> None:
    schedules = {schedule.id: schedule for schedule in state.scheduled_events if schedule.handler_id == STORY_NODE_HANDLER}
    if not schedules and not state.story_milestones and not state.story_reactions:
        return
    nodes = {schedule_id: read_story_node(schedule) for schedule_id, schedule in schedules.items()}
    reactions = {reaction.reaction_id: reaction for reaction in state.story_reactions}
    if len(reactions) != len(state.story_reactions) or set(nodes).intersection(reactions):
        raise ValueError("Story schedules and reactions must have distinct identifiers.")
    nodes.update({key: reaction.node for key, reaction in reactions.items()})
    allowed_outcomes = {key: {node.fallback.key, *(variant.outcome.key for variant in node.variants)}
                        for key, node in nodes.items()}
    records = {record.schedule_id: record for record in state.story_milestones}
    if len(records) != len(state.story_milestones) or set(records).difference(reactions) != {
        key for key, schedule in schedules.items() if schedule.status == "completed"
    }:
        raise ValueError("Completed story schedules must have exactly one milestone receipt.")
    if any(schedule.status == "queued" and schedule.due_tick < state.current_tick for schedule in schedules.values()):
        raise ValueError("A queued story milestone cannot precede the world clock.")
    for key, record in records.items():
        if ((key in schedules and record.resolved_tick != schedules[key].due_tick) or record.resolved_tick > state.current_tick
                or record.outcome not in allowed_outcomes[key]):
            raise ValueError("A story milestone receipt conflicts with its schedule.")
    dependencies = TopologicalSorter()
    for key, node in nodes.items():
        if len(set(node.participant_ids)) != len(node.participant_ids) or any(
            entity not in state.entities for entity in node.participant_ids
        ):
            raise ValueError("Story participants must reference distinct existing entities.")
        gates = [gate for variant in node.variants for gate in variant.when]
        if key in reactions:
            gates.extend(reactions[key].when)
        for gate in gates:
            if not isinstance(gate, StoryPriorOutcomeGate):
                continue
            prior = gate.schedule_id
            if gate.outcome not in allowed_outcomes.get(prior, set()) or (
                prior in schedules and key in schedules and schedules[prior].due_tick >= schedules[key].due_tick
            ):
                raise ValueError("Story prerequisites must reference an earlier valid outcome.")
            dependencies.add(key, prior)
        for variant in node.variants:
            transfer = variant.outcome.transfer
            if transfer is not None and not _transfer_accounts_match(state, transfer):
                raise ValueError("Story transfers must reference holder accounts for the same resource.")
    try:
        dependencies.prepare()
    except CycleError as error:
        raise ValueError("Story prerequisites cannot form a cycle.") from error
