"""Fixed, replayable policies for the narrow improvised-check rules."""

from __future__ import annotations

import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID


IMPROVISED_EXAMINE_POLICY = "improvised.examine.v1"
IMPROVISED_EXAMINE_DURATION = 60
IMPROVISED_EXAMINE_ROLL_LABEL = "improvised.examine"
HISTORICAL_EVIDENCE_OBSERVATION_SUMMARY = (
    "An authored historical record was inspected under the registered examination policy."
)


def improvised_examine_chance(skill_level: Decimal) -> int:
    """Return the server-fixed, bounded success chance for simple examination."""
    if not skill_level.is_finite() or skill_level < 0 or skill_level > 100:
        raise ValueError("improvised examination skill must be in [0, 100]")
    return min(95, 50 + int(skill_level // Decimal("2")))


def improvised_roll_digest(
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    label: str = IMPROVISED_EXAMINE_ROLL_LABEL,
) -> str:
    material = ":".join(
        (str(world_id), str(branch_id), str(command_id), ruleset_version, label)
    ).encode("utf-8")
    return sha256(material).hexdigest()


def source_text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def historical_evidence_digest(value: object) -> str:
    """Hash the canonical typed payload of one authored historical artifact."""
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise ValueError("historical evidence must be a validated component model")
    encoded = json.dumps(
        model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(b"world-engine:historical-evidence:v1\0" + encoded).hexdigest()
