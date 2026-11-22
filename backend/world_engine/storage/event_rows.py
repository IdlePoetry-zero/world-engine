"""Persist full event payloads without changing the logical event contract."""

from uuid import UUID

from sqlalchemy.orm import Session, object_session

from world_engine.domain.errors import Conflict

from .event_payload_codec import (
    CODEC_EVENT_TYPES, CompressedEventPayload, EventPayloadTooLarge, decode_event_payload,
    encode_event_payload,
)
from .models import EventPayloadRow, EventRow


_PAYLOAD_REFERENCE = "$event_payload"
_UNLOADED_SIDECAR = object()


def prepare_event_payload(
    event_id: UUID, event_type: str, payload: dict[str, object],
) -> tuple[dict[str, object], EventPayloadRow | None]:
    compressed = None
    if event_type in CODEC_EVENT_TYPES:
        try:
            compressed = encode_event_payload(event_type, payload).compressed_payload
        except EventPayloadTooLarge:
            if event_type != "core.background_needs_advanced.v1":
                raise
            # This event type was historically stored inline. Retain that path
            # for payloads that exceed the sidecar codec's bounded size.
            return payload, None
    if compressed is None:
        return payload, None
    reference = {_PAYLOAD_REFERENCE: compressed.codec_version, "sha256": compressed.sha256}
    return reference, EventPayloadRow(
        event_id=event_id, compressed_bytes=compressed.compressed_bytes,
        raw_byte_count=compressed.raw_byte_count, codec_version=compressed.codec_version, sha256=compressed.sha256,
    )


def append_event_row(session: Session, row: EventRow) -> None:
    row.payload_json, compressed = prepare_event_payload(row.event_id, row.event_type, row.payload_json)
    session.add(row)
    if compressed is not None:
        session.flush()
        session.add(compressed)


def read_event_payload(
    row: EventRow,
    *,
    prefetched_sidecar: EventPayloadRow | None | object = _UNLOADED_SIDECAR,
) -> dict[str, object]:
    payload = row.payload_json
    if _PAYLOAD_REFERENCE not in payload:
        return payload
    if prefetched_sidecar is _UNLOADED_SIDECAR:
        session = object_session(row)
        stored = session.get(EventPayloadRow, row.event_id) if session is not None else None
    else:
        stored = prefetched_sidecar
    if not isinstance(stored, EventPayloadRow) or payload != {
        _PAYLOAD_REFERENCE: stored.codec_version,
        "sha256": stored.sha256,
    }:
        raise Conflict("Stored event payload reference does not match its compressed record.")
    try:
        return decode_event_payload(row.event_type, compressed_payload=CompressedEventPayload(
            codec_version=stored.codec_version, raw_byte_count=stored.raw_byte_count,
            sha256=stored.sha256, compressed_bytes=stored.compressed_bytes,
        ))
    except ValueError as exc:
        raise Conflict("Stored compressed event payload failed verification.") from exc
