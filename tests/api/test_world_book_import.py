"""Bounded SillyTavern world-book import and retrieval contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.application.world_book_import import (
    MAX_WORLD_BOOK_CONTENT_CHARS,
    MAX_WORLD_BOOK_JSON_BYTES,
    matching_world_book_entries,
    load_world_book,
)
from world_engine.domain.errors import InvalidEvent


def test_imports_world_info_object_and_preserves_exact_bom_utf8_source() -> None:
    payload = {
        "entries": {
            "17": {
                "uid": 17,
                "comment": "Copper Bell",
                "key": ["Copperbell", "Old Quay"],
                "content": "Pilots seal harbor notices with blue wax.",
                "constant": False,
                "disable": False,
            },
            "23": {
                "uid": 23,
                "name": "Moon calendar",
                "keys": [],
                "content": "The tide turns at the second moon.",
                "constant": True,
            },
        }
    }
    raw = "\ufeff  " + json.dumps(payload, ensure_ascii=False, indent=2) + "\n  "

    entries, skipped, source = load_world_book(raw)

    assert skipped == []
    assert entries == [
        {
            "source_key": "17",
            "title": "Copper Bell",
            "content": "Pilots seal harbor notices with blue wax.",
            "keywords": ["Copperbell", "Old Quay"],
            "always_on": False,
        },
        {
            "source_key": "23",
            "title": "Moon calendar",
            "content": "The tide turns at the second moon.",
            "keywords": [],
            "always_on": True,
        },
    ]
    source_bytes = raw.encode("utf-8")
    assert source == {
        "original_json_base64": base64.b64encode(source_bytes).decode("ascii"),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
    }
    assert base64.b64decode(source["original_json_base64"]) == source_bytes


def test_imports_character_book_list_using_id_or_stable_list_index() -> None:
    payload = {
        "entries": [
            {
                "id": 51,
                "name": "Amber Road",
                "keys": ["Amber Road"],
                "content": "A public road crosses the salt flats.",
                "enabled": True,
            },
            {
                "title": "River ferry",
                "keys": ["ferry"],
                "content": "A ferry runs from dawn until dusk.",
            },
        ]
    }

    entries, skipped, _ = load_world_book(json.dumps(payload))

    assert skipped == []
    assert [entry["source_key"] for entry in entries] == ["51", "1"]
    assert [entry["title"] for entry in entries] == ["Amber Road", "River ferry"]


def test_blank_comments_fall_back_to_keyword_or_source_key_title() -> None:
    payload = {
        "entries": {
            "0": {"comment": "", "key": ["Salt Lantern"], "content": "A blue light marks the channel."},
            "always": {"comment": "", "constant": True, "content": "The old clock marks the flood."},
        }
    }

    entries, skipped, _ = load_world_book(json.dumps(payload, ensure_ascii=False))

    assert skipped == []
    assert [entry["title"] for entry in entries] == ["Salt Lantern", "条目 always"]


def test_disabled_invalid_and_empty_entries_are_reported_without_context_fields() -> None:
    payload = {
        "entries": {
            "0": {"disable": True, "comment": "Retired", "content": "Do not use."},
            "1": {"enabled": False, "name": "Old", "content": "Not active."},
            "2": {"key": ["harbor"], "content": "   "},
            "3": {"key": ["harbor"], "content": "Current guidance.", "comment": "Current"},
            "4": "not an entry object",
        },
        "extensions": {"regex": "/.*unsafe.*/", "recursion": True},
    }

    entries, skipped, _ = load_world_book(json.dumps(payload))

    assert entries == [
        {
            "source_key": "3",
            "title": "Current",
            "content": "Current guidance.",
            "keywords": ["harbor"],
            "always_on": False,
        }
    ]
    assert [item["source_key"] for item in skipped] == ["0", "1", "2", "4"]
    assert skipped[0]["reason"] == "disabled"
    assert all(set(item) == {"source_key", "reason"} for item in skipped)


def test_inactive_sillytavern_modifiers_are_archived_but_not_interpreted() -> None:
    payload = {
        "entries": {
            "0": {
                "uid": 0,
                "comment": "Regex-looking keyword",
                "key": ["/sea.*/i"],
                "content": "Literal key only.",
                "keysecondary": ["secret"],
                "selectiveLogic": 3,
                "use_regex": True,
                "extensions": {"recursive": True, "script": "host_access()"},
            }
        },
        "extensions": {"custom": {"opaque": "preserved"}},
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + " "

    entries, _, source = load_world_book(raw)

    assert entries[0]["keywords"] == ["/sea.*/i"]
    assert set(entries[0]) == {"source_key", "title", "content", "keywords", "always_on"}
    restored = json.loads(base64.b64decode(source["original_json_base64"]))
    assert restored == payload
    assert restored["entries"]["0"]["extensions"] == {"recursive": True, "script": "host_access()"}
    assert matching_world_book_entries(
        [{**entries[0], "entry_id": "book-entry", "visibility": "public"}],
        "The sea has monsters",
    ) == []
    assert matching_world_book_entries(
        [{**entries[0], "entry_id": "book-entry", "visibility": "public"}],
        "It says /SEA.*/I exactly",
    ) == [{"entry_id": "book-entry", "title": "Regex-looking keyword", "content": "Literal key only."}]


@pytest.mark.parametrize(
    "raw",
    [
        '{"entries":{"0":{"key":["x"],"content":"one","content":"two"}}}',
        '{"entries":{"0":{"key":["x"],"nested":{"x":1,"x":2},"content":"one"}}}',
        '{"entries":{"0":{"key":["x"],"content":"one"},"0":{"key":["y"],"content":"two"}}}',
    ],
)
def test_duplicate_json_fields_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidEvent):
        load_world_book(raw)


def test_duplicate_entry_ids_and_character_book_ids_are_rejected() -> None:
    object_book = {
        "entries": {
            "10": {"uid": 10, "key": ["a"], "content": "A"},
            "11": {"uid": 10, "key": ["b"], "content": "B"},
        }
    }
    list_book = {
        "entries": [
            {"id": "same", "keys": ["a"], "content": "A"},
            {"id": "same", "keys": ["b"], "content": "B"},
        ]
    }
    for payload in (object_book, list_book):
        with pytest.raises(InvalidEvent):
            load_world_book(json.dumps(payload))


def test_no_eligible_entries_and_oversized_source_are_rejected() -> None:
    with pytest.raises(InvalidEvent, match="between 1 and 128"):
        load_world_book('{"entries":{"0":{"disable":true,"content":"gone"}}}')
    with pytest.raises(InvalidEvent, match="upload limit"):
        load_world_book(" " * (MAX_WORLD_BOOK_JSON_BYTES + 1))


def test_overlong_fields_skip_entry_and_report_source_key() -> None:
    payload = {
        "entries": {
            "title": {"key": ["x"], "content": "valid", "title": "t" * 121},
            "content": {"key": ["x"], "content": "c" * (MAX_WORLD_BOOK_CONTENT_CHARS + 1)},
            "keyword": {"key": ["k" * 81], "content": "valid"},
            "keyword-count": {"key": ["k"] * 33, "content": "valid"},
            "0" * 81: {"constant": True, "content": "valid", "comment": "valid"},
            "good": {"key": ["x"], "content": "valid", "comment": "good"},
        }
    }

    entries, skipped, _ = load_world_book(json.dumps(payload))

    assert [entry["source_key"] for entry in entries] == ["good"]
    assert [entry["source_key"] for entry in skipped] == ["title", "content", "keyword", "keyword-count", "0" * 80]
    assert "source key exceeds" in skipped[-1]["reason"]


@pytest.mark.parametrize(
    ("bad_entry", "source_key", "reason"),
    [
        ('"bad\\ud800":{"key":["okay"],"content":"okay"}', "bad\\ud800", "source key is not valid UTF-8 text"),
        ('"bad":{"comment":"\\ud800","key":["okay"],"content":"okay"}', "bad", "entry title is not valid UTF-8 text"),
        ('"bad":{"key":["okay"],"content":"\\ud800"}', "bad", "entry content is not valid UTF-8 text"),
        ('"bad":{"key":["\\ud800"],"content":"okay"}', "bad", "entry keyword is not valid UTF-8 text"),
    ],
)
def test_malformed_escaped_surrogate_is_skipped_before_projection(
    bad_entry: str,
    source_key: str,
    reason: str,
) -> None:
    raw = '{"entries":{' + bad_entry + ',"good":{"key":["key"],"content":"content"}}}'

    entries, skipped, _ = load_world_book(raw)

    assert [entry["source_key"] for entry in entries] == ["good"]
    assert skipped == [{"source_key": source_key, "reason": reason}]


def test_too_many_eligible_entries_are_rejected() -> None:
    payload = {
        "entries": {
            str(index): {"key": [f"key-{index}"], "content": "valid"}
            for index in range(129)
        }
    }

    with pytest.raises(InvalidEvent, match="between 1 and 128"):
        load_world_book(json.dumps(payload))


def test_matching_is_public_literal_casefolded_stable_and_bounded() -> None:
    public = [
        {
            "entry_id": f"public-{index}",
            "title": f"Harbor {index}",
            "content": "x" * 1_000,
            "keywords": ["COPPERBELL"],
            "always_on": False,
            "visibility": "public",
        }
        for index in range(5)
    ]
    private = {
        "entry_id": "private",
        "title": "Private Harbor",
        "content": "must not leak",
        "keywords": ["copperbell"],
        "always_on": True,
        "visibility": "author_only",
    }
    missing_visibility = {
        "entry_id": "unclassified",
        "title": "Unclassified",
        "content": "must also stay private",
        "keywords": ["copperbell"],
        "always_on": True,
    }
    role_only_public = {
        "entry_id": "role-only",
        "title": "Role only",
        "content": "role must not grant publication",
        "keywords": ["copperbell"],
        "always_on": True,
        "role": "public",
    }
    privacy_alias_public = {
        "entry_id": "privacy-alias",
        "title": "Privacy alias",
        "content": "privacy alias must not grant publication",
        "keywords": ["copperbell"],
        "always_on": True,
        "privacy": "public",
    }
    subject = SimpleNamespace(
        entry_id="subject",
        title="Subject scope",
        content="should not be public",
        keywords=("copperbell",),
        always_on=False,
        visibility=SimpleNamespace(policy="subject_ids"),
    )

    result = matching_world_book_entries(
        [*public, private, missing_visibility, role_only_public, privacy_alias_public, subject],
        "A Copperbell pilot",
    )

    assert [entry["entry_id"] for entry in result] == [f"public-{index}" for index in range(4)]
    assert sum(len(entry["content"]) for entry in result) <= 4_000
    assert all(set(entry) == {"entry_id", "title", "content"} for entry in result)
    for denied in (private, missing_visibility, role_only_public, privacy_alias_public, subject):
        assert matching_world_book_entries([denied], "Copperbell") == []
    typed_public = SimpleNamespace(
        entry_id="typed-public",
        title="Typed public",
        content="visible",
        keywords=("copperbell",),
        always_on=False,
        visibility="public",
    )
    assert matching_world_book_entries([typed_public], "Copperbell") == [
        {"entry_id": "typed-public", "title": "Typed public", "content": "visible"}
    ]


def test_matching_always_on_and_skips_one_oversized_total_without_reordering() -> None:
    entries = [
        {"entry_id": "large", "title": "Large", "content": "a" * 3_000, "keywords": [], "always_on": True, "visibility": "public"},
        {"entry_id": "over-budget", "title": "Too large", "content": "b" * 1_500, "keywords": [], "always_on": True, "visibility": "public"},
        {"entry_id": "fits-after-skip", "title": "Fits", "content": "c" * 500, "keywords": [], "always_on": True, "visibility": "public"},
    ]

    result = matching_world_book_entries(entries, "")

    assert [entry["entry_id"] for entry in result] == ["large", "fits-after-skip"]
    assert sum(len(entry["content"]) for entry in result) == 3_500
