"""Author-only, command-bound DTOs for direct kinship link changes.

Reducer and author-authorization integration live at higher layers. These
contracts make event identity, effective time and source binding unambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from .components import Tick
from .errors import InvalidEvent
from .family_kinship import EventKinshipSource, KinshipLink, KinshipModel
from .json_types import JsonObject


KINSHIP_LINK_RECORDED = "core.kinship_link_recorded.v1"
KINSHIP_LINK_ENDED = "core.kinship_link_ended.v1"


def kinship_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: Literal["link_recorded", "link_ended"],
) -> UUID:
    """Derive the sole deterministic kinship event ID for a command operation."""

    if not ruleset_version or not ruleset_version.strip():
        raise ValueError("kinship event IDs require a nonempty ruleset version")
    return uuid5(command_id, f"{ruleset_version}:family_kinship.{operation}")


class KinshipLinkRecordedEvent(KinshipModel):
    """Record one new direct parent-to-child assertion at an exact tick."""

    type: Literal["core.kinship_link_recorded.v1"] = KINSHIP_LINK_RECORDED
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    effective_tick: Tick
    link: KinshipLink
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def event_and_link_are_command_bound(self) -> KinshipLinkRecordedEvent:
        if self.event_id != kinship_event_id(self.command_id, self.ruleset_version, "link_recorded"):
            raise ValueError("kinship-link event ID must be bound to its command and ruleset")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("kinship author event details must remain author-only")
        if (
            not isinstance(self.link.source, EventKinshipSource)
            or self.link.source.event_id != self.event_id
        ):
            raise ValueError("a recorded kinship link must cite its own committed event as source")
        if self.link.created_tick != self.effective_tick or self.link.ended_tick is not None:
            raise ValueError("a new kinship link must begin at the event tick and have no end tick")
        return self


class KinshipLinkEndedEvent(KinshipModel):
    """End one active direct link with a created-tick compare-and-swap."""

    type: Literal["core.kinship_link_ended.v1"] = KINSHIP_LINK_ENDED
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    effective_tick: Tick
    link_id: UUID
    expected_created_tick: Tick
    expected_relationship: Literal["birth", "adoptive"] | None = None
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def event_and_end_tick_are_command_bound(self) -> KinshipLinkEndedEvent:
        if self.event_id != kinship_event_id(self.command_id, self.ruleset_version, "link_ended"):
            raise ValueError("kinship-link-ended event ID must be bound to its command and ruleset")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("kinship author event details must remain author-only")
        if self.effective_tick < self.expected_created_tick:
            raise ValueError("a kinship link cannot end before its expected creation tick")
        return self


KINSHIP_EVENTS = (KinshipLinkRecordedEvent, KinshipLinkEndedEvent)


def validate_kinship_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, command_type: str | None = None,
) -> None:
    event = next((item for item in events if isinstance(item, KINSHIP_EVENTS)), None)
    if event is None:
        return
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("Kinship event must match the persisted world, branch, command, actor and ruleset.")
    expected = "author.record_kinship_link" if isinstance(event, KinshipLinkRecordedEvent) else "author.end_kinship_link"
    if command_type is not None and command_type != expected:
        raise InvalidEvent("Kinship event must match its author command type.")
