from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Thread
from uuid import uuid4

import pytest

from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import state_hash, state_json
from world_engine.storage.validated_state_cache import (
    StoredBranchStateIntegrityError,
    ValidatedBranchStateCache,
)


def _branch_state():
    world_id, branch_id, actor_id = uuid4(), uuid4(), uuid4()
    state = WorldState(
        player_id=actor_id,
        entities={actor_id: WorldEntity(
            id=actor_id,
            kind="character",
            name="Nara",
        )},
        metadata={"custom": {"value": "original", "nested": ["original"]}},
    )
    raw_payload = state_json(state)
    metadata = {
        "world_id": world_id,
        "branch_id": branch_id,
        "stored_digest": state_hash(state),
        "state_version": 7,
        "current_tick": state.current_tick,
    }
    return state, raw_payload, metadata


def _load(cache: ValidatedBranchStateCache, raw_payload, metadata):
    return cache.get_or_validate(raw_payload=raw_payload, **metadata)


def test_default_cache_keeps_two_large_heads_within_24_mib_budget():
    cache = ValidatedBranchStateCache()

    assert cache.max_entries == 2
    assert cache.max_serialized_bytes == 24 * 1024 * 1024


def test_history_text_fingerprint_skips_json_and_canonical_encoding_on_bound_hit(monkeypatch):
    state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()
    _loaded, cold_hit = _load(cache, raw_payload, metadata)
    assert not cold_hit

    raw_state_json = json.dumps(raw_payload, ensure_ascii=False, indent=2)
    player_id, names = cache.get_or_validate_history_context_from_text(
        world_id=metadata["world_id"],
        branch_id=metadata["branch_id"],
        raw_state_json=raw_state_json,
        stored_digest=metadata["stored_digest"],
        state_version=metadata["state_version"],
        current_tick=metadata["current_tick"],
    )
    assert player_id == state.player_id
    assert names == {entity_id: entity.name for entity_id, entity in state.entities.items()}

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("a bound history text fingerprint should avoid JSON work")

    monkeypatch.setattr(cache, "_encode_payload", unexpected_decode)
    monkeypatch.setattr("world_engine.storage.validated_state_cache.json.loads", unexpected_decode)
    warm_player_id, warm_names = cache.get_or_validate_history_context_from_text(
        world_id=metadata["world_id"],
        branch_id=metadata["branch_id"],
        raw_state_json=raw_state_json,
        stored_digest=metadata["stored_digest"],
        state_version=metadata["state_version"],
        current_tick=metadata["current_tick"],
    )
    assert warm_player_id == player_id and warm_names == names


def test_history_text_path_keeps_legacy_state_hash_normalization():
    state, raw_payload, metadata = _branch_state()
    legacy_payload = deepcopy(raw_payload)
    legacy_payload["investigation_cases"] = []
    legacy_payload["relationship_promises"] = []
    cache = ValidatedBranchStateCache()

    player_id, names = cache.get_or_validate_history_context_from_text(
        world_id=metadata["world_id"],
        branch_id=metadata["branch_id"],
        raw_state_json=json.dumps(legacy_payload, indent=1),
        stored_digest=metadata["stored_digest"],
        state_version=metadata["state_version"],
        current_tick=metadata["current_tick"],
    )
    assert player_id == state.player_id
    assert names == {entity_id: entity.name for entity_id, entity in state.entities.items()}
    assert cache.cache_info().entries == 1


def test_cold_validation_then_hit_returns_deeply_detached_state(monkeypatch: pytest.MonkeyPatch):
    _state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()
    validations = 0
    original_validate = cache._validate_payload

    def counted_validate(payload):
        nonlocal validations
        validations += 1
        return original_validate(payload)

    monkeypatch.setattr(cache, "_validate_payload", counted_validate)
    first, first_hit = _load(cache, raw_payload, metadata)
    assert not first_hit and validations == 1

    first.metadata["custom"]["value"] = "caller mutation"
    first.metadata["custom"]["nested"].append("caller mutation")
    raw_payload["metadata"]["custom"]["value"] = "input mutation"

    # The raw-payload mutation forces full validation and fails against the
    # unchanged digest; the previously returned object did not alter the cache.
    with pytest.raises(StoredBranchStateIntegrityError, match="integrity check"):
        _load(cache, raw_payload, metadata)
    assert validations == 2

    clean_cache = ValidatedBranchStateCache()
    clean_raw = state_json(_state)
    clean, clean_hit = clean_cache.get_or_validate(raw_payload=clean_raw, **metadata)
    assert not clean_hit
    clean.metadata["custom"]["value"] = "caller mutation"
    cached, cache_hit = clean_cache.get_or_validate(raw_payload=clean_raw, **metadata)
    assert cache_hit
    assert cached.metadata["custom"]["value"] == "original"
    assert cached.metadata["custom"]["nested"] == ["original"]


def test_tampered_raw_payload_with_unchanged_row_metadata_never_hits_cache(monkeypatch: pytest.MonkeyPatch):
    _state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()
    validations = 0
    original_validate = cache._validate_payload

    def counted_validate(payload):
        nonlocal validations
        validations += 1
        return original_validate(payload)

    monkeypatch.setattr(cache, "_validate_payload", counted_validate)
    _first, first_hit = _load(cache, raw_payload, metadata)
    assert not first_hit and validations == 1

    tampered = deepcopy(raw_payload)
    actor = str(next(iter(_state.entities)))
    tampered["entities"][actor]["name"] = "Changed in storage"
    with pytest.raises(StoredBranchStateIntegrityError, match="integrity check"):
        _load(cache, tampered, metadata)
    assert validations == 2
    assert cache.cache_info().entries == 1


def test_changed_metadata_forces_full_validation(monkeypatch: pytest.MonkeyPatch):
    _state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()
    validations = 0
    original_validate = cache._validate_payload

    def counted_validate(payload):
        nonlocal validations
        validations += 1
        return original_validate(payload)

    monkeypatch.setattr(cache, "_validate_payload", counted_validate)
    _first, first_hit = _load(cache, raw_payload, metadata)
    _second, second_hit = _load(cache, raw_payload, metadata | {"state_version": metadata["state_version"] + 1})
    assert not first_hit and not second_hit
    assert validations == 2


def test_trusted_post_write_publication_can_seed_a_hot_view(monkeypatch: pytest.MonkeyPatch):
    state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()
    assert cache.publish_trusted(raw_payload=raw_payload, state=state, **metadata)

    def unexpected_validation(_payload):
        raise AssertionError("published state should be reused without cold validation")

    monkeypatch.setattr(cache, "_validate_payload", unexpected_validation)
    cached, cache_hit = _load(cache, raw_payload, metadata)
    assert cache_hit and cached == state and cached is not state


def test_committed_publication_adopts_private_state_and_regular_reads_stay_detached(
    monkeypatch: pytest.MonkeyPatch,
):
    state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()
    encoded_payload = cache._encode_payload(raw_payload)

    def unexpected_copy(_state):
        raise AssertionError("committed publication should transfer its private reducer result")

    monkeypatch.setattr(cache, "_copy_state", unexpected_copy)
    assert cache._publish_committed_state(
        world_id=metadata["world_id"],
        branch_id=metadata["branch_id"],
        encoded_payload=encoded_payload,
        stored_digest=metadata["stored_digest"],
        state_version=metadata["state_version"],
        current_tick=metadata["current_tick"],
        state=state,
    )

    # Normal cache users still receive independent models even though the
    # private post-commit publisher retained the reducer result by reference.
    monkeypatch.undo()
    returned, cache_hit = _load(cache, raw_payload, metadata)
    assert cache_hit and returned is not state
    returned.metadata["custom"]["nested"].append("caller mutation")
    another, again_hit = _load(cache, raw_payload, metadata)
    assert again_hit
    assert another.metadata["custom"]["nested"] == ["original"]


def test_lru_entry_and_serialized_byte_budgets_are_enforced(monkeypatch: pytest.MonkeyPatch):
    cache = ValidatedBranchStateCache(max_entries=2, max_serialized_bytes=1_000_000)
    validations = 0
    original_validate = cache._validate_payload

    def counted_validate(payload):
        nonlocal validations
        validations += 1
        return original_validate(payload)

    monkeypatch.setattr(cache, "_validate_payload", counted_validate)
    states = [_branch_state() for _ in range(3)]
    for _state, raw, metadata in states[:2]:
        _load(cache, raw, metadata)
    _load(cache, states[0][1], states[0][2])  # Promote first entry to MRU.
    _load(cache, states[2][1], states[2][2])  # Evicts second entry.
    _again, evicted_hit = _load(cache, states[1][1], states[1][2])
    assert not evicted_hit and validations == 4
    assert cache.cache_info().entries == 2

    too_small = ValidatedBranchStateCache(max_serialized_bytes=1)
    _one, first_hit = _load(too_small, states[0][1], states[0][2])
    _two, second_hit = _load(too_small, states[0][1], states[0][2])
    assert not first_hit and not second_hit
    assert too_small.cache_info().entries == 0
    assert too_small.cache_info().serialized_bytes == 0


def test_oversized_cold_state_does_not_make_an_unused_cache_copy(monkeypatch: pytest.MonkeyPatch):
    _state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache(max_serialized_bytes=1)
    copies = 0
    original_copy = cache._copy_state

    def counted_copy(state):
        nonlocal copies
        copies += 1
        return original_copy(state)

    monkeypatch.setattr(cache, "_copy_state", counted_copy)
    result, hit = _load(cache, raw_payload, metadata)
    assert not hit and result.entities
    assert copies == 0
    assert cache.cache_info().entries == 0


def test_older_miss_does_not_evict_a_newer_branch_version():
    old_state, old_payload, old_metadata = _branch_state()
    actor_id = next(iter(old_state.entities))
    newer_payload = deepcopy(old_payload)
    newer_payload["entities"][str(actor_id)]["name"] = "Committed version"
    newer_state = WorldState.model_validate(newer_payload)
    newer_metadata = old_metadata | {
        "stored_digest": state_hash(newer_state),
        "state_version": old_metadata["state_version"] + 1,
    }
    cache = ValidatedBranchStateCache()

    committed, committed_hit = _load(cache, newer_payload, newer_metadata)
    stale, stale_hit = _load(cache, old_payload, old_metadata)
    current, current_hit = _load(cache, newer_payload, newer_metadata)

    assert not committed_hit and not stale_hit and current_hit
    assert stale.entities[actor_id].name == "Nara"
    assert committed.entities[actor_id].name == current.entities[actor_id].name == "Committed version"


def test_older_concurrent_miss_finishing_last_does_not_evict_new_entry(monkeypatch: pytest.MonkeyPatch):
    old_state, old_payload, old_metadata = _branch_state()
    actor_id = next(iter(old_state.entities))
    newer_payload = deepcopy(old_payload)
    newer_payload["entities"][str(actor_id)]["name"] = "Committed version"
    newer_state = WorldState.model_validate(newer_payload)
    newer_metadata = old_metadata | {
        "stored_digest": state_hash(newer_state),
        "state_version": old_metadata["state_version"] + 1,
    }
    cache = ValidatedBranchStateCache()
    original_validate = cache._validate_payload
    old_validation_started = Event()
    release_old_validation = Event()

    def delay_old_validation(payload):
        if payload["entities"][str(actor_id)]["name"] == "Nara":
            old_validation_started.set()
            assert release_old_validation.wait(timeout=5)
        return original_validate(payload)

    monkeypatch.setattr(cache, "_validate_payload", delay_old_validation)
    old_result: list[tuple[WorldState, bool]] = []
    old_thread = Thread(target=lambda: old_result.append(_load(cache, old_payload, old_metadata)))
    old_thread.start()
    try:
        assert old_validation_started.wait(timeout=5)
        newer, newer_hit = _load(cache, newer_payload, newer_metadata)
    finally:
        release_old_validation.set()
        old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert not newer_hit
    assert len(old_result) == 1 and old_result[0][0].entities[actor_id].name == "Nara"
    current, current_hit = _load(cache, newer_payload, newer_metadata)
    assert current_hit and current.entities[actor_id].name == newer.entities[actor_id].name == "Committed version"


def test_concurrent_hits_return_independent_world_state_models():
    _state, raw_payload, metadata = _branch_state()
    cache = ValidatedBranchStateCache()

    def read(_index):
        return _load(cache, raw_payload, metadata)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(16)))
    assert len({id(item) for item in results}) == len(results)
    assert all(item.entities[next(iter(item.entities))].name == "Nara" for item in results)
