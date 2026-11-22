"""HTTP-safe encoding preserving precision of simulation time and versions."""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder


_EXACT_INTEGERS = frozenset(
    {
        "tick", "current_tick", "to_tick", "from_tick", "due_tick", "birth_tick", "started_tick", "target_tick",
        "created_tick", "ended_tick", "effective_tick", "proposed_tick", "updated_tick", "starts_at_tick", "ends_at_tick", "expires_at_tick",
        "counter_expires_at_tick", "start_tick", "end_tick", "attended_tick", "recorded_tick",
        "departed_tick", "arrival_tick", "resolved_tick",
        "opens_tick", "closes_tick", "minimum_itinerary_elapsed_ticks",
        "snapshot_tick", "accepted_through_tick", "maturity_tick", "accepted_tick", "overdue_at_tick",
        "previous_maturity_tick", "new_maturity_tick", "accept_through_tick", "previous_overdue_tick",
        "committed_tick", "committed_through_tick", "received_tick", "valid_from",
        "valid_to", "state_version", "state_version_after", "expected_state_version",
        "sequence", "last_seq", "head_seq", "first_seq", "fork_seq", "next_cursor",
        "headcount", "unmodeled_headcount", "carrying_capacity",
        "promotion_serial", "last_advanced_year",
    }
)


def wire(value: Any) -> Any:
    """Encode domain envelopes, not an authorization or visibility boundary."""
    encoded = jsonable_encoder(value)

    def visit(node: Any, key: str | None = None) -> Any:
        if isinstance(node, bool):
            return node
        if isinstance(node, int) and key in _EXACT_INTEGERS:
            return str(node)
        if isinstance(node, dict):
            return {k: visit(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [visit(v) for v in node]
        return node

    return visit(encoded)
