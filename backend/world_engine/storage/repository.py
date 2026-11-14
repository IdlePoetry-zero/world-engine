"""Transactional repository for worlds, branches, events, and account controls."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from world_engine.domain.errors import (
    AccessDenied,
    Conflict,
    IdempotencyConflict,
    InvalidEvent,
    InvalidLease,
    NotFound,
    StaleVersion,
)
from world_engine.domain.events import event_payload, parse_event
from world_engine.domain.models import (
    AccessScope,
    BranchRecord,
    CommandEnvelope,
    CommitResult,
    DomainEvent,
    EventPage,
    EventRecord,
    JobLease,
    JobRecord,
    JobStatus,
    MembershipRole,
    NewJob,
    OutboxRecord,
    SnapshotRecord,
    NewWorld,
    WorldCreated,
    WorldRecord,
    WorldState,
    WorldView,
    normalize_datetime,
    utc_now,
)
from world_engine.domain.reducers import (
    LedgerPosting,
    apply_event,
    ledger_postings,
    state_hash,
    state_json,
    validate_ledger_postings,
)

from .database import Database
from .models import (
    ActorControlRow,
    Base,
    BranchLeaseRow,
    BranchRow,
    CommandRow,
    EventRow,
    JobRow,
    LedgerPostingRow,
    MembershipRow,
    OutboxRow,
    SnapshotRow,
    WorldRow,
)


MAX_EVENTS_PER_COMMAND = 10_000
MAX_EVENT_PAGE = 500
MUTATING_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR, MembershipRole.PLAYER}
AUTHOR_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR}


class WorldRepository:
    """Synchronous SQLAlchemy repository; each public operation uses a short transaction."""

    def __init__(self, database: Database | Engine | str) -> None:
        if isinstance(database, Database):
            self.database = database
        elif isinstance(database, Engine):
            self.database = Database.__new__(Database)
            self.database.engine = database
            from sqlalchemy.orm import sessionmaker

            self.database.session_factory = sessionmaker(
                bind=database, class_=Session, expire_on_commit=False, autoflush=False
            )
        else:
            self.database = Database(database)

    def create_schema(self) -> None:
        """Create tables for tests and scratch databases; production uses Alembic."""
        Base.metadata.create_all(self.database.engine)

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[Session]:
        """Open a short transaction; reserve SQLite's single writer up front."""
        with self.database.session_factory() as session:
            with session.begin():
                if write and self.database.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session

    def create_world(self, scope: AccessScope, new_world: NewWorld) -> WorldCreated:
        state = WorldState.model_validate(new_world.state.model_dump(mode="json"))
        now = utc_now()
        digest = state_hash(state)
        try:
            with self._transaction(write=True) as session:
                world_row = WorldRow(
                    world_id=new_world.world_id,
                    tenant_id=scope.tenant_id,
                    owner_id=scope.user_id,
                    title=new_world.title,
                    active_branch_id=new_world.branch_id,
                    ruleset_version=new_world.ruleset_version,
                    pack_version=new_world.pack_version,
                    created_at=now,
                )
                session.add(world_row)
                session.flush()
                session.add(
                    MembershipRow(
                        world_id=new_world.world_id,
                        user_id=scope.user_id,
                        tenant_id=scope.tenant_id,
                        role=MembershipRole.OWNER.value,
                        created_at=now,
                    )
                )
                session.add(
                    BranchRow(
                        branch_id=new_world.branch_id,
                        world_id=new_world.world_id,
                        name="main",
                        parent_branch_id=None,
                        fork_seq=0,
                        head_seq=0,
                        state_version=0,
                        initial_state_version=0,
                        current_tick=state.current_tick,
                        initial_state_hash=digest,
                        state_hash=digest,
                        initial_state_json=state_json(state),
                        state_json=state_json(state),
                        created_at=now,
                    )
                )
                if state.player_id is not None:
                    session.add(
                        ActorControlRow(
                            world_id=new_world.world_id,
                            user_id=scope.user_id,
                            actor_id=state.player_id,
                            granted_by=scope.user_id,
                            created_at=now,
                        )
                    )
                session.flush()
                return WorldCreated(
                    world=self._world_record(world_row),
                    branch=self._branch_record(session.get(BranchRow, new_world.branch_id)),
                )
        except IntegrityError as exc:
            raise Conflict("World or branch identifier already exists.") from exc

    def read_world(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID | None = None
    ) -> WorldView:
        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            target_branch_id = branch_id or world.active_branch_id
            branch = self._get_branch(session, world_id, target_branch_id)
            state = self._checked_branch_state(branch)
            return WorldView(
                world=self._world_record(world),
                branch=self._branch_record(branch),
                state=state,
                last_seq=branch.head_seq,
                state_hash=branch.state_hash,
            )

    def list_worlds(self, scope: AccessScope) -> list[WorldRecord]:
        with self._transaction() as session:
            rows = session.scalars(
                select(WorldRow)
                .join(MembershipRow, MembershipRow.world_id == WorldRow.world_id)
                .where(
                    WorldRow.tenant_id == scope.tenant_id,
                    MembershipRow.tenant_id == scope.tenant_id,
                    MembershipRow.user_id == scope.user_id,
                )
                .order_by(WorldRow.created_at, WorldRow.world_id)
            ).all()
            return [self._world_record(row) for row in rows]

    def list_branches(self, scope: AccessScope, world_id: UUID) -> list[BranchRecord]:
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            rows = session.scalars(
                select(BranchRow).where(BranchRow.world_id == world_id).order_by(BranchRow.created_at, BranchRow.name)
            ).all()
            return [self._branch_record(row) for row in rows]

    def list_events(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
        include_ancestors: bool = True,
    ) -> EventPage:
        if after_seq < 0:
            raise ValueError("after_seq must be nonnegative")
        if not 1 <= limit <= MAX_EVENT_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE}")
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id)
            chain = self._branch_chain(session, branch, branch.head_seq, include_ancestors)
            rows: list[EventRow] = []
            for row_branch_id, max_seq in chain:
                rows.extend(
                    session.scalars(
                        select(EventRow).where(
                            EventRow.branch_id == row_branch_id,
                            EventRow.sequence > after_seq,
                            EventRow.sequence <= max_seq,
                        )
                    ).all()
                )
            rows.sort(key=lambda row: row.sequence)
            has_more = len(rows) > limit
            selected = rows[:limit]
            events = [self._event_record(row) for row in selected]
            next_cursor = events[-1].sequence if events else after_seq
            return EventPage(branch_id=branch_id, events=events, next_cursor=next_cursor, has_more=has_more)

    def commit_command(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        command: CommandEnvelope,
        events: Sequence[DomainEvent | Mapping[str, object]],
        *,
        lease: object | None = None,
    ) -> CommitResult:
        """Atomically append typed events, update projection, ledger, command, and outbox."""
        if not events:
            raise InvalidEvent("A committed command must contain at least one domain event.")
        if len(events) > MAX_EVENTS_PER_COMMAND:
            raise InvalidEvent(f"A command may contain no more than {MAX_EVENTS_PER_COMMAND} events.")
        typed_events = tuple(parse_event(event) for event in events)
        event_ids = [event.event_id for event in typed_events]
        if len(set(event_ids)) != len(event_ids):
            raise InvalidEvent("Event IDs must be unique within a command.")
        request_hash = command.payload_hash()

        try:
            with self._transaction(write=True) as session:
                world, role = self._require_membership(session, scope, world_id)
                branch = self._get_branch(session, world_id, branch_id)
                self._require_mutation_role(role)
                self._require_actor_control(session, scope, world_id, role, command.actor_id)

                previous = session.scalar(
                    select(CommandRow).where(
                        CommandRow.branch_id == branch_id,
                        CommandRow.idempotency_key == command.idempotency_key,
                    )
                )
                if previous is not None:
                    return self._idempotent_result(previous, request_hash)

                if command.expected_state_version != branch.state_version:
                    raise StaleVersion(command.expected_state_version, branch.state_version)
                if lease is not None:
                    self._validate_worker_lease(session, lease, world_id, branch_id)
                self._validate_causation(session, world_id, branch, command.causation_ids)

                state = self._checked_branch_state(branch)
                now = utc_now()
                version_after = branch.state_version + 1
                first_seq = branch.head_seq + 1
                event_records: list[EventRecord] = []
                postings: list[LedgerPosting] = []
                next_state = state
                for index, event in enumerate(typed_events):
                    next_state = apply_event(next_state, event)
                    event_postings = ledger_postings(event)
                    validate_ledger_postings(event_postings)
                    postings.extend(event_postings)
                    event_records.append(
                        EventRecord(
                            event_id=event.event_id,
                            world_id=world_id,
                            branch_id=branch_id,
                            sequence=first_seq + index,
                            tick=next_state.current_tick,
                            type=event.type,
                            command_id=command.command_id,
                            actor_id=command.actor_id,
                            causation_ids=command.causation_ids,
                            correlation_id=command.correlation_id or scope.correlation_id,
                            ruleset_version=world.ruleset_version,
                            payload=event_payload(event),
                            visibility=event.visibility,
                            state_version_after=version_after,
                            commit_boundary=(index == len(typed_events) - 1),
                            committed_at=now,
                        )
                    )
                validate_ledger_postings(tuple(postings))
                next_hash = state_hash(next_state)
                last_seq = first_seq + len(typed_events) - 1

                result = session.execute(
                    update(BranchRow)
                    .where(
                        BranchRow.branch_id == branch_id,
                        BranchRow.world_id == world_id,
                        BranchRow.state_version == command.expected_state_version,
                    )
                    .values(
                        head_seq=last_seq,
                        state_version=version_after,
                        current_tick=next_state.current_tick,
                        state_hash=next_hash,
                        state_json=state_json(next_state),
                    )
                )
                if result.rowcount != 1:
                    concurrent = session.scalar(
                        select(CommandRow).where(
                            CommandRow.branch_id == branch_id,
                            CommandRow.idempotency_key == command.idempotency_key,
                        )
                    )
                    if concurrent is not None:
                        return self._idempotent_result(concurrent, request_hash)
                    current = session.get(BranchRow, branch_id)
                    raise StaleVersion(command.expected_state_version, current.state_version if current else -1)

                for event, record in zip(typed_events, event_records, strict=True):
                    session.add(
                        EventRow(
                            branch_id=branch_id,
                            sequence=record.sequence,
                            event_id=record.event_id,
                            world_id=world_id,
                            tick=record.tick,
                            event_type=record.type,
                            command_id=record.command_id,
                            actor_id=record.actor_id,
                            causation_json=[str(item) for item in record.causation_ids],
                            correlation_id=record.correlation_id,
                            ruleset_version=record.ruleset_version,
                            payload_json=record.payload,
                            visibility_json=record.visibility,
                            state_version_after=record.state_version_after,
                            commit_boundary=record.commit_boundary,
                            committed_at=now,
                        )
                    )
                    for posting_index, posting in enumerate(ledger_postings(event)):
                        session.add(
                            LedgerPostingRow(
                                posting_id=uuid4(),
                                event_id=event.event_id,
                                posting_index=posting_index,
                                account_id=posting.account_id,
                                resource_id=posting.resource_id,
                                delta=format(posting.delta, "f"),
                                created_at=now,
                            )
                        )
                    session.add(
                        OutboxRow(
                            outbox_id=uuid4(),
                            event_id=record.event_id,
                            world_id=world_id,
                            branch_id=branch_id,
                            topic=record.type,
                            payload_json=record.model_dump(mode="json"),
                            attempts=0,
                            created_at=now,
                            published_at=None,
                        )
                    )

                commit_result = CommitResult(
                    command_id=command.command_id,
                    branch_id=branch_id,
                    duplicate=False,
                    state_version=version_after,
                    first_seq=first_seq,
                    last_seq=last_seq,
                    events=event_records,
                    state_hash=next_hash,
                )
                session.add(
                    CommandRow(
                        command_id=command.command_id,
                        tenant_id=scope.tenant_id,
                        world_id=world_id,
                        branch_id=branch_id,
                        actor_id=command.actor_id,
                        idempotency_key=command.idempotency_key,
                        payload_hash=request_hash,
                        expected_state_version=command.expected_state_version,
                        command_type=command.command_type,
                        payload_json=command.payload,
                        result_json=commit_result.model_dump(mode="json"),
                        created_at=now,
                    )
                )
                session.flush()
                return commit_result
        except IntegrityError as exc:
            # Unique event/command/outbox constraints indicate a replay collision;
            # the whole transaction is rolled back so no partial state survives.
            raise Conflict("The command conflicts with an existing event or idempotency record.") from exc

    def authorize_role(
        self, scope: AccessScope, world_id: UUID, roles: set[MembershipRole]
    ) -> MembershipRole:
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in roles:
                raise AccessDenied("The membership role does not allow this operation.")
            return role

    def authorize_actor(
        self,
        scope: AccessScope,
        world_id: UUID,
        actor_id: UUID,
        *,
        require_control: bool = True,
        branch_id: UUID | None = None,
    ) -> UUID:
        """Authorize a POV actor before the API builds an audience projection."""
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id or world.active_branch_id)
            state = self._checked_branch_state(branch)
            actor = state.entities.get(actor_id)
            if actor is None or actor.kind not in {"character", "person"}:
                raise NotFound("Actor does not exist in this branch.")
            if require_control:
                if role == MembershipRole.OBSERVER:
                    raise AccessDenied("Observers cannot select a controlled actor POV.")
                if role == MembershipRole.PLAYER:
                    control = session.get(ActorControlRow, (world_id, scope.user_id, actor_id))
                    if control is None:
                        raise AccessDenied("This player does not control the selected actor.")
            return actor_id

    def controlled_actors(self, scope: AccessScope, world_id: UUID) -> list[UUID]:
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if role in AUTHOR_ROLES:
                branch = self._get_branch(session, world_id, world.active_branch_id)
                state = self._checked_branch_state(branch)
                return sorted(
                    (entity_id for entity_id, entity in state.entities.items() if entity.kind in {"character", "person"}),
                    key=str,
                )
            rows = session.scalars(
                select(ActorControlRow.actor_id)
                .where(ActorControlRow.world_id == world_id, ActorControlRow.user_id == scope.user_id)
                .order_by(ActorControlRow.actor_id)
            ).all()
            return list(rows)

    def grant_membership(
        self,
        scope: AccessScope,
        world_id: UUID,
        user_id: UUID,
        role: MembershipRole,
        *,
        controlled_actor_ids: Sequence[UUID] = (),
    ) -> None:
        """Create/update a member and replace that member's controlled actor set."""
        now = utc_now()
        with self._transaction(write=True) as session:
            world, caller_role = self._require_membership(session, scope, world_id)
            if caller_role != MembershipRole.OWNER:
                raise AccessDenied("Only a world owner may grant memberships.")
            active_branch = self._get_branch(session, world_id, world.active_branch_id)
            state = self._checked_branch_state(active_branch)
            for actor_id in controlled_actor_ids:
                actor = state.entities.get(actor_id)
                if actor is None or actor.kind not in {"character", "person"}:
                    raise NotFound(f"Controlled actor {actor_id} does not exist in the active branch.")
            membership = session.get(MembershipRow, (world_id, user_id))
            if membership is None:
                membership = MembershipRow(
                    world_id=world_id,
                    user_id=user_id,
                    tenant_id=scope.tenant_id,
                    role=role.value,
                    created_at=now,
                )
                session.add(membership)
            else:
                membership.role = role.value
                membership.tenant_id = scope.tenant_id
            session.execute(
                delete(ActorControlRow).where(
                    ActorControlRow.world_id == world_id,
                    ActorControlRow.user_id == user_id,
                )
            )
            if role == MembershipRole.PLAYER:
                for actor_id in set(controlled_actor_ids):
                    session.add(
                        ActorControlRow(
                            world_id=world_id,
                            user_id=user_id,
                            actor_id=actor_id,
                            granted_by=scope.user_id,
                            created_at=now,
                        )
                    )

    def grant_actor_control(
        self, scope: AccessScope, world_id: UUID, user_id: UUID, actor_id: UUID
    ) -> None:
        with self._transaction(write=True) as session:
            world, role = self._require_membership(session, scope, world_id)
            if role != MembershipRole.OWNER:
                raise AccessDenied("Only a world owner may grant actor control.")
            member = session.get(MembershipRow, (world_id, user_id))
            if member is None or member.role != MembershipRole.PLAYER.value:
                raise AccessDenied("Actor control can only be granted to a player membership.")
            branch = self._get_branch(session, world_id, world.active_branch_id)
            state = self._checked_branch_state(branch)
            actor = state.entities.get(actor_id)
            if actor is None or actor.kind not in {"character", "person"}:
                raise NotFound("Actor does not exist in the active branch.")
            if session.get(ActorControlRow, (world_id, user_id, actor_id)) is None:
                session.add(
                    ActorControlRow(
                        world_id=world_id,
                        user_id=user_id,
                        actor_id=actor_id,
                        granted_by=scope.user_id,
                        created_at=utc_now(),
                    )
                )

    def snapshot(self, scope: AccessScope, world_id: UUID, branch_id: UUID) -> SnapshotRecord:
        with self._transaction(write=True) as session:
            self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id)
            state = self._checked_branch_state(branch)
            existing = session.scalar(
                select(SnapshotRow).where(
                    SnapshotRow.branch_id == branch_id,
                    SnapshotRow.last_seq == branch.head_seq,
                )
            )
            if existing is not None:
                return self._snapshot_record(existing)
            row = SnapshotRow(
                snapshot_id=uuid4(),
                world_id=world_id,
                branch_id=branch_id,
                last_seq=branch.head_seq,
                state_version=branch.state_version,
                current_tick=branch.current_tick,
                state_hash=branch.state_hash,
                state_json=state_json(state),
                created_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return self._snapshot_record(row)

    def fork(
        self,
        scope: AccessScope,
        world_id: UUID,
        source_branch_id: UUID,
        fork_seq: int,
        label: str,
    ) -> BranchRecord:
        if fork_seq < 0:
            raise ValueError("fork_seq must be nonnegative")
        normalized_label = label.strip()
        if not normalized_label or len(normalized_label) > 160:
            raise ValueError("label must be non-empty text of at most 160 characters")
        now = utc_now()
        branch_id = uuid4()
        try:
            with self._transaction(write=True) as session:
                _, role = self._require_membership(session, scope, world_id)
                self._require_mutation_role(role)
                source = self._get_branch(session, world_id, source_branch_id)
                if fork_seq > source.head_seq:
                    raise Conflict("Cannot fork after the source branch head.")
                state, state_version = self._state_at_seq(session, world_id, source, fork_seq)
                digest = state_hash(state)
                row = BranchRow(
                    branch_id=branch_id,
                    world_id=world_id,
                    name=normalized_label,
                    parent_branch_id=source_branch_id,
                    fork_seq=fork_seq,
                    head_seq=fork_seq,
                    state_version=state_version,
                    initial_state_version=state_version,
                    current_tick=state.current_tick,
                    initial_state_hash=digest,
                    state_hash=digest,
                    initial_state_json=state_json(state),
                    state_json=state_json(state),
                    created_at=now,
                )
                session.add(row)
                session.flush()
                # The fork checkpoint makes the exact replay boundary explicit.
                session.add(
                    SnapshotRow(
                        snapshot_id=uuid4(),
                        world_id=world_id,
                        branch_id=branch_id,
                        last_seq=fork_seq,
                        state_version=state_version,
                        current_tick=state.current_tick,
                        state_hash=digest,
                        state_json=state_json(state),
                        created_at=now,
                    )
                )
                session.flush()
                return self._branch_record(row)
        except IntegrityError as exc:
            raise Conflict("Branch label already exists in this world.") from exc

    def replay(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        through_seq: int | None = None,
    ) -> WorldView:
        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id)
            target_seq = branch.head_seq if through_seq is None else through_seq
            if target_seq < 0 or target_seq > branch.head_seq:
                raise Conflict("Replay sequence must be between zero and the branch head.")
            state, _ = self._state_at_seq(session, world_id, branch, target_seq)
            return WorldView(
                world=self._world_record(world),
                branch=self._branch_record(branch),
                state=state,
                last_seq=target_seq,
                state_hash=state_hash(state),
            )

    def enqueue_job(self, scope: AccessScope, new_job: NewJob) -> JobRecord:
        now = utc_now()
        request_payload = {
            "world_id": str(new_job.world_id),
            "branch_id": str(new_job.branch_id),
            "job_type": new_job.job_type,
            "payload": new_job.payload,
        }
        request_hash = self._hash_json(request_payload)
        try:
            with self._transaction(write=True) as session:
                _, role = self._require_membership(session, scope, new_job.world_id)
                self._require_mutation_role(role)
                branch = self._get_branch(session, new_job.world_id, new_job.branch_id)
                existing = session.scalar(
                    select(JobRow).where(
                        JobRow.branch_id == new_job.branch_id,
                        JobRow.idempotency_key == new_job.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflict("Job idempotency key was reused with a different payload.")
                    return self._job_record(existing)
                row = JobRow(
                    job_id=new_job.job_id,
                    tenant_id=scope.tenant_id,
                    world_id=new_job.world_id,
                    branch_id=new_job.branch_id,
                    command_id=None,
                    job_type=new_job.job_type,
                    idempotency_key=new_job.idempotency_key,
                    request_hash=request_hash,
                    status=JobStatus.QUEUED.value,
                    payload_json=new_job.payload,
                    attempt_count=0,
                    worker_id=None,
                    lease_expires_at=None,
                    fencing_token=0,
                    committed_through_tick=branch.current_tick,
                    result_json=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return self._job_record(row)
        except IntegrityError as exc:
            raise Conflict("Job identifier or idempotency key already exists.") from exc

    def claim_job(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease | None:
        if not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("worker_id must be non-empty text of at most 200 characters")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        effective_now = normalize_datetime(now or utc_now())
        now_db = effective_now.replace(tzinfo=None)
        expires_at = effective_now + timedelta(seconds=lease_seconds)
        expires_db = expires_at.replace(tzinfo=None)
        with self._transaction(write=True) as session:
            candidates = session.scalars(
                select(JobRow)
                .where(
                    (JobRow.status == JobStatus.QUEUED.value)
                    | (
                        (JobRow.status == JobStatus.RUNNING.value)
                        & (JobRow.lease_expires_at <= now_db)
                    )
                )
                .order_by(JobRow.created_at, JobRow.job_id)
                .limit(1000)
            ).all()
            eligible = (JobRow.status == JobStatus.QUEUED.value) | (
                (JobRow.status == JobStatus.RUNNING.value) & (JobRow.lease_expires_at <= now_db)
            )
            for candidate in candidates:
                # Lock the branch before its job and lease. The branch row exists
                # even when the first lease row does not, closing that insert race.
                branch = session.scalar(
                    select(BranchRow).where(BranchRow.branch_id == candidate.branch_id).with_for_update()
                )
                if branch is None:
                    continue
                job = session.scalar(
                    select(JobRow)
                    .where(JobRow.job_id == candidate.job_id, eligible)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if job is None:
                    continue
                current_lease = session.scalar(
                    select(BranchLeaseRow)
                    .where(BranchLeaseRow.branch_id == job.branch_id)
                    .with_for_update()
                )
                if current_lease is not None and current_lease.lease_expires_at > now_db:
                    continue
                next_token = (current_lease.fencing_token if current_lease is not None else 0) + 1
                job.status = JobStatus.RUNNING.value
                job.worker_id = worker_id
                job.lease_expires_at = expires_db
                job.fencing_token = next_token
                job.attempt_count += 1
                job.updated_at = now_db
                if current_lease is None:
                    current_lease = BranchLeaseRow(
                        branch_id=job.branch_id,
                        job_id=job.job_id,
                        worker_id=worker_id,
                        fencing_token=next_token,
                        lease_expires_at=expires_db,
                    )
                    session.add(current_lease)
                else:
                    current_lease.job_id = job.job_id
                    current_lease.worker_id = worker_id
                    current_lease.fencing_token = next_token
                    current_lease.lease_expires_at = expires_db
                session.flush()
                return JobLease(
                    job_id=job.job_id,
                    world_id=job.world_id,
                    branch_id=job.branch_id,
                    worker_id=worker_id,
                    fencing_token=next_token,
                    expires_at=expires_at,
                )
            return None

    def renew_job(
        self,
        lease: JobLease,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        effective_now = normalize_datetime(now or utc_now())
        expires_at = effective_now + timedelta(seconds=lease_seconds)
        with self._transaction(write=True) as session:
            self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id, now=effective_now)
            job = session.get(JobRow, lease.job_id)
            current = session.get(BranchLeaseRow, lease.branch_id)
            if job is None or current is None or job.status != JobStatus.RUNNING.value:
                raise InvalidLease("Only a running job may renew its lease.")
            job.lease_expires_at = expires_at.replace(tzinfo=None)
            job.updated_at = effective_now.replace(tzinfo=None)
            current.lease_expires_at = expires_at.replace(tzinfo=None)
            return JobLease(
                job_id=lease.job_id,
                world_id=lease.world_id,
                branch_id=lease.branch_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                expires_at=expires_at,
            )

    def finish_job(
        self,
        lease: JobLease,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        committed_through_tick: int | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        if status not in {JobStatus.WAITING_INPUT, JobStatus.PAUSED, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise ValueError("A running job may finish only to a waiting, paused, or terminal state.")
        effective_now = normalize_datetime(now or utc_now())
        now_db = effective_now.replace(tzinfo=None)
        with self._transaction(write=True) as session:
            self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id, now=effective_now)
            job = session.get(JobRow, lease.job_id)
            current = session.get(BranchLeaseRow, lease.branch_id)
            if job is None or current is None or job.status != JobStatus.RUNNING.value:
                raise InvalidLease("The job is no longer running under this lease.")
            job.status = status.value
            job.result_json = result
            job.committed_through_tick = committed_through_tick
            job.updated_at = now_db
            job.lease_expires_at = now_db
            current.lease_expires_at = now_db
            session.flush()
            return self._job_record(job)

    def read_job(self, scope: AccessScope, job_id: UUID) -> JobRecord:
        with self._transaction() as session:
            job = session.get(JobRow, job_id)
            if job is None:
                raise NotFound("Job does not exist.")
            self._require_membership(session, scope, job.world_id)
            return self._job_record(job)

    def pending_outbox(self, *, limit: int = 100) -> list[OutboxRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._transaction() as session:
            rows = session.scalars(
                select(OutboxRow)
                .where(OutboxRow.published_at.is_(None))
                .order_by(OutboxRow.created_at, OutboxRow.outbox_id)
                .limit(limit)
            ).all()
            return [self._outbox_record(row) for row in rows]

    def mark_outbox_delivery(self, outbox_id: UUID, *, succeeded: bool) -> OutboxRecord:
        now = utc_now()
        with self._transaction(write=True) as session:
            row = session.get(OutboxRow, outbox_id)
            if row is None:
                raise NotFound("Outbox message does not exist.")
            row.attempts += 1
            if succeeded:
                row.published_at = now.replace(tzinfo=None)
            session.flush()
            return self._outbox_record(row)

    def _require_membership(
        self, session: Session, scope: AccessScope, world_id: UUID
    ) -> tuple[WorldRow, MembershipRole]:
        world = session.scalar(
            select(WorldRow).where(WorldRow.world_id == world_id, WorldRow.tenant_id == scope.tenant_id)
        )
        if world is None:
            raise NotFound("World does not exist in this tenant.")
        membership = session.get(MembershipRow, (world_id, scope.user_id))
        if membership is None or membership.tenant_id != scope.tenant_id:
            raise AccessDenied("The principal is not a member of this world.")
        try:
            role = MembershipRole(membership.role)
        except ValueError as exc:
            raise AccessDenied("The membership has an unsupported role.") from exc
        return world, role

    def _require_mutation_role(self, role: MembershipRole) -> None:
        if role not in MUTATING_ROLES:
            raise AccessDenied("This membership role cannot commit world mutations.")

    def _require_actor_control(
        self,
        session: Session,
        scope: AccessScope,
        world_id: UUID,
        role: MembershipRole,
        actor_id: UUID,
    ) -> None:
        if role == MembershipRole.PLAYER:
            control = session.get(ActorControlRow, (world_id, scope.user_id, actor_id))
            if control is None:
                raise AccessDenied("This player does not control the requested actor.")

    def _get_branch(self, session: Session, world_id: UUID, branch_id: UUID) -> BranchRow:
        branch = session.scalar(
            select(BranchRow).where(BranchRow.branch_id == branch_id, BranchRow.world_id == world_id)
        )
        if branch is None:
            raise NotFound("Branch does not exist in this world.")
        return branch

    def _validate_causation(
        self, session: Session, world_id: UUID, branch: BranchRow, causation_ids: Sequence[UUID]
    ) -> None:
        if not causation_ids:
            return
        chain = self._branch_chain(session, branch, branch.head_seq, include_ancestors=True)
        allowed_ids: set[UUID] = set()
        for branch_id, max_seq in chain:
            allowed_ids.update(
                session.scalars(
                    select(EventRow.event_id).where(
                        EventRow.world_id == world_id,
                        EventRow.branch_id == branch_id,
                        EventRow.sequence <= max_seq,
                    )
                ).all()
            )
        missing = set(causation_ids) - allowed_ids
        if missing:
            raise InvalidEvent(f"Causation references must point to committed events in this branch lineage: {missing}.")

    def _idempotent_result(self, row: CommandRow, request_hash: str) -> CommitResult:
        if row.payload_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was already used with a different command payload.")
        previous = CommitResult.model_validate(row.result_json)
        return previous.model_copy(update={"duplicate": True})

    def _validate_worker_lease(
        self,
        session: Session,
        lease: JobLease,
        world_id: UUID,
        branch_id: UUID,
        *,
        now: datetime | None = None,
    ) -> None:
        """Validated lazily to keep the domain contract independent of job storage."""
        if lease.branch_id != branch_id:
            raise InvalidLease("The worker lease does not match this branch.")
        branch = session.scalar(
            select(BranchRow).where(BranchRow.branch_id == branch_id, BranchRow.world_id == world_id).with_for_update()
        )
        current = session.scalar(
            select(BranchLeaseRow).where(BranchLeaseRow.branch_id == branch_id).with_for_update()
        )
        current_time = normalize_datetime(now or utc_now())
        if (
            branch is None
            or current is None
            or current.job_id != lease.job_id
            or current.worker_id != lease.worker_id
            or current.fencing_token != lease.fencing_token
            or normalize_datetime(current.lease_expires_at) <= current_time
        ):
            raise InvalidLease("The worker lease is expired or fenced by a newer worker.")
        job = session.scalar(select(JobRow).where(JobRow.job_id == lease.job_id).with_for_update())
        if job is None or job.world_id != world_id or job.branch_id != branch_id:
            raise InvalidLease("The worker job does not belong to this world branch.")

    def _branch_chain(
        self,
        session: Session,
        branch: BranchRow,
        through_seq: int,
        include_ancestors: bool,
    ) -> list[tuple[UUID, int]]:
        chain = [(branch.branch_id, min(through_seq, branch.head_seq))]
        if not include_ancestors:
            return chain
        cursor = branch
        cutoff = min(through_seq, cursor.head_seq)
        while cursor.parent_branch_id is not None:
            cutoff = min(cutoff, cursor.fork_seq)
            parent = session.get(BranchRow, cursor.parent_branch_id)
            if parent is None:
                raise Conflict("Branch lineage is incomplete.")
            chain.append((parent.branch_id, cutoff))
            cursor = parent
        return chain

    def _state_at_seq(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        through_seq: int,
    ) -> tuple[WorldState, int]:
        if through_seq < 0 or through_seq > branch.head_seq:
            raise Conflict("Replay sequence is outside the branch history.")
        if through_seq < branch.fork_seq:
            if branch.parent_branch_id is None:
                raise Conflict("Replay sequence precedes the world genesis state.")
            parent = self._get_branch(session, world_id, branch.parent_branch_id)
            return self._state_at_seq(session, world_id, parent, through_seq)
        if through_seq > branch.fork_seq:
            boundary = session.scalar(
                select(EventRow.commit_boundary).where(
                    EventRow.branch_id == branch.branch_id,
                    EventRow.sequence == through_seq,
                )
            )
            if boundary is not True:
                raise Conflict("Replay and fork positions must be complete command commit boundaries.")

        base_seq = branch.fork_seq
        base_version = branch.initial_state_version
        state = WorldState.model_validate(branch.initial_state_json)
        digest = state_hash(state)
        if digest != branch.initial_state_hash:
            raise Conflict("Branch fork checkpoint failed its integrity check.")

        snapshot = session.scalar(
            select(SnapshotRow)
            .where(
                SnapshotRow.branch_id == branch.branch_id,
                SnapshotRow.last_seq >= branch.fork_seq,
                SnapshotRow.last_seq <= through_seq,
            )
            .order_by(SnapshotRow.last_seq.desc())
            .limit(1)
        )
        if snapshot is not None:
            state = WorldState.model_validate(snapshot.state_json)
            if state_hash(state) != snapshot.state_hash or state.current_tick != snapshot.current_tick:
                raise Conflict("Snapshot failed its integrity check.")
            base_seq = snapshot.last_seq
            base_version = snapshot.state_version

        rows = session.scalars(
            select(EventRow)
            .where(
                EventRow.world_id == world_id,
                EventRow.branch_id == branch.branch_id,
                EventRow.sequence > base_seq,
                EventRow.sequence <= through_seq,
            )
            .order_by(EventRow.sequence)
        ).all()
        state_version = base_version
        expected_seq = base_seq + 1
        for row in rows:
            if row.sequence != expected_seq:
                raise Conflict("Branch event sequence is not contiguous after its replay checkpoint.")
            raw_event = {
                "type": row.event_type,
                "event_id": str(row.event_id),
                "visibility": row.visibility_json,
                **row.payload_json,
            }
            try:
                state = apply_event(state, parse_event(raw_event))
            except InvalidEvent as exc:
                raise Conflict(f"Stored event {row.event_id} cannot be replayed: {exc}") from exc
            if state.current_tick != row.tick:
                raise Conflict(f"Stored event {row.event_id} has an inconsistent tick.")
            state_version = row.state_version_after
            expected_seq += 1
        return state, state_version

    def _checked_branch_state(self, branch: BranchRow) -> WorldState:
        state = WorldState.model_validate(branch.state_json)
        digest = state_hash(state)
        if digest != branch.state_hash or state.current_tick != branch.current_tick:
            raise Conflict("Stored branch projection failed its integrity check.")
        return state

    def _world_record(self, row: WorldRow) -> WorldRecord:
        return WorldRecord(
            world_id=row.world_id,
            tenant_id=row.tenant_id,
            owner_id=row.owner_id,
            title=row.title,
            active_branch_id=row.active_branch_id,
            ruleset_version=row.ruleset_version,
            pack_version=row.pack_version,
            created_at=normalize_datetime(row.created_at),
        )

    def _branch_record(self, row: BranchRow | None) -> BranchRecord:
        if row is None:
            raise NotFound("Branch does not exist.")
        return BranchRecord(
            branch_id=row.branch_id,
            world_id=row.world_id,
            name=row.name,
            parent_branch_id=row.parent_branch_id,
            fork_seq=row.fork_seq,
            head_seq=row.head_seq,
            state_version=row.state_version,
            current_tick=row.current_tick,
            state_hash=row.state_hash,
            created_at=normalize_datetime(row.created_at),
        )

    def _event_record(self, row: EventRow) -> EventRecord:
        return EventRecord(
            event_id=row.event_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            sequence=row.sequence,
            tick=row.tick,
            type=row.event_type,
            command_id=row.command_id,
            actor_id=row.actor_id,
            causation_ids=[UUID(value) for value in row.causation_json],
            correlation_id=row.correlation_id,
            ruleset_version=row.ruleset_version,
            payload=row.payload_json,
            visibility=row.visibility_json,
            state_version_after=row.state_version_after,
            commit_boundary=row.commit_boundary,
            committed_at=normalize_datetime(row.committed_at),
        )

    def _snapshot_record(self, row: SnapshotRow) -> SnapshotRecord:
        return SnapshotRecord(
            snapshot_id=row.snapshot_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            last_seq=row.last_seq,
            state_version=row.state_version,
            current_tick=row.current_tick,
            state_hash=row.state_hash,
            state=WorldState.model_validate(row.state_json),
            created_at=normalize_datetime(row.created_at),
        )

    def _job_record(self, row: JobRow) -> JobRecord:
        return JobRecord(
            job_id=row.job_id,
            tenant_id=row.tenant_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            command_id=row.command_id,
            job_type=row.job_type,
            status=JobStatus(row.status),
            payload=row.payload_json,
            attempt_count=row.attempt_count,
            fencing_token=row.fencing_token,
            committed_through_tick=row.committed_through_tick,
            result=row.result_json,
            created_at=normalize_datetime(row.created_at),
            updated_at=normalize_datetime(row.updated_at),
        )

    def _outbox_record(self, row: OutboxRow) -> OutboxRecord:
        return OutboxRecord(
            outbox_id=row.outbox_id,
            event_id=row.event_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            topic=row.topic,
            payload=row.payload_json,
            attempts=row.attempts,
            created_at=normalize_datetime(row.created_at),
            published_at=normalize_datetime(row.published_at) if row.published_at else None,
        )

    def _hash_json(self, payload: dict[str, Any]) -> str:
        import hashlib

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
