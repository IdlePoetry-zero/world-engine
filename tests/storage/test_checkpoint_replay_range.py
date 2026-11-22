from uuid import uuid4

import pytest

from world_engine.domain.errors import Conflict, NotFound
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.storage.database import Database
from world_engine.storage.models import BranchRow, SnapshotRow
from world_engine.storage.repository import WorldRepository
from sqlalchemy import select


@pytest.fixture
def replay_world(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'checkpoint-replay.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    actor_id = uuid4()
    created = repository.create_world(
        scope,
        NewWorld(
            title="Checkpoint replay",
            state=WorldState(
                player_id=actor_id,
                entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Auditor")},
            ),
        ),
    )
    try:
        yield database, repository, scope, created, actor_id
    finally:
        database.dispose()


def _advance(repository, scope, created, actor_id, to_tick, *, branch_id=None, key=None):
    target_branch_id = branch_id or created.branch.branch_id
    branch = repository.read_world(scope, created.world.world_id, target_branch_id).branch
    return repository.commit_command(
        scope,
        created.world.world_id,
        target_branch_id,
        CommandEnvelope(
            actor_id=actor_id,
            idempotency_key=key or f"tick-{target_branch_id}-{to_tick}",
            expected_state_version=branch.state_version,
            command_type="core.tick",
        ),
        [TimeAdvancedEvent(to_tick=to_tick)],
    )


def test_replay_from_exact_checkpoint_visits_checkpoint_and_each_later_commit_once(replay_world):
    _, repository, scope, created, actor_id = replay_world
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    _advance(repository, scope, created, actor_id, 10)
    _advance(repository, scope, created, actor_id, 20)
    checkpoint = repository.snapshot(scope, world_id, branch_id, at_tick=20)
    _advance(repository, scope, created, actor_id, 30)
    end_checkpoint = repository.snapshot(scope, world_id, branch_id)

    visited = []
    replayed = repository.replay_from_checkpoint(
        scope,
        world_id,
        branch_id,
        checkpoint_seq=checkpoint.last_seq,
        through_seq=end_checkpoint.last_seq,
        checkpoint_hash=checkpoint.state_hash,
        visit_commit=lambda sequence, state: visited.append((sequence, state.current_tick)),
    )

    assert visited == [(checkpoint.last_seq, 20), (end_checkpoint.last_seq, 30)]
    assert replayed.last_seq == end_checkpoint.last_seq
    assert replayed.state_hash == end_checkpoint.state_hash
    assert replayed.state.current_tick == 30

    same_boundary_visits = []
    same_boundary = repository.replay_from_checkpoint(
        scope,
        world_id,
        branch_id,
        checkpoint_seq=checkpoint.last_seq,
        through_seq=checkpoint.last_seq,
        checkpoint_hash=checkpoint.state_hash,
        visit_commit=lambda sequence, state: same_boundary_visits.append((sequence, state.current_tick)),
    )
    assert same_boundary_visits == [(checkpoint.last_seq, 20)]
    assert same_boundary.state_hash == checkpoint.state_hash


def test_replay_from_ancestor_checkpoint_crosses_fork_without_duplicate_boundary(replay_world):
    _, repository, scope, created, actor_id = replay_world
    world_id, source_branch_id = created.world.world_id, created.branch.branch_id
    _advance(repository, scope, created, actor_id, 10)
    ancestor_checkpoint = repository.snapshot(scope, world_id, source_branch_id, at_tick=10)
    _advance(repository, scope, created, actor_id, 20)
    child = repository.fork(scope, world_id, source_branch_id, 2, "alternate")
    _advance(repository, scope, created, actor_id, 30, branch_id=child.branch_id)
    # A later source-branch commit is outside the child's visible history.
    _advance(repository, scope, created, actor_id, 40)

    visited = []
    replayed = repository.replay_from_checkpoint(
        scope,
        world_id,
        child.branch_id,
        checkpoint_seq=ancestor_checkpoint.last_seq,
        through_seq=3,
        checkpoint_hash=ancestor_checkpoint.state_hash,
        visit_commit=lambda sequence, state: visited.append((sequence, state.current_tick)),
    )
    assert visited == [(1, 10), (2, 20), (3, 30)]
    assert replayed.state.current_tick == 30
    assert replayed.state_hash == repository.replay(scope, world_id, child.branch_id).state_hash

    with repository._transaction() as session:
        fork_row = session.scalar(
            select(SnapshotRow).where(
                SnapshotRow.branch_id == child.branch_id,
                SnapshotRow.last_seq == child.fork_seq,
            )
        )
        assert fork_row is not None
        fork_checkpoint = repository._snapshot_record(fork_row)
    fork_visits = []
    fork_replay = repository.replay_from_checkpoint(
        scope,
        world_id,
        child.branch_id,
        checkpoint_seq=fork_checkpoint.last_seq,
        through_seq=3,
        checkpoint_hash=fork_checkpoint.state_hash,
        visit_commit=lambda sequence, state: fork_visits.append((sequence, state.current_tick)),
    )
    assert fork_visits == [(2, 20), (3, 30)]
    assert fork_replay.state_hash == replayed.state_hash


def test_replay_from_checkpoint_rejects_bad_pin_scope_and_head_hash(replay_world):
    database, repository, scope, created, actor_id = replay_world
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    _advance(repository, scope, created, actor_id, 10)
    checkpoint = repository.snapshot(scope, world_id, branch_id, at_tick=10)
    _advance(repository, scope, created, actor_id, 20)

    with pytest.raises(Conflict, match="Checkpoint hash does not match"):
        repository.replay_from_checkpoint(
            scope,
            world_id,
            branch_id,
            checkpoint_seq=checkpoint.last_seq,
            through_seq=2,
            checkpoint_hash="0" * 64,
        )
    with pytest.raises(Conflict, match="No persisted checkpoint"):
        repository.replay_from_checkpoint(
            scope,
            world_id,
            branch_id,
            checkpoint_seq=0,
            through_seq=2,
            checkpoint_hash="0" * 64,
        )
    with pytest.raises(NotFound):
        repository.replay_from_checkpoint(
            AccessScope(tenant_id=uuid4(), user_id=uuid4()),
            world_id,
            branch_id,
            checkpoint_seq=checkpoint.last_seq,
            through_seq=2,
            checkpoint_hash=checkpoint.state_hash,
        )

    with repository._transaction(write=True) as session:
        session.get(BranchRow, branch_id).state_hash = "0" * 64
    with pytest.raises(Conflict, match="Branch head differs"):
        repository.replay_from_checkpoint(
            scope,
            world_id,
            branch_id,
            checkpoint_seq=checkpoint.last_seq,
            through_seq=2,
            checkpoint_hash=checkpoint.state_hash,
        )
