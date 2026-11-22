from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event, text
from sqlalchemy.orm import Session

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.models import AccessScope, CommandEnvelope, JobStatus, TimeAdvancedEvent
from world_engine.simulation import LongActionPlan
from world_engine.simulation.engine import SimulationEngine, SimulationPolicy
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository
from world_engine.storage.validated_state_cache import PlayerHistoryContextCache
import world_engine.storage.repository as repository_module


@pytest.fixture
def world_env(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'history-cache.db').as_posix()}"
    database = Database(database_url)
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    draft = ContentCatalog().build_world("long_life_cultivation", "History cache", "Traveler")
    created = repository.create_world(scope, draft)
    try:
        yield database, database_url, repository, scope, draft, created.world.world_id, created.branch.branch_id
    finally:
        database.dispose()


def _player_history(repository, scope, world_id, branch_id):
    session = LocalSession(
        str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}),
    )
    app = create_app(WorldApplication(repository, None), session)
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        response = client.get(
            f"/api/v1/worlds/{world_id}/history",
            params={"branch_id": str(branch_id), "mode": "player"},
        )
        response.raise_for_status()
        return response.json()


def _finish_two_window_rest(repository, scope, draft, world_id, branch_id, *, after_first_window=None):
    jobs = SimulationJobs(
        repository, SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=1800)),
    )
    job = jobs.enqueue(
        scope, world_id, branch_id,
        LongActionPlan(
            actor_id=draft.state.player_id, started_tick=0, target_tick=3600, activity="rest",
        ),
        idempotency_key="history-cache-rest", speed="balanced", depth="deep",
    )
    lease = repository.claim_job("history-cache-worker")
    assert lease is not None and lease.job_id == job.job_id
    first = jobs.advance(scope, lease)
    assert first.outcome == "advanced"
    assert repository.read_job(scope, job.job_id).status is JobStatus.RUNNING
    if after_first_window is not None:
        after_first_window(job, first)
    final = jobs.advance(scope, lease)
    assert final.outcome == "completed"
    assert repository.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED
    return job, final


def test_terminal_long_action_publishes_exact_player_history_context_and_restart_is_independent(world_env, monkeypatch):
    database, database_url, repository, scope, draft, world_id, branch_id = world_env

    # This authenticated HTTP read opts this branch into the bounded terminal
    # publication path. Intermediate windows must not retain a candidate.
    before = _player_history(repository, scope, world_id, branch_id)
    cache = repository._player_history_context_cache
    assert cache.cache_info().entries == 1
    intermediate_checks = []
    original_publish = repository.publish_terminal_player_history_context_candidate

    def publish_only_after_durable_finish(access, finished):
        durable = repository.read_job(access, finished.job_id)
        assert durable.status is finished.status is JobStatus.SUCCEEDED
        assert cache.cache_info().pending_candidates == 1
        intermediate_checks.append("finished")
        return original_publish(access, finished)

    monkeypatch.setattr(
        repository, "publish_terminal_player_history_context_candidate", publish_only_after_durable_finish,
    )

    def assert_no_intermediate_publication(_job, _resolution):
        branch = repository.read_world(scope, world_id, branch_id).branch
        info = cache.cache_info()
        assert info.pending_candidates == 0
        assert info.entries == 1
        cached = cache._entries[(world_id, branch_id)]
        assert cached.state_version < branch.state_version
        intermediate_checks.append("intermediate")

    job, _ = _finish_two_window_rest(
        repository, scope, draft, world_id, branch_id, after_first_window=assert_no_intermediate_publication,
    )
    assert intermediate_checks == ["intermediate", "finished"]
    assert cache.cache_info().pending_candidates == 0
    assert cache.cache_info().entries == 1

    hits_before = cache.cache_info().hits
    terminal_response = _player_history(repository, scope, world_id, branch_id)
    assert terminal_response["mode"] == before["mode"] == "player"
    assert cache.cache_info().hits == hits_before + 1
    assert repository.replay(scope, world_id, branch_id).state_hash == repository.read_world(
        scope, world_id, branch_id,
    ).state_hash

    # A newly opened repository has no retained projection. It validates the
    # persisted head and must produce the exact same authenticated response.
    database.dispose()
    reopened_database = Database(database_url)
    try:
        reopened = WorldRepository(reopened_database)
        assert reopened._player_history_context_cache.cache_info().entries == 0
        assert _player_history(reopened, scope, world_id, branch_id) == terminal_response
        assert reopened._player_history_context_cache.cache_info().hits == 0
        assert reopened.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED
    finally:
        reopened_database.dispose()


def test_inactive_branch_does_not_build_or_retain_terminal_context(world_env, monkeypatch):
    _database, _database_url, repository, scope, draft, world_id, branch_id = world_env
    cache = repository._player_history_context_cache
    called = False

    def fail_if_built(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("inactive history branch must not build a candidate")

    monkeypatch.setattr(cache, "build_terminal_candidate", fail_if_built)
    _finish_two_window_rest(repository, scope, draft, world_id, branch_id)
    assert called is False
    info = cache.cache_info()
    assert info.entries == 0 and info.pending_candidates == 0 and info.retained_bytes == 0


def test_exact_raw_bytes_must_match_before_cached_context_is_used(world_env, monkeypatch):
    _database, _database_url, repository, scope, draft, world_id, branch_id = world_env
    _player_history(repository, scope, world_id, branch_id)
    _finish_two_window_rest(repository, scope, draft, world_id, branch_id)
    cache = repository._player_history_context_cache
    assert cache.cache_info().entries == 1

    validated = 0
    original = repository._branch_state_cache.get_or_validate_history_context_from_text

    def count_validation(**kwargs):
        nonlocal validated
        validated += 1
        return original(**kwargs)

    monkeypatch.setattr(
        repository._branch_state_cache,
        "get_or_validate_history_context_from_text",
        count_validation,
    )
    # Whitespace preserves JSON meaning and canonical state hash but changes
    # the exact persisted byte representation.
    with repository._transaction() as session:
        before_branch, raw_before = repository._get_branch_with_state_text(session, world_id, branch_id)
        state_hash_before = before_branch.state_hash
    with repository._transaction(write=True) as session:
        result = session.execute(text(
            "UPDATE world_branches SET state_json = state_json || ' ' WHERE branch_id = :branch_id",
        ), {"branch_id": branch_id.hex})
        assert result.rowcount == 1
    with repository._transaction() as session:
        after_branch, raw_after = repository._get_branch_with_state_text(session, world_id, branch_id)
        assert after_branch.state_hash == state_hash_before
        assert raw_after == raw_before + " "

    _, player_id, entity_names = repository.read_player_history_context(scope, world_id, branch_id)
    assert player_id == draft.state.player_id
    assert entity_names[draft.state.player_id] == "Traveler"
    assert validated == 1
    assert cache.cache_info().misses >= 1


def test_terminal_publish_declines_head_advanced_before_readback(world_env, monkeypatch):
    database, _database_url, repository, scope, draft, world_id, branch_id = world_env
    _player_history(repository, scope, world_id, branch_id)
    external = WorldRepository(database)
    original_publish = repository.publish_terminal_player_history_context_candidate

    def advance_elsewhere_then_publish(access, job):
        view = external.read_world(access, world_id, branch_id)
        external.commit_command(
            access, world_id, branch_id,
            CommandEnvelope(
                actor_id=draft.state.player_id,
                idempotency_key="external-head-advance",
                expected_state_version=view.branch.state_version,
                command_type="core.tick",
            ),
            [TimeAdvancedEvent(to_tick=view.state.current_tick + 1)],
        )
        return original_publish(access, job)

    monkeypatch.setattr(repository, "publish_terminal_player_history_context_candidate", advance_elsewhere_then_publish)
    _job, _resolution = _finish_two_window_rest(repository, scope, draft, world_id, branch_id)
    info = repository._player_history_context_cache.cache_info()
    assert info.pending_candidates == 0 and info.entries == 0
    before_misses = info.misses
    _, player_id, names = repository.read_player_history_context(scope, world_id, branch_id)
    assert player_id == draft.state.player_id and names[player_id] == "Traveler"
    assert repository._player_history_context_cache.cache_info().misses == before_misses + 1


def test_concurrent_head_change_after_readback_cannot_make_stale_context_a_hit(world_env, monkeypatch):
    database, _database_url, repository, scope, draft, world_id, branch_id = world_env
    _player_history(repository, scope, world_id, branch_id)
    external = WorldRepository(database)
    cache = repository._player_history_context_cache
    original_promote = cache.promote_verified_candidate
    interleaved = False

    def advance_between_readback_and_publish(candidate):
        nonlocal interleaved
        view = external.read_world(scope, world_id, branch_id)
        external.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=draft.state.player_id,
                idempotency_key="interleaved-head-advance",
                expected_state_version=view.branch.state_version,
                command_type="core.tick",
            ),
            [TimeAdvancedEvent(to_tick=view.state.current_tick + 1)],
        )
        interleaved = True
        return original_promote(candidate)

    monkeypatch.setattr(cache, "promote_verified_candidate", advance_between_readback_and_publish)
    _finish_two_window_rest(repository, scope, draft, world_id, branch_id)
    assert interleaved
    # The interleaving can leave an old projection in the process-local LRU,
    # but its row metadata and raw digest no longer match; lookup evicts it and
    # uses the full validated-state path for the current head.
    validations = 0
    original = repository._branch_state_cache.get_or_validate_history_context_from_text

    def count_validation(**kwargs):
        nonlocal validations
        validations += 1
        return original(**kwargs)

    monkeypatch.setattr(
        repository._branch_state_cache,
        "get_or_validate_history_context_from_text",
        count_validation,
    )
    _, player_id, names = repository.read_player_history_context(scope, world_id, branch_id)
    assert player_id == draft.state.player_id and names[player_id] == "Traveler"
    assert validations == 1
    assert cache.cache_info().entries == 1


def test_failed_terminal_commit_never_publishes_a_context_candidate(world_env, monkeypatch):
    _database, _database_url, repository, scope, draft, world_id, branch_id = world_env
    _player_history(repository, scope, world_id, branch_id)
    jobs = SimulationJobs(
        repository, SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=1800)),
    )
    job = jobs.enqueue(
        scope, world_id, branch_id,
        LongActionPlan(actor_id=draft.state.player_id, started_tick=0, target_tick=3600, activity="rest"),
        idempotency_key="history-cache-commit-failure", speed="balanced", depth="deep",
    )
    lease = repository.claim_job("history-cache-worker")
    assert lease is not None
    assert jobs.advance(scope, lease).outcome == "advanced"

    publish_calls = []
    original_publish = repository.publish_terminal_player_history_context_candidate

    def record_publish(*args, **kwargs):
        publish_calls.append(True)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(repository, "publish_terminal_player_history_context_candidate", record_publish)

    registered = []
    removed = []

    class EventListenerTracker:
        def listen(self, target, identifier, listener):
            registered.append((target, identifier, listener))
            sa_event.listen(target, identifier, listener)

        def remove(self, target, identifier, listener):
            removed.append((target, identifier, listener))
            sa_event.remove(target, identifier, listener)

    monkeypatch.setattr(repository_module, "sqlalchemy_event", EventListenerTracker())
    original_execute = Session.execute
    interrupted = False

    def fail_terminal_branch_update(self, statement, *args, **kwargs):
        nonlocal interrupted
        if not interrupted and str(statement).lstrip().lower().startswith("update world_branches"):
            interrupted = True
            raise RuntimeError("simulated terminal commit failure")
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail_terminal_branch_update)
    with pytest.raises(RuntimeError, match="simulated terminal commit failure"):
        jobs.advance(scope, lease)
    assert interrupted
    assert len(registered) == len(removed) == 1
    assert registered[0] == removed[0]
    assert repository.read_job(scope, job.job_id).status is JobStatus.FAILED
    info = repository._player_history_context_cache.cache_info()
    assert info.pending_candidates == 0 and info.entries == 1
    assert publish_calls == []


def test_failed_durable_finish_discards_pending_candidate_without_publication(world_env, monkeypatch):
    _database, _database_url, repository, scope, draft, world_id, branch_id = world_env
    _player_history(repository, scope, world_id, branch_id)
    jobs = SimulationJobs(
        repository, SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=1800)),
    )
    job = jobs.enqueue(
        scope, world_id, branch_id,
        LongActionPlan(actor_id=draft.state.player_id, started_tick=0, target_tick=3600, activity="rest"),
        idempotency_key="history-cache-finish-failure", speed="balanced", depth="deep",
    )
    lease = repository.claim_job("history-cache-worker")
    assert lease is not None
    assert jobs.advance(scope, lease).outcome == "advanced"

    original_finish = repository.finish_job
    original_publish = repository.publish_terminal_player_history_context_candidate
    failed_once = False
    publish_calls = []

    def fail_first_success_finish(current_lease, status, **kwargs):
        nonlocal failed_once
        if not failed_once and status is JobStatus.SUCCEEDED:
            failed_once = True
            raise RuntimeError("simulated durable finish failure")
        return original_finish(current_lease, status, **kwargs)

    def record_publish(*args, **kwargs):
        publish_calls.append(True)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(repository, "finish_job", fail_first_success_finish)
    monkeypatch.setattr(repository, "publish_terminal_player_history_context_candidate", record_publish)
    with pytest.raises(RuntimeError, match="simulated durable finish failure"):
        jobs.advance(scope, lease)
    assert failed_once
    assert repository.read_job(scope, job.job_id).status is JobStatus.FAILED
    info = repository._player_history_context_cache.cache_info()
    assert info.pending_candidates == 0 and info.entries == 0
    assert publish_calls == []


def test_history_projection_cache_is_bounded_by_entries_and_bytes():
    cache = PlayerHistoryContextCache(max_entries=2, max_bytes=100_000)
    branches = [(uuid4(), uuid4()) for _ in range(3)]
    for index, (world_id, branch_id) in enumerate(branches):
        assert cache.remember_verified_context(
            world_id=world_id,
            branch_id=branch_id,
            head_seq=0,
            state_version=0,
            current_tick=0,
            state_hash=f"{index + 1:064x}",
            raw_state_json='{"state":%d}' % index,
            player_id=uuid4(),
            entity_names={uuid4(): "Person"},
        )
    info = cache.cache_info()
    assert info.entries == 2
    assert info.pending_candidates == 0
    assert 0 < info.retained_bytes <= 100_000
    assert cache.lookup(
        world_id=branches[0][0], branch_id=branches[0][1],
        head_seq=0, state_version=0, current_tick=0,
        state_hash=f"{1:064x}", raw_state_json='{"state":0}',
    ) is None

    pending_cache = PlayerHistoryContextCache(max_entries=2, max_bytes=100_000)
    for index, (world_id, branch_id) in enumerate(branches):
        job_id = uuid4()
        pending_cache.note_authorized_read(world_id, branch_id)
        candidate = pending_cache.build_terminal_candidate(
            job_id=job_id, world_id=world_id, branch_id=branch_id,
            head_seq=1, state_version=1, current_tick=1,
            state_hash=f"{index + 11:064x}", raw_fingerprint=f"{index + 21:064x}",
            player_id=uuid4(), entity_names={uuid4(): "Person"},
        )
        assert candidate is not None and pending_cache.retain_terminal_candidate(candidate)
    pending_info = pending_cache.cache_info()
    assert pending_info.entries == 0
    assert pending_info.pending_candidates == 2
    assert 0 < pending_info.retained_bytes <= 100_000
