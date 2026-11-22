"""Regression checks for the compact action-proposal JSON schema."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from world_engine.application.actions import (
    _ActionProposalSchemaGenerator,
    _build_action_proposal_schema,
    RulesActionDispatcher,
)
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext


def _titles_removed(value):
    if isinstance(value, dict):
        return {key: _titles_removed(item) for key, item in value.items() if key != "title"}
    if isinstance(value, list):
        return [_titles_removed(item) for item in value]
    return value


def _compact_json_bytes(schema: dict) -> int:
    return len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _title_count(value) -> int:
    if isinstance(value, dict):
        return int("title" in value) + sum(_title_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_title_count(item) for item in value)
    return 0


def test_action_schema_omits_only_inferred_titles_and_reduces_size():
    baseline = _build_action_proposal_schema(ACTION_INTENT_ADAPTER)
    actual = RulesActionDispatcher(
        None, None, None, ACTION_INTENT_ADAPTER, RuleContext,
    ).schema

    baseline_defs = baseline["$defs"]
    actual_defs = actual["$defs"]
    assert len(baseline_defs) == len(actual_defs) == 69
    assert baseline_defs.keys() == actual_defs.keys()

    baseline_actions = baseline["properties"]["action"]["anyOf"][0]
    actual_actions = actual["properties"]["action"]["anyOf"][0]
    assert len(baseline_actions["oneOf"]) == len(actual_actions["oneOf"]) == 66
    assert baseline_actions["discriminator"] == actual_actions["discriminator"]
    assert _titles_removed(baseline) == _titles_removed(actual)

    baseline_size = _compact_json_bytes(baseline)
    actual_size = _compact_json_bytes(actual)
    assert baseline_size == 47_694
    assert actual_size == 42_327
    assert baseline_size - actual_size == 5_367
    assert _title_count(baseline) == 328
    assert _title_count(actual) == 74

    Draft202012Validator.check_schema(baseline)
    Draft202012Validator.check_schema(actual)
    old_validator = Draft202012Validator(baseline)
    new_validator = Draft202012Validator(actual)
    move = {
        "action": {"kind": "move", "destination_id": str(uuid4())},
        "clarification": "",
    }
    trade = {"kind": "trade", "listing_id": str(uuid4()), "quantity": "2"}
    limit = {"max_total_minor": 6, "currency_resource_id": "modern.credit"}
    loan = {
        "kind": "offer_institutional_credit", "lender_organization_id": str(uuid4()),
        "borrower_organization_id": str(uuid4()), "currency_resource_id": "modern.credit",
        "principal_minor": 20, "accepted_through_tick": 50, "maturity_tick": 100,
    }
    reserve = {"kind": "mobilize_treasury_reserve", "organization_id": str(uuid4()),
               "currency_resource_id": "modern.credit", "amount_minor": 4}
    extension = {"kind": "propose_credit_extension", "credit_id": str(uuid4()),
                 "organization_id": str(uuid4()), "expected_revision": 3, "new_maturity_tick": 300,
                 "accept_through_tick": 150}
    examples = (
        (move, True),
        ({"action": trade, "clarification": ""}, True),
        ({"action": {**trade, "price_ceiling": limit}, "clarification": ""}, True),
        ({"action": {**trade, "price_ceiling": {**limit, "max_total_minor": -1}}, "clarification": ""}, False),
        ({"action": loan, "clarification": ""}, True),
        ({"action": {**loan, "principal_minor": 0}, "clarification": ""}, False),
        ({"action": reserve, "clarification": ""}, True),
        ({"action": {**reserve, "amount_minor": 1.5}, "clarification": ""}, False),
        ({"action": extension, "clarification": ""}, True),
        ({"action": {**extension, "expected_revision": 0}, "clarification": ""}, False),
        ({"action": None, "clarification": ""}, True),
        ({"action": {"kind": "move"}, "clarification": ""}, False),
        ({"action": {"kind": "unknown"}, "clarification": ""}, False),
        ({"action": {"kind": "move", "destination_id": str(uuid4()), "extra": 1}, "clarification": ""}, False),
        ({"action": None, "clarification": "", "extra": True}, False),
    )
    for instance, expected in examples:
        old_accepts = not list(old_validator.iter_errors(instance))
        new_accepts = not list(new_validator.iter_errors(instance))
        assert old_accepts is expected
        assert new_accepts is expected

    ACTION_INTENT_ADAPTER.validate_python(move["action"])
    with pytest.raises(ValidationError):
        ACTION_INTENT_ADAPTER.validate_python({"kind": "move"})


def test_action_schema_generator_preserves_explicit_and_class_metadata():
    class MetadataFixture(BaseModel):
        model_config = ConfigDict(
            title="Authored Proposal",
            json_schema_extra={"x-contract": "stable"},
        )

        inferred_name: str
        authored_name: str = Field(
            title="Manual Caption",
            description="kept description",
            examples=["sample"],
            default="fallback",
        )

    ordinary = MetadataFixture.model_json_schema()
    compact = MetadataFixture.model_json_schema(schema_generator=_ActionProposalSchemaGenerator)

    assert ordinary["title"] == compact["title"] == "Authored Proposal"
    assert ordinary["x-contract"] == compact["x-contract"] == "stable"
    assert ordinary["properties"]["inferred_name"]["title"] == "Inferred Name"
    assert "title" not in compact["properties"]["inferred_name"]
    assert compact["properties"]["authored_name"] == {
        "default": "fallback",
        "description": "kept description",
        "examples": ["sample"],
        "title": "Manual Caption",
        "type": "string",
    }
