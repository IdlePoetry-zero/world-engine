"""Local content-addressed media storage with host-enforced audience grants.

The store is deliberately a passive adapter: it neither dispatches plugins nor
serves HTTP. The caller supplies a request already authorized by the host, an
already verified receipt, and an internal authorization proof produced after
world/branch membership and actor checks. Blob paths are derived only from the
verified SHA-256 digest; plugin metadata and request strings are never paths.

The file publication pattern uses a same-directory temporary file followed by an
atomic no-replace hard link. See Python's `os.link` documentation and SQLite's
transaction documentation for the underlying filesystem and metadata primitives:
https://docs.python.org/3/library/os.html#os.link
https://www.sqlite.org/lang_transaction.html
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
import zlib
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from world_engine.application.media_contracts import AssetHash, AssetScope, GeneratedMedia, MediaRequest
from world_engine.domain.models import AccessScope, MembershipRole


MAX_MEDIA_ASSET_BYTES = 64 * 1024 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1


class MediaAssetError(RuntimeError):
    """Base error for local media asset operations."""


class MediaAssetValidationError(MediaAssetError):
    """The supplied receipt, request, or bytes do not describe one asset."""


class MediaAssetAccessDenied(MediaAssetError):
    """The host authorization proof does not permit this operation."""


class MediaAssetNotFound(MediaAssetError):
    """No asset grant in the authorized scope permits this read."""


class MediaAssetConflict(MediaAssetError):
    """A request ID was reused with different semantic content or permissions."""


class MediaAssetIntegrityError(MediaAssetError):
    """Stored bytes or metadata failed integrity validation."""


class MediaAssetStorageError(MediaAssetError):
    """The isolated local store could not safely complete an operation."""


class MediaAssetAuth(BaseModel):
    """Internal host proof; construct only after repository authorization.

    `verified_subject_ids` must come from an authorization check such as
    `WorldRepository.authorize_actor`, not from request or plugin input.
    `trusted_host` is reserved for in-process host dispatches and is never a
    client-controlled API field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: AccessScope
    world_id: UUID
    branch_id: UUID
    membership_role: MembershipRole
    verified_subject_ids: frozenset[UUID] = Field(default_factory=frozenset)
    trusted_host: bool = False


class StoredMediaAsset(BaseModel):
    """Safe public result; filesystem paths and private descriptions are omitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    world_id: UUID
    branch_id: UUID
    source_event_ids: tuple[UUID, ...]
    content_hash: AssetHash
    content_type: str
    byte_size: int = Field(gt=0, le=MAX_MEDIA_ASSET_BYTES)
    visibility: AssetScope


class MediaAssetStore:
    """An isolated SQLite grant index and immutable SHA-256 blob directory."""

    def __init__(self, root: str | Path, *, maximum_asset_bytes: int = MAX_MEDIA_ASSET_BYTES):
        if not 0 < maximum_asset_bytes <= MAX_MEDIA_ASSET_BYTES:
            raise ValueError(f"maximum_asset_bytes must be between 1 and {MAX_MEDIA_ASSET_BYTES}")

        requested_root = Path(root).expanduser()
        if requested_root.is_symlink():
            raise MediaAssetStorageError("The configured media root cannot be a symbolic link")
        self.root = requested_root.resolve()
        self.maximum_asset_bytes = maximum_asset_bytes
        self.blob_root = self.root / "blobs" / "sha256"
        self.index_path = self.root / "grants.sqlite3"

        self._ensure_directory(self.root)
        self._ensure_directory(self.blob_root)
        self._reject_symlink_file(self.index_path)
        self._initialize_index()

    def store(
        self,
        request: MediaRequest,
        receipt: GeneratedMedia,
        data: bytes,
        auth: MediaAssetAuth,
    ) -> StoredMediaAsset:
        """Atomically publish verified bytes and a non-widening request grant.

        If a crash occurs after blob publication but before the SQLite commit, an
        unreferenced immutable blob may remain. A grant is never committed before
        its complete blob exists, so reads cannot observe a dangling grant.
        """

        auth = self._validate_auth(auth)
        request, receipt = self._validate_input(request, receipt, data)
        self._check_scope_binding(auth, request.world_id, request.branch_id)
        author_user_id = self._authorize_write(request, auth)
        request_fingerprint = self._request_fingerprint(request)
        subjects_json = self._json([str(item) for item in sorted(request.visibility.subject_ids, key=str)])
        events_json = self._json([str(item) for item in request.source_event_ids])
        creator_id = str(auth.scope.user_id)

        values = (
            str(auth.scope.tenant_id),
            str(request.world_id),
            str(request.branch_id),
            str(request.request_id),
            receipt.content_hash,
            receipt.content_type,
            receipt.byte_size,
            request.kind,
            request.visibility.policy,
            subjects_json,
            events_json,
            str(request.recipient_id) if request.recipient_id else None,
            str(author_user_id) if author_user_id else None,
            creator_id,
            request_fingerprint,
        )

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM media_asset_grants
                   WHERE tenant_id=? AND world_id=? AND branch_id=? AND request_id=?""",
                values[:4],
            ).fetchone()
            if existing is not None:
                if not self._same_grant(existing, values):
                    raise MediaAssetConflict("This media request ID is already bound to another receipt or audience")
                self._publish_or_verify_blob(receipt.content_hash, receipt.content_type, data)
                connection.commit()
                return self._stored_result(request, receipt)

            self._publish_or_verify_blob(receipt.content_hash, receipt.content_type, data)
            connection.execute(
                """INSERT INTO media_asset_grants (
                    tenant_id, world_id, branch_id, request_id, content_hash,
                    content_type, byte_size, media_kind, visibility_policy,
                    subject_ids_json, source_event_ids_json, recipient_id,
                    author_user_id, creator_user_id, request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            connection.commit()
            return self._stored_result(request, receipt)
        except MediaAssetError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise MediaAssetStorageError("The media grant index could not be updated") from exc
        finally:
            connection.close()

    def lookup_request(
        self,
        request: MediaRequest,
        auth: MediaAssetAuth,
    ) -> StoredMediaAsset | None:
        """Recover a completed publication by exact request ID and fresh audience proof.

        This is used after a worker crash between the asset-store commit and
        durable job completion. It never searches by digest alone and verifies
        the immutable blob before returning a result.
        """
        auth = self._validate_auth(auth)
        try:
            raw = request.model_dump(mode="python") if isinstance(request, MediaRequest) else request
            request = MediaRequest.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise MediaAssetValidationError("A valid media request is required") from exc
        self._check_scope_binding(auth, request.world_id, request.branch_id)
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM media_asset_grants
                   WHERE tenant_id=? AND world_id=? AND branch_id=? AND request_id=?""",
                (
                    str(auth.scope.tenant_id),
                    str(request.world_id),
                    str(request.branch_id),
                    str(request.request_id),
                ),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MediaAssetStorageError("The media grant index could not be read") from exc
        finally:
            connection.close()
        if row is None:
            return None

        fingerprint = self._request_fingerprint(request)
        expected_subjects = self._json(
            [str(item) for item in sorted(request.visibility.subject_ids, key=str)]
        )
        expected_events = self._json([str(item) for item in request.source_event_ids])
        if (
            row["request_fingerprint"] != fingerprint
            or row["media_kind"] != request.kind
            or row["visibility_policy"] != request.visibility.policy
            or row["subject_ids_json"] != expected_subjects
            or row["source_event_ids_json"] != expected_events
            or row["recipient_id"] != (str(request.recipient_id) if request.recipient_id else None)
        ):
            raise MediaAssetConflict("This media request ID is already bound to different request content or audience")
        if not self._grant_allows(row, auth):
            raise MediaAssetAccessDenied("Current authorization does not permit recovery of this media request")
        try:
            receipt = GeneratedMedia(
                request_id=request.request_id,
                content_hash=row["content_hash"],
                content_type=row["content_type"],
                byte_size=row["byte_size"],
            )
            receipt.validate_for(request, maximum_bytes=self.maximum_asset_bytes)
        except (TypeError, ValueError) as exc:
            raise MediaAssetIntegrityError("A stored media receipt does not match its request") from exc
        self._read_verified_blob(
            receipt.content_hash,
            content_type=receipt.content_type,
            byte_size=receipt.byte_size,
        )
        return self._stored_result(request, receipt)

    def read(self, content_hash: AssetHash, auth: MediaAssetAuth) -> bytes:
        """Read only through a grant for the exact tenant, world, and branch."""
        data, _ = self.read_with_content_type(content_hash, auth)
        return data

    def read_with_content_type(self, content_hash: AssetHash, auth: MediaAssetAuth) -> tuple[bytes, str]:
        """Return verified bytes and the content type from the same authorized grant."""
        auth = self._validate_auth(auth)
        if not isinstance(content_hash, str) or not _DIGEST_RE.fullmatch(content_hash):
            raise MediaAssetNotFound("No authorized media asset exists in this scope")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM media_asset_grants
                   WHERE tenant_id=? AND world_id=? AND branch_id=? AND content_hash=?
                   ORDER BY request_id""",
                (str(auth.scope.tenant_id), str(auth.world_id), str(auth.branch_id), content_hash),
            ).fetchall()
        except sqlite3.Error as exc:
            raise MediaAssetStorageError("The media grant index could not be read") from exc
        finally:
            connection.close()

        authorized = [row for row in rows if self._grant_allows(row, auth)]
        if not authorized:
            # Hide whether another tenant, branch, or audience has this digest.
            raise MediaAssetNotFound("No authorized media asset exists in this scope")

        grant = authorized[0]
        data = self._read_verified_blob(
            content_hash,
            content_type=grant["content_type"],
            byte_size=grant["byte_size"],
        )
        return data, grant["content_type"]

    def _validate_input(
        self,
        request: MediaRequest,
        receipt: GeneratedMedia,
        data: bytes,
    ) -> tuple[MediaRequest, GeneratedMedia]:
        try:
            request_data = request.model_dump(mode="python") if isinstance(request, MediaRequest) else request
            receipt_data = receipt.model_dump(mode="python") if isinstance(receipt, GeneratedMedia) else receipt
            request = MediaRequest.model_validate(request_data)
            receipt = GeneratedMedia.model_validate(receipt_data)
            if not isinstance(data, bytes):
                raise ValueError("Asset data must be bytes")
            receipt.validate_for(request, maximum_bytes=self.maximum_asset_bytes)
        except (TypeError, ValueError) as exc:
            raise MediaAssetValidationError(str(exc)) from exc

        if len(data) != receipt.byte_size:
            raise MediaAssetValidationError("Receipt byte_size does not equal the supplied byte length")
        actual_hash = sha256(data).hexdigest()
        if actual_hash != receipt.content_hash:
            raise MediaAssetValidationError("Receipt content_hash does not match the supplied bytes")
        if not self._mime_matches(receipt.content_type, data):
            raise MediaAssetValidationError("Receipt content_type does not match the asset byte signature")
        return request, receipt

    @staticmethod
    def _validate_auth(auth: MediaAssetAuth) -> MediaAssetAuth:
        try:
            data = auth.model_dump(mode="python") if isinstance(auth, MediaAssetAuth) else auth
            return MediaAssetAuth.model_validate(data)
        except (TypeError, ValueError) as exc:
            raise MediaAssetAccessDenied("A valid host authorization proof is required") from exc

    @staticmethod
    def _check_scope_binding(auth: MediaAssetAuth, world_id: UUID, branch_id: UUID) -> None:
        if auth.world_id != world_id or auth.branch_id != branch_id:
            raise MediaAssetAccessDenied("The authorization proof is bound to another world or branch")
        if not isinstance(auth.membership_role, MembershipRole):
            raise MediaAssetAccessDenied("A host-verified world membership role is required")

    @staticmethod
    def _authorize_write(request: MediaRequest, auth: MediaAssetAuth) -> UUID | None:
        role = auth.membership_role
        if request.visibility.policy == "participants":
            if role == MembershipRole.OBSERVER:
                raise MediaAssetAccessDenied("Observers cannot publish participant-scoped media")
            audience = set(request.visibility.subject_ids)
            if request.recipient_id is not None:
                if request.recipient_id not in audience or request.recipient_id not in auth.verified_subject_ids:
                    raise MediaAssetAccessDenied("The request recipient is not verified in the participant audience")
            elif not audience.intersection(auth.verified_subject_ids):
                raise MediaAssetAccessDenied("A verified participant subject is required to publish this grant")
            return None

        if role in {MembershipRole.OWNER, MembershipRole.EDITOR} or auth.trusted_host:
            return auth.scope.user_id if request.visibility.policy == "author_only" else None
        raise MediaAssetAccessDenied("Publishing public or author-only media requires an editor or trusted host")

    @staticmethod
    def _grant_allows(row: sqlite3.Row, auth: MediaAssetAuth) -> bool:
        policy = row["visibility_policy"]
        if policy not in {"public", "participants", "author_only"}:
            raise MediaAssetIntegrityError("A stored media audience policy is not registered")
        expected_prefix = "audio/" if row["media_kind"] == "speech" else "image/"
        if row["media_kind"] not in {"speech", "image"} or not row["content_type"].startswith(expected_prefix):
            raise MediaAssetIntegrityError("A stored media kind does not match its declared type")
        try:
            raw_subjects = json.loads(row["subject_ids_json"])
            if not isinstance(raw_subjects, list) or any(not isinstance(item, str) for item in raw_subjects):
                raise ValueError("audience is not a string array")
            subject_ids = {UUID(item) for item in raw_subjects}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaAssetIntegrityError("A stored media audience grant is malformed") from exc
        if len(subject_ids) > 256 or len(subject_ids) != len(raw_subjects):
            raise MediaAssetIntegrityError("A stored media audience has invalid cardinality or duplicates")
        if (policy == "participants") != bool(subject_ids):
            raise MediaAssetIntegrityError("A stored media audience does not match its visibility policy")
        if policy == "author_only" and not row["author_user_id"]:
            raise MediaAssetIntegrityError("An author-only media grant has no creator identity")
        if policy != "author_only" and row["author_user_id"] is not None:
            raise MediaAssetIntegrityError("A non-author media grant unexpectedly names an author")
        if policy == "public":
            return True
        if policy == "participants":
            return (
                auth.membership_role != MembershipRole.OBSERVER
                and bool(subject_ids.intersection(auth.verified_subject_ids))
            )
        if policy == "author_only":
            return (
                auth.membership_role in {MembershipRole.OWNER, MembershipRole.EDITOR}
                and row["author_user_id"] == str(auth.scope.user_id)
            )
        raise MediaAssetIntegrityError("A stored media audience policy is not registered")

    def _request_fingerprint(self, request: MediaRequest) -> str:
        material = request.model_dump(mode="json")
        material["visibility"]["subject_ids"] = sorted(material["visibility"]["subject_ids"])
        encoded = self._json(material).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _same_grant(row: sqlite3.Row, values: tuple[object, ...]) -> bool:
        # INSERT column order above. The first four values are the primary key.
        columns = (
            "tenant_id", "world_id", "branch_id", "request_id", "content_hash",
            "content_type", "byte_size", "media_kind", "visibility_policy",
            "subject_ids_json", "source_event_ids_json", "recipient_id",
            "author_user_id", "creator_user_id", "request_fingerprint",
        )
        return all(row[column] == value for column, value in zip(columns, values, strict=True))

    @staticmethod
    def _stored_result(request: MediaRequest, receipt: GeneratedMedia) -> StoredMediaAsset:
        return StoredMediaAsset(
            request_id=request.request_id,
            world_id=request.world_id,
            branch_id=request.branch_id,
            source_event_ids=request.source_event_ids,
            content_hash=receipt.content_hash,
            content_type=receipt.content_type,
            byte_size=receipt.byte_size,
            visibility=request.visibility,
        )

    @staticmethod
    def _mime_matches(content_type: str, data: bytes) -> bool:
        if content_type == "image/png":
            return MediaAssetStore._has_png_signature(data)
        if content_type == "image/jpeg":
            return len(data) >= 4 and data[:3] == b"\xff\xd8\xff" and data.endswith(b"\xff\xd9")
        if content_type == "image/webp":
            return (
                len(data) >= 12
                and data[:4] == b"RIFF"
                and data[8:12] == b"WEBP"
                and int.from_bytes(data[4:8], "little") + 8 == len(data)
            )
        if content_type == "audio/wav":
            return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        if content_type == "audio/ogg":
            return len(data) >= 27 and data[:4] == b"OggS" and data[4] == 0
        if content_type == "audio/mpeg":
            return MediaAssetStore._has_mpeg_signature(data)
        return False

    @staticmethod
    def _has_png_signature(data: bytes) -> bool:
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        offset = 8
        saw_header = False
        saw_data = False
        while offset + 12 <= len(data):
            chunk_size = int.from_bytes(data[offset : offset + 4], "big")
            chunk_start = offset + 4
            chunk_end = chunk_start + 4 + chunk_size + 4
            if chunk_end > len(data):
                return False
            chunk_type = data[chunk_start : chunk_start + 4]
            chunk_data = data[chunk_start + 4 : chunk_start + 4 + chunk_size]
            expected_crc = int.from_bytes(data[chunk_end - 4 : chunk_end], "big")
            if (zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF) != expected_crc:
                return False
            if not saw_header:
                if chunk_type != b"IHDR" or chunk_size != 13:
                    return False
                saw_header = True
            elif chunk_type == b"IHDR":
                return False
            if chunk_type == b"IDAT":
                saw_data = True
            offset = chunk_end
            if chunk_type == b"IEND":
                return chunk_size == 0 and saw_data and offset == len(data)
        return False

    @staticmethod
    def _has_mpeg_signature(data: bytes) -> bool:
        if data.startswith(b"ID3"):
            return len(data) >= 10 and (data[3] in {2, 3, 4}) and (data[4] == 0)
        if len(data) < 4 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
            return False
        layer = (data[1] >> 1) & 0x03
        bitrate_index = (data[2] >> 4) & 0x0F
        sample_rate_index = (data[2] >> 2) & 0x03
        return layer != 0 and bitrate_index not in {0, 0x0F} and sample_rate_index != 0x03

    def _publish_or_verify_blob(self, digest: str, content_type: str, data: bytes) -> None:
        directory = self._blob_directory(digest, create=True)
        target = directory / digest
        if os.path.lexists(target):
            existing = self._read_verified_blob(digest, content_type=content_type, byte_size=len(data))
            if existing != data:
                raise MediaAssetIntegrityError("The existing content-addressed blob differs from the verified bytes")
            return

        descriptor = -1
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".media-", suffix=".tmp", dir=directory)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Hard-link publication is atomic and refuses to replace an existing
                # target, unlike os.replace. Temp and final paths share a directory.
                os.link(temporary, target)
            except FileExistsError:
                existing = self._read_verified_blob(digest, content_type=content_type, byte_size=len(data))
                if existing != data:
                    raise MediaAssetIntegrityError("A competing writer published different blob bytes")
            except OSError as exc:
                raise MediaAssetStorageError(
                    "The filesystem cannot atomically publish an immutable media blob"
                ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        self._read_verified_blob(digest, content_type=content_type, byte_size=len(data))

    def _read_verified_blob(self, digest: str, *, content_type: str, byte_size: int) -> bytes:
        if not _DIGEST_RE.fullmatch(digest):
            raise MediaAssetIntegrityError("Stored media digest is malformed")
        directory = self._blob_directory(digest, create=False)
        target = directory / digest
        try:
            info = target.lstat()
        except OSError as exc:
            raise MediaAssetIntegrityError("An authorized media grant has no readable blob") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MediaAssetIntegrityError("A media blob path is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                file_info = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_info.st_mode):
                    raise MediaAssetIntegrityError("A media blob path is not a regular file")
                payload = stream.read(self.maximum_asset_bytes + 1)
        except MediaAssetError:
            raise
        except OSError as exc:
            raise MediaAssetIntegrityError("An authorized media blob cannot be opened") from exc
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)

        if len(payload) != byte_size or len(payload) > self.maximum_asset_bytes:
            raise MediaAssetIntegrityError("Stored media byte size does not match its grant")
        if sha256(payload).hexdigest() != digest:
            raise MediaAssetIntegrityError("Stored media digest does not match its content address")
        if not self._mime_matches(content_type, payload):
            raise MediaAssetIntegrityError("Stored media bytes do not match their declared type")
        return payload

    def _blob_directory(self, digest: str, *, create: bool) -> Path:
        directory = self.blob_root / digest[:2]
        if create:
            self._ensure_directory(directory)
        else:
            self._check_directory_chain(directory)
        return directory

    def _ensure_directory(self, directory: Path) -> None:
        try:
            relative = directory.relative_to(self.root)
        except ValueError as exc:
            raise MediaAssetStorageError("A derived media directory escaped the configured root") from exc
        current = self.root
        try:
            current.mkdir(mode=0o700, parents=True, exist_ok=True)
            if current.is_symlink() or not current.is_dir():
                raise MediaAssetStorageError("The configured media root is not a safe directory")
            self._restrict_directory(current)
            for component in relative.parts:
                current = current / component
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise MediaAssetStorageError("A media directory contains a symlink or non-directory")
                self._restrict_directory(current)
        except MediaAssetError:
            raise
        except OSError as exc:
            raise MediaAssetStorageError("The isolated media directory could not be prepared") from exc

    def _check_directory_chain(self, directory: Path) -> None:
        try:
            relative = directory.relative_to(self.root)
        except ValueError as exc:
            raise MediaAssetStorageError("A derived media directory escaped the configured root") from exc
        current = self.root
        try:
            root_info = current.lstat()
        except OSError as exc:
            raise MediaAssetIntegrityError("The isolated media root is missing") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise MediaAssetIntegrityError("The isolated media root is not a safe directory")
        for component in relative.parts:
            current = current / component
            try:
                info = current.lstat()
            except OSError as exc:
                raise MediaAssetIntegrityError("An authorized media blob directory is missing") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise MediaAssetIntegrityError("A media blob directory is not safe")

    def _reject_symlink_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise MediaAssetStorageError("The media index path cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MediaAssetStorageError("The media index must be a regular file, not a symlink")

    def _connect(self) -> sqlite3.Connection:
        self._reject_symlink_file(self.index_path)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.index_path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=30000")
            self._restrict_file(self.index_path)
            return connection
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise MediaAssetStorageError("The media grant index could not be opened") from exc

    def _initialize_index(self) -> None:
        try:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, _SCHEMA_VERSION}:
                    raise MediaAssetStorageError("The media grant index schema version is unsupported")
                if version == 0:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS media_asset_grants (
                            tenant_id TEXT NOT NULL,
                            world_id TEXT NOT NULL,
                            branch_id TEXT NOT NULL,
                            request_id TEXT NOT NULL,
                            content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                            content_type TEXT NOT NULL,
                            byte_size INTEGER NOT NULL CHECK(byte_size > 0),
                            media_kind TEXT NOT NULL CHECK(media_kind IN ('speech','image')),
                            visibility_policy TEXT NOT NULL CHECK(
                                visibility_policy IN ('public','participants','author_only')
                            ),
                            subject_ids_json TEXT NOT NULL,
                            source_event_ids_json TEXT NOT NULL,
                            recipient_id TEXT,
                            author_user_id TEXT,
                            creator_user_id TEXT NOT NULL,
                            request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64),
                            PRIMARY KEY (tenant_id, world_id, branch_id, request_id)
                        );
                        CREATE INDEX IF NOT EXISTS media_asset_hash_scope_idx
                            ON media_asset_grants (tenant_id, world_id, branch_id, content_hash);
                        PRAGMA user_version=1;
                        """
                    )
                quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
                if quick_check is None or quick_check[0] != "ok":
                    raise MediaAssetIntegrityError("The media grant index failed its integrity check")
            finally:
                connection.close()
        except MediaAssetError:
            raise
        except sqlite3.Error as exc:
            raise MediaAssetStorageError("The media grant index could not be initialized") from exc

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _restrict_directory(path: Path) -> None:
        if os.name != "nt":
            os.chmod(path, 0o700)

    @staticmethod
    def _restrict_file(path: Path) -> None:
        if os.name != "nt" and path.exists():
            os.chmod(path, 0o600)
