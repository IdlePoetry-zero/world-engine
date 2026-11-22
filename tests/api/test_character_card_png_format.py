from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from uuid import uuid4

import pytest

from world_engine.application.character_cards import (
    MAX_CARD_JSON_BYTES,
    MAX_CARD_PNG_BYTES,
    character_card_draft,
    character_card_png_draft,
    load_character_card,
    load_character_card_png,
)
from world_engine.domain.errors import InvalidEvent


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
_IDAT = zlib.compress(b"\x00\x00\x00\x00\xff")


def _chunk(name: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big") + name + payload
        + (zlib.crc32(name + payload) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def _card_json(name: str, *, version: str = "v1") -> str:
    if version == "v1":
        card = {"name": name, "description": "A brief description.", "personality": "curious"}
    elif version == "v2":
        card = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {"name": name, "description": "A brief description.", "personality": "curious"},
        }
    elif version == "v3":
        card = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {"name": name, "description": "A brief description.", "personality": "curious"},
        }
    else:
        raise AssertionError(f"unknown test card version {version}")
    return json.dumps(card, separators=(",", ":"), ensure_ascii=False)


def _text(keyword: str, payload: bytes) -> bytes:
    return _chunk(b"tEXt", keyword.encode("latin-1") + b"\x00" + payload)


def _png(
    metadata: list[tuple[str, bytes]] | None = None,
    *,
    extra_chunks: list[bytes] | None = None,
    idat_payload: bytes = _IDAT,
    include_idat: bool = True,
    ihdr_first: bool = True,
    include_iend: bool = True,
) -> bytes:
    text_chunks = [_text(key, value) for key, value in (metadata or [])]
    chunks = [_chunk(b"IHDR", _IHDR)]
    if not ihdr_first:
        chunks = text_chunks[:1] + chunks
        text_chunks = text_chunks[1:]
    if include_idat:
        chunks.append(_chunk(b"IDAT", idat_payload))
    chunks.extend(extra_chunks or [])
    chunks.extend(text_chunks)
    if include_iend:
        chunks.append(_chunk(b"IEND", b""))
    return _PNG_SIGNATURE + b"".join(chunks)


def _encoded_card(name: str, *, version: str = "v1") -> bytes:
    return base64.b64encode(_card_json(name, version=version).encode("utf-8"))


def test_character_card_png_extracts_v1_and_v2_text_metadata() -> None:
    location_id = uuid4()
    for card_version, wire_keyword, version in (
        ("v1", "chara", "1"),
        ("v2", "chara", "2.0"),
    ):
        draft, report = character_card_png_draft(
            _png([(wire_keyword, _encoded_card("Mira", version=card_version))]),
            location_id,
        )
        assert draft == {
            "name": "Mira",
            "location_id": str(location_id),
            "public_role": "",
            "description": "A brief description.",
            "values": [],
            "traits": ["curious"],
            "secret": "",
            "goal": "",
        }
        assert report == {
            "card_version": version,
            "mapped_fields": ["description", "name", "personality"],
            "unmapped_fields": [],
        }


def test_ccv3_metadata_maps_exact_v3_envelope_and_wins_over_chara() -> None:
    v2 = _encoded_card("Legacy name", version="v2")
    v3 = _encoded_card("Current name", version="v3")

    draft, report = character_card_png_draft(
        _png([("chara", v2), ("CCV3", v3)]),
        uuid4(),
    )

    assert draft["name"] == "Current name"
    assert report["card_version"] == "3.0"
    assert report["mapped_fields"] == ["description", "name", "personality"]


@pytest.mark.parametrize(
    ("spec", "spec_version"),
    [
        ("chara_card_v3", "2.0"),
        ("chara_card_v2", "3.0"),
        ("chara_card_v3", None),
        (None, "3.0"),
        ([], "2.0"),
        ("chara_card_v3", {}),
        ({"invalid": "spec"}, ["3.0"]),
    ],
)
def test_json_parser_rejects_nonexact_v3_envelope(spec, spec_version) -> None:
    card = {"data": {"name": "Mira", "description": ""}}
    if spec is not None:
        card["spec"] = spec
    if spec_version is not None:
        card["spec_version"] = spec_version

    with pytest.raises(InvalidEvent, match="V1, V2 and V3"):
        character_card_draft(json.dumps(card), uuid4())


def test_v3_json_requires_its_data_object() -> None:
    card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": []}
    with pytest.raises(InvalidEvent, match="V2 or V3 card requires a data object"):
        character_card_draft(json.dumps(card), uuid4())


@pytest.mark.parametrize(
    ("keyword", "version"),
    [("ccv3", "v1"), ("ccv3", "v2"), ("chara", "v3")],
)
def test_png_metadata_key_must_match_card_version(keyword, version) -> None:
    with pytest.raises(InvalidEvent, match="PNG metadata chunk"):
        character_card_png_draft(_png([(keyword, _encoded_card("Mira", version=version))]), uuid4())


def test_png_metadata_must_be_unique_strict_base64_and_utf8() -> None:
    card = _encoded_card("Mira")
    for metadata in (
        [("chara", card), ("CHARA", card)],
        [("ccv3", _encoded_card("Mira", version="v3")), ("CCV3", _encoded_card("Again", version="v3"))],
    ):
        with pytest.raises(InvalidEvent, match="duplicate recognized"):
            character_card_png_draft(_png(metadata), uuid4())

    with pytest.raises(InvalidEvent, match="strict base64"):
        character_card_png_draft(_png([("chara", b"***")]), uuid4())
    with pytest.raises(InvalidEvent, match="strict base64"):
        character_card_png_draft(_png([("chara", base64.b64encode(b"\xff"))]), uuid4())


def test_png_requires_signature_ihdr_idat_and_terminal_iend() -> None:
    metadata = [("chara", _encoded_card("Mira"))]
    valid = _png(metadata)

    with pytest.raises(InvalidEvent, match="valid PNG"):
        character_card_png_draft(b"not a png" + valid[8:], uuid4())
    with pytest.raises(InvalidEvent, match="begin with a valid IHDR"):
        character_card_png_draft(_png(metadata, ihdr_first=False), uuid4())
    with pytest.raises(InvalidEvent, match="IEND or is missing image data"):
        character_card_png_draft(_png(metadata, include_idat=False), uuid4())
    with pytest.raises(InvalidEvent, match="terminal IEND"):
        character_card_png_draft(_png(metadata, include_iend=False), uuid4())
    with pytest.raises(InvalidEvent, match="after its terminal IEND"):
        character_card_png_draft(valid + b"trailing", uuid4())


def test_png_rejects_truncated_chunks_and_bad_crc() -> None:
    valid = _png([("chara", _encoded_card("Mira"))])
    with pytest.raises(InvalidEvent, match="truncated chunk"):
        character_card_png_draft(valid[:-2], uuid4())

    corrupt = bytearray(valid)
    corrupt[29] ^= 0x01  # First chunk's CRC begins after signature, header and IHDR payload.
    with pytest.raises(InvalidEvent, match="invalid CRC"):
        character_card_png_draft(bytes(corrupt), uuid4())


def test_png_skips_unknown_text_extensions_without_decompression_or_pixel_decode() -> None:
    broken_ztext = _chunk(b"zTXt", b"comment\x00\x00not a zlib stream")
    png = _png(
        [("chara", _encoded_card("Mira"))],
        extra_chunks=[broken_ztext],
        idat_payload=b"not decoded as image pixels",
    )

    draft, report = character_card_png_draft(png, uuid4())

    assert draft["name"] == "Mira"
    assert report["card_version"] == "1"


def test_png_and_decoded_json_limits_are_enforced() -> None:
    assert MAX_CARD_PNG_BYTES == 8 * 1024 * 1024
    with pytest.raises(InvalidEvent, match="PNG exceeds the upload limit"):
        character_card_png_draft(b"x" * (MAX_CARD_PNG_BYTES + 1), uuid4())

    oversized_json = json.dumps({"name": "Mira", "description": "x" * MAX_CARD_JSON_BYTES})
    oversized_png = _png([("chara", base64.b64encode(oversized_json.encode("utf-8")))])
    with pytest.raises(InvalidEvent, match="JSON exceeds the upload limit"):
        character_card_png_draft(oversized_png, uuid4())


def test_png_chunk_iteration_is_bounded() -> None:
    from world_engine.application.character_cards import _MAX_PNG_CHUNKS

    no_op_chunk = _chunk(b"vpAg", b"")
    metadata = [("chara", _encoded_card("Mira"))]
    png = _png(metadata, extra_chunks=[no_op_chunk] * _MAX_PNG_CHUNKS)

    with pytest.raises(InvalidEvent, match="too many chunks"):
        character_card_png_draft(png, uuid4())


def test_json_loader_preserves_exact_bom_trailing_space_and_unknown_extensions() -> None:
    card = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "Mira",
            "description": "",
            "extensions": {"book": {"chapter": ["kept", {"detail": "original"}]}},
        },
    }
    original_json = "\ufeff" + json.dumps(card, ensure_ascii=False, separators=(",", ":")) + " \n"
    location_id = uuid4()

    draft, report, source = load_character_card(original_json, location_id)

    assert source == {
        "source_format": "json",
        "card_version": "2.0",
        "original_json": original_json,
        "sha256": hashlib.sha256(original_json.encode("utf-8")).hexdigest(),
    }
    assert set(source) == {"source_format", "card_version", "original_json", "sha256"}
    assert report["unmapped_fields"] == ["extensions"]
    assert draft == character_card_draft(original_json, location_id)[0]
    assert (draft, report) == character_card_draft(original_json, location_id)


def test_png_loader_preserves_only_selected_decoded_json_and_old_wrapper_shape() -> None:
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "Current Mira",
            "description": "",
            "extensions": {"book": {"chapter": ["source-only", {"detail": "retained"}]}},
        },
    }
    original_json = "\ufeff" + json.dumps(card, ensure_ascii=False, separators=(",", ":")) + "  \n"
    raw_png = _png([
        ("chara", _encoded_card("Legacy Mira", version="v2")),
        ("ccv3", base64.b64encode(original_json.encode("utf-8"))),
    ])
    location_id = uuid4()

    draft, report, source = load_character_card_png(raw_png, location_id)

    assert source == {
        "source_format": "png",
        "card_version": "3.0",
        "original_json": original_json,
        "sha256": hashlib.sha256(original_json.encode("utf-8")).hexdigest(),
    }
    assert set(source) == {"source_format", "card_version", "original_json", "sha256"}
    assert source["original_json"] != raw_png
    assert report["unmapped_fields"] == ["extensions"]
    assert draft["name"] == "Current Mira"
    assert (draft, report) == character_card_png_draft(raw_png, location_id)
