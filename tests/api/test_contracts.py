import pytest
from pydantic import ValidationError

from world_engine.api.contracts import CreateWorldRequest, MessageRequest
from world_engine.api.serialization import wire


def test_http_cannot_override_session_identity():
    with pytest.raises(ValidationError):
        CreateWorldRequest(pack_id="test", title="world", player_name="p", tenant_id="forged")


def test_create_world_npc_autonomy_mode_is_opt_in_and_versioned():
    legacy = CreateWorldRequest(pack_id="test", title="world", player_name="p")
    assert legacy.npc_autonomy_mode == "off"
    assert CreateWorldRequest(
        pack_id="test", title="world", player_name="p", npc_autonomy_mode="limited_v1",
    ).npc_autonomy_mode == "limited_v1"
    with pytest.raises(ValidationError):
        CreateWorldRequest(pack_id="test", title="world", player_name="p", npc_autonomy_mode="full")


def test_large_world_time_is_encoded_as_decimal_string():
    value = 2**63 - 1
    assert wire({"current_tick": value, "nested": {"state_version": value}, "health": 80, "enabled": True}) == {
        "current_tick": str(value), "nested": {"state_version": str(value)}, "health": 80, "enabled": True
    }


def test_message_version_must_not_arrive_as_rounded_json_number():
    with pytest.raises(ValidationError):
        MessageRequest(
            request_id="00000000-0000-0000-0000-000000000001",
            world_id="00000000-0000-0000-0000-000000000001",
            branch_id="00000000-0000-0000-0000-000000000001",
            session_id="00000000-0000-0000-0000-000000000001",
            actor_id="00000000-0000-0000-0000-000000000001",
            expected_state_version=2**63-1,
            text="我只是询问当前情况",
        )
