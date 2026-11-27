"""Calendar-bound opportunities for scoped, model-proposed world events."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import GoalCondition, GoalRecord, GovernanceComponent, KnowledgeRecord, ScheduledEventRecord, VisibilityScope

if TYPE_CHECKING:
    from .models import WorldState

AMBIENT_EVENT_HANDLER = "world.ambient_event"
AMBIENT_EVENT_VERSION = "1.0.0"
AMBIENT_EVENT_RESOLVED = "core.ambient_event_resolved.v1"
AMBIENT_FOLLOWUP_POLICY = "ambient_followup_v1"
_MAX_TICK = 2**63 - 1
_PLACE_KINDS = frozenset({"place", "location"})
_ORGANIZATION_KINDS = frozenset({"organization", "polity", "faction", "company", "government"})
_PEOPLE = frozenset({"character", "person"})


class AmbientEventPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_kind: Literal["region", "organization"]
    scope_id: UUID
    check_every_days: StrictInt = Field(ge=1, le=360)
    chance_basis_points: StrictInt = Field(ge=0, le=10_000)
    cooldown_days: StrictInt = Field(default=1, ge=0, le=360)
    concurrent_limit: StrictInt = Field(default=1, ge=1, le=16)


class AmbientEventProposal(BaseModel):
    """A prospective situation. Its text has no authority to cause side effects."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: Literal["invitation", "request_for_help", "local_observation", "rumor", "organization_agenda"]
    place_id: UUID
    initiator_id: UUID
    participant_ids: tuple[UUID, ...] = Field(default=(), max_length=4)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=420)
    duration_days: StrictInt = Field(default=1, ge=1, le=14)
    follow_up: Literal["inspect_site"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def distinct_participants(self) -> AmbientEventProposal:
        if self.initiator_id in self.participant_ids or len(self.participant_ids) != len(set(self.participant_ids)):
            raise ValueError("The initiator and participants must be distinct")
        if self.follow_up is not None and self.kind not in {"request_for_help", "local_observation", "organization_agenda"}:
            raise ValueError("Only a request, observation, or organization agenda can suggest an inspection")
        return self


class AmbientEventRecord(BaseModel):
    """A validated situation with its source and active time window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    schedule_id: UUID
    scope_id: UUID
    start_tick: StrictInt
    end_tick: StrictInt
    cooldown_until_tick: StrictInt
    proposal: AmbientEventProposal


class AmbientEventResolvedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["core.ambient_event_resolved.v1"] = AMBIENT_EVENT_RESOLVED
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    schedule_id: UUID
    occurrence: StrictInt = Field(ge=0, le=999_999)
    checked_tick: StrictInt
    roll: StrictInt = Field(ge=0, lt=10_000)
    outcome: Literal["missed", "cooldown", "capacity", "empty_scope", "deferred", "unavailable", "accepted"]
    model_call_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    proposal: AmbientEventProposal | None = None
    visibility: dict[str, Literal["author_only"]] = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def valid_occurrence(self) -> AmbientEventResolvedEvent:
        if self.event_id != ambient_event_id(self.schedule_id, self.branch_id, self.occurrence):
            raise ValueError("The event ID must bind this scheduled occurrence and branch")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("Raw ambient event receipts are author-only")
        if self.outcome == "accepted" and (self.proposal is None or self.model_call_id is None):
            raise ValueError("An accepted proposal requires a valid model call receipt")
        if self.outcome != "accepted" and self.proposal is not None:
            raise ValueError("An unaccepted proposal cannot enter world state")
        if self.outcome in {"missed", "cooldown", "capacity", "empty_scope"} and self.model_call_id is not None:
            raise ValueError("An ineligible opportunity cannot use a model call")
        return self


def ambient_event_id(schedule_id: UUID, branch_id: UUID, occurrence: int) -> UUID:
    return uuid5(schedule_id, f"{AMBIENT_EVENT_VERSION}:{branch_id}:{occurrence}")


def build_ambient_event_schedule(
    state: WorldState, policy: AmbientEventPolicy, *, first_tick: int, max_runs: int = 1_000_000,
) -> ScheduledEventRecord:
    scope = state.entities.get(policy.scope_id)
    kinds = _PLACE_KINDS if policy.scope_kind == "region" else _ORGANIZATION_KINDS
    if scope is None or scope.kind not in kinds:
        raise ValueError("An ambient policy needs a registered scope of its declared kind")
    interval = state.calendar.day_seconds * policy.check_every_days
    if first_tick < state.current_tick or first_tick + (max_runs - 1) * interval > _MAX_TICK:
        raise ValueError("Ambient schedule is outside the world clock range")
    return ScheduledEventRecord(
        id=uuid5(policy.scope_id, f"{AMBIENT_EVENT_HANDLER}:{policy.scope_kind}"),
        due_tick=first_tick, phase="decision", priority=-10, handler_id=AMBIENT_EVENT_HANDLER,
        handler_version=AMBIENT_EVENT_VERSION, repeat_ticks=interval, max_runs=max_runs,
        payload=policy.model_dump(mode="json"),
    )


def ambient_roll(world_seed: str, branch_id: UUID, schedule: ScheduledEventRecord) -> int:
    """Stable under a retry and independent of unrelated command counts."""
    decision = f"{AMBIENT_EVENT_VERSION}:{branch_id}:{schedule.id}:{schedule.completed_runs}:{schedule.due_tick}"
    seed = sha256(world_seed.encode("utf-8")).digest()
    return int.from_bytes(sha256(seed + decision.encode("ascii")).digest(), "big") % 10_000


def _inside_region(state: WorldState, place_id: UUID, region_id: UUID) -> bool:
    traversed = set()
    current = state.entities.get(place_id)
    while current is not None and current.kind in _PLACE_KINDS and current.id not in traversed:
        if current.id == region_id:
            return True
        traversed.add(current.id)
        current = state.entities.get(current.location_id)
    return False


def ambient_participants(
    state: WorldState, policy: AmbientEventPolicy, *, controlled: frozenset[UUID] = frozenset(),
) -> dict[UUID, UUID]:
    """Return currently available NPC IDs with their allowed occurrence places."""
    scope = state.entities.get(policy.scope_id)
    if scope is None or scope.status != "active":
        return {}
    group = policy.scope_kind == "organization"
    if scope.kind not in (_ORGANIZATION_KINDS if group else _PLACE_KINDS):
        return {}
    members = set(scope.components.get("governance", {}).get("members", ())) if group else set()
    transit = {journey.actor_id for journey in state.travel_journeys if journey.status == "in_transit"}
    participants = {}
    for person in state.entities.values():
        if (person.kind not in _PEOPLE or not person.name or person.status != "active"
                or person.id == state.player_id or person.id in controlled or person.id in transit
                or person.location_id is None):
            continue
        place = state.entities.get(person.location_id)
        if place is None or place.kind not in _PLACE_KINDS or place.status != "active":
            continue
        if (str(person.id) in members if group else _inside_region(state, place.id, scope.id)):
            participants[person.id] = place.id
    return participants


def ambient_agenda_author_ids(state: WorldState, policy: AmbientEventPolicy) -> frozenset[UUID]:
    if policy.scope_kind != "organization":
        return frozenset()
    organization = state.entities.get(policy.scope_id)
    governance_data = organization.components.get("governance") if organization is not None else None
    if governance_data is None:
        return frozenset()
    governance = GovernanceComponent.model_validate(governance_data)
    authorities = {governance.leader_id, *(office.holder_id for office in governance.offices if office.authority)}
    return frozenset(authorities.intersection(governance.members))


def validate_ambient_proposal(
    proposal: AmbientEventProposal, eligible: dict[UUID, UUID], *, agenda_authors: frozenset[UUID] = frozenset(),
) -> None:
    people = (proposal.initiator_id, *proposal.participant_ids)
    if any(eligible.get(person) != proposal.place_id for person in people):
        raise ValueError("Every participant must be eligible and present at the proposed location")
    if proposal.kind == "organization_agenda" and proposal.initiator_id not in agenda_authors:
        raise ValueError("An organization agenda requires a current officer or leader of the organization")


def ambient_inspection_actor(proposal: AmbientEventProposal) -> UUID:
    return proposal.participant_ids[0] if proposal.participant_ids else proposal.initiator_id


def ambient_inspection_actors(state: WorldState, eligible: dict[UUID, UUID]) -> frozenset[UUID]:
    from .npc_plans import NPC_SHARED_EXPLORATION_POLICY

    if (
        state.metadata.get(AMBIENT_FOLLOWUP_POLICY) is not True
        or state.metadata.get(NPC_SHARED_EXPLORATION_POLICY) is not True
    ):
        return frozenset()
    return frozenset(
        actor_id for actor_id, place_id in eligible.items()
        if state.entities[place_id].components.get("hidden") is not True
    )


def validate_ambient_follow_up(
    proposal: AmbientEventProposal, state: WorldState, eligible: dict[UUID, UUID],
) -> None:
    if proposal.follow_up is not None and ambient_inspection_actor(proposal) not in ambient_inspection_actors(state, eligible):
        raise ValueError("The proposed character cannot take up an inspection at this location")


def ambient_policy_result(
    state: WorldState, schedule: ScheduledEventRecord, policy: AmbientEventPolicy, roll: int,
) -> Literal["missed", "cooldown", "capacity", "empty_scope"] | None:
    if roll >= policy.chance_basis_points:
        return "missed"
    current = [row for row in state.ambient_events if row.schedule_id == schedule.id]
    if sum(row.end_tick > state.current_tick for row in current) >= policy.concurrent_limit:
        return "capacity"
    if any(row.cooldown_until_tick > state.current_tick for row in current):
        return "cooldown"
    if not ambient_participants(state, policy):
        return "empty_scope"
    return None


def ambient_world_seed(state: WorldState, world_id: UUID) -> str:
    seed = state.metadata.get("generation_seed")
    if seed is None:
        seed = state.metadata.get("seed")
    return str(world_id if seed is None or seed == "" else seed)


def resolve_ambient_event_state(state: WorldState, event: AmbientEventResolvedEvent) -> dict[str, object]:
    schedule = next((row for row in state.scheduled_events if row.id == event.schedule_id), None)
    if (
        schedule is None or schedule.handler_id != AMBIENT_EVENT_HANDLER
        or schedule.handler_version != AMBIENT_EVENT_VERSION or schedule.phase != "decision"
        or schedule.status != "queued" or schedule.due_tick != state.current_tick
        or event.checked_tick != state.current_tick or event.occurrence != schedule.completed_runs
    ):
        raise ValueError("The ambient event must settle its exact queued occurrence")
    policy = AmbientEventPolicy.model_validate(schedule.payload)
    interval = policy.check_every_days * state.calendar.day_seconds
    if schedule.repeat_ticks != interval or schedule.id != uuid5(
        policy.scope_id, f"{AMBIENT_EVENT_HANDLER}:{policy.scope_kind}",
    ):
        raise ValueError("The event schedule does not match its registered policy")
    seed = ambient_world_seed(state, event.world_id)
    roll = ambient_roll(seed, event.branch_id, schedule)
    if event.roll != roll:
        raise ValueError("The recorded roll does not match the deterministic opportunity")
    required = ambient_policy_result(state, schedule, policy, roll)
    if (required is not None and required != event.outcome) or (
        required is None and event.outcome not in {"deferred", "unavailable", "accepted"}
    ):
        raise ValueError("The outcome does not match the current opportunity")
    completed_runs = schedule.completed_runs + 1
    repeats = completed_runs < schedule.max_runs
    replacement = ScheduledEventRecord.model_validate(schedule.model_dump(mode="python") | {
        "completed_runs": completed_runs, "status": "queued" if repeats else "completed",
        "due_tick": state.current_tick + interval if repeats else state.current_tick,
        "revision": schedule.revision + 1,
    })
    active = [row for row in state.ambient_events if max(row.end_tick, row.cooldown_until_tick) > state.current_tick]
    updates = {
        "scheduled_events": [replacement if row.id == schedule.id else row for row in state.scheduled_events],
        "ambient_events": active,
    }
    if event.proposal is not None:
        proposal = event.proposal
        eligible = ambient_participants(state, policy)
        validate_ambient_follow_up(proposal, state, eligible)
        validate_ambient_proposal(
            proposal, eligible, agenda_authors=ambient_agenda_author_ids(state, policy),
        )
        end_tick = state.current_tick + proposal.duration_days * state.calendar.day_seconds
        cooldown = state.current_tick + policy.cooldown_days * state.calendar.day_seconds
        if max(end_tick, cooldown) > _MAX_TICK:
            raise ValueError("The approved event exceeds the world clock range")
        active.append(AmbientEventRecord(
            event_id=event.event_id, schedule_id=schedule.id, scope_id=policy.scope_id,
            start_tick=state.current_tick, end_tick=end_tick, cooldown_until_tick=cooldown, proposal=proposal,
        ))
        updates["knowledge"] = [*state.knowledge, *(
            KnowledgeRecord(
                id=uuid5(event.event_id, f"learned:{actor_id}"), holder_id=actor_id,
                subject_id=proposal.place_id, claim=proposal.description, summary=proposal.title,
                source_event_id=event.event_id, received_tick=state.current_tick,
                kind="rumor" if proposal.kind == "rumor" else "observation",
                visibility=VisibilityScope(policy="subject_ids", subject_ids=[actor_id]),
            )
            for actor_id in (proposal.initiator_id, *proposal.participant_ids)
        )]
        is_agenda = proposal.kind == "organization_agenda"
        if is_agenda or (proposal.kind in {"invitation", "request_for_help"} and proposal.participant_ids):
            from .npc_plan_review import schedule_npc_commitment_review

            recipient = proposal.initiator_id if is_agenda else proposal.participant_ids[0]
            counterpart = next(iter(proposal.participant_ids), None) if is_agenda else proposal.initiator_id
            title = f"研究组织议题「{proposal.title}」" if is_agenda else f"考虑回应「{proposal.title}」"
            topic_id = uuid5(event.event_id, f"learned:{recipient}")
            conditions = [GoalCondition(kind="discuss_known_topic", subject_id=recipient,
                                        target_id=counterpart, evidence_id=topic_id)] if counterpart is not None else []
            goal = GoalRecord(
                id=uuid5(event.event_id, f"consider-response:{recipient}"), owner_id=recipient,
                counterpart_id=counterpart, title=title,
                description=proposal.description, priority=60, deadline=end_tick,
                created_tick=state.current_tick, updated_tick=state.current_tick,
                evidence_refs=[topic_id], conditions=conditions, auto_complete=bool(conditions),
            )
            updates["goals"] = [*state.goals, goal]
            # Wake the existing planner. The offer adds neither an agreement nor an action.
            review_state = state.model_copy(update=updates)
            review = schedule_npc_commitment_review(review_state, event.world_id, recipient, end_tick)
            if review is not None:
                schedules = [row for row in review_state.scheduled_events if row.id != review.id]
                updates["scheduled_events"] = [*schedules, review]
        if proposal.follow_up == "inspect_site":
            from .npc_plan_review import schedule_npc_commitment_review

            actor = ambient_inspection_actor(proposal)
            topic = uuid5(event.event_id, f"learned:{actor}")
            counterpart = proposal.initiator_id if actor != proposal.initiator_id else None
            goal = GoalRecord(
                id=uuid5(event.event_id, f"inspect-site:{actor}"), owner_id=actor,
                title=f"现场核查「{proposal.title}」", description=proposal.description,
                priority=60, deadline=end_tick, created_tick=state.current_tick, updated_tick=state.current_tick,
                evidence_refs=[topic], auto_complete=True,
                conditions=[GoalCondition(kind="inspect_known_topic", subject_id=actor,
                                          target_id=proposal.place_id, evidence_id=topic)],
            )
            goals = [goal]
            if counterpart is not None:
                goals.append(GoalRecord(
                    id=uuid5(event.event_id, f"report-inspection:{actor}"), owner_id=actor, counterpart_id=counterpart,
                    title=f"考虑向委托者反馈「{proposal.title}」的核查结果",
                    description="在核查完成后，可依据自己的发现决定是否和原知情者沟通。",
                    priority=55, deadline=end_tick, created_tick=state.current_tick, updated_tick=state.current_tick,
                    evidence_refs=[topic], auto_complete=True,
                    conditions=[
                        GoalCondition(kind="goal_completed", target_id=goal.id),
                        GoalCondition(kind="report_topic_inspection", subject_id=actor,
                                      target_id=counterpart, evidence_id=topic),
                    ],
                ))
            updates["goals"] = [*updates.get("goals", state.goals), *goals]
            review_state = state.model_copy(update=updates)
            review = schedule_npc_commitment_review(review_state, event.world_id, actor, end_tick)
            if review is not None:
                updates["scheduled_events"] = [
                    *(row for row in review_state.scheduled_events if row.id != review.id), review,
                ]
    return updates
