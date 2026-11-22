"""Branch-local continuation of player control after a character's death."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid5

from .components import LifeComponent
from .errors import InvalidEvent
from .gameplay_events import PlayerCharacterContinuedEvent
from .models import DomainEvent, WorldState


PLAYER_CONTINUE_AFTER_DEATH_COMMAND = "player.continue_after_death"
PLAYER_CHARACTER_CONTINUED_EVENT_TYPE = "core.player_character_continued.v1"
_UNAVAILABLE = "Player continuation is unavailable."


def is_player_continuation_candidate(state: WorldState, new_player_id: UUID) -> bool:
    """Return whether an entity is a living, non-expired continuation target."""
    target = state.entities.get(new_player_id)
    if target is None or target.kind not in {"character", "person"} or target.status != "active":
        return False
    raw_life = target.components.get("life")
    if raw_life is None:
        return True
    try:
        life = LifeComponent.model_validate(raw_life)
    except Exception:
        return False
    return (
        life.is_immortal
        or life.lifespan_ticks is None
        or state.current_tick < life.birth_tick + life.lifespan_ticks
    )


def _validate_continuation_state(
    state: WorldState,
    *,
    actor_id: UUID,
    previous_player_id: UUID,
    new_player_id: UUID,
) -> None:
    if (
        state.player_id is None
        or state.player_id != previous_player_id
        or actor_id != previous_player_id
        or previous_player_id == new_player_id
    ):
        raise InvalidEvent(_UNAVAILABLE)

    previous = state.entities.get(previous_player_id)
    if (
        previous is None
        or previous.kind not in {"character", "person"}
        or previous.status != "dead"
        or not is_player_continuation_candidate(state, new_player_id)
    ):
        raise InvalidEvent(_UNAVAILABLE)


def plan_player_character_continuation(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID,
    previous_player_id: UUID,
    new_player_id: UUID,
    source_death_event_id: UUID,
) -> PlayerCharacterContinuedEvent:
    """Plan a deterministic zero-time player-pointer change after a death.

    The storage layer verifies that ``source_death_event_id`` is the persisted
    terminal transition for ``previous_player_id``. This pure domain planner
    checks the authoritative pre-state and the selected living target.
    """
    try:
        _validate_continuation_state(
            state,
            actor_id=actor_id,
            previous_player_id=previous_player_id,
            new_player_id=new_player_id,
        )
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent(_UNAVAILABLE) from exc

    event_id = uuid5(command_id, f"{ruleset_version}:player_character_continued")
    if source_death_event_id == event_id:
        raise InvalidEvent(_UNAVAILABLE)
    return PlayerCharacterContinuedEvent(
        event_id=event_id,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        previous_player_id=previous_player_id,
        new_player_id=new_player_id,
        source_death_event_id=source_death_event_id,
        current_tick=state.current_tick,
    )


def validate_player_character_continued_batch(
    state: WorldState,
    actor_id: UUID,
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    """Validate the exact player-continuation command and unchanged pre-state."""
    continuations = [
        event for event in events if isinstance(event, PlayerCharacterContinuedEvent)
    ]
    if not continuations:
        if command_type == PLAYER_CONTINUE_AFTER_DEATH_COMMAND:
            raise InvalidEvent(_UNAVAILABLE)
        return
    if (command_type is None) != (command_payload is None):
        raise InvalidEvent(_UNAVAILABLE)
    if len(events) != 1 or len(continuations) != 1:
        raise InvalidEvent(_UNAVAILABLE)

    event = continuations[0]
    expected_event_id = uuid5(command_id, f"{ruleset_version}:player_character_continued")
    if (
        event.event_id != expected_event_id
        or event.world_id != world_id
        or event.branch_id != branch_id
        or event.command_id != command_id
        or event.ruleset_version != ruleset_version
        or event.actor_id != actor_id
        or event.current_tick != state.current_tick
        or event.source_death_event_id == event.event_id
    ):
        raise InvalidEvent(_UNAVAILABLE)
    try:
        _validate_continuation_state(
            state,
            actor_id=actor_id,
            previous_player_id=event.previous_player_id,
            new_player_id=event.new_player_id,
        )
    except InvalidEvent as exc:
        raise InvalidEvent(_UNAVAILABLE) from exc

    if command_type is not None and (
        command_type != PLAYER_CONTINUE_AFTER_DEATH_COMMAND
        or command_payload != {
            "previous_player_id": str(event.previous_player_id),
            "new_player_id": str(event.new_player_id),
            "source_death_event_id": str(event.source_death_event_id),
        }
    ):
        raise InvalidEvent(_UNAVAILABLE)
