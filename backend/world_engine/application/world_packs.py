"""Bounded, declarative world-template archives."""

from __future__ import annotations

import io
import json
import math
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from world_engine import __version__
from world_engine.application.media_assets import MediaAssetStore
from world_engine.domain.json_types import JsonObject
from world_engine.domain.models import ContractModel


MAX_WORLD_PACK_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 128 * 1024
_VERSION_PATTERN = r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
_SCHEMA_PATTERN = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_ENGINE_COMPARISON = re.compile(rf"(>=|<=|==|>|<|=)?\s*({_VERSION_PATTERN})")
_REFERENCE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|(?<![:/\\])(?://|\\\\)[^\s/\\]"
    r"|\b(?:https?|ftps?|sftp|javascript|vbscript|data|file|mailto|blob|about):(?=\S))"
)
_SCRIPT_MARKUP = re.compile(r"(?i)<\s*/?\s*(?:script|iframe|object|embed|svg)\b")
_MAX_JSON_STRUCTURE_ITEMS = 1_000_000
_FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "access_token", "refresh_token", "client_secret", "bearer_token",
    "credential", "credentials", "credential_ref", "secret_ref", "secret_reference",
    "secret_value", "provider_key", "provider_secret", "provider_id", "provider_url",
    "base_url", "default_model", "provider_config", "provider_configuration", "provider_settings",
    "model_config", "model_settings", "script", "scripts", "script_url", "javascript",
    "lua_script", "python_script", "preinstall", "postinstall", "entrypoint", "entry_point",
    "executable", "executables", "shell_command",
})
_ASSET_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
}
_WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", re.IGNORECASE)
_IDENTIFIER = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]
_VERSION = Annotated[str, Field(pattern=rf"^{_VERSION_PATTERN}$")]
_DIGEST = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
STORY_OPENING_ID_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$"
_STORY_OPENING_ID = Annotated[str, Field(pattern=STORY_OPENING_ID_PATTERN)]


class WorldPackArchiveError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_WORLD_PACK"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorldPackLimits:
    compressed_bytes: int = MAX_WORLD_PACK_BYTES
    expanded_bytes: int = 32 * 1024 * 1024
    member_bytes: int = 16 * 1024 * 1024
    members: int = 128


class WorldPackDependency(ContractModel):
    pack_id: _IDENTIFIER
    version: _VERSION


class WorldPackMember(ContractModel):
    path: str = Field(min_length=1, max_length=240)
    size: int = Field(strict=True, ge=0)
    sha256: _DIGEST


class WorldPackManifest(ContractModel):
    schema_version: str = Field(default="1.0", max_length=20)
    pack_id: _IDENTIFIER
    version: _VERSION
    engine_range: str = Field(default="*", min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    dependencies: tuple[WorldPackDependency, ...] = ()
    license: str = Field(default="UNLICENSED", min_length=1, max_length=120)
    content_hash: _DIGEST | None = None
    members: tuple[WorldPackMember, ...] = ()

    def supports_engine(self, version: str) -> bool:
        return _engine_compatible(self.engine_range, version)


class OpeningPlayerProfile(ContractModel):
    profile_id: _STORY_OPENING_ID
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    food_requirement: Literal["opening", "not_required"] = "opening"


class OpeningPlayerSetup(ContractModel):
    profile_id: _STORY_OPENING_ID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=240)


class _StoryOpening(ContractModel):
    opening_id: _STORY_OPENING_ID
    title: str = Field(min_length=1, max_length=120)
    position: str = Field(min_length=1, max_length=160)
    known_history: tuple[Annotated[str, Field(min_length=1, max_length=360)], ...] = Field(max_length=12)
    scenario: JsonObject
    player_profiles: tuple[OpeningPlayerProfile, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def distinct_player_profiles(self):
        if len(self.player_profiles) != len({profile.profile_id for profile in self.player_profiles}):
            raise ValueError("Opening player profiles must have unique IDs.")
        return self


class _WorldPackTemplate(ContractModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    base_pack_id: _IDENTIFIER | None = None
    base_pack_version: _VERSION | None = None
    seed: int | None = Field(default=None, strict=True, ge=-(2**53 - 1), le=2**53 - 1)
    population_mode: Literal["natural", "sustained"] = "natural"
    ruleset_version: _VERSION | None = None
    scenario: JsonObject | None = None
    story_openings: tuple[_StoryOpening, ...] = Field(default=(), max_length=12)


@dataclass(frozen=True, slots=True)
class VerifiedWorldPack:
    manifest: WorldPackManifest
    template: JsonObject
    assets: dict[str, bytes]
    inert_metadata: dict[str, JsonObject]
    compatibility_report: JsonObject


def load_world_pack(
    data: bytes, *,
    limits: WorldPackLimits = WorldPackLimits(),
    engine_version: str | None = __version__,
) -> VerifiedWorldPack:
    """Read and verify a template ZIP without publishing files or resolving references."""
    if len(data) > limits.compressed_bytes:
        raise WorldPackArchiveError("世界包 ZIP 超过上传大小限制。", code="WORLD_PACK_TOO_LARGE")
    if not data.startswith(b"PK\x03\x04") or data[-22:-18] != b"PK\x05\x06":
        raise WorldPackArchiveError("世界包必须是完整的 ZIP 文件。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = _index_members(archive, limits)
            manifest, manifest_extra = _read_manifest(archive, data, members, engine_version)
            payload = {
                member.path: _read_member(archive, data, members[member.path], limit=member.size)
                for member in manifest.members
            }
    except (
        zipfile.BadZipFile, UnicodeError, EOFError, NotImplementedError, RuntimeError,
        OverflowError, struct.error, zlib.error,
    ):
        raise WorldPackArchiveError("世界包 ZIP 损坏或使用了不支持的压缩格式。") from None
    for member in manifest.members:
        if sha256(payload[member.path]).hexdigest() != member.sha256:
            raise WorldPackArchiveError("世界包文件与清单中的校验和不符。")

    raw_template = _parse_json(payload.pop("template.json"))
    known_template, template_extra = _split_fields(raw_template, _WorldPackTemplate)
    try:
        template = _WorldPackTemplate.model_validate(known_template)
    except ValidationError:
        raise WorldPackArchiveError("世界包模板的数据字段无效。") from None
    if template.base_pack_id is None and template.scenario is None and not template.story_openings:
        raise WorldPackArchiveError("世界包模板必须提供已有背景或初始场景。")
    if template.story_openings:
        if template.base_pack_id is not None or template.scenario is not None:
            raise WorldPackArchiveError("可选介入点应分别提供开局场景。")
        if len({item.opening_id for item in template.story_openings}) != len(template.story_openings):
            raise WorldPackArchiveError("介入点标识必须唯一。")
    if template.base_pack_id is None and template.base_pack_version is not None:
        raise WorldPackArchiveError("世界包中的背景版本需要对应的背景标识。")
    for path, content in payload.items():
        if not MediaAssetStore._mime_matches(_asset_type(path), content):
            raise WorldPackArchiveError("世界包资源内容与允许的媒体格式不符。")

    inert_metadata = {"manifest": manifest_extra, "template": template_extra}
    report: JsonObject = {
        "schema_version": manifest.schema_version,
        "engine_range": manifest.engine_range,
        "dependencies": [dep.model_dump(mode="json") for dep in manifest.dependencies],
        "unsupported_fields": [
            {"section": section, "field": key}
            for section, fields in inert_metadata.items()
            for key in sorted(fields)
        ],
        "inert_asset_count": len(payload),
    }
    return VerifiedWorldPack(
        manifest, template.model_dump(mode="json", exclude_unset=True), payload, inert_metadata, report,
    )


def write_world_pack(
    *,
    manifest: WorldPackManifest,
    template: JsonObject,
    assets: dict[str, bytes] | None = None,
    limits: WorldPackLimits = WorldPackLimits(),
) -> bytes:
    """Build an author-owned template archive using the same import policy."""
    assets = assets or {}
    if len(assets) + 2 > limits.members:
        raise WorldPackArchiveError("世界包中的文件数量超过限制。")
    for path in assets:
        _asset_type(path)
    payload = {"template.json": _canonical_json(template), **assets}
    members = tuple(
        WorldPackMember(path=path, size=len(content), sha256=sha256(content).hexdigest())
        for path, content in sorted(payload.items())
    )
    manifest = manifest.model_copy(update={"members": members, "content_hash": _content_hash(members)})
    payload["manifest.json"] = _canonical_json(manifest.model_dump(mode="json"))
    if any(len(content) > limits.member_bytes for content in payload.values()) or (
        sum(map(len, payload.values())) > limits.expanded_bytes
    ):
        raise WorldPackArchiveError("世界包解压大小超过限制。", code="WORLD_PACK_TOO_LARGE")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
        for path, content in payload.items():
            info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    result = output.getvalue()
    load_world_pack(result, limits=limits)
    return result


def _index_members(archive: zipfile.ZipFile, limits: WorldPackLimits) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if archive.comment or not 2 <= len(entries) <= limits.members:
        raise WorldPackArchiveError("世界包的 ZIP 文件结构无效。")
    if sum(member.file_size for member in entries) > limits.expanded_bytes:
        raise WorldPackArchiveError("世界包解压大小超过限制。", code="WORLD_PACK_TOO_LARGE")
    members = {}
    normalized_paths = set()
    for member in entries:
        name = member.orig_filename
        _check_path(name)
        normalized = unicodedata.normalize("NFC", name.casefold())
        if normalized in normalized_paths or name != member.filename:
            raise WorldPackArchiveError("世界包中包含重复或不安全的文件路径。")
        normalized_paths.add(normalized)
        mode = member.external_attr >> 16
        if (
            member.flag_bits & ~0x80E
            or member.volume
            or member.comment
            or member.extra
            or member.external_attr & 0x10
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            or mode & 0o111
            or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            raise WorldPackArchiveError("世界包只允许未加密的普通数据文件。")
        if member.file_size > limits.member_bytes or member.compress_size > limits.compressed_bytes:
            raise WorldPackArchiveError("世界包文件超过大小限制。", code="WORLD_PACK_TOO_LARGE")
        if name not in {"manifest.json", "template.json"}:
            _asset_type(name)
        members[name] = member
    if not {"manifest.json", "template.json"} <= members.keys():
        raise WorldPackArchiveError("世界包缺少 manifest.json 或 template.json。")
    parts_used_as_directories = {
        parent for name in normalized_paths
        for parent in (name[:index] for index, character in enumerate(name) if character == "/")
    }
    if parts_used_as_directories & normalized_paths:
        raise WorldPackArchiveError("世界包中包含同时用作文件和目录的路径。")
    return members


def _check_path(path: str) -> None:
    if len(path) > 240 or not path or path != unicodedata.normalize("NFC", path):
        raise WorldPackArchiveError("世界包中包含不安全的文件路径。")
    for part in path.split("/"):
        if (
            not part
            or part.startswith(".")
            or part.strip() != part
            or part.endswith(".")
            or any(char in '\\:<>|?*"' or unicodedata.category(char).startswith("C") for char in part)
            or _WINDOWS_DEVICE.fullmatch(part.partition(".")[0].rstrip())
        ):
            raise WorldPackArchiveError("世界包中包含不安全的文件路径。")


def _asset_type(path: str) -> str:
    _check_path(path)
    _, separator, extension = path.rsplit("/", 1)[-1].rpartition(".")
    content_type = _ASSET_TYPES.get(f".{extension.lower()}") if separator else None
    if not path.startswith("assets/") or content_type is None:
        raise WorldPackArchiveError("世界包资源必须是本地的受支持音频或图片文件。")
    return content_type


def _read_manifest(
    archive: zipfile.ZipFile, source: bytes,
    members: dict[str, zipfile.ZipInfo], engine_version: str | None,
) -> tuple[WorldPackManifest, JsonObject]:
    raw = _parse_json(_read_member(archive, source, members["manifest.json"], limit=_MAX_MANIFEST_BYTES))
    known, extra = _split_fields(raw, WorldPackManifest)
    if known.keys() != WorldPackManifest.model_fields.keys():
        raise WorldPackArchiveError("世界包清单缺少必填字段。")
    try:
        manifest = WorldPackManifest.model_validate(known)
    except ValidationError:
        raise WorldPackArchiveError("世界包清单字段无效。") from None
    schema = _SCHEMA_PATTERN.fullmatch(manifest.schema_version)
    if not schema or schema[1] != "1":
        raise WorldPackArchiveError("此世界包采用了不支持的清单版本。", code="WORLD_PACK_INCOMPATIBLE")
    if engine_version is not None and not manifest.supports_engine(engine_version):
        raise WorldPackArchiveError("此世界包与当前引擎版本不兼容。", code="WORLD_PACK_INCOMPATIBLE")
    dependency_ids = {dependency.pack_id.casefold() for dependency in manifest.dependencies}
    if len(dependency_ids) != len(manifest.dependencies) or manifest.pack_id.casefold() in dependency_ids:
        raise WorldPackArchiveError("世界包依赖存在重复或自引用。")
    declared = {member.path: member.size for member in manifest.members}
    indexed = {name: info.file_size for name, info in members.items() if name != "manifest.json"}
    if len(declared) != len(manifest.members) or declared != indexed:
        raise WorldPackArchiveError("世界包文件列表或大小与清单不符。")
    if manifest.content_hash != _content_hash(manifest.members):
        raise WorldPackArchiveError("世界包内容摘要与清单不符。")
    return manifest, extra


def _read_member(archive: zipfile.ZipFile, source: bytes, info: zipfile.ZipInfo, *, limit: int) -> bytes:
    if info.file_size > limit:
        raise WorldPackArchiveError("世界包文件超过大小限制。", code="WORLD_PACK_TOO_LARGE")
    # ZipFile checks the local header, name, and member boundaries. Verify
    # DEFLATE separately because ZipExtFile stops at the declared output size.
    # Local header layout: PKWARE APPNOTE 4.3.7.
    # https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT
    with archive.open(info):
        flags, compression = struct.unpack_from("<HH", source, info.header_offset + 6)
        name_length, extra_length = struct.unpack_from("<HH", source, info.header_offset + 26)
        if flags != info.flag_bits or compression != info.compress_type or extra_length:
            raise WorldPackArchiveError("世界包的 ZIP 文件头不一致。")
        offset = info.header_offset + 30 + name_length
        encoded = memoryview(source)[offset:offset + info.compress_size]
    if info.compress_type == zipfile.ZIP_DEFLATED:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        data = decoder.decompress(encoded, info.file_size + 1)
        if not decoder.eof or decoder.unused_data:
            raise WorldPackArchiveError("世界包文件的解压数据与声明不符。")
    else:
        if info.compress_size != info.file_size:
            raise WorldPackArchiveError("世界包文件的实际大小与声明不符。")
        data = bytes(encoded)
    if len(data) != info.file_size or zlib.crc32(data) != info.CRC:
        raise WorldPackArchiveError("世界包文件的实际大小与声明不符。")
    return data


def _engine_compatible(value: str, engine_version: str) -> bool:
    if not re.fullmatch(_VERSION_PATTERN, engine_version):
        return False
    if value == "*":
        return True
    version = tuple(map(int, engine_version.split(".")))
    for term in value.split(","):
        match = _ENGINE_COMPARISON.fullmatch(term.strip())
        if not match:
            return False
        comparator = match[1] or "=="
        bound = tuple(map(int, match[2].split(".")))
        if not {
            ">": version > bound, "<": version < bound, "<=": version <= bound,
            ">=": version >= bound, "==": version == bound, "=": version == bound,
        }[comparator]:
            return False
    return True


def _parse_json(data: bytes) -> JsonObject:
    if sum(data.count(token) for token in (b"{", b"[", b",", b":")) > _MAX_JSON_STRUCTURE_ITEMS:
        raise WorldPackArchiveError("世界包 JSON 的字段或元素数量超过限制。")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields)
        if not isinstance(value, dict):
            raise ValueError
        _check_json(value)
    except WorldPackArchiveError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise WorldPackArchiveError("世界包 JSON 无效或包含重复字段。") from None
    return value


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _check_json(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise WorldPackArchiveError("世界包 JSON 的嵌套层级超过限制。")
    if isinstance(value, dict):
        for key, child in value.items():
            key.encode("utf-8")
            normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or _REFERENCE.search(key) or _SCRIPT_MARKUP.search(key):
                raise WorldPackArchiveError("世界包包含脚本、远程引用或提供商凭据配置。")
            _check_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json(child, depth + 1)
    elif isinstance(value, str):
        value.encode("utf-8")
        if _REFERENCE.search(value) or _SCRIPT_MARKUP.search(value):
            raise WorldPackArchiveError("世界包包含脚本或远程引用。")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError


def _split_fields(value: JsonObject, model: type[ContractModel]) -> tuple[JsonObject, JsonObject]:
    known, unknown = {}, {}
    for key, child in value.items():
        target = known if key in model.model_fields else unknown
        target[key] = child
    return known, unknown


def _content_hash(members: tuple[WorldPackMember, ...]) -> str:
    items = [member.model_dump(mode="json") for member in sorted(members, key=lambda member: member.path)]
    return sha256(_canonical_json(items)).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise WorldPackArchiveError("世界包数据无法编码为 JSON。") from None
