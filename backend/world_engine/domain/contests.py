"""Local, consensual contests with declared routines and deterministic results."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .components import ExactDecimal, ResourceName, SkillsComponent, Tick
from .errors import InvalidEvent
from .json_types import JsonObject

if TYPE_CHECKING:
    from .models import WorldState

CONTEST_COMMAND = "action.local_contest"
CONTEST_CHANGED = "core.local_contest_changed.v1"
_Title = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class _ContestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, revalidate_instances="always")


class ContestRoutine(_ContestModel):
    key: ResourceName
    title: _Title
    skills: tuple[ResourceName, ...] = Field(min_length=1, max_length=5)


class ContestDefinition(_ContestModel):
    version: Literal["1.0.0"] = "1.0.0"
    definition_id: UUID
    venue_id: UUID
    title: _Title
    terms: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    routines: tuple[ContestRoutine, ...] = Field(min_length=1, max_length=12)
    proposer_challenge_id: UUID | None = None
    offer_seconds: Annotated[int, Field(strict=True, gt=0, le=86400)] = 900
    npc_routine: dict[UUID, ResourceName | None] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def verify_routines(self) -> ContestDefinition:
        keys = {routine.key for routine in self.routines}
        if len(keys) != len(self.routines) or len({len(row.skills) for row in self.routines}) != 1:
            raise ValueError("Contest routines must be distinct and specify the same number of rounds.")
        if not {routine for routine in self.npc_routine.values() if routine is not None} <= keys:
            raise ValueError("An NPC policy must choose a registered routine.")
        return self


class InviteContest(_ContestModel):
    operation: Literal["invite"]
    definition_id: UUID
    recipient_id: UUID
    routine: ResourceName


class ReplyContest(_ContestModel):
    operation: Literal["reply"]
    contest_id: UUID
    routine: ResourceName | None = None


class AdvanceContest(_ContestModel):
    operation: Literal["ask_npc", "withdraw", "settle"]
    contest_id: UUID


ContestAction = Annotated[InviteContest | ReplyContest | AdvanceContest, Field(discriminator="operation")]


class ContestRound(_ContestModel):
    proposer_skill: ExactDecimal = Field(ge=0, le=100)
    recipient_skill: ExactDecimal = Field(ge=0, le=100)


class ContestRecord(_ContestModel):
    contest_id: UUID
    definition_id: UUID
    proposer_id: UUID
    recipient_id: UUID
    proposer_routine: ResourceName
    recipient_routine: ResourceName | None = None
    status: Literal["invited", "accepted", "declined", "withdrawn", "settled"] = "invited"
    opened_tick: Tick
    expiry_tick: Tick
    updated_tick: Tick
    rounds: tuple[ContestRound, ...] = Field(default=(), max_length=5)


class ContestChangedEvent(_ContestModel):
    type: Literal[CONTEST_CHANGED] = CONTEST_CHANGED
    event_id: UUID
    actor_id: UUID
    action: ContestAction
    contest: ContestRecord
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


def _at_venue(state: WorldState, actor_id: UUID, venue_id: UUID) -> bool:
    actor = state.entities.get(actor_id)
    return (actor is not None and actor.kind in {"person", "character"} and actor.status == "active"
            and actor.location_id == venue_id and actor.components.get("hidden") is not True)


def _routine(state: WorldState, definition: ContestDefinition, actor_id: UUID, key: str) -> tuple[Decimal, ...]:
    routine = next((row for row in definition.routines if row.key == key), None)
    if routine is None:
        raise InvalidEvent("请从赛制登记的技艺中选择一项。")
    skills = SkillsComponent.model_validate(state.entities[actor_id].components.get("skills", {})).skills
    if any(name not in skills for name in routine.skills):
        raise InvalidEvent("当前参赛者尚未习得这套技艺。")
    return tuple(skills[name].level for name in routine.skills)


def _qualified(state: WorldState, actor_id: UUID, definition: ContestDefinition) -> bool:
    return definition.proposer_challenge_id is None or any(
        row.actor_id == actor_id and row.challenge_id == definition.proposer_challenge_id and row.passed
        for row in state.challenge_attempts
    )


def _contest_current(state: WorldState, row: ContestRecord, definition: ContestDefinition) -> bool:
    venue = state.entities.get(definition.venue_id)
    return (venue is not None and venue.status == "active" and venue.components.get("hidden") is not True
            and state.current_tick < row.expiry_tick
            and all(_at_venue(state, actor, definition.venue_id) for actor in (row.proposer_id, row.recipient_id)))


def plan_contest(state: WorldState, actor_id: UUID, action: ContestAction, event_id: UUID) -> ContestChangedEvent:
    definitions = {row.definition_id: row for row in state.contest_definitions}
    if isinstance(action, InviteContest):
        definition = definitions.get(action.definition_id)
        if definition is None or actor_id == action.recipient_id or not _qualified(state, actor_id, definition):
            raise InvalidEvent("当前角色无法发起这项对局。")
        expiry = state.current_tick + definition.offer_seconds
        if expiry > 2**63 - 1:
            raise InvalidEvent("当前世界时钟无法登记新的对局期限。")
        contest = ContestRecord(contest_id=uuid5(event_id, "invitation"), definition_id=definition.definition_id,
            proposer_id=actor_id, recipient_id=action.recipient_id, proposer_routine=action.routine,
            opened_tick=state.current_tick, expiry_tick=expiry, updated_tick=state.current_tick)
        if not _contest_current(state, contest, definition):
            raise InvalidEvent("双方必须在指定的公开场地内。")
        _routine(state, definition, actor_id, action.routine)
        if any(row.definition_id == definition.definition_id and row.status in {"invited", "accepted"}
               and actor_id in {row.proposer_id, row.recipient_id} and _contest_current(state, row, definition)
               for row in state.contests):
            raise InvalidEvent("已有此赛制的待处理对局。")
    else:
        existing = next((row for row in state.contests if row.contest_id == action.contest_id), None)
        if existing is None or actor_id not in {existing.proposer_id, existing.recipient_id}:
            raise InvalidEvent("当前角色无法处理这项对局。")
        definition = definitions[existing.definition_id]
        if existing.status not in {"invited", "accepted"} or not _contest_current(state, existing, definition):
            raise InvalidEvent("这项对局已结束、过期，或双方已离开场地。")
        updates: dict[str, object] = {"updated_tick": state.current_tick}
        if isinstance(action, ReplyContest) or action.operation == "ask_npc":
            if existing.status != "invited":
                raise InvalidEvent("该邀约已获得答复。")
            if isinstance(action, ReplyContest):
                if actor_id != existing.recipient_id:
                    raise InvalidEvent("只有受邀者能答复。")
                selected = action.routine
            else:
                if actor_id != existing.proposer_id or existing.recipient_id not in definition.npc_routine:
                    raise InvalidEvent("当前受邀者没有可用的自主答复规则。")
                selected = definition.npc_routine[existing.recipient_id]
            if selected is not None:
                _routine(state, definition, existing.recipient_id, selected)
            updates.update(status="declined" if selected is None else "accepted", recipient_routine=selected)
        elif action.operation == "withdraw":
            updates["status"] = "withdrawn"
        else:
            if existing.status != "accepted" or existing.recipient_routine is None:
                raise InvalidEvent("双方确认各自技艺后才可以结算。")
            proposer = _routine(state, definition, existing.proposer_id, existing.proposer_routine)
            recipient = _routine(state, definition, existing.recipient_id, existing.recipient_routine)
            updates.update(status="settled", rounds=tuple(ContestRound(proposer_skill=left, recipient_skill=right)
                for left, right in zip(proposer, recipient, strict=True)))
        contest = ContestRecord.model_validate(existing.model_dump() | updates)
    return ContestChangedEvent(event_id=event_id, actor_id=actor_id, action=action, contest=contest)


def contest_score(row: ContestRecord) -> tuple[int, int]:
    proposer = sum(item.proposer_skill > item.recipient_skill for item in row.rounds)
    recipient = sum(item.proposer_skill < item.recipient_skill for item in row.rounds)
    return proposer, recipient


def describe_contest(row: ContestRecord) -> dict:
    proposer, recipient = contest_score(row)
    winner = row.proposer_id if proposer > recipient else row.recipient_id if recipient > proposer else None
    return {"contest_id": str(row.contest_id), "status": row.status, "proposer_id": str(row.proposer_id),
            "recipient_id": str(row.recipient_id), "winner_id": str(winner) if winner else None,
            "proposer_wins": proposer, "recipient_wins": recipient, "rounds": len(row.rounds)}


def contest_view(state: WorldState, actor_id: UUID) -> list[dict]:
    options = []
    for definition in state.contest_definitions:
        if not _at_venue(state, actor_id, definition.venue_id):
            continue
        venue = state.entities[definition.venue_id]
        if venue.status != "active" or venue.components.get("hidden") is True:
            continue
        opponents = [{"id": str(entity.id), "name": entity.name or "在场人物"} for entity in state.entities.values()
                     if entity.id != actor_id and _at_venue(state, entity.id, definition.venue_id)]
        recent = [describe_contest(row) | {"can_ask_npc": row.status == "invited" and row.proposer_id == actor_id
                    and row.recipient_id in definition.npc_routine} for row in state.contests
                  if row.definition_id == definition.definition_id and actor_id in {row.proposer_id, row.recipient_id}
                  and (row.status not in {"invited", "accepted"} or _contest_current(state, row, definition))]
        if not opponents and not recent:
            continue
        options.append({"definition_id": str(definition.definition_id), "title": definition.title, "terms": definition.terms,
                        "can_invite": _qualified(state, actor_id, definition), "opponents": opponents,
                        "routines": [{"key": item.key, "title": item.title} for item in definition.routines], "recent": recent[-8:]})
    return options


def validate_contest_state(state: WorldState) -> None:
    if not state.contest_definitions and not state.contests:
        return
    definitions = {item.definition_id: item for item in state.contest_definitions}
    qualifications = {item.challenge_id for item in state.challenges}
    if len(definitions) != len(state.contest_definitions):
        raise ValueError("Contest definitions must have distinct IDs.")
    for item in definitions.values():
        venue = state.entities.get(item.venue_id)
        if venue is None or venue.kind not in {"place", "location"}:
            raise ValueError("A contest must reference a known venue.")
        if item.proposer_challenge_id is not None and item.proposer_challenge_id not in qualifications:
            raise ValueError("A contest qualification must refer to a registered challenge.")
        if any((actor := state.entities.get(ident)) is None or actor.kind not in {"character", "person"} for ident in item.npc_routine):
            raise ValueError("Contest NPC policies must reference existing characters.")
    if len({row.contest_id for row in state.contests}) != len(state.contests):
        raise ValueError("Contest records must have distinct IDs.")
    for row in state.contests:
        definition = definitions.get(row.definition_id)
        if (definition is None or row.proposer_id == row.recipient_id
                or row.proposer_id not in state.entities or row.recipient_id not in state.entities
                or not row.opened_tick <= row.updated_tick <= state.current_tick or row.expiry_tick <= row.updated_tick):
            raise ValueError("The contest receipt must refer to registered participants and valid times.")
        routines = {routine.key for routine in definition.routines}
        if (row.proposer_routine not in routines or (row.recipient_routine is not None and row.recipient_routine not in routines)
                or (row.status in {"accepted", "settled"} and row.recipient_routine is None)
                or (row.status in {"invited", "declined"} and row.recipient_routine is not None)
                or len(row.rounds) != (len(definition.routines[0].skills) if row.status == "settled" else 0)):
            raise ValueError("A contest receipt has an inconsistent state or round count.")
