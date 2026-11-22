from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from world_engine.application.room_coordination import (
    BarrierBatch,
    RoomCoordinator,
    RoomMember,
    RoomScope,
    RoomSnapshot,
    TimeoutPolicy,
)
from world_engine.domain.components import InventoryComponent, InventoryLot, MarketComponent, MarketOffer
from world_engine.domain.errors import AccessDenied, Conflict, IdempotencyConflict, InvalidLease, StaleVersion
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


NOW = datetime(2026, 11, 22, tzinfo=UTC)
DEADLINE = NOW + timedelta(seconds=10)
EXPIRY = DEADLINE + timedelta(seconds=20)
RULESET = "room-contract.v1"


@dataclass(frozen=True)
class RoomFixture:
    repository: WorldRepository
    snapshot: RoomSnapshot
    owner: AccessScope
    players: tuple[AccessScope, AccessScope]
    observer: AccessScope
    members: tuple[RoomMember, ...]
    actors: tuple[UUID, UUID]
    listing_id: UUID
    stock_id: UUID

    def coordinator(self, policy=TimeoutPolicy.WAIT, *, offline=False):
        return RoomCoordinator(
            self.snapshot.scope, self.members, self.repository,
            timeout_policy=policy, allow_offline_advance=offline,
        )

    def open(self, room, *, expiry=EXPIRY, window_id=None):
        leases = tuple(
            room.issue_lease(self.owner, self.members[index + 1].member_id, actor, now=NOW, expires_at=expiry)
            for index, actor in enumerate(self.actors)
        )
        window = room.open_window(
            self.owner, window_id or uuid4(), self.snapshot, self.actors, now=NOW, deadline=DEADLINE,
        )
        return window, leases

    def submit(self, room, window, index, lease, *, now=NOW, key="intent-1", quantity="1"):
        return room.submit(
            self.players[index], window.window_id, window.snapshot, lease, idempotency_key=key,
            action={"kind": "trade", "listing_id": self.listing_id, "quantity": quantity}, now=now,
        )


@pytest.fixture
def room_case(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'room-contract.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        tenant_id = uuid4()
        principals = tuple(AccessScope(tenant_id=tenant_id, user_id=uuid4()) for _ in range(4))
        owner, first, second, observer = principals
        actor_a, actor_b = sorted((uuid4(), uuid4()))
        market, seller, listing, stock, lot = (uuid4() for _ in range(5))
        state = WorldState(
            player_id=actor_a,
            entities={
                actor_a: WorldEntity(id=actor_a, kind="character", location_id=market),
                actor_b: WorldEntity(id=actor_b, kind="character", location_id=market),
                seller: WorldEntity(
                    id=seller, kind="organization", location_id=market,
                    components={"inventory": InventoryComponent(lots=[InventoryLot(
                        lot_id=lot, account_id=stock, resource_id="core.ore", quantity="1",
                    )]).model_dump(mode="json")},
                ),
                market: WorldEntity(id=market, kind="place", components={"market": MarketComponent(
                    currency_resource_id="core.coin", offers=[MarketOffer(
                        listing_id=listing, resource_id="core.ore", stock_account_id=stock,
                        seller_id=seller, unit_price_minor=5, minimum_quantity="1",
                    )],
                ).model_dump(mode="json")}),
            },
            resources={
                stock: ResourceAccount(resource_id="core.ore", balance="1", owner_id=seller),
                **{uuid4(): ResourceAccount(resource_id="core.coin", balance="10", owner_id=actor)
                   for actor in (actor_a, actor_b, seller)},
                **{uuid4(): ResourceAccount(resource_id="core.ore", balance="0", owner_id=actor)
                   for actor in (actor_a, actor_b)},
            },
        )
        created = repository.create_world(owner, NewWorld(title="Room contract", state=state))
        world_id = created.world.world_id
        repository.grant_membership(owner, world_id, first.user_id, MembershipRole.PLAYER, controlled_actor_ids=(actor_a,))
        repository.grant_membership(owner, world_id, second.user_id, MembershipRole.PLAYER, controlled_actor_ids=(actor_b,))
        repository.grant_membership(owner, world_id, observer.user_id, MembershipRole.OBSERVER)
        scope = RoomScope(tenant_id=tenant_id, world_id=world_id, branch_id=created.branch.branch_id, room_id=uuid4())
        snapshot = RoomSnapshot(scope=scope, world_version=created.branch.state_version, world_tick=created.branch.current_tick)
        members = tuple(RoomMember(member_id=uuid4(), user_id=principal.user_id) for principal in principals)
        yield RoomFixture(repository, snapshot, owner, (first, second), observer, members, (actor_a, actor_b), listing, stock)
    finally:
        database.dispose()


def test_same_snapshot_contention_reaches_real_market_rules_in_stable_batch_order(room_case):
    scope = room_case.snapshot.scope
    view = room_case.repository.read_world(room_case.owner, scope.world_id, scope.branch_id)

    class MarketRules:
        def resolve(self, batch: BarrierBatch):
            assert batch.window.snapshot == room_case.snapshot
            projected = view.state
            outcomes = []
            for proposal in reversed(batch.proposals):
                # Exercise a rules-side priority independent of batch ordering.
                command = proposal.command
                result = RuleEngine().evaluate(projected, command.payload, RuleContext(
                    world_id=scope.world_id, branch_id=scope.branch_id, actor_id=command.actor_id,
                    command_id=command.command_id, ruleset_version=RULESET,
                ))
                outcomes.append((command.actor_id, result.outcome.code))
                for event in result.events:
                    projected = apply_event(projected, event)
            return outcomes, projected.resources[room_case.stock_id].balance

    window_id = uuid4()
    resolutions = []
    for arrival_order in ((0, 1), (1, 0)):
        room = room_case.coordinator()
        window, leases = room_case.open(room, window_id=window_id)
        for index in arrival_order:
            room_case.submit(room, window, index, leases[index])
        batch = room.close_window(room_case.owner, window_id, room_case.snapshot, now=NOW)
        assert batch is not None
        assert tuple(p.command.actor_id for p in batch.proposals) == room_case.actors
        assert all(p.command.expected_state_version == room_case.snapshot.world_version for p in batch.proposals)
        resolutions.append(batch.resolve(MarketRules()))

    assert resolutions[0] == resolutions[1]
    outcomes, stock_remaining = resolutions[0]
    assert outcomes == [(room_case.actors[1], "trade_completed"), (room_case.actors[0], "insufficient_stock")]
    assert stock_remaining == 0
    after = room_case.repository.read_world(room_case.owner, scope.world_id, scope.branch_id)
    assert after.branch == view.branch
    assert after.state.resources[room_case.stock_id].balance == 1


def test_owner_and_authenticated_actor_control_are_rechecked_with_expiry_and_fencing(room_case):
    room = room_case.coordinator()
    member = room_case.members[1].member_id
    actor = room_case.actors[0]
    with pytest.raises(AccessDenied):
        room.issue_lease(room_case.players[0], member, actor, now=NOW, expires_at=EXPIRY)
    with pytest.raises(AccessDenied):
        room.issue_lease(room_case.owner, room_case.members[3].member_id, actor, now=NOW, expires_at=EXPIRY)
    with pytest.raises(AccessDenied):
        room.issue_lease(room_case.owner, member, room_case.actors[1], now=NOW, expires_at=EXPIRY)

    window, (old, other) = room_case.open(room, expiry=DEADLINE)
    with pytest.raises(Conflict):
        room.issue_lease(room_case.owner, member, actor, now=NOW, expires_at=EXPIRY)
    with pytest.raises(AccessDenied):
        room_case.submit(room, window, 1, old)
    with pytest.raises(InvalidLease):
        room_case.submit(room, window, 0, old, now=DEADLINE)

    fresh = room.issue_lease(room_case.owner, member, actor, now=DEADLINE, expires_at=EXPIRY)
    assert fresh.fencing_token == old.fencing_token + 1
    with pytest.raises(InvalidLease):
        room.revoke_lease(room_case.owner, old)
    room_case.submit(room, window, 0, fresh, now=DEADLINE)
    room.revoke_lease(room_case.owner, fresh)
    with pytest.raises(InvalidLease):
        room_case.submit(room, window, 0, fresh, now=DEADLINE, key="another-request")
    assert room.close_window(room_case.owner, window.window_id, window.snapshot, now=DEADLINE) is None

    scope = room_case.snapshot.scope
    room_case.repository.grant_membership(room_case.owner, scope.world_id, room_case.players[0].user_id, MembershipRole.OBSERVER)
    with pytest.raises(AccessDenied):
        room_case.submit(room, window, 0, fresh, now=DEADLINE)
    with pytest.raises(InvalidLease):
        room_case.submit(room, window, 1, other, now=DEADLINE)


def test_scope_snapshot_and_idempotency_are_bound_to_the_submission(room_case):
    room = room_case.coordinator()
    window, (lease, other) = room_case.open(room)
    actor = room_case.players[0]
    action = {"kind": "trade", "listing_id": room_case.listing_id, "quantity": "1"}
    wrong_tenant = AccessScope(tenant_id=uuid4(), user_id=actor.user_id)
    with pytest.raises(AccessDenied):
        room.submit(wrong_tenant, window.window_id, window.snapshot, lease, idempotency_key="x", action=action, now=NOW)
    for field in ("tenant_id", "world_id", "branch_id", "room_id"):
        misplaced = window.snapshot.scope.model_copy(update={field: uuid4()})
        with pytest.raises(AccessDenied):
            room_case.submit(room, window, 0, lease.model_copy(update={"scope": misplaced}))
    stale = window.snapshot.model_copy(update={"world_version": window.snapshot.world_version + 1})
    with pytest.raises(StaleVersion):
        room.submit(actor, window.window_id, stale, lease, idempotency_key="x", action=action, now=NOW)
    with pytest.raises(StaleVersion):
        room.close_window(room_case.owner, window.window_id, stale, now=NOW)
    wrong_tick = window.snapshot.model_copy(update={"world_tick": window.snapshot.world_tick + 1})
    with pytest.raises(Conflict, match="different world tick"):
        room.close_window(room_case.owner, window.window_id, wrong_tick, now=NOW)

    proposal = room_case.submit(room, window, 0, lease)
    assert room_case.submit(room, window, 0, lease) is proposal
    with pytest.raises(IdempotencyConflict):
        room_case.submit(room, window, 0, lease, quantity="2")
    with pytest.raises(Conflict, match="already submitted"):
        room_case.submit(room, window, 0, lease, key="another-request")
    room_case.submit(room, window, 1, other)
    batch = room.close_window(room_case.owner, window.window_id, window.snapshot, now=NOW)
    assert batch is not None
    mutated = proposal.command
    mutated.payload["quantity"] = "50"
    assert batch.proposals[0].command.payload["quantity"] == "1"
    assert room_case.submit(room, window, 0, lease, now=EXPIRY) is proposal
    assert room.close_window(room_case.owner, window.window_id, window.snapshot, now=EXPIRY) is batch
    with pytest.raises(StaleVersion):
        room.close_window(room_case.owner, window.window_id, stale, now=EXPIRY)


@pytest.mark.parametrize("remove_control", ["world_grant", "room_lease"])
def test_accepted_proposal_loses_eligibility_when_control_is_withdrawn(room_case, remove_control):
    room = room_case.coordinator(TimeoutPolicy.ABSTAIN)
    window, (first, second) = room_case.open(room)
    room_case.submit(room, window, 0, first)
    room_case.submit(room, window, 1, second)
    if remove_control == "world_grant":
        room_case.repository.grant_membership(
            room_case.owner, window.snapshot.scope.world_id, room_case.players[0].user_id, MembershipRole.PLAYER,
        )
    else:
        room.revoke_lease(room_case.owner, first)

    assert room.close_window(room_case.owner, window.window_id, window.snapshot, now=NOW) is None
    batch = room.close_window(room_case.owner, window.window_id, window.snapshot, now=DEADLINE)
    assert batch is not None
    assert tuple(proposal.command.actor_id for proposal in batch.proposals) == (second.actor_id,)
    assert batch.abstained_actor_ids == (first.actor_id,)


@pytest.mark.parametrize("policy", list(TimeoutPolicy))
def test_timeout_requires_explicit_room_policy(room_case, policy):
    room = room_case.coordinator(policy)
    window, (first, second) = room_case.open(room)
    room_case.submit(room, window, 0, first)
    assert room.close_window(room_case.owner, window.window_id, window.snapshot, now=NOW) is None
    if policy == TimeoutPolicy.WAIT:
        assert room.close_window(room_case.owner, window.window_id, window.snapshot, now=DEADLINE) is None
        room_case.submit(room, window, 1, second, now=DEADLINE)
    else:
        with pytest.raises(Conflict, match="submission interval"):
            room_case.submit(room, window, 1, second, now=DEADLINE)
    batch = room.close_window(room_case.owner, window.window_id, window.snapshot, now=DEADLINE)
    assert batch is not None
    missing = (second.actor_id,)
    assert len(batch.proposals) == (2 if policy == TimeoutPolicy.WAIT else 1)
    assert batch.abstained_actor_ids == (missing if policy == TimeoutPolicy.ABSTAIN else ())
    assert batch.skipped_actor_ids == (missing if policy == TimeoutPolicy.CONTINUE else ())


def test_room_pauses_without_active_players_unless_owner_enables_offline_policy(room_case):
    room = room_case.coordinator(TimeoutPolicy.CONTINUE)
    with pytest.raises(Conflict, match="no active players"):
        room.open_window(room_case.owner, uuid4(), room_case.snapshot, (), now=NOW, deadline=DEADLINE)
    window, (first, _) = room_case.open(room, expiry=DEADLINE)
    room_case.submit(room, window, 0, first)
    assert room.close_window(room_case.owner, window.window_id, window.snapshot, now=DEADLINE) is None
    room.cancel_window(room_case.owner, window.window_id)
    with pytest.raises(Conflict):
        room.open_window(room_case.owner, window.window_id, room_case.snapshot, (), now=NOW, deadline=DEADLINE)

    offline_room = room_case.coordinator(TimeoutPolicy.CONTINUE, offline=True)
    offline_window = offline_room.open_window(room_case.owner, uuid4(), room_case.snapshot, (), now=NOW, deadline=DEADLINE)
    batch = offline_room.close_window(room_case.owner, offline_window.window_id, offline_window.snapshot, now=DEADLINE)
    assert batch is not None and not batch.proposals
