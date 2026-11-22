"""Typed, author-only event contract for bilateral safe-conduct changes."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .json_types import JsonObject
from .safe_conduct import SafeConductTreaty


SAFE_CONDUCT_CHANGED_EVENT_TYPE = "core.safe_conduct_changed.v1"
SafeConductEventOperation = Literal["propose", "ratify", "decline", "withdraw", "revoke", "counter", "accept_counter"]
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MAX_REVISION = 2**63 - 1


def safe_conduct_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: SafeConductEventOperation,
    treaty_id: UUID,
) -> UUID:
    """Derive an event ID from command, ruleset, operation and treaty ID."""

    if not isinstance(command_id, UUID) or not isinstance(treaty_id, UUID):
        raise ValueError("safe-conduct event IDs require UUID command and treaty IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("safe-conduct event IDs require a nonempty ruleset version")
    if operation not in {"propose", "ratify", "decline", "withdraw", "revoke", "counter", "accept_counter"}:
        raise ValueError("safe-conduct operation is unsupported")
    return uuid5(command_id, f"{ruleset_version}:safe_conduct.{operation}:{treaty_id}")


class SafeConductEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class SafeConductChangedEvent(SafeConductEventModel):
    """Bind a proposal or bilateral treaty transition to its command."""

    type: Literal["core.safe_conduct_changed.v1"] = SAFE_CONDUCT_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    expected_revision: StrictInt = Field(ge=0, le=_MAX_REVISION)
    operation: SafeConductEventOperation
    resulting_treaty: SafeConductTreaty
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def transition_is_command_bound_and_author_only(self) -> SafeConductChangedEvent:
        treaty = self.resulting_treaty
        if self.event_id != safe_conduct_event_id(
            self.command_id, self.ruleset_version, self.operation, treaty.treaty_id,
        ):
            raise ValueError("safe-conduct event ID must be bound to its command, operation and treaty")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("safe-conduct terms and party identities are author_only")

        expected_status = {
            "propose": "pending",
            "ratify": "active",
            "decline": "declined",
            "withdraw": "withdrawn",
            "revoke": "revoked",
            "counter": "countered",
            "accept_counter": "active",
        }[self.operation]
        if treaty.status != expected_status:
            raise ValueError("safe-conduct result status does not match its operation")

        if self.operation == "propose":
            if self.expected_revision != 0 or treaty.revision != 1:
                raise ValueError("a safe-conduct proposal must expect revision zero and create revision one")
            if self.actor_id != treaty.proposer_actor_id:
                raise ValueError("only the captured proposer may propose safe conduct")
            return self

        if self.expected_revision < 1 or self.expected_revision >= _MAX_REVISION:
            raise ValueError("a safe-conduct transition must expect a valid existing revision")
        if treaty.revision != self.expected_revision + 1:
            raise ValueError("a safe-conduct transition must advance exactly one revision")
        if self.operation == "ratify" and self.actor_id != treaty.recipient_actor_id:
            raise ValueError("only the captured recipient may ratify safe conduct")
        if self.operation == "counter" and self.actor_id != treaty.recipient_actor_id:
            raise ValueError("only the captured recipient may counter safe conduct")
        if self.operation == "accept_counter" and self.actor_id != treaty.proposer_actor_id:
            raise ValueError("only the captured proposer may accept a counteroffer")
        if self.requested_by is not None and (
            self.operation not in {"ratify", "decline", "counter"} or self.requested_by != treaty.proposer_actor_id
            or self.actor_id != treaty.recipient_actor_id
        ):
            raise ValueError("hosted safe conduct requires the captured proposer and recipient")
        # Withdrawal and revocation authority depends on current governance and
        # is validated by the pure transition and host, not this snapshot DTO.
        return self


SAFE_CONDUCT_EVENT_ADAPTER = TypeAdapter(SafeConductChangedEvent)


def parse_safe_conduct_event(
    value: SafeConductChangedEvent | dict[str, object],
) -> SafeConductChangedEvent:
    """Parse a standalone safe-conduct event DTO."""

    return SAFE_CONDUCT_EVENT_ADAPTER.validate_python(value)
