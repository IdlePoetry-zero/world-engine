from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest

from world_engine.domain.errors import (
    AccessDenied,
    Conflict,
    InvalidLease,
    IdempotencyConflict,
    InsufficientResources,
    NotFound,
    StaleVersion,
)
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EntityFieldChangedEvent,
    MembershipRole,
    NewWorld,
    NewJob,
    JobStatus,
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
    utc_now,
)
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def world_repo() -> WorldRepository:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        yield repository
    finally:
        database.dispose()


def create_sample_world(repository: WorldRepository):
    tenant_id, owner_id, player_id, npc_id, place_id = (uuid4() for _ in range(5))
    source_account, target_account = uuid4(), uuid4()
    scope = AccessScope(tenant_id=tenant_id, user_id=owner_id)
    created = repository.create_world(
        scope,
        NewWorld(
            title="Tidehaven",
            state=WorldState(
                entities={
                    place_id: WorldEntity(id=place_id, kind="place", name="Harbor"),
                    player_id: WorldEntity(id=player_id, kind="character", name="Mira", location_id=place_id),
                    npc_id: WorldEntity(id=npc_id, kind="character", name="Sora", location_id=place_id),
                },
                player_id=player_id,
                resources={
                    source_account: ResourceAccount(
                        resource_id="core.silver", balance="10", owner_id=player_id
                    ),
                    target_account: ResourceAccount(
                        resource_id="core.silver", balance="0", owner_id=npc_id
                    ),
                },
            ),
        ),
    )
    return scope, created, player_id, npc_id, source_account, target_account


def test_create_read_list_and_membership_scope(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, _, _ = create_sample_world(world_repo)

    view = world_repo.read_world(scope, created.world.world_id)
    assert view.world.title == "Tidehaven"
    assert view.branch.branch_id == created.branch.branch_id
    assert view.state.player_id == player_id
    assert world_repo.list_worlds(scope) == [created.world]
    assert world_repo.list_branches(scope, created.world.world_id) == [created.branch]
    assert world_repo.controlled_actors(scope, created.world.world_id) == sorted(
        [player_id, next(entity_id for entity_id in view.state.entities if entity_id != player_id and view.state.entities[entity_id].kind == "character")],
        key=str,
    )

    with pytest.raises(NotFound):
        world_repo.read_world(AccessScope(tenant_id=uuid4(), user_id=scope.user_id), created.world.world_id)
    with pytest.raises(AccessDenied):
        world_repo.read_world(AccessScope(tenant_id=scope.tenant_id, user_id=uuid4()), created.world.world_id)


def test_commit_is_atomic_and_event_state_ledger_and_outbox_commit_together(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, source_account, target_account = create_sample_world(world_repo)
    command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="transfer-once",
        expected_state_version=0,
        command_type="core.transfer",
        payload={"quantity": "2.5"},
    )
    result = world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        command,
        [
            ResourceTransferredEvent(
                from_account_id=source_account,
                to_account_id=target_account,
                resource_id="core.silver",
                quantity="2.5",
            ),
            TimeAdvancedEvent(to_tick=30),
        ],
    )

    view = world_repo.read_world(scope, created.world.world_id)
    page = world_repo.list_events(scope, created.world.world_id, created.branch.branch_id)
    assert result.state_version == 1
    assert (result.first_seq, result.last_seq) == (1, 2)
    assert view.branch.head_seq == 2
    assert view.state.current_tick == 30
    assert view.state.resources[source_account].balance == Decimal("7.5")
    assert view.state.resources[target_account].balance == Decimal("2.5")
    assert [event.type for event in page.events] == [
        "core.resource_transferred.v1",
        "core.time_advanced.v1",
    ]
    assert all(event.command_id == command.command_id for event in page.events)

    duplicate = world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        command,
        [
            ResourceTransferredEvent(
                from_account_id=source_account,
                to_account_id=target_account,
                resource_id="core.silver",
                quantity="2.5",
            ),
            TimeAdvancedEvent(to_tick=30),
        ],
    )
    assert duplicate.duplicate is True
    assert duplicate.state_version == 1
    assert world_repo.read_world(scope, created.world.world_id).state.resources[source_account].balance == Decimal("7.5")


def test_idempotency_conflict_and_optimistic_version_are_enforced(world_repo: WorldRepository) -> None:
    scope, created, player_id, npc_id, _, _ = create_sample_world(world_repo)
    command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="rename-once",
        expected_state_version=0,
        command_type="core.rename",
        payload={"name": "Mira Chen"},
    )
    world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        command,
        [EntityFieldChangedEvent(entity_id=player_id, field_name="name", value="Mira Chen")],
    )

    retried_with_new_trace = command.model_copy(update={"correlation_id": uuid4()})
    duplicate = world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        retried_with_new_trace,
        [EntityFieldChangedEvent(entity_id=player_id, field_name="name", value="Mira Chen")],
    )
    assert duplicate.duplicate is True

    different_payload = command.model_copy(update={"payload": {"name": "Not Mira"}})
    with pytest.raises(IdempotencyConflict):
        world_repo.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            different_payload,
            [EntityFieldChangedEvent(entity_id=player_id, field_name="name", value="Not Mira")],
        )

    stale = CommandEnvelope(
        actor_id=npc_id,
        idempotency_key="stale-version",
        expected_state_version=0,
        command_type="core.time",
    )
    with pytest.raises(StaleVersion):
        world_repo.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            stale,
            [TimeAdvancedEvent(to_tick=20)],
        )


def test_failed_late_event_rolls_back_earlier_projection_and_rows(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, source_account, target_account = create_sample_world(world_repo)
    command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="rollback-whole-batch",
        expected_state_version=0,
        command_type="core.transfer",
    )
    with pytest.raises(InsufficientResources):
        world_repo.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            command,
            [
                TimeAdvancedEvent(to_tick=99),
                ResourceTransferredEvent(
                    from_account_id=source_account,
                    to_account_id=target_account,
                    resource_id="core.silver",
                    quantity="11",
                ),
            ],
        )

    view = world_repo.read_world(scope, created.world.world_id)
    assert view.branch.head_seq == 0
    assert view.branch.state_version == 0
    assert view.state.current_tick == 0
    assert view.state.resources[source_account].balance == Decimal("10")
    assert world_repo.list_events(scope, created.world.world_id, created.branch.branch_id).events == []


def test_player_role_cannot_commit_for_an_uncontrolled_npc(world_repo: WorldRepository) -> None:
    owner_scope, created, player_id, npc_id, _, _ = create_sample_world(world_repo)
    player_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=uuid4())
    world_repo.grant_membership(
        owner_scope,
        created.world.world_id,
        player_scope.user_id,
        MembershipRole.PLAYER,
        controlled_actor_ids=[player_id],
    )

    allowed = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="controlled-player",
        expected_state_version=0,
        command_type="core.time",
    )
    world_repo.commit_command(
        player_scope,
        created.world.world_id,
        created.branch.branch_id,
        allowed,
        [TimeAdvancedEvent(to_tick=1)],
    )

    denied = CommandEnvelope(
        actor_id=npc_id,
        idempotency_key="uncontrolled-npc",
        expected_state_version=1,
        command_type="core.time",
    )
    with pytest.raises(AccessDenied):
        world_repo.commit_command(
            player_scope,
            created.world.world_id,
            created.branch.branch_id,
            denied,
            [TimeAdvancedEvent(to_tick=2)],
        )


def test_snapshot_fork_and_replay_are_exact_and_isolated(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, _, _ = create_sample_world(world_repo)
    first = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="tick-batch",
        expected_state_version=0,
        command_type="core.tick",
    )
    world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        first,
        [TimeAdvancedEvent(to_tick=5), TimeAdvancedEvent(to_tick=10)],
    )

    with pytest.raises(Conflict, match="complete command commit boundaries"):
        world_repo.replay(scope, created.world.world_id, created.branch.branch_id, through_seq=1)
    with pytest.raises(Conflict, match="complete command commit boundaries"):
        world_repo.fork(scope, created.world.world_id, created.branch.branch_id, fork_seq=1, label="partial")

    saved = world_repo.snapshot(scope, created.world.world_id, created.branch.branch_id)
    same_saved = world_repo.snapshot(scope, created.world.world_id, created.branch.branch_id)
    assert same_saved.snapshot_id == saved.snapshot_id
    assert saved.last_seq == 2
    assert saved.state.current_tick == 10

    child = world_repo.fork(scope, created.world.world_id, created.branch.branch_id, fork_seq=2, label="quiet harbor")
    second = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="parent-later",
        expected_state_version=1,
        command_type="core.tick",
    )
    world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        second,
        [TimeAdvancedEvent(to_tick=20)],
    )
    child_command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="child-tick",
        expected_state_version=1,
        command_type="core.tick",
    )
    world_repo.commit_command(
        scope,
        created.world.world_id,
        child.branch_id,
        child_command,
        [TimeAdvancedEvent(to_tick=12)],
    )

    replayed = world_repo.replay(scope, created.world.world_id, child.branch_id)
    inherited = world_repo.list_events(scope, created.world.world_id, child.branch_id)
    branch_only = world_repo.list_events(
        scope, created.world.world_id, child.branch_id, include_ancestors=False
    )
    assert replayed.state.current_tick == 12
    assert replayed.branch.head_seq == 3
    assert [event.sequence for event in inherited.events] == [1, 2, 3]
    assert [event.sequence for event in branch_only.events] == [3]


def test_job_lease_renews_and_stale_worker_is_fenced(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, _, _ = create_sample_world(world_repo)
    job = world_repo.enqueue_job(
        scope,
        NewJob(
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            job_type="world.advance",
            idempotency_key="advance-job",
            payload={"target_tick": 20},
        ),
    )
    assert job.status == JobStatus.QUEUED
    now = utc_now()
    lease_a = world_repo.claim_job("worker-a", lease_seconds=2, now=now)
    assert lease_a is not None
    assert lease_a.job_id == job.job_id
    renewed = world_repo.renew_job(lease_a, lease_seconds=5, now=now)

    command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="fenced-worker-command",
        expected_state_version=0,
        command_type="core.tick",
    )
    committed = world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        command,
        [TimeAdvancedEvent(to_tick=1)],
        lease=renewed,
    )
    assert committed.state_version == 1

    terminal = world_repo.finish_job(renewed, JobStatus.SUCCEEDED, committed_through_tick=1, now=now)
    assert terminal.status == JobStatus.SUCCEEDED
    with pytest.raises(InvalidLease):
        world_repo.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            command.model_copy(update={"idempotency_key": "stale-fence", "expected_state_version": 1}),
            [TimeAdvancedEvent(to_tick=2)],
            lease=renewed,
        )


def test_competing_sqlite_threads_preserve_version_and_idempotency(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'competing.db').as_posix()}"
    database = Database(database_url)
    database.create_schema()
    repository = WorldRepository(database)
    try:
        # Same key, same semantic request, different request trace IDs: exactly
        # one transaction applies the event and the other returns its receipt.
        scope, created, player_id, _, _, _ = create_sample_world(repository)
        barrier = Barrier(2)

        def same_idempotency_request(index: int) -> str:
            command = CommandEnvelope(
                actor_id=player_id,
                idempotency_key="race-same-key",
                expected_state_version=0,
                command_type="core.tick",
                payload={"target": 1},
                correlation_id=uuid4(),
            )
            barrier.wait(timeout=5)
            result = repository.commit_command(
                scope,
                created.world.world_id,
                created.branch.branch_id,
                command,
                [TimeAdvancedEvent(to_tick=1)],
            )
            return "duplicate" if result.duplicate else "committed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            same_key_results = list(executor.map(same_idempotency_request, range(2)))
        assert sorted(same_key_results) == ["committed", "duplicate"]
        assert repository.read_world(scope, created.world.world_id).branch.state_version == 1
        assert len(repository.list_events(scope, created.world.world_id, created.branch.branch_id).events) == 1

        # Distinct keys cannot both commit from the same expected version.
        scope2, created2, player_id2, _, _, _ = create_sample_world(repository)
        barrier2 = Barrier(2)

        def competing_version_request(index: int) -> str:
            command = CommandEnvelope(
                actor_id=player_id2,
                idempotency_key=f"race-key-{index}",
                expected_state_version=0,
                command_type="core.tick",
            )
            barrier2.wait(timeout=5)
            try:
                repository.commit_command(
                    scope2,
                    created2.world.world_id,
                    created2.branch.branch_id,
                    command,
                    [TimeAdvancedEvent(to_tick=index + 1)],
                )
                return "committed"
            except StaleVersion:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as executor:
            version_results = list(executor.map(competing_version_request, range(2)))
        assert sorted(version_results) == ["committed", "stale"]
        final = repository.read_world(scope2, created2.world.world_id)
        assert final.branch.state_version == 1
        assert final.branch.head_seq == 1
    finally:
        database.dispose()


def test_competing_workers_cannot_claim_the_same_expired_branch_lease(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'lease-race.db').as_posix()}"
    database = Database(database_url)
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope, created, player_id, _, _, _ = create_sample_world(repository)
        job = repository.enqueue_job(
            scope,
            NewJob(
                world_id=created.world.world_id,
                branch_id=created.branch.branch_id,
                job_type="world.advance",
                idempotency_key="expired-lease-race",
            ),
        )
        started_at = utc_now()
        expired_lease = repository.claim_job("worker-original", lease_seconds=1, now=started_at)
        assert expired_lease is not None
        retry_time = started_at + timedelta(seconds=2)
        barrier = Barrier(2)

        def take_expired_job(worker_id: str):
            barrier.wait(timeout=5)
            return repository.claim_job(worker_id, lease_seconds=30, now=retry_time)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(take_expired_job, ["worker-a", "worker-b"]))

        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        assert winners[0].job_id == job.job_id
        assert winners[0].fencing_token == expired_lease.fencing_token + 1

        stale_command = CommandEnvelope(
            actor_id=player_id,
            idempotency_key="old-worker-write",
            expected_state_version=0,
            command_type="core.tick",
        )
        with pytest.raises(InvalidLease):
            repository.commit_command(
                scope,
                created.world.world_id,
                created.branch.branch_id,
                stale_command,
                [TimeAdvancedEvent(to_tick=1)],
                lease=expired_lease,
            )
    finally:
        database.dispose()
