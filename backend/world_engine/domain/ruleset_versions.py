"""Ruleset versions and policies shared by action evaluation and event validation."""

from .json_types import JsonObject

LEGACY_RULESET_VERSION = "1.0.0"
MAGIC_GATE_RULESET_VERSION = "1.1.0"
SUPPORTED_RULESET_VERSIONS = frozenset({LEGACY_RULESET_VERSION, MAGIC_GATE_RULESET_VERSION})

CULTIVATION_DISABLED_CODE = "cultivation_disabled_by_world_rules"
CULTIVATION_DISABLED_MESSAGE = "This world's rules disable cultivation breakthroughs."


def cultivation_disabled_by_world_rules(ruleset_version: str, metadata: JsonObject) -> bool:
    """Apply the opt-in rule only when the world explicitly disables magic."""
    if ruleset_version != MAGIC_GATE_RULESET_VERSION:
        return False
    content_rules = metadata.get("content_rules")
    if not isinstance(content_rules, dict):
        return False
    magic = content_rules.get("magic")
    return isinstance(magic, dict) and magic.get("enabled") is False
