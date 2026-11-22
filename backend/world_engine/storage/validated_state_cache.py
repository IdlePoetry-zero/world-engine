"""Small, integrity-checked cache for validated branch head projections.

Cache hits still fingerprint the exact JSON value read from storage and compare
the stored digest, state version, and tick. Cold reads and any mismatch fully
validate and hash the payload. A trusted publisher is available to callers that
have just committed a validated state and its canonical payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import RLock
from time import monotonic
from uuid import UUID
from sys import getsizeof

from world_engine.domain.models import WorldState
from world_engine.domain.reducers import state_hash


_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_MAX_ENTRIES = 2
DEFAULT_MAX_SERIALIZED_BYTES = 24 * 1024 * 1024


def _raw_text_fingerprint(raw_state_json: str) -> str | None:
    if not isinstance(raw_state_json, str):
        return None
    try:
        return hashlib.sha256(raw_state_json.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


class StoredBranchStateIntegrityError(ValueError):
    """Stored branch state is malformed or does not match its row metadata."""


class StoredPromiseTrustIntegrityError(StoredBranchStateIntegrityError):
    """Stored promise-trust state belongs to a different world."""


@dataclass(frozen=True, slots=True)
class PlayerHistoryContextProjection:
    """Immutable, state-free projection used by authorized player history."""

    world_id: UUID
    branch_id: UUID
    head_seq: int
    state_version: int
    current_tick: int
    state_hash: str
    raw_fingerprint: str
    player_id: UUID | None
    entity_names: tuple[tuple[UUID, str], ...]
    estimated_bytes: int


@dataclass(frozen=True, slots=True)
class PendingPlayerHistoryContext:
    """A context projection held privately until its long action is finished."""

    job_id: UUID
    projection: PlayerHistoryContextProjection


@dataclass(frozen=True, slots=True)
class PlayerHistoryContextCacheInfo:
    entries: int
    pending_candidates: int
    retained_bytes: int
    recent_branches: int
    hits: int
    misses: int


class PlayerHistoryContextCache:
    """Small synchronized cache of exact, validated player-history primitives.

    This cache never retains a ``WorldState`` or raw state payload. Its branch
    entries contain only the default player ID, immutable UUID/name pairs and
    exact committed-head metadata. A separate bounded pending slot may hold a
    terminal long-action candidate until the worker records its durable status.
    """

    def __init__(
        self,
        *,
        max_entries: int = 2,
        max_bytes: int = 1_000_000,
        recent_window_seconds: float = 600.0,
        max_recent_branches: int = 64,
    ) -> None:
        if isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if recent_window_seconds <= 0:
            raise ValueError("recent_window_seconds must be positive")
        if isinstance(max_recent_branches, bool) or max_recent_branches < 1:
            raise ValueError("max_recent_branches must be a positive integer")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.recent_window_seconds = recent_window_seconds
        self.max_recent_branches = max_recent_branches
        self._lock = RLock()
        self._entries: OrderedDict[tuple[UUID, UUID], PlayerHistoryContextProjection] = OrderedDict()
        self._pending: OrderedDict[UUID, PendingPlayerHistoryContext] = OrderedDict()
        self._recent: OrderedDict[tuple[UUID, UUID], float] = OrderedDict()
        self._retained_bytes = 0
        self._hits = 0
        self._misses = 0

    def note_authorized_read(self, world_id: UUID, branch_id: UUID) -> None:
        identity = (world_id, branch_id)
        now = monotonic()
        with self._lock:
            self._prune_recent(now)
            self._recent[identity] = now
            self._recent.move_to_end(identity)
            while len(self._recent) > self.max_recent_branches:
                self._recent.popitem(last=False)

    def has_recent_authorized_read(self, world_id: UUID, branch_id: UUID) -> bool:
        identity = (world_id, branch_id)
        now = monotonic()
        with self._lock:
            self._prune_recent(now)
            accessed = self._recent.get(identity)
            if accessed is None or now - accessed > self.recent_window_seconds:
                self._recent.pop(identity, None)
                return False
            self._recent.move_to_end(identity)
            return True

    def lookup(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        head_seq: int,
        state_version: int,
        current_tick: int,
        state_hash: str,
        raw_state_json: str,
    ) -> tuple[UUID | None, dict[UUID, str]] | None:
        fingerprint = _raw_text_fingerprint(raw_state_json)
        identity = (world_id, branch_id)
        with self._lock:
            entry = self._entries.get(identity)
            if entry is not None and (
                entry.head_seq == head_seq
                and entry.state_version == state_version
                and entry.current_tick == current_tick
                and entry.state_hash == state_hash
                and fingerprint is not None
                and entry.raw_fingerprint == fingerprint
            ):
                self._entries.move_to_end(identity)
                self._hits += 1
                return entry.player_id, dict(entry.entity_names)
            self._misses += 1
            if entry is not None:
                self._remove_entry(identity)
            return None

    def remember_verified_context(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        head_seq: int,
        state_version: int,
        current_tick: int,
        state_hash: str,
        raw_state_json: str,
        player_id: UUID | None,
        entity_names: Mapping[UUID, str],
    ) -> bool:
        projection = self._build_projection(
            world_id=world_id, branch_id=branch_id, head_seq=head_seq,
            state_version=state_version, current_tick=current_tick, state_hash=state_hash,
            raw_fingerprint=_raw_text_fingerprint(raw_state_json), player_id=player_id,
            entity_names=entity_names,
        )
        return self._remember_projection(projection) if projection is not None else False

    def build_terminal_candidate(
        self,
        *,
        job_id: UUID,
        world_id: UUID,
        branch_id: UUID,
        head_seq: int,
        state_version: int,
        current_tick: int,
        state_hash: str,
        raw_fingerprint: str | None,
        player_id: UUID | None,
        entity_names: Mapping[UUID, str],
    ) -> PendingPlayerHistoryContext | None:
        if not self.has_recent_authorized_read(world_id, branch_id):
            return None
        projection = self._build_projection(
            world_id=world_id, branch_id=branch_id, head_seq=head_seq,
            state_version=state_version, current_tick=current_tick, state_hash=state_hash,
            raw_fingerprint=raw_fingerprint, player_id=player_id, entity_names=entity_names,
        )
        if projection is None:
            return None
        return PendingPlayerHistoryContext(job_id=job_id, projection=projection)

    def retain_terminal_candidate(self, candidate: PendingPlayerHistoryContext) -> bool:
        projection = candidate.projection
        identity = (projection.world_id, projection.branch_id)
        if projection.estimated_bytes > self.max_bytes:
            return False
        with self._lock:
            self._remove_entry(identity)
            for pending_id, pending in tuple(self._pending.items()):
                if (pending_id == candidate.job_id
                        or (pending.projection.world_id, pending.projection.branch_id) == identity):
                    self._remove_pending(pending_id)
            self._pending[candidate.job_id] = candidate
            self._retained_bytes += projection.estimated_bytes
            self._trim()
            return candidate.job_id in self._pending

    def take_terminal_candidate(self, job_id: UUID) -> PendingPlayerHistoryContext | None:
        with self._lock:
            candidate = self._pending.pop(job_id, None)
            if candidate is not None:
                self._retained_bytes -= candidate.projection.estimated_bytes
            return candidate

    def promote_verified_candidate(self, candidate: PendingPlayerHistoryContext) -> bool:
        return self._remember_projection(candidate.projection)

    def discard_terminal_candidate(self, job_id: UUID) -> None:
        self.take_terminal_candidate(job_id)

    def cache_info(self) -> PlayerHistoryContextCacheInfo:
        with self._lock:
            self._prune_recent(monotonic())
            return PlayerHistoryContextCacheInfo(
                entries=len(self._entries), pending_candidates=len(self._pending),
                retained_bytes=self._retained_bytes, recent_branches=len(self._recent),
                hits=self._hits, misses=self._misses,
            )

    def _remember_projection(self, projection: PlayerHistoryContextProjection | None) -> bool:
        if projection is None or projection.estimated_bytes > self.max_bytes:
            return False
        identity = (projection.world_id, projection.branch_id)
        with self._lock:
            pending_ids = [
                job_id for job_id, candidate in self._pending.items()
                if (candidate.projection.world_id, candidate.projection.branch_id) == identity
            ]
            for job_id in pending_ids:
                self._remove_pending(job_id)
            existing = self._entries.get(identity)
            if existing is not None and existing.state_version > projection.state_version:
                return False
            self._remove_entry(identity)
            self._entries[identity] = projection
            self._retained_bytes += projection.estimated_bytes
            self._trim()
            return identity in self._entries

    def _build_projection(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        head_seq: int,
        state_version: int,
        current_tick: int,
        state_hash: str,
        raw_fingerprint: str | None,
        player_id: UUID | None,
        entity_names: Mapping[UUID, str],
    ) -> PlayerHistoryContextProjection | None:
        if raw_fingerprint is None or _HASH_PATTERN.fullmatch(raw_fingerprint) is None:
            return None
        if type(head_seq) is not int or head_seq < 0:
            return None
        try:
            ValidatedBranchStateCache._validate_metadata(
                world_id, branch_id, state_hash, state_version, current_tick,
            )
            pre_estimate = 512 + getsizeof(()) + sum(
                getsizeof((entity_id, name)) + getsizeof(entity_id) + getsizeof(name)
                for entity_id, name in entity_names.items()
            )
        except Exception:
            return None
        if pre_estimate > self.max_bytes:
            return None
        if player_id is not None and not isinstance(player_id, UUID):
            return None
        if any(not isinstance(entity_id, UUID) or not isinstance(name, str) for entity_id, name in entity_names.items()):
            return None
        names = tuple(sorted(entity_names.items(), key=lambda item: str(item[0])))
        estimated_bytes = 512 + getsizeof(names) + sum(
            getsizeof(pair) + getsizeof(pair[0]) + getsizeof(pair[1]) for pair in names
        )
        if estimated_bytes > self.max_bytes:
            return None
        return PlayerHistoryContextProjection(
            world_id=world_id, branch_id=branch_id, head_seq=head_seq,
            state_version=state_version, current_tick=current_tick, state_hash=state_hash,
            raw_fingerprint=raw_fingerprint, player_id=player_id,
            entity_names=names, estimated_bytes=estimated_bytes,
        )

    def _trim(self) -> None:
        while len(self._entries) + len(self._pending) > self.max_entries or self._retained_bytes > self.max_bytes:
            if self._entries:
                self._remove_entry(next(iter(self._entries)))
            elif self._pending:
                self._remove_pending(next(iter(self._pending)))
            else:
                break

    def _remove_entry(self, identity: tuple[UUID, UUID]) -> None:
        entry = self._entries.pop(identity, None)
        if entry is not None:
            self._retained_bytes -= entry.estimated_bytes

    def _remove_pending(self, job_id: UUID) -> None:
        candidate = self._pending.pop(job_id, None)
        if candidate is not None:
            self._retained_bytes -= candidate.projection.estimated_bytes

    def _prune_recent(self, now: float) -> None:
        for identity, accessed in tuple(self._recent.items()):
            if now - accessed > self.recent_window_seconds:
                self._recent.pop(identity, None)


@dataclass(frozen=True, slots=True)
class ValidatedStateCacheInfo:
    entries: int
    serialized_bytes: int
    hits: int
    misses: int


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    fingerprint: str
    stored_digest: str
    state_version: int
    current_tick: int
    serialized_bytes: int
    state: WorldState
    raw_fingerprint: str | None = None


class ValidatedBranchStateCache:
    """Bounded thread-safe LRU cache keyed by exact branch-head contents.

    ``get_or_validate`` returns a detached WorldState and whether it was a hit.
    Mutating the returned model or the original input payload cannot mutate the
    cached value. The mapping path fingerprints canonical JSON, while the
    database text path also remembers the exact UTF-8 representation.
    Both remain separate from the canonical domain digest, so any raw change
    forces parsing and verified canonical lookup before the text hash is trusted.

    ``publish_trusted`` is only for a post-write call site that already holds a
    fully validated state and knows the supplied payload is that state's exact
    canonical JSON representation and that the row write succeeded. It checks
    the payload digest and tick before caching. It does not perform full state
    validation.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_serialized_bytes: int = DEFAULT_MAX_SERIALIZED_BYTES,
    ) -> None:
        if isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if isinstance(max_serialized_bytes, bool) or max_serialized_bytes < 1:
            raise ValueError("max_serialized_bytes must be a positive integer")
        self.max_entries = max_entries
        self.max_serialized_bytes = max_serialized_bytes
        self._lock = RLock()
        self._entries: OrderedDict[tuple[UUID, UUID], _CacheEntry] = OrderedDict()
        self._serialized_bytes = 0
        self._hits = 0
        self._misses = 0

    def get_or_validate(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_payload: Mapping[str, object],
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> tuple[WorldState, bool]:
        """Return a detached state, validating the full payload on any miss."""

        state, cache_hit = self._get_or_validate_state(
            world_id=world_id,
            branch_id=branch_id,
            raw_payload=raw_payload,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            copy_for_cache=True,
        )
        if cache_hit:
            return state.model_copy(deep=True), True
        return state, False

    def get_or_validate_history_context(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_payload: Mapping[str, object],
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> tuple[UUID | None, dict[UUID, str]]:
        """Return detached player-history primitives without copying a cached graph."""

        state, _cache_hit = self._get_or_validate_state(
            world_id=world_id,
            branch_id=branch_id,
            raw_payload=raw_payload,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            copy_for_cache=False,
        )
        return self._history_context(world_id, state)

    def get_or_validate_history_context_from_text(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_state_json: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> tuple[UUID | None, dict[UUID, str]]:
        """Read a history projection from exact DB text, skipping decode on a verified hit."""

        state = self.borrow_verified_state_from_text(
            world_id=world_id,
            branch_id=branch_id,
            raw_state_json=raw_state_json,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
        )
        return self._history_context(world_id, state)

    def get_or_validate_player_id_from_text(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_state_json: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> UUID | None:
        """Return only the verified default player ID from exact database text."""

        state = self.borrow_verified_state_from_text(
            world_id=world_id,
            branch_id=branch_id,
            raw_state_json=raw_state_json,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
        )
        return state.player_id

    def borrow_verified_state_from_text(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_state_json: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> WorldState:
        """Borrow verified state for trusted, synchronous read-only projection.

        The caller must treat the entire graph as read-only and keep its
        references within the projection.
        """

        return self._get_or_validate_state_from_text(
            world_id=world_id,
            branch_id=branch_id,
            raw_state_json=raw_state_json,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            detached=False,
        )

    def get_or_validate_detached_state_from_text(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_state_json: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> WorldState:
        """Return a verified detached state, reusing exact database text when cached."""

        return self._get_or_validate_state_from_text(
            world_id=world_id,
            branch_id=branch_id,
            raw_state_json=raw_state_json,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            detached=True,
        )

    def _get_or_validate_state_from_text(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_state_json: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
        detached: bool,
    ) -> WorldState:
        """Resolve, verify, and optionally detach state from exact database JSON."""

        self._validate_metadata(world_id, branch_id, stored_digest, state_version, current_tick)
        if not isinstance(raw_state_json, str):
            raise StoredBranchStateIntegrityError("Stored branch projection must be JSON text.")
        try:
            raw_bytes = raw_state_json.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StoredBranchStateIntegrityError("Stored branch projection is not valid UTF-8 text.") from exc
        raw_fingerprint = hashlib.sha256(raw_bytes).hexdigest()
        identity = (world_id, branch_id)
        with self._lock:
            entry = self._entries.get(identity)
            if entry is not None and self._matches_raw(
                entry, raw_fingerprint, stored_digest, state_version, current_tick,
            ):
                self._entries.move_to_end(identity)
                self._hits += 1
                cached_state = entry.state
            else:
                cached_state = None

        if cached_state is not None:
            self._validate_promise_trust(world_id, cached_state)
            return self._copy_state(cached_state) if detached else cached_state

        try:
            snapshot = json.loads(raw_state_json)
            if not isinstance(snapshot, dict):
                raise TypeError("branch state must be a JSON object")
            encoded = self._encode_payload(snapshot)
        except StoredBranchStateIntegrityError:
            raise
        except Exception as exc:
            raise StoredBranchStateIntegrityError("Stored branch projection is not valid world state JSON.") from exc

        state, cache_hit = self._get_or_validate_encoded_state(
            world_id=world_id,
            branch_id=branch_id,
            encoded=encoded,
            validate_json=True,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            copy_for_cache=False,
        )
        self._validate_promise_trust(world_id, state)
        self._bind_raw_fingerprint(
            identity, state, raw_fingerprint, stored_digest, state_version, current_tick,
        )
        # Another reader may still hold a borrowed reference after an eviction.
        # A state that could have been cached must stay detached for public reads.
        if detached and (cache_hit or len(encoded) <= self.max_serialized_bytes):
            return self._copy_state(state)
        return state

    @staticmethod
    def _validate_promise_trust(world_id: UUID, state: WorldState) -> None:
        if state.promise_trust is not None and state.promise_trust.get("world_id") != str(world_id):
            raise StoredPromiseTrustIntegrityError("Stored promise trust projection belongs to another world.")

    @classmethod
    def _history_context(cls, world_id: UUID, state: WorldState) -> tuple[UUID | None, dict[UUID, str]]:
        cls._validate_promise_trust(world_id, state)
        return state.player_id, {entity_id: entity.name for entity_id, entity in state.entities.items()}

    def _bind_raw_fingerprint(
        self,
        identity: tuple[UUID, UUID],
        state: WorldState,
        raw_fingerprint: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> None:
        with self._lock:
            entry = self._entries.get(identity)
            if (
                entry is not None
                and entry.state is state
                and entry.stored_digest == stored_digest
                and entry.state_version == state_version
                and entry.current_tick == current_tick
            ):
                self._entries[identity] = replace(entry, raw_fingerprint=raw_fingerprint)

    def _get_or_validate_state(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_payload: Mapping[str, object],
        stored_digest: str,
        state_version: int,
        current_tick: int,
        copy_for_cache: bool,
    ) -> tuple[WorldState, bool]:
        """Return an internal state; public callers must detach it before returning."""

        self._validate_metadata(world_id, branch_id, stored_digest, state_version, current_tick)
        encoded = self._encode_payload(raw_payload)
        return self._get_or_validate_encoded_state(
            world_id=world_id,
            branch_id=branch_id,
            encoded=encoded,
            validate_json=False,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            copy_for_cache=copy_for_cache,
        )

    def _get_or_validate_encoded_state(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        encoded: bytes,
        validate_json: bool,
        stored_digest: str,
        state_version: int,
        current_tick: int,
        copy_for_cache: bool,
    ) -> tuple[WorldState, bool]:
        self._validate_metadata(world_id, branch_id, stored_digest, state_version, current_tick)
        fingerprint = hashlib.sha256(encoded).hexdigest()
        identity = (world_id, branch_id)
        with self._lock:
            entry = self._entries.get(identity)
            if entry is not None and self._matches(
                entry, fingerprint, stored_digest, state_version, current_tick,
            ):
                self._entries.move_to_end(identity)
                self._hits += 1
                cached_state = entry.state
            else:
                self._misses += 1
                cached_state = None

        if cached_state is not None:
            return cached_state, True

        try:
            if validate_json:
                state = WorldState.model_validate_json(encoded)
            else:
                snapshot = json.loads(encoded.decode("utf-8"))
                if not isinstance(snapshot, dict):
                    raise TypeError("branch state must be a JSON object")
                state = self._validate_payload(snapshot)
        except Exception as exc:
            raise StoredBranchStateIntegrityError("Stored branch projection is not valid world state JSON.") from exc

        if state_hash(state) != stored_digest or state.current_tick != current_tick:
            raise StoredBranchStateIntegrityError("Stored branch projection failed its integrity check.")

        if len(encoded) <= self.max_serialized_bytes:
            self._insert(
                identity,
                _CacheEntry(
                    fingerprint=fingerprint,
                    stored_digest=stored_digest,
                    state_version=state_version,
                    current_tick=current_tick,
                    serialized_bytes=len(encoded),
                    state=self._copy_state(state) if copy_for_cache else state,
                ),
            )
        return state, False

    def publish_trusted(
        self,
        *,
        world_id: UUID,
        branch_id: UUID,
        raw_payload: Mapping[str, object],
        stored_digest: str,
        state_version: int,
        current_tick: int,
        state: WorldState,
    ) -> bool:
        """Publish a just-written, prevalidated state; return false if too large.

        The caller must invoke this only after the DB write succeeds and only
        with the exact canonical payload/digest and already validated state
        produced for that write. A mismatch is rejected instead of cached.
        """

        self._validate_metadata(world_id, branch_id, stored_digest, state_version, current_tick)
        encoded = self._encode_payload(raw_payload)
        fingerprint = hashlib.sha256(encoded).hexdigest()
        if fingerprint != stored_digest or state.current_tick != current_tick:
            raise StoredBranchStateIntegrityError("Trusted state publication does not match the written branch head.")
        if len(encoded) > self.max_serialized_bytes:
            return False
        entry = _CacheEntry(
            fingerprint=fingerprint,
            stored_digest=stored_digest,
            state_version=state_version,
            current_tick=current_tick,
            serialized_bytes=len(encoded),
            state=self._copy_state(state),
        )
        self._insert((world_id, branch_id), entry)
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._serialized_bytes = 0

    def cache_info(self) -> ValidatedStateCacheInfo:
        with self._lock:
            return ValidatedStateCacheInfo(
                entries=len(self._entries),
                serialized_bytes=self._serialized_bytes,
                hits=self._hits,
                misses=self._misses,
            )

    @staticmethod
    def _validate_payload(payload: dict[str, object]) -> WorldState:
        return WorldState.model_validate(payload)

    @staticmethod
    def _copy_state(state: WorldState) -> WorldState:
        return state.model_copy(deep=True)

    @staticmethod
    def _encode_payload(payload: Mapping[str, object]) -> bytes:
        if not isinstance(payload, Mapping):
            raise StoredBranchStateIntegrityError("Stored branch projection must be a JSON object.")
        try:
            return json.dumps(
                dict(payload), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoredBranchStateIntegrityError("Stored branch projection is not canonical JSON.") from exc

    @staticmethod
    def _validate_metadata(
        world_id: UUID,
        branch_id: UUID,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> None:
        if not isinstance(world_id, UUID) or not isinstance(branch_id, UUID):
            raise StoredBranchStateIntegrityError("Branch identity is invalid.")
        if not isinstance(stored_digest, str) or _HASH_PATTERN.fullmatch(stored_digest) is None:
            raise StoredBranchStateIntegrityError("Stored branch digest is invalid.")
        if isinstance(state_version, bool) or not isinstance(state_version, int) or state_version < 0:
            raise StoredBranchStateIntegrityError("Stored branch version is invalid.")
        if (
            isinstance(current_tick, bool) or not isinstance(current_tick, int)
            or not -(2**63) <= current_tick <= 2**63 - 1
        ):
            raise StoredBranchStateIntegrityError("Stored branch tick is invalid.")

    @staticmethod
    def _matches(
        entry: _CacheEntry,
        fingerprint: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> bool:
        return (
            entry.fingerprint == fingerprint
            and entry.stored_digest == stored_digest
            and entry.state_version == state_version
            and entry.current_tick == current_tick
        )

    @staticmethod
    def _matches_raw(
        entry: _CacheEntry,
        raw_fingerprint: str,
        stored_digest: str,
        state_version: int,
        current_tick: int,
    ) -> bool:
        return (
            entry.raw_fingerprint == raw_fingerprint
            and entry.stored_digest == stored_digest
            and entry.state_version == state_version
            and entry.current_tick == current_tick
        )

    def _insert(self, identity: tuple[UUID, UUID], entry: _CacheEntry) -> None:
        if entry.serialized_bytes > self.max_serialized_bytes:
            return
        with self._lock:
            existing = self._entries.get(identity)
            if existing is not None and existing.state_version >= entry.state_version:
                if self._matches(
                    existing, entry.fingerprint, entry.stored_digest,
                    entry.state_version, entry.current_tick,
                ):
                    self._entries.move_to_end(identity)
                return
            self._remove(identity)
            self._entries[identity] = entry
            self._serialized_bytes += entry.serialized_bytes
            while (
                len(self._entries) > self.max_entries
                or self._serialized_bytes > self.max_serialized_bytes
            ):
                oldest_identity = next(iter(self._entries))
                self._remove(oldest_identity)

    def _remove(self, identity: tuple[UUID, UUID]) -> None:
        entry = self._entries.pop(identity, None)
        if entry is not None:
            self._serialized_bytes -= entry.serialized_bytes
