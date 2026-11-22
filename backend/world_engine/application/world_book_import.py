"""Bounded SillyTavern world-info and character-book import helpers."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from world_engine.domain.errors import InvalidEvent


MAX_WORLD_BOOK_JSON_BYTES = 256 * 1024
MAX_WORLD_BOOK_ENTRIES = 128
MAX_WORLD_BOOK_TITLE_CHARS = 120
MAX_WORLD_BOOK_CONTENT_CHARS = 2_000
MAX_WORLD_BOOK_KEYWORDS = 32
MAX_WORLD_BOOK_KEYWORD_CHARS = 80
MAX_WORLD_BOOK_SOURCE_KEY_CHARS = 80
MAX_MATCHED_WORLD_BOOK_ENTRIES = 4
MAX_MATCHED_WORLD_BOOK_CONTENT_CHARS = 4_000


def load_world_book(book_json: str) -> tuple[list[dict], list[dict], dict]:
    """Validate a bounded world book while preserving its exact original source."""
    try:
        source_bytes = book_json.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise InvalidEvent("World-book source must be UTF-8 text.") from exc
    if len(source_bytes) > MAX_WORLD_BOOK_JSON_BYTES:
        raise InvalidEvent("World-book JSON exceeds the upload limit.")
    try:
        book = json.loads(
            book_json.removeprefix("\ufeff"),
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InvalidEvent("World book must contain valid JSON with unique field names.") from exc
    if not isinstance(book, dict):
        raise InvalidEvent("World book must be a JSON object.")
    raw_entries = book.get("entries")
    if not isinstance(raw_entries, (dict, list)):
        raise InvalidEvent("World book requires entries as an object or list.")

    candidates = _entry_candidates(raw_entries)
    entries: list[dict] = []
    skipped: list[dict] = []
    for source_key, raw_entry, pre_skip_reason in candidates:
        report_key = _safe_report_source_key(source_key)
        if pre_skip_reason is not None:
            skipped.append({"source_key": report_key, "reason": pre_skip_reason})
            continue
        if not _is_utf8_text(source_key):
            skipped.append({"source_key": report_key, "reason": "source key is not valid UTF-8 text"})
            continue
        if len(source_key) > MAX_WORLD_BOOK_SOURCE_KEY_CHARS:
            skipped.append({"source_key": report_key, "reason": "source key exceeds 80 characters"})
            continue
        if not source_key:
            skipped.append({"source_key": report_key, "reason": "source key is empty"})
            continue
        if source_key != source_key.strip():
            skipped.append({"source_key": report_key, "reason": "source key has surrounding whitespace"})
            continue
        normalized, reason = _normalize_entry(source_key, raw_entry)
        if reason is not None:
            skipped.append({"source_key": report_key, "reason": reason})
            continue
        assert normalized is not None
        entries.append(normalized)

    if not 1 <= len(entries) <= MAX_WORLD_BOOK_ENTRIES:
        raise InvalidEvent("World book must contain between 1 and 128 eligible entries.")
    source = {
        "original_json_base64": base64.b64encode(source_bytes).decode("ascii"),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
    }
    return entries, skipped, source


def matching_world_book_entries(entries: Any, query: str) -> list[dict]:
    """Return a small public-only literal-keyword projection in stable input order."""
    if not isinstance(query, str) or entries is None:
        return []
    query_folded = query.casefold()
    result: list[dict] = []
    content_characters = 0
    try:
        iterator = iter(entries)
    except TypeError:
        return []
    for entry in iterator:
        if not _entry_is_public(entry):
            continue
        entry_id = _entry_value(entry, "entry_id", _entry_value(entry, "source_key"))
        title = _entry_value(entry, "title")
        content = _entry_value(entry, "content")
        if entry_id is None or not isinstance(title, str) or not isinstance(content, str):
            continue
        if not title.strip() or not content.strip():
            continue
        always_on = _entry_value(entry, "always_on") is True
        if not always_on:
            keywords = _entry_value(entry, "keywords", ())
            if not isinstance(keywords, (list, tuple)):
                continue
            if not any(
                isinstance(keyword, str) and keyword.strip() and keyword.casefold() in query_folded
                for keyword in keywords
            ):
                continue
        if len(content) + content_characters > MAX_MATCHED_WORLD_BOOK_CONTENT_CHARS:
            continue
        result.append({"entry_id": str(entry_id), "title": title, "content": content})
        content_characters += len(content)
        if len(result) >= MAX_MATCHED_WORLD_BOOK_ENTRIES:
            break
    return result


def _entry_candidates(raw_entries: dict | list) -> list[tuple[str, object, str | None]]:
    candidates: list[tuple[str, object, str | None]] = []
    seen_source_keys: set[str] = set()
    seen_entry_ids: set[str] = set()
    if isinstance(raw_entries, dict):
        for source_key, raw_entry in raw_entries.items():
            source_key = str(source_key)
            if source_key in seen_source_keys:
                raise InvalidEvent("World-book entry keys must be unique.")
            seen_source_keys.add(source_key)
            pre_skip_reason = None
            if isinstance(raw_entry, dict) and "uid" in raw_entry:
                uid = _identifier(raw_entry["uid"])
                if uid is None:
                    pre_skip_reason = "entry uid is invalid"
                else:
                    if uid in seen_entry_ids:
                        raise InvalidEvent("World-book entry IDs must be unique.")
                    seen_entry_ids.add(uid)
                    if uid != source_key:
                        pre_skip_reason = "entry uid does not match its source key"
            candidates.append((source_key, raw_entry, pre_skip_reason))
        return candidates

    for index, raw_entry in enumerate(raw_entries):
        fallback_key = str(index)
        pre_skip_reason = None
        if isinstance(raw_entry, dict) and "id" in raw_entry:
            source_key = _identifier(raw_entry["id"])
            if source_key is None:
                source_key = fallback_key
                pre_skip_reason = "entry id is invalid"
        else:
            source_key = fallback_key
        if source_key in seen_source_keys:
            raise InvalidEvent("World-book entry keys and IDs must be unique.")
        seen_source_keys.add(source_key)
        if isinstance(raw_entry, dict) and "id" in raw_entry:
            entry_id = _identifier(raw_entry["id"])
            if entry_id is not None:
                if entry_id in seen_entry_ids:
                    raise InvalidEvent("World-book entry IDs must be unique.")
                seen_entry_ids.add(entry_id)
        candidates.append((source_key, raw_entry, pre_skip_reason))
    return candidates


def _normalize_entry(source_key: str, raw_entry: object) -> tuple[dict | None, str | None]:
    if not isinstance(raw_entry, dict):
        return None, "entry is not an object"

    disabled = raw_entry.get("disable", False)
    enabled = raw_entry.get("enabled", True)
    if not isinstance(disabled, bool) or not isinstance(enabled, bool):
        return None, "entry disable/enabled flag is invalid"
    if "disable" in raw_entry and "enabled" in raw_entry and enabled == disabled:
        return None, "entry disable/enabled flags conflict"
    if disabled or not enabled:
        return None, "disabled"

    constant = raw_entry.get("constant", raw_entry.get("always_on", False))
    if not isinstance(constant, bool):
        return None, "entry constant flag is invalid"
    if "constant" in raw_entry and "always_on" in raw_entry:
        alternate = raw_entry["always_on"]
        if not isinstance(alternate, bool) or alternate != constant:
            return None, "entry always-on flags conflict"

    raw_content = raw_entry.get("content")
    if not isinstance(raw_content, str):
        return None, "entry content is invalid"
    content = raw_content.strip()
    if not content:
        return None, "entry content is empty"
    if not _is_utf8_text(content):
        return None, "entry content is not valid UTF-8 text"
    if len(content) > MAX_WORLD_BOOK_CONTENT_CHARS:
        return None, "entry content exceeds 2000 characters"

    raw_keywords = raw_entry.get("key", raw_entry.get("keys", []))
    if raw_keywords is None:
        raw_keywords = []
    if not isinstance(raw_keywords, list) or len(raw_keywords) > MAX_WORLD_BOOK_KEYWORDS:
        return None, "entry keywords are invalid or exceed 32 items"
    keywords: list[str] = []
    for raw_keyword in raw_keywords:
        if not isinstance(raw_keyword, str):
            return None, "entry keyword is not text"
        keyword = raw_keyword.strip()
        if not keyword:
            continue
        if not _is_utf8_text(keyword):
            return None, "entry keyword is not valid UTF-8 text"
        if len(keyword) > MAX_WORLD_BOOK_KEYWORD_CHARS:
            return None, "entry keyword exceeds 80 characters"
        keywords.append(keyword)
    if not keywords and not constant:
        return None, "entry has no keywords and is not always-on"

    title = _title(raw_entry)
    if title is None:
        title = keywords[0] if keywords else f"条目 {source_key}"
    if not _is_utf8_text(title):
        return None, "entry title is not valid UTF-8 text"
    if len(title) > MAX_WORLD_BOOK_TITLE_CHARS:
        return None, "entry title exceeds 120 characters"

    return {
        "source_key": source_key,
        "title": title,
        "content": content,
        "keywords": keywords,
        "always_on": constant,
    }, None


def _title(entry: dict) -> str | None:
    for field in ("comment", "name", "title"):
        if field not in entry or entry[field] is None:
            continue
        value = entry[field]
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value:
            return value
    return None


def _identifier(value: object) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _entry_is_public(entry: object) -> bool:
    visibility = _entry_value(entry, "visibility")
    return visibility == "public"


def _entry_value(entry: object, name: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for field, value in pairs:
        if field in result:
            raise ValueError("duplicate field")
        result[field] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _is_utf8_text(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _safe_report_source_key(value: str) -> str:
    if _is_utf8_text(value):
        return value[:MAX_WORLD_BOOK_SOURCE_KEY_CHARS]
    return value.encode("unicode_escape").decode("ascii")[:MAX_WORLD_BOOK_SOURCE_KEY_CHARS]
