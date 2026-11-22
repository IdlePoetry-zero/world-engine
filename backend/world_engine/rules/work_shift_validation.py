"""Validate a paid shift batch against the rules that generated its timeline."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import DomainEvent, WorldState
from world_engine.domain.work_shift_events import WorkShiftPaidEvent, validate_work_shift_batch

from .contracts import RuleContext, WorkShiftIntent
from .engine import RuleEngine


def validate_completed_work_shift(
    state: WorldState, events: Sequence[DomainEvent], *, world_id: UUID, branch_id: UUID,
    command_id: UUID, actor_id: UUID, ruleset_version: str,
    command_type: str | None = None, command_payload: Mapping[str, object] | None = None,
) -> None:
    validate_work_shift_batch(
        events, world_id=world_id, branch_id=branch_id, command_id=command_id,
        actor_id=actor_id, ruleset_version=ruleset_version, start_tick=state.current_tick,
        command_type=command_type, command_payload=command_payload,
    )
    payment = next((event for event in events if isinstance(event, WorkShiftPaidEvent)), None)
    if payment is None:
        return
    context = RuleContext(world_id=world_id, branch_id=branch_id, command_id=command_id,
                          actor_id=actor_id, ruleset_version=ruleset_version)
    resolution = RuleEngine().evaluate(state, WorkShiftIntent(assignment_id=payment.plan.assignment_id), context)
    if [event.model_dump(mode="json") for event in resolution.events] != [event.model_dump(mode="json") for event in events]:
        raise InvalidEvent("Paid shift must match the rule-generated clock, needs and wage events.")
