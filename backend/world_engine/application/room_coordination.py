"""Application contract for a future host coordinating one room per branch.

The host serializes transitions, supplies trusted time and branch metadata, and
keeps the coordinator and its receipts for the lifetime of the room. A closed
batch is input to canonical rules. The host still needs a version-checked
transaction and writer lease to commit rule outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field

from world_engine.domain.errors import AccessDenied, Conflict, IdempotencyConflict, InvalidLease, NotFound, StaleVersion
from world_engine.domain.models import AccessScope, CommandEnvelope, ContractModel, MembershipRole, normalize_datetime
from world_engine.rules.contracts import ActionIntent, parse_action


class RoomScope(ContractModel):
    tenant_id: UUID
    world_id: UUID
    branch_id: UUID
    room_id: UUID


class RoomMember(ContractModel):
    member_id: UUID
    user_id: UUID


class RoomSnapshot(ContractModel):
    scope: RoomScope
    world_version: int = Field(ge=0)
    world_tick: int = Field(ge=-(2**63), le=2**63 - 1)


class ActorControlLease(ContractModel):
    scope: RoomScope
    member_id: UUID
    actor_id: UUID
    fencing_token: int = Field(ge=1)
    issued_at: AwareDatetime
    expires_at: AwareDatetime


class TimeoutPolicy(StrEnum):
    WAIT = "wait"
    ABSTAIN = "abstain"
    CONTINUE = "continue"


class BarrierWindow(ContractModel):
    window_id: UUID
    snapshot: RoomSnapshot
    actor_ids: tuple[UUID, ...]
    opens_at: AwareDatetime
    deadline: AwareDatetime
    timeout_policy: TimeoutPolicy


@dataclass(frozen=True)
class RoomProposal:
    window: BarrierWindow
    lease: ActorControlLease
    _command_json: str = field(repr=False)

    @property
    def command(self) -> CommandEnvelope:
        """Return a detached envelope so edits cannot change the receipt or batch."""
        return CommandEnvelope.model_validate_json(self._command_json)


@dataclass(frozen=True)
class BarrierBatch:
    window: BarrierWindow
    proposals: tuple[RoomProposal, ...]
    abstained_actor_ids: tuple[UUID, ...]
    skipped_actor_ids: tuple[UUID, ...]

    def resolve(self, rules: CanonicalRoomRules[Resolution]) -> Resolution:
        return rules.resolve(self)


Resolution = TypeVar("Resolution", covariant=True)


class CanonicalRoomRules(Protocol[Resolution]):
    """Resolve the entire batch against its pinned snapshot using canonical rules.

    Proposal order is stable for reproducibility. Contention priority and
    game outcomes belong to the rules, and must be independent of arrival.
    """

    def resolve(self, batch: BarrierBatch) -> Resolution: ...


class RoomAuthority(Protocol):
    """Use the current world membership and actor grants of the authenticated user."""

    def authorize_role(
        self, scope: AccessScope, world_id: UUID, roles: set[MembershipRole]
    ) -> MembershipRole: ...

    def authorize_actor(
        self, scope: AccessScope, world_id: UUID, actor_id: UUID, *,
        require_control: bool = True, branch_id: UUID | None = None,
    ) -> UUID: ...


_ACTING_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR, MembershipRole.PLAYER}


def _trusted_time(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Room timestamps must have a time zone.")
    return normalize_datetime(value)


class RoomCoordinator:
    def __init__(
        self, scope: RoomScope, members: tuple[RoomMember, ...], authority: RoomAuthority, *,
        timeout_policy: TimeoutPolicy = TimeoutPolicy.WAIT, allow_offline_advance: bool = False,
    ) -> None:
        self.scope = scope
        self._members = {member.member_id: member.user_id for member in members}
        if len(self._members) != len(members) or len(set(self._members.values())) != len(members):
            raise ValueError("Room member IDs and user IDs must be unique.")
        self._authority = authority
        self._timeout_policy = TimeoutPolicy(timeout_policy)
        self._allow_offline_advance = allow_offline_advance
        self._leases: dict[UUID, ActorControlLease] = {}
        self._fences: dict[UUID, int] = {}
        self._window: BarrierWindow | None = None
        self._proposals: dict[UUID, RoomProposal] = {}
        self._receipts: dict[tuple[UUID, str], tuple[str, RoomProposal]] = {}
        self._closed: dict[UUID, BarrierBatch | None] = {}

    def issue_lease(
        self, owner: AccessScope, member_id: UUID, actor_id: UUID, *, now: datetime, expires_at: datetime,
    ) -> ActorControlLease:
        self._authorize_owner(owner)
        now, expires_at = _trusted_time(now), _trusted_time(expires_at)
        if expires_at <= now:
            raise InvalidLease("The actor control lease must expire after issuance.")
        self._authorize_actor(self._member_access(member_id), actor_id)
        current = self._leases.get(actor_id)
        if current is not None and now < current.expires_at:
            raise Conflict("The actor already has an active control lease.")
        lease = ActorControlLease(
            scope=self.scope, member_id=member_id, actor_id=actor_id,
            fencing_token=self._fences.get(actor_id, 0) + 1, issued_at=now, expires_at=expires_at,
        )
        self._leases[actor_id] = lease
        self._fences[actor_id] = lease.fencing_token
        return lease

    def revoke_lease(self, owner: AccessScope, lease: ActorControlLease) -> None:
        self._authorize_owner(owner)
        if lease.scope != self.scope or self._leases.get(lease.actor_id) != lease:
            raise InvalidLease("The actor control lease is stale or belongs to another room.")
        del self._leases[lease.actor_id]

    def open_window(
        self, owner: AccessScope, window_id: UUID, snapshot: RoomSnapshot, actor_ids: tuple[UUID, ...], *,
        now: datetime, deadline: datetime,
    ) -> BarrierWindow:
        self._authorize_owner(owner)
        self._check_scope(snapshot.scope)
        now, deadline = _trusted_time(now), _trusted_time(deadline)
        if deadline <= now or len(set(actor_ids)) != len(actor_ids):
            raise ValueError("A window needs a future deadline and distinct actor IDs.")
        if self._window is not None or window_id in self._closed:
            raise Conflict("A barrier is already open or the window ID has been used.")
        if not actor_ids and not self._allow_offline_advance:
            raise Conflict("The room pauses when it has no active players.")
        if any(self._active_lease(actor_id, now) is None for actor_id in actor_ids):
            raise InvalidLease("Every participant needs valid actor control at window opening.")
        self._window = BarrierWindow(
            window_id=window_id, snapshot=snapshot, actor_ids=tuple(sorted(actor_ids)),
            opens_at=now, deadline=deadline, timeout_policy=self._timeout_policy,
        )
        return self._window

    def submit(
        self, access: AccessScope, window_id: UUID, snapshot: RoomSnapshot, lease: ActorControlLease, *,
        idempotency_key: str, action: ActionIntent | dict[str, object], now: datetime,
    ) -> RoomProposal:
        self._check_scope(snapshot.scope)
        self._check_scope(lease.scope)
        if access.tenant_id != self.scope.tenant_id or self._member_access(lease.member_id).user_id != access.user_id:
            raise AccessDenied("The authenticated user does not own this room membership.")
        self._authorize_actor(access, lease.actor_id)
        client_key = idempotency_key.strip()
        if not client_key or len(client_key) > 200:
            raise ValueError("An idempotency key must contain between 1 and 200 characters.")
        intent = parse_action(action)
        command = CommandEnvelope(
            command_id=uuid5(self.scope.room_id, f"{window_id}:{lease.actor_id}"),
            idempotency_key=f"room:{self.scope.room_id}:{window_id}:{lease.actor_id}",
            actor_id=lease.actor_id, expected_state_version=snapshot.world_version,
            command_type=f"action.{intent.kind}", payload=intent.model_dump(mode="json"),
        )
        key = (lease.member_id, client_key)
        request_hash = command.payload_hash()
        receipt = self._receipts.get(key)
        if receipt is not None:
            previous_hash, previous = receipt
            if (previous_hash, previous.lease, previous.window.window_id, previous.window.snapshot) != (
                request_hash, lease, window_id, snapshot,
            ):
                raise IdempotencyConflict("The room idempotency key belongs to a different submission.")
            return previous

        window = self._require_window(window_id)
        self._check_snapshot(window.snapshot, snapshot)
        now = _trusted_time(now)
        if now < window.opens_at or (now >= window.deadline and window.timeout_policy != TimeoutPolicy.WAIT):
            raise Conflict("The proposal window is outside its submission interval.")
        if lease.actor_id not in window.actor_ids:
            raise AccessDenied("The actor does not participate in this window.")
        if self._leases.get(lease.actor_id) != lease or not lease.issued_at <= now < lease.expires_at:
            raise InvalidLease("The actor control lease is expired, revoked, or fenced.")
        previous = self._proposals.get(lease.actor_id)
        if previous is not None and previous.lease == lease:
            raise Conflict("The actor already submitted a proposal for this window.")
        proposal = RoomProposal(window, lease, command.model_dump_json())
        self._proposals[lease.actor_id] = proposal
        self._receipts[key] = (request_hash, proposal)
        return proposal

    def close_window(
        self, owner: AccessScope, window_id: UUID, current: RoomSnapshot, *, now: datetime,
    ) -> BarrierBatch | None:
        """Return the closed batch, or None while the time barrier is waiting."""
        self._authorize_owner(owner)
        self._check_scope(current.scope)
        closed = self._closed.get(window_id)
        if closed is not None:
            self._check_snapshot(closed.window.snapshot, current)
            return closed
        window = self._require_window(window_id)
        self._check_snapshot(window.snapshot, current)
        now = _trusted_time(now)
        if now < window.opens_at:
            raise Conflict("The proposal window has not opened.")
        active = {actor_id: self._active_lease(actor_id, now) for actor_id in window.actor_ids}
        if not self._allow_offline_advance and not any(active.values()):
            return None
        proposals = tuple(
            self._proposals[actor_id] for actor_id in window.actor_ids
            if actor_id in self._proposals and self._proposals[actor_id].lease == active[actor_id]
        )
        submitted = {proposal.lease.actor_id for proposal in proposals}
        missing = tuple(actor_id for actor_id in window.actor_ids if actor_id not in submitted)
        if missing and (now < window.deadline or window.timeout_policy == TimeoutPolicy.WAIT):
            return None
        batch = BarrierBatch(
            window=window, proposals=proposals,
            abstained_actor_ids=missing if window.timeout_policy == TimeoutPolicy.ABSTAIN else (),
            skipped_actor_ids=missing if window.timeout_policy == TimeoutPolicy.CONTINUE else (),
        )
        self._finish_window(window_id, batch)
        return batch

    def cancel_window(self, owner: AccessScope, window_id: UUID) -> None:
        self._authorize_owner(owner)
        self._require_window(window_id)
        self._finish_window(window_id, None)

    def _finish_window(self, window_id: UUID, batch: BarrierBatch | None) -> None:
        self._closed[window_id] = batch
        self._window = None
        self._proposals.clear()

    def _require_window(self, window_id: UUID) -> BarrierWindow:
        if self._window is None or self._window.window_id != window_id:
            raise Conflict("The proposal window is not open.")
        return self._window

    def _active_lease(self, actor_id: UUID, now: datetime) -> ActorControlLease | None:
        lease = self._leases.get(actor_id)
        if lease is None or not lease.issued_at <= now < lease.expires_at:
            return None
        try:
            self._authorize_actor(self._member_access(lease.member_id), actor_id)
        except (AccessDenied, NotFound):
            return None
        return lease

    def _member_access(self, member_id: UUID) -> AccessScope:
        user_id = self._members.get(member_id)
        if user_id is None:
            raise AccessDenied("The user is not a member of this room.")
        return AccessScope(tenant_id=self.scope.tenant_id, user_id=user_id)

    def _authorize_owner(self, owner: AccessScope) -> None:
        if owner.tenant_id != self.scope.tenant_id or owner.user_id not in self._members.values():
            raise AccessDenied("The user is not a member of this room.")
        self._authority.authorize_role(owner, self.scope.world_id, {MembershipRole.OWNER})

    def _authorize_actor(self, access: AccessScope, actor_id: UUID) -> None:
        self._authority.authorize_role(access, self.scope.world_id, set(_ACTING_ROLES))
        self._authority.authorize_actor(access, self.scope.world_id, actor_id, branch_id=self.scope.branch_id)

    def _check_scope(self, scope: RoomScope) -> None:
        if scope != self.scope:
            raise AccessDenied("The request belongs to a different room scope.")

    @staticmethod
    def _check_snapshot(expected: RoomSnapshot, current: RoomSnapshot) -> None:
        if current.world_version != expected.world_version:
            raise StaleVersion(expected.world_version, current.world_version)
        if current.world_tick != expected.world_tick:
            raise Conflict("The request belongs to a different world tick.")
