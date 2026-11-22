import base64
import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import EVENT_TYPES, event_payload, parse_event
from world_engine.domain.models import WorldState
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.domain.world_books import WorldBookEntry, WorldBookImportedEvent


def _entry(*, entry_id=None, source_key="lore:moon", visibility="public") -> WorldBookEntry:
    return WorldBookEntry(
        entry_id=entry_id or uuid4(),
        source_key=source_key,
        title="Moon rites",
        content="The moon marks the start of the winter observance.",
        keywords=("moon", "winter"),
        always_on=False,
        visibility=visibility,
    )


def _event(*, entries=None, source_bytes=None) -> WorldBookImportedEvent:
    raw = source_bytes if source_bytes is not None else b'\xef\xbb\xbf{"entries":[]}  \n'
    return WorldBookImportedEvent(
        title="Village lore",
        entries=tuple(entries if entries is not None else (_entry(),)),
        original_json_base64=base64.b64encode(raw).decode("ascii"),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_world_book_event_is_registered_private_and_replays_to_verified_state_hash():
    event = _event()
    assert EVENT_TYPES[event.type] is WorldBookImportedEvent

    raw_event = {
        "type": event.type,
        "event_id": str(event.event_id),
        **event_payload(event),
        "visibility": {"policy": "author_only"},
    }
    parsed = parse_event(raw_event)
    assert isinstance(parsed, WorldBookImportedEvent)
    assert parsed.original_json_base64 == event.original_json_base64

    empty = WorldState()
    stored = apply_event(empty, raw_event)
    replayed = apply_event(WorldState.model_validate(state_json(empty)), event.model_dump(mode="json"))
    assert stored.world_book_entries == list(event.entries)
    assert stored == replayed
    assert state_hash(stored) == state_hash(replayed)
    assert state_json(stored)["world_book_entries"] == [entry.model_dump(mode="json") for entry in event.entries]
    assert "world_book_entries" not in state_json(empty)
    assert state_hash(empty) == state_hash(WorldState.model_validate(empty.model_dump(mode="json")))


def test_world_book_event_rejects_bad_source_visibility_and_duplicate_ids():
    event = _event()
    payload = event.model_dump(mode="json")

    with pytest.raises(ValidationError, match="author_only"):
        WorldBookImportedEvent.model_validate({**payload, "visibility": {"policy": "public"}})
    with pytest.raises(ValidationError, match="SHA-256"):
        WorldBookImportedEvent.model_validate({**payload, "sha256": "0" * 64})

    with pytest.raises(ValidationError, match="canonical encoding"):
        WorldBookImportedEvent.model_validate({**payload, "original_json_base64": "Zh=="})
    invalid_utf8 = b"\xff"
    with pytest.raises(ValidationError, match="valid UTF-8"):
        WorldBookImportedEvent.model_validate({
            **payload,
            "original_json_base64": base64.b64encode(invalid_utf8).decode("ascii"),
            "sha256": hashlib.sha256(invalid_utf8).hexdigest(),
        })
    oversized_source = b" " * (256 * 1024 + 1)
    with pytest.raises(ValidationError, match="1 to 256 KiB"):
        WorldBookImportedEvent.model_validate({
            **payload,
            "original_json_base64": base64.b64encode(oversized_source).decode("ascii"),
            "sha256": hashlib.sha256(oversized_source).hexdigest(),
        })

    repeated = _entry()
    with pytest.raises(ValidationError, match="unique within an import"):
        WorldBookImportedEvent.model_validate({**payload, "entries": [repeated.model_dump(mode="json")] * 2})


def test_world_book_entries_are_globally_unique_and_import_appends_only_distinct_entries():
    entry = _entry()
    with pytest.raises(ValidationError, match="unique within a world"):
        WorldState(world_book_entries=[entry, entry])

    state = apply_event(WorldState(), _event(entries=[entry]))
    with pytest.raises(InvalidEvent, match="previously unused IDs"):
        apply_event(state, _event(entries=[entry]))

    too_many = [_entry() for _ in range(128)]
    full_state = WorldState(world_book_entries=[_entry() for _ in range(512)])
    with pytest.raises(InvalidEvent, match="more than 512"):
        apply_event(full_state, _event(entries=too_many))
