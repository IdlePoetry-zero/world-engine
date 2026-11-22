"""Typed, author-imported world book content and its event envelope."""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, StringConstraints, model_validator

from .json_types import JsonObject


MAX_WORLD_BOOK_SOURCE_BYTES = 256 * 1024
MAX_WORLD_BOOK_SOURCE_BASE64_BYTES = ((MAX_WORLD_BOOK_SOURCE_BYTES + 2) // 3) * 4

_SourceKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_EntryTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
_EntryContent = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
_Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_SourceField = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class WorldBookCitation(BaseModel):
    """A locator in a source register, without a remotely loaded asset."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    source_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")]
    work: _SourceField
    locator: _SourceField
    viewpoint: _SourceField | None = None


class WorldBookProvenance(BaseModel):
    """Editorial status is independent of an entry's visibility or authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    category: Literal["person", "place", "organization", "historical_event", "rule", "other"]
    status: Literal["documented", "inferred", "conflicting", "unknown", "game_configuration"]
    citations: tuple[WorldBookCitation, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def check_citations(self) -> WorldBookProvenance:
        locations = {(item.source_id, item.locator) for item in self.citations}
        if len(locations) != len(self.citations):
            raise ValueError("source locations must be unique")
        minimum = {"documented": 1, "inferred": 1, "conflicting": 2}.get(self.status, 0)
        if len(locations) < minimum:
            raise ValueError(f"{self.status} entries require at least {minimum} source locations")
        return self


class WorldBookEntry(BaseModel):
    """One mapped world book entry retained as typed domain content."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True, allow_inf_nan=False)

    entry_id: UUID
    source_key: _SourceKey
    title: _EntryTitle
    content: _EntryContent
    keywords: tuple[_Keyword, ...] = Field(max_length=32)
    always_on: StrictBool
    visibility: Literal["public", "author_only"]
    provenance: WorldBookProvenance | None = Field(default=None, exclude_if=lambda value: value is None)


class WorldBookImportedEvent(BaseModel):
    """Import mapped entries while retaining the exact bounded source document."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    type: Literal["core.world_book_imported.v1"] = "core.world_book_imported.v1"
    event_id: UUID = Field(default_factory=uuid4)
    title: _EntryTitle
    entries: tuple[WorldBookEntry, ...] = Field(min_length=1, max_length=128)
    original_json_base64: StrictStr = Field(
        min_length=4,
        max_length=MAX_WORLD_BOOK_SOURCE_BASE64_BYTES,
    )
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def source_and_visibility_are_valid(self) -> WorldBookImportedEvent:
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("world book import details must remain author_only")
        entry_ids = [entry.entry_id for entry in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("world book entry IDs must be unique within an import")

        try:
            encoded = self.original_json_base64.encode("ascii", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("world book source base64 must contain only ASCII characters") from error
        if not encoded or len(encoded) > MAX_WORLD_BOOK_SOURCE_BASE64_BYTES:
            raise ValueError("world book source base64 exceeds the bounded encoded size")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("world book source base64 is malformed") from error
        if base64.b64encode(decoded) != encoded:
            raise ValueError("world book source base64 must use canonical encoding")
        if not decoded or len(decoded) > MAX_WORLD_BOOK_SOURCE_BYTES:
            raise ValueError("world book source must contain 1 to 256 KiB of UTF-8 text")
        try:
            decoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("world book source must be valid UTF-8 text") from error
        if hashlib.sha256(decoded).hexdigest() != self.sha256:
            raise ValueError("world book source SHA-256 does not match its exact decoded bytes")
        return self
