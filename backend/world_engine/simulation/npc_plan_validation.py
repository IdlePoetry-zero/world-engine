"""Pure validation for committed NPC plan review and execution batches.

This validator independently regenerates the engine/plan transition from the
leased snapshot. It performs no persistence or I/O. Model-call provenance is
not established here; a trusted worker must verify the call trace against its
leased request and operation record before commit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID

from pydantic import Field

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.json_types import JsonObject
from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent
from world_engine.domain.models import DomainEvent, WorldState
from world_engine.domain.npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent
from world_engine.domain.npc_plan_review import (
    NPC_PLANNING_REJECTIONS,
    npc_plan_review_request_id,
    validate_npc_plan_review_schedule,
)
from world_engine.domain.npc_plan_review_events import NpcPlanReviewResolvedEvent
from world_engine.domain.relationship_invitation_events import RelationInvitationChangedEvent
from world_engine.domain.promise_events import PromiseItemReturnedEvent
from world_engine.domain.promise_trust_events import PromiseItemReturnedWithTrustEvent
from world_engine.domain.reducers import state_hash
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.rules.engine import RuleEngine

from .contracts import DecisionResponse, FrozenModel, SimulationContext, SimulationPlan
from .npc_capabilities import NpcCapabilityRegistry


class NpcPlanSimulationBatchValidation(FrozenModel):
    """Validated metadata for the existing strict domain batch validators."""

    mode: Literal["review", "execution"]
    actual_npc_actor_id: UUID
    core_rule_events: tuple[DomainEvent, ...] = ()
    action_command_type: str | None = None
    action_command_payload: JsonObject | None = None
    transition_outcome: str = Field(min_length=1, max_length=120)


def validate_npc_plan_simulation_batch(
    state: WorldState,
    events: Sequence[DomainEvent | dict[str, object]],
    plan: SimulationPlan,
    context: SimulationContext,
    *,
    journey_resolution_receipts: Mapping[UUID, TravelJourneyChangedEvent] | None = None,
    registry: NpcCapabilityRegistry | None = None,
    rule_engine: RuleEngine | None = None,
) -> NpcPlanSimulationBatchValidation:
    """Recompute an NPC plan transaction and return trusted validator inputs.

    Review proposals are reconstructed as the engine's typed response and
    passed through ``SimulationEngine.plan_window``. Execution proposals are
    regenerated through ``build_npc_plan_execution_transition`` using the
    exact current plan and any actual travel-resolution receipts.
    """
    parsed = _parse_events(events)
    review_events = [event for event in parsed if isinstance(event, NpcPlanReviewResolvedEvent)]
    if review_events:
        return _validate_review_batch(
            state,
            parsed,
            plan,
            context,
            registry=registry,
            rule_engine=rule_engine,
        )
    return _validate_execution_batch(
        state,
        parsed,
        plan,
        context,
        journey_resolution_receipts=journey_resolution_receipts,
        registry=registry,
        rule_engine=rule_engine,
    )


def _parse_events(events: Sequence[DomainEvent | dict[str, object]]) -> tuple[DomainEvent, ...]:
    try:
        parsed = tuple(parse_event(event) for event in events)
    except Exception as exc:
        raise InvalidEvent("NPC plan batch contains an invalid or unregistered event.") from exc
    if not parsed:
        raise InvalidEvent("An NPC plan transaction must contain its recomputed event batch.")
    event_ids = [getattr(event, "event_id", None) for event in parsed]
    event_ids = [event_id for event_id in event_ids if isinstance(event_id, UUID)]
    if len(event_ids) != len(set(event_ids)):
        raise InvalidEvent("NPC plan batch contains duplicate event IDs.")
    return parsed


def _validate_review_batch(
    state: WorldState,
    events: tuple[DomainEvent, ...],
    plan: SimulationPlan,
    context: SimulationContext,
    *,
    registry: NpcCapabilityRegistry | None,
    rule_engine: RuleEngine | None,
) -> NpcPlanSimulationBatchValidation:
    review_events = [event for event in events if isinstance(event, NpcPlanReviewResolvedEvent)]
    if len(review_events) != 1:
        raise InvalidEvent("A simulation window can resolve exactly one NPC plan review.")
    review = review_events[0]
    if any(not isinstance(event, (NpcPlanReviewResolvedEvent, NpcPlanCreatedEvent, NpcPlanRevisedEvent)) for event in events):
        raise InvalidEvent("An NPC plan review batch cannot include unrelated rule or time events.")
    if (
        (review.world_id, review.branch_id, review.command_id, review.ruleset_version)
        != (context.world_id, context.branch_id, context.command_id, context.ruleset_version)
        or review.at_tick != state.current_tick
        or review.based_on_state_version != context.state_version
    ):
        raise InvalidEvent("NPC plan review event is not bound to the exact simulation snapshot.")
    schedule = next((item for item in state.scheduled_events if item.id == review.scheduled_event_id), None)
    if (
        schedule is None
        or schedule.status != "queued"
        or schedule.due_tick != state.current_tick
        or schedule.revision != review.expected_schedule_revision
    ):
        raise InvalidEvent("NPC plan review does not match one exact due queued schedule.")
    actor_id = validate_npc_plan_review_schedule(state, schedule, context.world_id)
    if (
        actor_id != review.actor_id
        or review.decision_request_id != npc_plan_review_request_id(
            context.command_id, context.ruleset_version, schedule,
        )
    ):
        raise InvalidEvent("NPC plan review actor or decision request is not bound to its schedule.")

    from .engine import SimulationEngine

    engine = SimulationEngine(rule_engine=rule_engine, npc_capability_registry=registry)
    probe_context = context.model_copy(update={"decision_responses": ()})
    try:
        probe = engine.plan_window(state, plan, probe_context)
    except Exception as exc:
        raise InvalidEvent("The engine could not reproduce this NPC review boundary.") from exc

    # These policy outcomes have no model decision. The engine determines
    # whether the actor is unavailable, controlled, goal-less, or already has
    # an active plan; replaying a caller-supplied fake response would bypass
    # those checks.
    engine_policy_outcome = (
        review.source == "policy"
        and (
            review.outcome == "skipped"
            or (review.outcome == "defer" and review.reason == "npc_review_existing_plan_in_progress")
        )
    )
    if engine_policy_outcome:
        if context.decision_responses:
            raise InvalidEvent("Engine-resolved policy skips cannot consume a supplied decision response.")
        expected = probe
    else:
        if review.source == "model":
            actor = state.entities.get(actor_id)
            if (
                actor is None
                or actor.status != "active"
                or actor_id == state.player_id
                or actor_id in context.externally_controlled_actor_ids
            ):
                raise InvalidEvent("A model-driven review cannot control an unavailable or user-controlled actor.")
        if (
            probe.outcome != "decision_required"
            or probe.events
            or len(probe.decision_requests) != 1
            or not _request_matches_review(probe.decision_requests[0], review, context)
        ):
            raise InvalidEvent("The engine did not select this exact NPC review for a decision.")

        created = [event for event in events if isinstance(event, NpcPlanCreatedEvent)]
        if review.outcome == "ready":
            if len(created) != 1 or created[0].plan.plan_id != review.plan_id:
                raise InvalidEvent("A ready NPC review must include its single exact created plan.")
            if created[0].actor_id != actor_id:
                raise InvalidEvent("A ready NPC review cannot create a plan for another actor.")
            proposed_plan = created[0].plan
        else:
            if created or review.plan_id is not None:
                raise InvalidEvent("A non-ready NPC review cannot create or reference a plan.")
            proposed_plan = None
        try:
            response = DecisionResponse(
                request_id=review.decision_request_id,
                state_version=review.based_on_state_version,
                at_tick=review.at_tick,
                source=review.source,
                option_id="plan",
                npc_planning_status=review.outcome,
                npc_planning_rejection=review.reason if review.reason in NPC_PLANNING_REJECTIONS else None,
                npc_plan=proposed_plan,
                npc_plan_state_hash=state_hash(state),
                npc_model_call_id=review.model_call_id,
            )
            if context.decision_responses and (
                len(context.decision_responses) != 1
                or not _response_matches_review(context.decision_responses[0], response)
            ):
                raise InvalidEvent("Supplied decision response differs from the typed review event batch.")
            response_context = probe_context.model_copy(update={"decision_responses": (response,)})
            expected = engine.plan_window(state, plan, response_context)
        except Exception as exc:
            raise InvalidEvent("The engine could not reproduce this typed NPC plan response.") from exc

    if (
        expected.metrics.integration_boundary != "npc_plan_review_resolved"
        or expected.metrics.ticks_advanced != 0
        or not expected.events
        or tuple(expected.events) != events
    ):
        raise InvalidEvent("NPC plan review batch differs from the exact engine-selected transition.")
    return NpcPlanSimulationBatchValidation(
        mode="review",
        actual_npc_actor_id=actor_id,
        core_rule_events=(),
        transition_outcome=review.outcome,
    )


def _request_matches_review(request, event: NpcPlanReviewResolvedEvent, context: SimulationContext) -> bool:
    return (
        request.kind == "npc.plan_review"
        and request.request_id == event.decision_request_id
        and request.causal_subject_id == event.scheduled_event_id
        and request.actor_id == event.actor_id
        and request.at_tick == event.at_tick
        and request.state_version == context.state_version
    )


def _response_matches_review(supplied: DecisionResponse, rebuilt: DecisionResponse) -> bool:
    """Compare authoritative response fields while ignoring free-form explanation."""
    return all(
        getattr(supplied, name) == getattr(rebuilt, name)
        for name in (
            "request_id",
            "state_version",
            "at_tick",
            "source",
            "option_id",
            "npc_planning_status",
            "npc_planning_rejection",
            "npc_plan",
            "npc_plan_state_hash",
            "npc_model_call_id",
        )
    )


def _validate_execution_batch(
    state: WorldState,
    events: tuple[DomainEvent, ...],
    plan: SimulationPlan,
    context: SimulationContext,
    *,
    journey_resolution_receipts: Mapping[UUID, TravelJourneyChangedEvent] | None,
    registry: NpcCapabilityRegistry | None,
    rule_engine: RuleEngine | None,
) -> NpcPlanSimulationBatchValidation:
    if context.decision_responses:
        raise InvalidEvent("NPC plan execution cannot consume an unbound decision response.")
    if any(isinstance(event, (NpcPlanCreatedEvent, NpcPlanReviewResolvedEvent)) for event in events):
        raise InvalidEvent("Plan creation and review events require the review transaction path.")
    revisions = [event for event in events if isinstance(event, NpcPlanRevisedEvent)]
    plan_ids = {event.plan.plan_id for event in revisions}
    if not revisions or len(plan_ids) != 1:
        raise InvalidEvent("An execution batch must revise exactly one existing NPC plan ID.")
    plan_id = next(iter(plan_ids))
    stored = [plan for plan in state.npc_plans if plan.plan_id == plan_id]
    if len(stored) != 1:
        raise InvalidEvent("NPC plan execution requires one existing stored plan.")

    from .npc_plan_execution import build_npc_plan_execution_transition

    try:
        transition = build_npc_plan_execution_transition(
            state,
            plan_id,
            context,
            registry=registry,
            rule_engine=rule_engine,
            journey_resolution_receipts=journey_resolution_receipts,
        )
    except Exception as exc:
        raise InvalidEvent("NPC plan execution transition could not be recomputed.") from exc
    if transition.outcome == "interval_required":
        # The pure capability layer correctly proposes only the required
        # duration. The simulation engine owns shared-clock policy and turns
        # that non-committable proposal into one bounded blocked-plan revision.
        from .engine import SimulationEngine

        try:
            resolution = SimulationEngine(
                rule_engine=rule_engine,
                npc_capability_registry=registry,
            ).plan_window(
                state,
                plan,
                context,
            )
        except Exception as exc:
            raise InvalidEvent("The engine could not reproduce the shared-clock NPC interval decision.") from exc
        if (
            resolution.metrics.integration_boundary != "npc_plan_execution_resolved"
            or resolution.metrics.ticks_advanced != 0
            or tuple(resolution.events) != events
        ):
            raise InvalidEvent("NPC interval resolution differs from the exact shared-clock engine transition.")
        if any(isinstance(event, NpcPlanRevisedEvent) is False for event in events):
            raise InvalidEvent("An interval-required NPC plan must commit only its blocked-plan revision.")
        if any(
            getattr(event, "world_id", context.world_id) != context.world_id
            or getattr(event, "branch_id", context.branch_id) != context.branch_id
            or getattr(event, "command_id", context.command_id) != context.command_id
            or getattr(event, "ruleset_version", context.ruleset_version) != context.ruleset_version
            for event in events
        ):
            raise InvalidEvent("NPC interval maintenance event is not bound to the active simulation context.")
        return NpcPlanSimulationBatchValidation(
            mode="execution",
            actual_npc_actor_id=stored[0].actor_id,
            core_rule_events=(),
            transition_outcome=transition.outcome,
        )
    before = state_hash(state)
    if (
        transition.plan_id != plan_id
        or transition.actor_id != stored[0].actor_id
        or transition.source_state_hash != before
        or transition.input_state_hash_after != before
        or transition.tick_before != state.current_tick
        or transition.tick_after != state.current_tick
        or transition.time_advanced is not False
        or transition.committed is not False
        or not transition.events
        or tuple(transition.events) != events
    ):
        raise InvalidEvent("NPC plan execution batch differs from its exact revalidated transition.")
    if any(
        getattr(event, "world_id", context.world_id) != context.world_id
        or getattr(event, "branch_id", context.branch_id) != context.branch_id
        or getattr(event, "command_id", context.command_id) != context.command_id
        or getattr(event, "ruleset_version", context.ruleset_version) != context.ruleset_version
        for event in events
    ):
        raise InvalidEvent("NPC plan execution events are not bound to the active simulation context.")

    core_events = tuple(
        event for event in transition.events if not isinstance(event, NpcPlanRevisedEvent)
    )
    resolution_events = tuple(transition.rule_resolution.events) if transition.rule_resolution is not None else ()
    if core_events != resolution_events:
        raise InvalidEvent("NPC plan execution rule events do not match the original RuleResolution.")
    command_type, command_payload = _action_command(core_events, stored[0].actor_id)
    return NpcPlanSimulationBatchValidation(
        mode="execution",
        actual_npc_actor_id=stored[0].actor_id,
        core_rule_events=core_events,
        action_command_type=command_type,
        action_command_payload=command_payload,
        transition_outcome=transition.outcome,
    )


def _action_command(
    events: tuple[DomainEvent, ...],
    actor_id: UUID,
) -> tuple[str | None, JsonObject | None]:
    if len(events) == 1 and isinstance(events[0], (PromiseItemReturnedEvent, PromiseItemReturnedWithTrustEvent)):
        event = events[0]
        settlement = event.settlement if isinstance(event, PromiseItemReturnedWithTrustEvent) else event
        if settlement.actor_id != actor_id:
            raise InvalidEvent("An NPC plan must return its own promised goods.")
        return "action.fulfill_return_promise", {
            "kind": "fulfill_return_promise",
            "promise_id": str(settlement.promise_id),
        }
    invitations = [event for event in events if isinstance(event, RelationInvitationChangedEvent)]
    ballots = [event for event in events if isinstance(event, LeadershipBallotChangedEvent)]
    if invitations and ballots:
        raise InvalidEvent("An NPC action cannot combine invitation and ballot commands.")
    if invitations:
        if len(invitations) != 1:
            raise InvalidEvent("One NPC plan step cannot emit multiple invitation transitions.")
        event = invitations[0]
        invitation = event.resulting_invitation
        if event.actor_id != actor_id or event.requested_by is not None:
            raise InvalidEvent("NPC plan invitation actions must be direct acts by the planned actor.")
        if event.operation == "propose":
            payload: JsonObject = {
                "kind": "propose_relationship_invitation",
                "target_id": str(invitation.invitee_id),
            }
            if invitation.meeting is not None:
                payload["meeting"] = invitation.meeting.model_dump(mode="json")
            return "action.propose_relationship_invitation", payload
        if event.operation == "attend":
            return "action.attend_relationship_meeting", {
                "kind": "attend_relationship_meeting",
                "invitation_id": str(invitation.invitation_id),
            }
        return "action.resolve_relationship_invitation", {
            "kind": "resolve_relationship_invitation",
            "invitation_id": str(invitation.invitation_id),
            "operation": event.operation,
        }
    if ballots:
        if len(ballots) != 1:
            raise InvalidEvent("One NPC plan step cannot emit multiple ballot transitions.")
        event = ballots[0]
        if event.actor_id != actor_id or event.requested_by is not None or event.operation != "cast":
            raise InvalidEvent("NPC plan ballots must be a direct vote by the planned actor.")
        votes = [vote for vote in event.resulting_ballot.votes if vote.voter_id == actor_id]
        if len(votes) != 1:
            raise InvalidEvent("NPC ballot action must include exactly one vote by the planned actor.")
        return "action.cast_leadership_ballot_vote", {
            "kind": "cast_leadership_ballot_vote",
            "ballot_id": str(event.resulting_ballot.ballot_id),
            "expected_ballot_revision": event.expected_revision,
            "choice": votes[0].choice,
        }
    return None, None

