"""HTTP-safe encoding preserving precision of simulation time and versions."""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder


_EXACT_INTEGERS = frozenset(
    {
        "tick", "current_tick", "to_tick", "from_tick", "due_tick", "birth_tick",
        "committed_tick", "committed_through_tick", "received_tick", "valid_from",
        "valid_to", "state_version", "state_version_after", "expected_state_version",
        "sequence", "last_seq", "head_seq", "first_seq", "fork_seq", "next_cursor",
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
