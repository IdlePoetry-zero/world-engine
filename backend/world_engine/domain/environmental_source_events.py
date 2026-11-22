"""Typed event for opening a finite environmental source on an authored place.

Environmental harvest source accounts begin at zero. Their configured maximum
is stored on the place's renewable harvest policy; the account's balance and
credit limit then advance together as harvests are issued.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .json_types import JsonObject

if TYPE_CHECKING:
    from .models import WorldState


ENVIRONMENTAL_SOURCE_ACCOUNT_OPENED_EVENT_TYPE = "core.environmental_source_account_opened.v1"
_RESOURCE_ID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]


class EnvironmentalSourceEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class EnvironmentalSourceAccountOpenedEvent(EnvironmentalSourceEventModel):
    """Open an empty, finite environmental source owned by an existing place."""

    type: Literal["core.environmental_source_account_opened.v1"] = ENVIRONMENTAL_SOURCE_ACCOUNT_OPENED_EVENT_TYPE
    event_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    owner_id: UUID
    resource_id: _RESOURCE_ID
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def require_author_visibility(self) -> EnvironmentalSourceAccountOpenedEvent:
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("environmental source account details must remain author-only")
        return self


def apply_environmental_source_account_opened(
    state: WorldState,
    event: EnvironmentalSourceAccountOpenedEvent,
) -> WorldState:
    """Apply one empty source account and validate the full world state.

    Imports of the state models are intentionally local: ``models`` includes
    this event in its closed ``DomainEvent`` union.
    """

    from .errors import InvalidEvent
    from .models import ResourceAccount, WorldState as RuntimeWorldState

    try:
        event = EnvironmentalSourceAccountOpenedEvent.model_validate(event.model_dump(mode="python"))
        event.require_author_visibility()
        owner = state.entities.get(event.owner_id)
        if owner is None or owner.kind not in {"place", "location"}:
            raise InvalidEvent("Environmental sources must be owned by an existing place.")
        if event.account_id in state.resources:
            raise InvalidEvent(f"Resource account {event.account_id} already exists.")
        if any(
            account.account_type == "environmental_source"
            and account.owner_id == event.owner_id
            and account.resource_id == event.resource_id
            for account in state.resources.values()
        ):
            raise InvalidEvent("The place already has an environmental source account for this resource.")

        resources = dict(state.resources)
        resources[event.account_id] = ResourceAccount(
            resource_id=event.resource_id,
            balance=Decimal("0"),
            owner_id=event.owner_id,
            account_type="environmental_source",
            credit_limit=Decimal("0"),
        )
        payload = state.model_dump(mode="python")
        payload["resources"] = resources
        return RuntimeWorldState.model_validate(payload)
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent("Environmental source account event is invalid.") from exc
