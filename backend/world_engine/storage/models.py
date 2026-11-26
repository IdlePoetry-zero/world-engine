"""SQLAlchemy tables for authoritative state, event history, and durable jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MAX_WORLD_PACK_ARCHIVE_BYTES = 16 * 1024 * 1024


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class WorldRow(Base):
    __tablename__ = "worlds"
    __table_args__ = (Index("ix_worlds_tenant_created", "tenant_id", "created_at"),)

    world_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    active_branch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(100), nullable=False)
    pack_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldPackArchiveRow(Base):
    __tablename__ = "world_pack_archives"
    __table_args__ = (
        CheckConstraint(
            f"length(archive_bytes) BETWEEN 1 AND {MAX_WORLD_PACK_ARCHIVE_BYTES}",
            name="archive_size_bounded",
        ),
    )

    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    archive_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class MembershipRow(Base):
    __tablename__ = "world_memberships"
    __table_args__ = (
        Index("ix_memberships_tenant_user", "tenant_id", "user_id"),
        CheckConstraint("role in ('owner', 'editor', 'player', 'observer')", name="valid_role"),
    )

    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActorControlRow(Base):
    __tablename__ = "world_actor_controls"
    __table_args__ = (Index("ix_actor_controls_user", "world_id", "user_id"),)

    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    granted_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BranchRow(Base):
    __tablename__ = "world_branches"
    __table_args__ = (
        UniqueConstraint("world_id", "name", name="uq_branch_world_name"),
        Index("ix_branches_world_created", "world_id", "created_at"),
        CheckConstraint("fork_seq >= 0", name="fork_seq_nonnegative"),
        CheckConstraint("head_seq >= fork_seq", name="head_seq_at_least_fork"),
        CheckConstraint("state_version >= 0", name="state_version_nonnegative"),
    )

    branch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    parent_branch_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="RESTRICT"), nullable=True
    )
    fork_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    head_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    initial_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommandRow(Base):
    __tablename__ = "world_commands"
    __table_args__ = (
        UniqueConstraint("branch_id", "idempotency_key", name="uq_command_branch_idempotency"),
        Index("ix_commands_world_created", "world_id", "created_at"),
    )

    command_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    command_type: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRow(Base):
    __tablename__ = "world_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_id"),
        Index("ix_events_world_tick", "world_id", "tick"),
        Index("ix_events_command", "command_id"),
        Index(
            "ix_events_direct_move_branch_sequence", "world_id", "branch_id", "sequence",
            sqlite_where=text("event_type = 'core.movement_completed.v1'"),
            postgresql_where=text("event_type = 'core.movement_completed.v1'"),
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("state_version_after >= 1", name="state_version_positive"),
    )

    branch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    command_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    causation_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ruleset_version: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    visibility_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state_version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_boundary: Mapped[bool] = mapped_column(Boolean, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventPayloadRow(Base):
    """Lossless compressed bytes for one event; event JSON stays in the log row when inline."""

    __tablename__ = "world_event_payloads"
    __table_args__ = (
        CheckConstraint("codec_version >= 1", name="codec_version_positive"),
        CheckConstraint(
            "raw_byte_count > 0 AND raw_byte_count <= 67108864",
            name="raw_byte_count_bounded_positive",
        ),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("length(compressed_bytes) > 0", name="compressed_payload_nonempty"),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("world_events.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    compressed_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    codec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class LedgerPostingRow(Base):
    __tablename__ = "ledger_postings"
    __table_args__ = (
        UniqueConstraint("event_id", "posting_index", name="uq_ledger_event_posting"),
        Index("ix_ledger_account_resource", "account_id", "resource_id"),
        CheckConstraint("posting_index >= 0", name="posting_index_nonnegative"),
    )

    posting_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_events.event_id", ondelete="CASCADE"), nullable=False
    )
    posting_index: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    delta: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SnapshotRow(Base):
    __tablename__ = "world_snapshots"
    __table_args__ = (
        UniqueConstraint("branch_id", "last_seq", name="uq_snapshot_branch_seq"),
        Index("ix_snapshots_world_created", "world_id", "created_at"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="CASCADE"), nullable=False
    )
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobRow(Base):
    __tablename__ = "world_jobs"
    __table_args__ = (
        UniqueConstraint("branch_id", "idempotency_key", name="uq_job_branch_idempotency"),
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_branch_status", "branch_id", "status"),
        CheckConstraint(
            "status in ('queued', 'running', 'waiting_input', 'paused', 'succeeded', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("fencing_token >= 0", name="fencing_token_nonnegative"),
    )

    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="CASCADE"), nullable=False
    )
    command_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    job_type: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    committed_through_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BranchLeaseRow(Base):
    __tablename__ = "branch_writer_leases"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_branch_lease_job"),
        CheckConstraint("fencing_token >= 1", name="fencing_token_positive"),
    )

    branch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_jobs.job_id", ondelete="CASCADE"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxRow(Base):
    __tablename__ = "world_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_outbox_event"),
        Index("ix_outbox_pending_created", "published_at", "created_at"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
    )

    outbox_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_events.event_id", ondelete="CASCADE"), nullable=False
    )
    world_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worlds.world_id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_branches.branch_id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
