from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from world_engine.application.media_assets import MediaAssetAuth, MediaAssetStore
from world_engine.application.media_authorization import RepositoryMediaAuthorizer
from world_engine.application.media_contracts import (
    AssetScope,
    AuthorizedMediaSource,
    GeneratedMedia,
    MediaManifest,
    MediaRegistry,
    MediaSubscription,
)
from world_engine.application.media_jobs import MediaJobEnqueuer, MediaJobWorker
from world_engine.domain.errors import AccessDenied
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EventRecord,
    JobStatus,
    MembershipRole,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
    utc_now,
)
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


_WAV = b"RIFF\x04\x00\x00\x00WAVE"


class _Plugin:
    def __init__(self, data: bytes, *, on_fetch=None, delay_seconds=0):
        self.data = data
        self.calls = []
        self.on_fetch = on_fetch
        self.delay_seconds = delay_seconds

    async def generate(self, request):
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.calls.append(request)
        return GeneratedMedia(
            request_id=request.request_id,
            content_hash=sha256(self.data).hexdigest(),
            content_type="audio/wav",
            byte_size=len(self.data),
        )

    async def fetch(self, request, receipt, *, maximum_bytes):
        if self.on_fetch is not None:
            self.on_fetch()
        return self.data


class _Authorizer:
    def __init__(self, *, allow=True):
        self.allow = allow
        self.calls = 0

    def authorize(self, job, plan):
        self.calls += 1
        if not self.allow:
            raise AccessDenied("The source or audience has been revoked")
        request = plan.request
        return MediaAssetAuth(
            scope=AccessScope(
                tenant_id=job.tenant_id,
                user_id=UUID(job.payload["submitted_by"]),
            ),
            world_id=job.world_id,
            branch_id=job.branch_id,
            membership_role=MembershipRole.OWNER,
            verified_subject_ids=frozenset(request.visibility.subject_ids),
            trusted_host=True,
        )


def _source(world_id, branch_id, *, recipient_id, visibility=None):
    event_id = uuid4()
    visibility = visibility or AssetScope(policy="public")
    return AuthorizedMediaSource(
        event=EventRecord(
            event_id=event_id,
            world_id=world_id,
            branch_id=branch_id,
            sequence=1,
            tick=0,
            type="core.market_trade_executed.v1",
            command_id=uuid4(),
            actor_id=uuid4(),
            ruleset_version="1.0.0",
            payload={"private_detail": "never forward this raw event payload"},
            visibility=(
                {"policy": "private", "subject_ids": [str(item) for item in visibility.subject_ids]}
                if visibility.policy == "participants"
                else {"policy": "public"}
            ),
            state_version_after=1,
            commit_boundary=True,
            committed_at=utc_now(),
        ),
        recipient_id=recipient_id,
        authorized_description="The merchant completes a brief public exchange.",
        visibility=visibility,
    )


def _services(tmp_path, *, on_fetch=None, visibility=None):
    db = Database(f"sqlite:///{(tmp_path / 'worlds.sqlite3').as_posix()}")
    db.create_schema()
    repository = WorldRepository(db)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    created = repository.create_world(
        scope,
        NewWorld(title="Media worker", state=WorldState()),
    )
    store = MediaAssetStore(tmp_path / "media")
    plugin = _Plugin(_WAV, on_fetch=on_fetch)
    registry = MediaRegistry()
    registry.register(
        MediaManifest(
            id="local.voice",
            version="1.0",
            capabilities={"speech"},
            subscriptions=[MediaSubscription(event_type="core.market_trade_executed.v1", kind="speech")],
            maximum_asset_bytes=len(_WAV),
        ),
        plugin,
        artifact_transport=plugin,
    )
    source = _source(
        created.world.world_id,
        created.branch.branch_id,
        recipient_id=(visibility.subject_ids[0] if visibility else scope.user_id),
        visibility=visibility,
    )
    return db, repository, scope, created, store, plugin, registry, source


@pytest.mark.asyncio
async def test_media_worker_enqueues_idempotently_and_does_not_mutate_world(tmp_path):
    db, repository, scope, created, store, plugin, registry, source = _services(tmp_path)
    try:
        before = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        enqueuer = MediaJobEnqueuer(repository, registry)
        jobs = enqueuer.enqueue_source(scope, source)
        repeated = enqueuer.enqueue_source(scope, source)
        assert len(jobs) == len(repeated) == 1
        assert jobs[0].job_id == repeated[0].job_id
        assert jobs[0].job_type == "media.generate"
        assert "never forward this raw event payload" not in str(jobs[0].payload)
        assert "secret" not in jobs[0].payload["planned"]["request"]["authorized_description"]

        worker = MediaJobWorker(repository, registry, store, _Authorizer())
        completed = await worker.process_one("media-worker-a")
        assert completed is not None
        assert completed.status == JobStatus.SUCCEEDED, completed.result
        assert completed.result["asset"]["world_id"] == str(created.world.world_id)
        assert completed.result["asset"]["branch_id"] == str(created.branch.branch_id)
        assert len(plugin.calls) == 1
        assert store.lookup_request(jobs[0].payload["planned"]["request"], MediaAssetAuth(
            scope=scope,
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            membership_role=MembershipRole.OWNER,
            trusted_host=True,
        )) is not None
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert (after.state_hash, after.last_seq, after.branch.state_version) == (
            before.state_hash,
            before.last_seq,
            before.branch.state_version,
        )
    finally:
        db.dispose()


@pytest.mark.asyncio
async def test_media_worker_recovers_store_commit_before_job_finish_without_regeneration(tmp_path):
    db, repository, scope, created, store, plugin, registry, source = _services(tmp_path)
    try:
        job = MediaJobEnqueuer(repository, registry).enqueue_source(scope, source)[0]
        started = utc_now()
        claim = repository.claim_media_job("media-worker-crash", now=started)
        assert claim is not None and claim.job.job_id == job.job_id
        worker = MediaJobWorker(repository, registry, store, _Authorizer())
        original_finish = repository.finish_media_job

        def crash_after_publication(lease, status, *, result=None, publish=None, now=None):
            if status == JobStatus.SUCCEEDED and publish is not None:
                publish()
                raise RuntimeError("simulated process termination after durable asset publication")
            return original_finish(lease, status, result=result, publish=publish, now=now)

        repository.finish_media_job = crash_after_publication
        with pytest.raises(RuntimeError, match="simulated process termination"):
            await worker.process_claim(claim)
        assert len(plugin.calls) == 1

        repository.finish_media_job = original_finish
        retry = repository.claim_media_job(
            "media-worker-recovery",
            now=started + timedelta(seconds=61),
        )
        assert retry is not None and retry.job.job_id == job.job_id
        completed = await worker.process_claim(retry)
        assert completed.status == JobStatus.SUCCEEDED
        assert len(plugin.calls) == 1
        assert completed.result["asset"]["content_hash"] == sha256(_WAV).hexdigest()
    finally:
        db.dispose()


@pytest.mark.asyncio
async def test_media_worker_rechecks_audience_after_generation_and_publishes_nothing_on_revoke(tmp_path):
    subject_id = uuid4()
    holder = {}

    def revoke_after_generation():
        holder["authorizer"].allow = False

    visibility = AssetScope(policy="participants", subject_ids=(subject_id,))
    db, repository, scope, created, store, plugin, registry, source = _services(
        tmp_path,
        on_fetch=revoke_after_generation,
        visibility=visibility,
    )
    try:
        job = MediaJobEnqueuer(repository, registry).enqueue_source(scope, source)[0]
        authorizer = _Authorizer()
        holder["authorizer"] = authorizer
        worker = MediaJobWorker(repository, registry, store, authorizer)

        completed = await worker.process_one("media-worker-revoked")

        assert completed is not None and completed.status == JobStatus.FAILED
        assert completed.result == {"reason_code": "MEDIA_AUTHORIZATION_REVOKED"}
        assert authorizer.calls == 2
        assert len(plugin.calls) == 1
        with sqlite3.connect(store.index_path) as connection:
            assert connection.execute(
                "SELECT count(*) FROM media_asset_grants WHERE request_id=?",
                (str(job.payload["planned"]["request"]["request_id"]),),
            ).fetchone()[0] == 0
    finally:
        db.dispose()


@pytest.mark.asyncio
async def test_media_worker_reauthorizes_the_exact_recorded_branch_event(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'recorded.sqlite3').as_posix()}")
    db.create_schema()
    repository = WorldRepository(db)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    actor_id = uuid4()
    created = repository.create_world(
        scope,
        NewWorld(
            title="Recorded media source",
            state=WorldState(
                player_id=actor_id,
                entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Mira")},
            ),
        ),
    )
    store = MediaAssetStore(tmp_path / "recorded-media")
    plugin = _Plugin(_WAV)
    registry = MediaRegistry()
    registry.register(
        MediaManifest(
            id="recorded-voice",
            version="1.0",
            capabilities={"speech"},
            subscriptions=[MediaSubscription(event_type="core.time_advanced.v1", kind="speech")],
            maximum_asset_bytes=len(_WAV),
        ),
        plugin,
        artifact_transport=plugin,
    )
    try:
        committed = repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            CommandEnvelope(
                actor_id=actor_id,
                idempotency_key="media-source-tick",
                expected_state_version=0,
                command_type="core.tick",
            ),
            [TimeAdvancedEvent(to_tick=5, visibility={"policy": "author_only"})],
        )
        source = AuthorizedMediaSource(
            event=committed.events[0],
            recipient_id=scope.user_id,
            authorized_description="The owner records the passing of time.",
            visibility=AssetScope(policy="author_only"),
        )
        job = MediaJobEnqueuer(repository, registry).enqueue_source(scope, source)[0]
        before = repository.read_world(scope, created.world.world_id, created.branch.branch_id)

        completed = await MediaJobWorker(
            repository,
            registry,
            store,
            RepositoryMediaAuthorizer(repository),
        ).process_one("media-worker-recorded")

        assert completed is not None and completed.job_id == job.job_id
        assert completed.status == JobStatus.SUCCEEDED
        assert len(plugin.calls) == 1
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert (after.state_hash, after.last_seq) == (before.state_hash, before.last_seq)
    finally:
        db.dispose()


@pytest.mark.asyncio
async def test_media_worker_renews_lease_during_bounded_plugin_call(tmp_path):
    db, repository, scope, created, store, plugin, registry, source = _services(tmp_path)
    plugin.delay_seconds = 1.25
    try:
        job = MediaJobEnqueuer(repository, registry).enqueue_source(scope, source)[0]
        worker = MediaJobWorker(
            repository,
            registry,
            store,
            _Authorizer(),
            lease_seconds=1,
            heartbeat_interval_seconds=0.15,
            plugin_timeout_seconds=3,
        )

        completed = await worker.process_one("media-worker-slow-plugin")

        assert completed is not None and completed.job_id == job.job_id
        assert completed.status == JobStatus.SUCCEEDED
        assert completed.attempt_count == 1
        assert len(plugin.calls) == 1
    finally:
        db.dispose()
