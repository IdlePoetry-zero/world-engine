"""Command and event consistency for institutional finance mutations."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from .institutional_credit_state import validate_institutional_credit_batch
from .institutional_credit_extension_events import validate_credit_extension_batch
from .institutional_treasury_reserve_events import validate_reserve_mobilization_batch


def validate_institutional_finance_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    scope = {
        "world_id": world_id, "branch_id": branch_id, "command_id": command_id, "actor_id": actor_id,
        "ruleset_version": ruleset_version, "command_type": command_type, "command_payload": command_payload,
    }
    validate_institutional_credit_batch(events, **scope)
    validate_credit_extension_batch(events, **scope)
    validate_reserve_mobilization_batch(events, **scope)
