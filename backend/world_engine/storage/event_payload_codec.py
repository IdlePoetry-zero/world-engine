"""Bounded, lossless storage codec for selected large event payloads.

The event log remains authoritative JSON. This module only describes a compact
representation for explicitly allowlisted event types; it has no ORM or
migration dependencies so storage callers can choose where to persist the envelope.
"""

from __future__ import annotations

import hashlib
import json
import math
import zlib
from dataclasses import dataclass
from typing import Mapping


EVENT_PAYLOAD_CODEC_VERSION = 1
CODEC_EVENT_TYPES = frozenset({
    "core.background_routine_advanced.v1",
    "core.background_needs_advanced.v1",
    "core.renewable_resource_harvested.v1",
})
MIN_COMPRESS_RAW_BYTES = 64 * 1024
MIN_COMPRESS_SAVING_BYTES = 1024
MIN_COMPRESS_SAVING_RATIO = 0.20
MAX_RAW_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_COMPRESSED_PAYLOAD_BYTES = MAX_RAW_PAYLOAD_BYTES
MAX_JSON_DEPTH = 128


class EventPayloadTooLarge(ValueError):
    """A payload exceeds the codec's bounded raw-size limit."""


@dataclass(frozen=True, slots=True)
class CompressedEventPayload:
    """Typed side-table envelope for one compressed canonical JSON payload."""

    codec_version: int
    raw_byte_count: int
    sha256: str
    compressed_bytes: bytes


@dataclass(frozen=True, slots=True)
class EncodedEventPayload:
    """Exactly one of inline JSON or a compressed side-table envelope."""

    inline_payload: dict[str, object] | None
    compressed_payload: CompressedEventPayload | None

    @property
    def is_compressed(self) -> bool:
        return self.compressed_payload is not None


def encode_event_payload(
    event_type: str,
    payload: Mapping[str, object],
) -> EncodedEventPayload:
    """Canonicalize a JSON object and compress it only when worthwhile.

    Compressed storage is restricted to ``CODEC_EVENT_TYPES``. For all other
    types, small payloads, and payloads with less than 20% savings, the
    canonical JSON object is returned inline.
    """

    raw = _canonical_json_bytes(payload)
    if len(raw) > MAX_RAW_PAYLOAD_BYTES:
        raise EventPayloadTooLarge("event payload exceeds the configured raw byte limit")
    normalized = _strict_json_object(raw)

    if event_type not in CODEC_EVENT_TYPES or len(raw) < MIN_COMPRESS_RAW_BYTES:
        return EncodedEventPayload(inline_payload=normalized, compressed_payload=None)

    compressed = zlib.compress(raw, level=6)
    required_saving = max(
        MIN_COMPRESS_SAVING_BYTES,
        math.ceil(len(raw) * MIN_COMPRESS_SAVING_RATIO),
    )
    if len(compressed) > len(raw) - required_saving:
        return EncodedEventPayload(inline_payload=normalized, compressed_payload=None)

    return EncodedEventPayload(
        inline_payload=None,
        compressed_payload=CompressedEventPayload(
            codec_version=EVENT_PAYLOAD_CODEC_VERSION,
            raw_byte_count=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            compressed_bytes=compressed,
        ),
    )


def decode_event_payload(
    event_type: str,
    *,
    inline_payload: Mapping[str, object] | None = None,
    compressed_payload: CompressedEventPayload | None = None,
) -> dict[str, object]:
    """Decode either inline JSON or a verified compressed side-table envelope."""

    if (inline_payload is None) == (compressed_payload is None):
        raise ValueError("event payload storage must contain exactly one inline or compressed representation")

    if inline_payload is not None:
        raw = _canonical_json_bytes(inline_payload)
        if len(raw) > MAX_RAW_PAYLOAD_BYTES:
            raise ValueError("event payload exceeds the configured raw byte limit")
        return _strict_json_object(raw)

    if event_type not in CODEC_EVENT_TYPES:
        raise ValueError("compressed storage is not registered for this event type")
    assert compressed_payload is not None
    if not isinstance(compressed_payload, CompressedEventPayload):
        raise ValueError("compressed event payload envelope has an unsupported type")
    _validate_envelope(compressed_payload)

    try:
        decoder = zlib.decompressobj(wbits=zlib.MAX_WBITS)
        raw = decoder.decompress(
            compressed_payload.compressed_bytes,
            MAX_RAW_PAYLOAD_BYTES + 1,
        )
    except zlib.error as exc:
        raise ValueError("compressed event payload is corrupt") from exc

    if len(raw) > MAX_RAW_PAYLOAD_BYTES or decoder.unconsumed_tail:
        raise ValueError("decompressed event payload exceeds the configured raw byte limit")
    if not decoder.eof:
        raise ValueError("compressed event payload is truncated")
    if decoder.unused_data:
        raise ValueError("compressed event payload has trailing or concatenated data")

    try:
        raw += decoder.flush()
    except zlib.error as exc:
        raise ValueError("compressed event payload is corrupt") from exc
    if len(raw) > MAX_RAW_PAYLOAD_BYTES:
        raise ValueError("decompressed event payload exceeds the configured raw byte limit")
    if len(raw) != compressed_payload.raw_byte_count:
        raise ValueError("compressed event payload raw byte count does not match its envelope")
    if hashlib.sha256(raw).hexdigest() != compressed_payload.sha256:
        raise ValueError("compressed event payload digest does not match its envelope")

    payload = _strict_json_object(raw)
    if _canonical_json_bytes(payload) != raw:
        raise ValueError("compressed event payload is not canonical JSON")
    return payload


def _validate_envelope(envelope: CompressedEventPayload) -> None:
    if type(envelope.codec_version) is not int or envelope.codec_version != EVENT_PAYLOAD_CODEC_VERSION:
        raise ValueError("compressed event payload codec version is unsupported")
    if type(envelope.raw_byte_count) is not int or not 0 < envelope.raw_byte_count <= MAX_RAW_PAYLOAD_BYTES:
        raise ValueError("compressed event payload raw byte count is outside configured limits")
    if (
        not isinstance(envelope.sha256, str)
        or len(envelope.sha256) != 64
        or any(character not in "0123456789abcdef" for character in envelope.sha256)
    ):
        raise ValueError("compressed event payload digest is invalid")
    if (
        not isinstance(envelope.compressed_bytes, bytes)
        or not envelope.compressed_bytes
        or len(envelope.compressed_bytes) > MAX_COMPRESSED_PAYLOAD_BYTES
    ):
        raise ValueError("compressed event payload bytes are outside configured limits")


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    if not isinstance(payload, Mapping):
        raise ValueError("event payload must be a JSON object")
    _validate_json_value(payload, depth=0, active_ids=set())
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError("event payload is not strict JSON") from exc


def _validate_json_value(value: object, *, depth: int, active_ids: set[int]) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("event payload exceeds the configured JSON nesting limit")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("event payload numbers must be finite")
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("event payload strings must contain valid Unicode scalar values") from exc
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("event payload cannot contain a recursive object")
        active_ids.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("event payload object keys must be strings")
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise ValueError("event payload object keys must contain valid Unicode scalar values") from exc
                _validate_json_value(item, depth=depth + 1, active_ids=active_ids)
        finally:
            active_ids.remove(identity)
        return
    if isinstance(value, list):
        identity = id(value)
        if identity in active_ids:
            raise ValueError("event payload cannot contain a recursive array")
        active_ids.add(identity)
        try:
            for item in value:
                _validate_json_value(item, depth=depth + 1, active_ids=active_ids)
        finally:
            active_ids.remove(identity)
        return
    raise ValueError("event payload contains a value outside strict JSON types")


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("event payload JSON has a duplicate object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"event payload JSON contains unsupported numeric constant {value}")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("event payload bytes do not contain strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("event payload JSON root must be an object")
    _validate_json_value(value, depth=0, active_ids=set())
    return value
