from __future__ import annotations

import hashlib
import json
import random
import zlib
from dataclasses import replace

import pytest

import world_engine.storage.event_payload_codec as codec
from world_engine.storage.event_payload_codec import (
    CODEC_EVENT_TYPES,
    EVENT_PAYLOAD_CODEC_VERSION,
    CompressedEventPayload,
    decode_event_payload,
    encode_event_payload,
)


def _large_routine_payload() -> dict[str, object]:
    return {
        "actor_id": "00000000-0000-0000-0000-000000000001",
        "from_tick": 0,
        "to_tick": 2_592_000,
        "expected_agents": [
            {
                "entity_id": f"00000000-0000-0000-0000-{index:012d}",
                "expected_needs_revision": 1,
                "food": {
                    "action_count": 12,
                    "resource_id": "core.food",
                    "consumed_quantity": "3",
                    "consumption_lots": [
                        {"account_id": "00000000-0000-0000-0000-000000000002", "quantity": "3"}
                    ],
                },
                "water": None,
            }
            for index in range(1_500)
        ],
    }


def _envelope(raw: bytes, *, raw_byte_count: int | None = None, sha256: str | None = None):
    return CompressedEventPayload(
        codec_version=EVENT_PAYLOAD_CODEC_VERSION,
        raw_byte_count=len(raw) if raw_byte_count is None else raw_byte_count,
        sha256=hashlib.sha256(raw).hexdigest() if sha256 is None else sha256,
        compressed_bytes=zlib.compress(raw, level=6),
    )


def test_allowlisted_large_payload_roundtrips_exactly_and_canonically():
    payload = _large_routine_payload()
    encoded = encode_event_payload("core.background_routine_advanced.v1", payload)

    assert encoded.is_compressed
    assert encoded.inline_payload is None
    envelope = encoded.compressed_payload
    assert envelope is not None
    assert envelope.codec_version == 1

    decoded = decode_event_payload(
        "core.background_routine_advanced.v1",
        compressed_payload=envelope,
    )
    assert decoded == payload

    reordered = dict(reversed(list(payload.items())))
    encoded_again = encode_event_payload("core.background_routine_advanced.v1", reordered)
    assert encoded_again.compressed_payload == envelope


def test_only_allowlisted_large_payloads_with_substantial_savings_are_compressed():
    small = encode_event_payload(
        "core.background_routine_advanced.v1",
        {"from_tick": 0, "to_tick": 1},
    )
    assert not small.is_compressed
    assert small.inline_payload == {"from_tick": 0, "to_tick": 1}

    non_allowlisted = encode_event_payload("core.relationship_promise_recorded.v1", _large_routine_payload())
    assert not non_allowlisted.is_compressed

    # A large random printable string is valid JSON but deflates by less than
    # the configured 20% threshold, so it stays inline.
    alphabet = "".join(chr(value) for value in range(33, 127))
    text = "".join(random.Random(42).choices(alphabet, k=70_000))
    poor_savings = encode_event_payload(
        "core.renewable_resource_harvested.v1",
        {"description": text},
    )
    assert not poor_savings.is_compressed
    assert poor_savings.inline_payload == {"description": text}


def test_inline_payload_is_normalized_and_requires_exactly_one_representation():
    encoded = encode_event_payload("core.promise_released.v1", {"z": [1, True], "a": "值"})
    assert decode_event_payload("core.promise_released.v1", inline_payload=encoded.inline_payload) == {
        "a": "值", "z": [1, True],
    }

    with pytest.raises(ValueError, match="exactly one"):
        decode_event_payload("core.promise_released.v1")
    with pytest.raises(ValueError, match="exactly one"):
        decode_event_payload(
            "core.promise_released.v1",
            inline_payload={"a": 1},
            compressed_payload=CompressedEventPayload(1, 2, "0" * 64, b"xx"),
        )


@pytest.mark.parametrize(
    ("envelope_update", "message"),
    [
        ({"codec_version": 99}, "version is unsupported"),
        ({"raw_byte_count": 1}, "byte count does not match"),
        ({"sha256": "0" * 64}, "digest does not match"),
        ({"compressed_bytes": b"not a zlib stream"}, "corrupt"),
    ],
)
def test_compressed_envelope_integrity_is_verified(envelope_update, message):
    encoded = encode_event_payload("core.background_routine_advanced.v1", _large_routine_payload())
    envelope = encoded.compressed_payload
    assert envelope is not None
    tampered = replace(envelope, **envelope_update)
    with pytest.raises(ValueError, match=message):
        decode_event_payload("core.background_routine_advanced.v1", compressed_payload=tampered)


def test_truncated_concatenated_and_trailing_zlib_streams_are_rejected():
    encoded = encode_event_payload("core.background_routine_advanced.v1", _large_routine_payload())
    envelope = encoded.compressed_payload
    assert envelope is not None

    with pytest.raises(ValueError, match="truncated"):
        decode_event_payload(
            "core.background_routine_advanced.v1",
            compressed_payload=replace(envelope, compressed_bytes=envelope.compressed_bytes[:-1]),
        )
    with pytest.raises(ValueError, match="trailing or concatenated"):
        decode_event_payload(
            "core.background_routine_advanced.v1",
            compressed_payload=replace(envelope, compressed_bytes=envelope.compressed_bytes + b"tail"),
        )
    with pytest.raises(ValueError, match="trailing or concatenated"):
        decode_event_payload(
            "core.background_routine_advanced.v1",
            compressed_payload=replace(
                envelope,
                compressed_bytes=envelope.compressed_bytes + zlib.compress(b"{}"),
            ),
        )


def test_decompression_limit_and_strict_json_object_are_enforced(monkeypatch):
    monkeypatch.setattr(codec, "MAX_RAW_PAYLOAD_BYTES", 128)
    oversized_raw = json.dumps({"value": "x" * 500}, separators=(",", ":")).encode()
    oversized = _envelope(oversized_raw, raw_byte_count=128)
    with pytest.raises(ValueError, match="exceeds the configured raw byte limit"):
        decode_event_payload("core.background_routine_advanced.v1", compressed_payload=oversized)

    monkeypatch.setattr(codec, "MAX_RAW_PAYLOAD_BYTES", 64 * 1024 * 1024)
    for raw in (
        b"[1,2,3]",
        b'{"key":1,"key":2}',
        b'{"value":NaN}',
        b'{"z":1,"a":2}',
        b'{"text":"\\ud800"}',
    ):
        with pytest.raises(ValueError):
            decode_event_payload(
                "core.background_routine_advanced.v1",
                compressed_payload=_envelope(raw),
            )


def test_compression_is_forbidden_for_unregistered_event_types_and_input_is_strict():
    assert CODEC_EVENT_TYPES == frozenset({
        "core.background_routine_advanced.v1",
        "core.background_needs_advanced.v1",
        "core.renewable_resource_harvested.v1",
    })
    encoded = encode_event_payload("core.background_routine_advanced.v1", _large_routine_payload())
    assert encoded.compressed_payload is not None
    with pytest.raises(ValueError, match="not registered"):
        decode_event_payload("core.promise_released.v1", compressed_payload=encoded.compressed_payload)

    for invalid in ({"value": float("nan")}, {1: "not a string key"}, {"bytes": b"raw"}):
        with pytest.raises(ValueError):
            encode_event_payload("core.background_routine_advanced.v1", invalid)
