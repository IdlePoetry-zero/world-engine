"""Authored, one-attempt competency checks independent of the setting's genre."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from .components import CapabilitiesComponent, ExactDecimal, ResourceName, SkillsComponent, Tick
from .errors import InvalidEvent
from .json_types import JsonObject
from .story_nodes import StoryTransfer, transfer_is_affordable

if TYPE_CHECKING:
    from .models import WorldState

CHALLENGE_RESOLVED = "core.challenge_resolved.v1"
CHALLENGE_COMMAND = "action.perform_challenge"


class _ChallengeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, revalidate_instances="always")


class ChallengeFee(_ChallengeModel):
    receiving_account_id: UUID
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=0)


class ChallengeDefinition(_ChallengeModel):
    version: Literal["1.0.0"] = "1.0.0"
    challenge_id: UUID
    venue_id: UUID
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    skill_id: ResourceName
    skill_label: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    pass_level: ExactDecimal = Field(ge=0, le=100)
    required_capability: ResourceName | None = None
    prerequisite_id: UUID | None = None
    fee: ChallengeFee | None = None


class ChallengeAttempt(_ChallengeModel):
    challenge_id: UUID
    actor_id: UUID
    completed_tick: Tick
    passed: StrictBool


class ChallengeResolvedEvent(_ChallengeModel):
    type: Literal[CHALLENGE_RESOLVED] = CHALLENGE_RESOLVED
    event_id: UUID
    attempt: ChallengeAttempt
    fee: StoryTransfer | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


def _available_at_venue(state: WorldState, actor_id: UUID, definition: ChallengeDefinition) -> bool:
    actor = state.entities.get(actor_id)
    venue = state.entities.get(definition.venue_id)
    return (actor is not None and actor.kind in {"person", "character"} and actor.status == "active"
            and actor.location_id == definition.venue_id and venue is not None and venue.status == "active"
            and venue.components.get("hidden") is not True)


def _prerequisite_passed(state: WorldState, actor_id: UUID, definition: ChallengeDefinition) -> bool:
    return definition.prerequisite_id is None or any(
        row.actor_id == actor_id and row.challenge_id == definition.prerequisite_id and row.passed
        for row in state.challenge_attempts
    )


def open_challenges(state: WorldState, actor_id: UUID) -> list[dict]:
    own_attempts = {row.challenge_id: row for row in state.challenge_attempts if row.actor_id == actor_id}
    catalog = state.metadata.get("resource_catalog", {})
    options = []
    for row in state.challenges:
        if not _available_at_venue(state, actor_id, row) or not _prerequisite_passed(state, actor_id, row):
            continue
        receipt = own_attempts.get(row.challenge_id)
        label = catalog.get(row.fee.resource_id, row.fee.resource_id) if row.fee and isinstance(catalog, dict) else None
        options.append({
            "challenge_id": str(row.challenge_id), "title": row.title, "description": row.description,
            "skill_label": row.skill_label or row.skill_id, "pass_level": str(row.pass_level),
            "fee": ({"resource_id": row.fee.resource_id, "quantity": str(row.fee.quantity),
                     "label": label if isinstance(label, str) else row.fee.resource_id} if row.fee else None),
            "passed": receipt.passed if receipt else None,
        })
    return options


def settle_challenge(state: WorldState, challenge_id: UUID, actor_id: UUID, event_id: UUID) -> ChallengeResolvedEvent:
    definition = next((row for row in state.challenges if row.challenge_id == challenge_id), None)
    if (definition is None or not _available_at_venue(state, actor_id, definition)
            or not _prerequisite_passed(state, actor_id, definition)):
        raise InvalidEvent("当前角色在这里无法参加这项考核。")
    if any(row.actor_id == actor_id and row.challenge_id == challenge_id for row in state.challenge_attempts):
        raise InvalidEvent("当前角色已结算过这项考核。")
    actor = state.entities[actor_id]
    capabilities = CapabilitiesComponent.model_validate(actor.components.get("capabilities", {}))
    if definition.required_capability is not None and (
        definition.required_capability not in capabilities.grants or definition.required_capability in capabilities.restrictions
    ):
        raise InvalidEvent("当前角色缺少参加这项考核的资格。")
    skill = SkillsComponent.model_validate(actor.components.get("skills", {})).skills.get(definition.skill_id)
    if skill is None:
        raise InvalidEvent("请先学习这项考核所要求的技能。")
    fee = None
    if definition.fee is not None:
        charge = definition.fee
        source_id = next((ident for ident, account in state.resources.items() if account.account_type == "holder"
                          and account.resource_id == charge.resource_id and account.owner_id == actor_id), None)
        if source_id is None or source_id == charge.receiving_account_id:
            raise InvalidEvent("当前角色没有可用于缴费的账户。")
        fee = StoryTransfer(from_account_id=source_id, to_account_id=charge.receiving_account_id,
                            resource_id=charge.resource_id, quantity=charge.quantity)
        if not transfer_is_affordable(state, fee):
            raise InvalidEvent("费用无法按登记金额结算。")
    return ChallengeResolvedEvent(event_id=event_id, fee=fee, attempt=ChallengeAttempt(
        challenge_id=challenge_id, actor_id=actor_id, completed_tick=state.current_tick, passed=skill.level >= definition.pass_level,
    ))


def validate_challenge_state(state: WorldState) -> None:
    if not state.challenges and not state.challenge_attempts:
        return
    definitions = {row.challenge_id: row for row in state.challenges}
    if len(definitions) != len(state.challenges):
        raise ValueError("Challenge identifiers must be unique.")
    prerequisites = TopologicalSorter()
    for row in state.challenges:
        venue = state.entities.get(row.venue_id)
        if venue is None or venue.kind not in {"place", "location"}:
            raise ValueError("A challenge venue must be a registered place.")
        if row.fee is not None:
            account = state.resources.get(row.fee.receiving_account_id)
            if account is None or account.account_type != "holder" or account.resource_id != row.fee.resource_id:
                raise ValueError("A challenge fee must name a registered holder account for its resource.")
        if row.prerequisite_id is not None:
            if row.prerequisite_id not in definitions:
                raise ValueError("Challenge prerequisites must refer to a registered challenge.")
            prerequisites.add(row.challenge_id, row.prerequisite_id)
    try:
        prerequisites.prepare()
    except CycleError as error:
        raise ValueError("Challenge prerequisites cannot form a cycle.") from error
    attempts = set()
    for row in state.challenge_attempts:
        actor = state.entities.get(row.actor_id)
        key = (row.actor_id, row.challenge_id)
        if (key in attempts or row.challenge_id not in definitions or row.completed_tick > state.current_tick
                or actor is None or actor.kind not in {"person", "character"}):
            raise ValueError("A challenge receipt must refer to one valid completed attempt.")
        attempts.add(key)
