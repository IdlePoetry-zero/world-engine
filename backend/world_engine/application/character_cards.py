"""Translate supported character-card JSON fields into an author-reviewable draft."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zlib
from uuid import UUID

from world_engine.domain.errors import InvalidEvent


MAX_CARD_JSON_BYTES = 256 * 1024
MAX_CARD_PNG_BYTES = 8 * 1024 * 1024
_SOURCE_FORMATS = {"json", "png"}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_MAX_CHUNK_LENGTH = (1 << 31) - 1
_MAX_PNG_CHUNKS = 65_536
_MAX_CARD_JSON_BASE64_BYTES = ((MAX_CARD_JSON_BYTES + 2) // 3) * 4


def character_card_draft(card_json: str, location_id: UUID) -> tuple[dict, dict]:
    """Map CCv1, CCv2 or CCv3 JSON for author review."""
    try:
        if len(card_json.encode("utf-8")) > MAX_CARD_JSON_BYTES:
            raise InvalidEvent("Character card JSON exceeds the upload limit.")
        card = json.loads(
            card_json.removeprefix("\ufeff"), object_pairs_hook=_unique_fields, parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InvalidEvent("Character card must contain valid JSON with unique field names.") from exc
    if not isinstance(card, dict):
        raise InvalidEvent("Character card must be a JSON object.")

    version = "1"
    if "spec" in card or "spec_version" in card:
        spec = card.get("spec")
        spec_version = card.get("spec_version")
        is_v2 = spec == "chara_card_v2" and spec_version == "2.0"
        is_v3 = spec == "chara_card_v3" and spec_version == "3.0"
        if not is_v2 and not is_v3:
            raise InvalidEvent("Only Character Card V1, V2 and V3 JSON formats are supported.")
        data = card.get("data")
        if not isinstance(data, dict):
            raise InvalidEvent("A V2 or V3 card requires a data object.")
        version = "2.0" if is_v2 else "3.0"
    else:
        data = card

    name = _text(data, "name")
    if not name or len(name) > 120:
        raise InvalidEvent("The character name must contain 1 to 120 characters.")
    description = _text(data, "description")
    personality = _text(data, "personality")
    if len(description) > 1000:
        raise InvalidEvent("The character description exceeds 1000 characters. Shorten it before importing.")
    mapped = {"name", "description"}
    traits = []
    if 0 < len(personality) <= 120:
        traits = [personality]
        mapped.add("personality")
    skipped = sorted(field for field, value in data.items() if field not in mapped and value not in ("", [], {}, None))
    draft = {
        "name": name, "location_id": str(location_id), "public_role": "", "description": description,
        "values": [], "traits": traits, "secret": "", "goal": "",
    }
    return draft, {"card_version": version, "mapped_fields": sorted(mapped.intersection(data)),
                   "unmapped_fields": skipped}


def load_character_card(card_json: str, location_id: UUID) -> tuple[dict, dict, dict]:
    """Map a JSON card and preserve its exact validated source text and digest."""
    draft, report = character_card_draft(card_json, location_id)
    return draft, report, _character_card_source("json", card_json, report)


def character_card_png_draft(card_png: bytes, location_id: UUID) -> tuple[dict, dict]:
    """Extract a CCv1/2/3 text payload from a bounded PNG, then map it as JSON."""
    draft, report, _source = load_character_card_png(card_png, location_id)
    return draft, report


def load_character_card_png(card_png: bytes, location_id: UUID) -> tuple[dict, dict, dict]:
    """Map selected PNG metadata and preserve only that decoded JSON source."""
    metadata_key, card_json = _character_card_png_payload(card_png)
    draft, report = character_card_draft(card_json, location_id)
    version = report["card_version"]
    if metadata_key == "ccv3" and version != "3.0":
        raise InvalidEvent("A ccv3 PNG metadata chunk must contain an exact Character Card V3 envelope.")
    if metadata_key == "chara" and version == "3.0":
        raise InvalidEvent("A chara PNG metadata chunk can contain only Character Card V1 or V2.")
    return draft, report, _character_card_source("png", card_json, report)


def _character_card_source(source_format: str, card_json: str, report: dict) -> dict:
    if source_format not in _SOURCE_FORMATS:
        raise ValueError("Unsupported character-card source format.")
    try:
        source_bytes = card_json.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise InvalidEvent("Character card source must be valid UTF-8 text.") from exc
    return {
        "source_format": source_format,
        "card_version": report["card_version"],
        "original_json": card_json,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
    }


def _character_card_png_payload(card_png: bytes) -> tuple[str, str]:
    if not isinstance(card_png, bytes):
        raise InvalidEvent("Character card PNG must be bytes.")
    if len(card_png) > MAX_CARD_PNG_BYTES:
        raise InvalidEvent("Character card PNG exceeds the upload limit.")
    if len(card_png) < len(_PNG_SIGNATURE) + 12 or card_png[:8] != _PNG_SIGNATURE:
        raise InvalidEvent("Character card image is not a valid PNG datastream.")

    offset = len(_PNG_SIGNATURE)
    chunk_count = 0
    seen_ihdr = False
    seen_idat = False
    idat_sequence_ended = False
    seen_iend = False
    recognized_text: dict[str, bytes] = {}
    view = memoryview(card_png)

    while offset < len(card_png):
        chunk_count += 1
        if chunk_count > _MAX_PNG_CHUNKS:
            raise InvalidEvent("Character card PNG contains too many chunks.")
        if len(card_png) - offset < 12:
            raise InvalidEvent("Character card PNG has a truncated chunk header or CRC.")

        chunk_length = int.from_bytes(view[offset:offset + 4], "big")
        chunk_type = bytes(view[offset + 4:offset + 8])
        if chunk_length > _PNG_MAX_CHUNK_LENGTH:
            raise InvalidEvent("Character card PNG chunk length is outside the PNG range.")
        if any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in chunk_type):
            raise InvalidEvent("Character card PNG contains an invalid chunk type.")
        if chunk_type[2] & 0x20:
            raise InvalidEvent("Character card PNG uses a reserved lowercase chunk-type bit.")
        if not seen_ihdr and (chunk_type != b"IHDR" or chunk_length != 13):
            raise InvalidEvent("Character card PNG must begin with a valid IHDR chunk.")
        if chunk_type == b"IHDR" and seen_ihdr:
            raise InvalidEvent("Character card PNG must contain exactly one IHDR chunk.")

        data_start = offset + 8
        data_end = data_start + chunk_length
        crc_end = data_end + 4
        if crc_end > len(card_png):
            raise InvalidEvent("Character card PNG chunk exceeds the input boundary.")
        expected_crc = int.from_bytes(view[data_end:crc_end], "big")
        actual_crc = zlib.crc32(view[offset + 4:data_end]) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise InvalidEvent("Character card PNG contains a chunk with an invalid CRC.")

        if chunk_type == b"IHDR":
            ihdr = view[data_start:data_end]
            width = int.from_bytes(ihdr[0:4], "big")
            height = int.from_bytes(ihdr[4:8], "big")
            bit_depth, color_type, compression, filtering, interlace = ihdr[8:13]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                width == 0 or height == 0 or width > _PNG_MAX_CHUNK_LENGTH or height > _PNG_MAX_CHUNK_LENGTH
                or color_type not in valid_depths or bit_depth not in valid_depths[color_type]
                or compression != 0 or filtering != 0 or interlace not in {0, 1}
            ):
                raise InvalidEvent("Character card PNG contains an invalid IHDR header.")
            seen_ihdr = True

        if seen_idat and chunk_type not in {b"IDAT", b"IEND"}:
            idat_sequence_ended = True
        if chunk_type == b"IDAT":
            if idat_sequence_ended:
                raise InvalidEvent("Character card PNG IDAT chunks must be consecutive.")
            seen_idat = True
        elif chunk_type == b"tEXt":
            data = bytes(view[data_start:data_end])
            separator = data.find(b"\x00")
            if separator <= 0 or separator > 79 or b"\x00" in data[separator + 1:]:
                raise InvalidEvent("Character card PNG contains a malformed tEXt chunk.")
            keyword = data[:separator].decode("latin-1").casefold()
            if keyword in {"chara", "ccv3"}:
                if keyword in recognized_text:
                    raise InvalidEvent("Character card PNG contains duplicate recognized metadata keys.")
                text_payload = data[separator + 1:]
                if len(text_payload) > _MAX_CARD_JSON_BASE64_BYTES:
                    raise InvalidEvent("Character card JSON exceeds the upload limit.")
                recognized_text[keyword] = text_payload
        elif chunk_type == b"IEND":
            if chunk_length != 0 or not seen_idat:
                raise InvalidEvent("Character card PNG has an invalid IEND or is missing image data.")
            if crc_end != len(card_png):
                raise InvalidEvent("Character card PNG has data after its terminal IEND chunk.")
            seen_iend = True
            offset = crc_end
            break

        offset = crc_end

    if not seen_iend or offset != len(card_png):
        raise InvalidEvent("Character card PNG is missing a terminal IEND chunk.")
    if not seen_ihdr or not seen_idat:
        raise InvalidEvent("Character card PNG is missing required image chunks.")
    encoded = recognized_text.get("ccv3", recognized_text.get("chara"))
    if encoded is None:
        raise InvalidEvent("PNG image does not contain a recognized Character Card tEXt chunk.")
    metadata_key = "ccv3" if "ccv3" in recognized_text else "chara"
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) > MAX_CARD_JSON_BYTES:
            raise InvalidEvent("Character card JSON exceeds the upload limit.")
        return metadata_key, decoded.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidEvent("Character card PNG metadata must contain strict base64-encoded UTF-8 JSON.") from exc


def _text(data: dict, field: str) -> str:
    value = data.get(field, "")
    if not isinstance(value, str):
        raise InvalidEvent(f"Character card field {field} must be text.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise InvalidEvent(f"Character card field {field} contains invalid Unicode.") from exc
    return value.strip()


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for field, value in pairs:
        field.encode("utf-8")
        if field in result:
            raise ValueError("duplicate field")
        result[field] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite number")
