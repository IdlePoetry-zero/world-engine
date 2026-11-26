from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from sqlite3 import SQLITE_BUSY, SQLITE_FULL, connect
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select

from world_engine.application.backups import BackupApplication
from world_engine.application.media_authorization import RepositoryMediaAuthorizer
from world_engine.application.media_contracts import AssetScope, MediaRequest, MediaSubscription, PlannedMediaRequest
from world_engine.domain.errors import (
    AccessDenied,
    Conflict,
    InvalidLease,
    IdempotencyConflict,
    InsufficientResources,
    InsufficientStorage,
    NotFound,
    StaleVersion,
    StorageBusy,
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
from world_engine.domain.gameplay_events import (
    AuthorComponentRevisedEvent, BackgroundNeedsAdvancedEvent, BackgroundRoutineAdvancedEvent, ProvenanceReference,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.reducers import author_value_hash
from world_engine.storage.database import Database
from world_engine.storage.models import BranchRow, CommandRow, EventRow, OutboxRow
from world_engine.storage.repository import WorldRepository
from world_engine.rules import RuleContext, RuleEngine
from world_engine.simulation import LongActionPlan


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


def test_branch_json_columns_are_deferred_for_world_reads_and_event_pages(world_repo):
    scope, created, player_id, *_ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    world_repo.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            actor_id=player_id,
            idempotency_key="deferred-branch-json",
            expected_state_version=0,
            command_type="core.tick",
        ),
        [TimeAdvancedEvent(to_tick=1)],
    )
    child = world_repo.fork(scope, world_id, branch_id, 1, "deferred-read")

    branch_selects: list[str] = []

    def capture_branch_select(connection, cursor, statement, parameters, context, executemany):
        normalized = statement.lower()
        if normalized.lstrip().startswith("select") and "world_branches" in normalized:
            branch_selects.append(normalized)

    event.listen(world_repo.database.engine, "before_cursor_execute", capture_branch_select)
    try:
        view = world_repo.read_world(scope, world_id, branch_id)
    finally:
        event.remove(world_repo.database.engine, "before_cursor_execute", capture_branch_select)
    assert view.state.current_tick == 1
    assert len(branch_selects) == 1
    assert "state_json" in branch_selects[0]
    assert "initial_state_json" not in branch_selects[0]

    branch_selects.clear()
    event.listen(world_repo.database.engine, "before_cursor_execute", capture_branch_select)
    try:
        page = world_repo.list_events(scope, world_id, child.branch_id)
    finally:
        event.remove(world_repo.database.engine, "before_cursor_execute", capture_branch_select)
    assert [record.type for record in page.events] == ["core.time_advanced.v1"]
    assert len(branch_selects) == 2
    assert all("state_json" not in statement for statement in branch_selects)
    assert all("initial_state_json" not in statement for statement in branch_selects)


def test_improvised_check_rejects_fabricated_observation_and_preserves_replay(world_repo):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    place, actor = uuid4(), uuid4()
    wid, bid = UUID(int=1), UUID(int=2)
    world_repo.create_world(scope, NewWorld(world_id=wid, branch_id=bid, title="Examination", state=WorldState(
        player_id=actor,
        entities={
            place: WorldEntity(id=place, kind="place", components={"public_description": "A date was carved into the stone."}),
            actor: WorldEntity(id=actor, kind="character", location_id=place,
                               components={"skills": {"skills": {"skill.investigation": {"level": "90"}}}}),
        },
    )))
    before = world_repo.read_world(scope, wid, bid)
    engine = RuleEngine()
    for seed in range(1, 12):
        context = RuleContext(world_id=wid, branch_id=bid, command_id=UUID(int=seed), actor_id=actor,
                              ruleset_version=before.world.ruleset_version)
        resolution = engine.evaluate(before.state, {"kind": "improvised_check", "target_id": place,
                                                     "skill_id": "skill.investigation"}, context)
        if resolution.outcome.status == "succeeded":
            break
    assert resolution.outcome.status == "succeeded"
    command = CommandEnvelope(command_id=context.command_id, actor_id=actor, idempotency_key="check",
                              expected_state_version=0, command_type="action.improvised_check",
                              payload={"kind": "improvised_check", "target_id": str(place), "skill_id": "skill.investigation"})
    clock, audit, observation = resolution.events
    with pytest.raises(InvalidEvent, match="command proposal"):
        world_repo.commit_command(scope, wid, bid, command.model_copy(update={"payload": {}}), resolution.events)
    forged = observation.model_copy(update={"record": observation.record.model_copy(update={"claim": "Fabricated clue."})})
    with pytest.raises(InvalidEvent, match="verified description"):
        world_repo.commit_command(scope, wid, bid, command, [clock, audit, forged])
    with pytest.raises(InvalidEvent, match="produce one observation"):
        world_repo.commit_command(scope, wid, bid, command, [clock, audit])
    assert world_repo.read_world(scope, wid, bid).last_seq == 0
    result = world_repo.commit_command(scope, wid, bid, command, resolution.events)
    assert world_repo.replay(scope, wid, bid).state_hash == result.state_hash
    backups = BackupApplication(world_repo)
    assert backups.validate_archive(backups.export_world(scope, wid)).summary()["state_hashes"][str(bid)] == result.state_hash


def test_world_advance_job_requires_owner_and_binds_commits_to_owner(world_repo):
    owner, created, player, _, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    editor = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
    world_repo.grant_membership(owner, world_id, editor.user_id, MembershipRole.EDITOR)
    new_job = NewJob(world_id=world_id, branch_id=branch_id, job_type="simulation.world_advance",
                     idempotency_key="advance")
    with pytest.raises(AccessDenied):
        world_repo.enqueue_job(editor, new_job)
    world_repo.grant_membership(owner, world_id, editor.user_id, MembershipRole.OWNER)
    with pytest.raises(AccessDenied):
        world_repo.enqueue_job(editor, new_job)
    job = world_repo.enqueue_job(owner, new_job)
    with pytest.raises(AccessDenied):
        world_repo.control_job(editor, job.job_id, "pause")
    lease = world_repo.claim_job("worker", job_types=("simulation.long_action", "simulation.world_advance"))
    assert lease is not None and lease.job_id == job.job_id
    command = CommandEnvelope(actor_id=player, idempotency_key="advance:1", expected_state_version=0,
                              command_type="simulation.window")
    with pytest.raises(AccessDenied):
        world_repo.commit_command(owner, world_id, branch_id, command, [TimeAdvancedEvent(to_tick=10)], lease=lease)
    authorized = command.model_copy(update={"actor_id": owner.user_id})
    world_repo.commit_command(owner, world_id, branch_id, authorized, [TimeAdvancedEvent(to_tick=10)], lease=lease)
    persisted = world_repo.read_job(owner, job.job_id)
    assert persisted.committed_through_tick == 10 and persisted.payload["committed_state_version"] == 1


def test_player_controls_only_long_actions_for_the_plan_actor_they_control(world_repo):
    owner, created, actor, npc, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    controller = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
    other_player = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
    observer = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
    world_repo.grant_membership(
        owner, world_id, controller.user_id, MembershipRole.PLAYER, controlled_actor_ids=(actor,),
    )
    world_repo.grant_membership(
        owner, world_id, other_player.user_id, MembershipRole.PLAYER, controlled_actor_ids=(npc,),
    )
    world_repo.grant_membership(owner, world_id, observer.user_id, MembershipRole.OBSERVER)

    plan = LongActionPlan(actor_id=actor, started_tick=0, target_tick=60, activity="wait")

    def enqueue(key: str, payload: dict):
        return world_repo.enqueue_job(owner, NewJob(
            world_id=world_id,
            branch_id=branch_id,
            job_type="simulation.long_action",
            idempotency_key=key,
            payload=payload,
        ))

    job = enqueue("controlled-long-action", {"plan": plan.model_dump(mode="json")})
    assert world_repo.control_job(controller, job.job_id, "pause").status is JobStatus.PAUSED
    assert world_repo.control_job(controller, job.job_id, "resume").status is JobStatus.QUEUED
    assert world_repo.control_job(controller, job.job_id, "cancel").status is JobStatus.CANCELLED

    other_player_job = enqueue("other-player-long-action", {"plan": plan.model_dump(mode="json")})
    with pytest.raises(AccessDenied):
        world_repo.control_job(other_player, other_player_job.job_id, "pause")
    with pytest.raises(AccessDenied):
        world_repo.control_job(observer, other_player_job.job_id, "pause")

    malformed_job = enqueue("malformed-long-action", {"plan": plan.model_dump(mode="json") | {"unexpected": True}})
    with pytest.raises(AccessDenied):
        world_repo.control_job(controller, malformed_job.job_id, "pause")


def test_world_advance_routine_batch_rejects_unleased_and_wrong_intervals(world_repo):
    scope, created, player, _, _, _ = create_sample_world(world_repo)
    wid, bid = created.world.world_id, created.branch.branch_id
    command = CommandEnvelope(actor_id=scope.user_id, idempotency_key="routine", expected_state_version=0,
                              command_type="simulation.window")
    routine = BackgroundRoutineAdvancedEvent(
        actor_id=scope.user_id, include_actor=True, from_tick=1, to_tick=10,
        expected_agents=[{"entity_id": player, "expected_needs_revision": 1}],
        provenance=[ProvenanceReference(kind="rule", reference_id="background.routine")],
    )
    clock = TimeAdvancedEvent(to_tick=10)
    with pytest.raises(AccessDenied, match="world advancement job"):
        world_repo.commit_command(scope, wid, bid, command, [clock, routine])
    job = world_repo.enqueue_job(scope, NewJob(world_id=wid, branch_id=bid, job_type="simulation.world_advance",
                                               idempotency_key="routine"))
    lease = world_repo.claim_job("routine-worker", job_type="simulation.world_advance")
    with pytest.raises(InvalidEvent, match="exact clock advance"):
        world_repo.commit_command(scope, wid, bid, command, [clock, routine], lease=lease)
    wrong_actor = routine.model_copy(update={"from_tick": 0, "actor_id": player})
    with pytest.raises(InvalidEvent, match="exact clock advance"):
        world_repo.commit_command(scope, wid, bid, command, [clock, wrong_actor], lease=lease)
    assert world_repo.read_world(scope, wid, bid).last_seq == 0
    assert world_repo.read_job(scope, job.job_id).committed_through_tick == 0


def test_background_needs_reject_wrong_interval_atomically(world_repo: WorldRepository) -> None:
    actor, npc = uuid4(), uuid4()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    created = world_repo.create_world(scope, NewWorld(title="Background clock", state=WorldState(
        player_id=actor, entities={
            actor: WorldEntity(id=actor, kind="character", name="Mira"),
            npc: WorldEntity(id=npc, kind="character", name="Sora",
                             components={"needs": {"water": {"value": 70}}}),
        },
    )))
    wid, bid = created.world.world_id, created.branch.branch_id
    command = CommandEnvelope(actor_id=actor, idempotency_key="wait", expected_state_version=0,
                              command_type="action.wait")
    clock = TimeAdvancedEvent(to_tick=3600)
    background = BackgroundNeedsAdvancedEvent(
        actor_id=actor, from_tick=1, to_tick=3600,
        expected_agents=[{"entity_id": npc, "expected_revision": 1}],
        provenance=[ProvenanceReference(kind="rule", reference_id="background.needs")],
    )
    with pytest.raises(InvalidEvent, match="exact clock advance"):
        world_repo.commit_command(scope, wid, bid, command, [clock, background])
    unchanged = world_repo.read_world(scope, wid, bid)
    assert unchanged.last_seq == unchanged.state.current_tick == 0
    assert unchanged.state.entities[npc].components["needs"]["revision"] == 1
    correct = background.model_copy(update={"from_tick": 0})
    committed = world_repo.commit_command(scope, wid, bid, command, [clock, correct])
    assert world_repo.replay(scope, wid, bid).state_hash == committed.state_hash
    backups = BackupApplication(world_repo)
    verified = backups.validate_archive(backups.export_world(scope, wid))
    assert verified.summary()["state_hashes"][str(bid)] == committed.state_hash
    with world_repo._transaction(write=True) as session:
        row = session.get(EventRow, (bid, 2))
        row.payload_json = {**row.payload_json, "from_tick": 1}
    with pytest.raises(Conflict, match="exact clock advance"):
        world_repo.replay(scope, wid, bid)


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


def test_compact_receipts_and_outbox_hydrate_with_legacy_inline_compatibility(world_repo: WorldRepository) -> None:
    scope, created, player_id, _npc_id, source_account, target_account = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="compact-receipt-once",
        expected_state_version=0,
        command_type="core.transfer",
        payload={"quantity": "2.5"},
    )
    events = [
        ResourceTransferredEvent(
            from_account_id=source_account,
            to_account_id=target_account,
            resource_id="core.silver",
            quantity="2.5",
        ),
        TimeAdvancedEvent(to_tick=30),
    ]
    committed = world_repo.commit_command(scope, world_id, branch_id, command, events)
    expected_payloads = {record.event_id: record.model_dump(mode="json") for record in committed.events}

    with world_repo._transaction() as session:
        command_row = session.get(CommandRow, command.command_id)
        outbox_rows = session.scalars(select(OutboxRow).where(OutboxRow.branch_id == branch_id)).all()
        assert command_row.result_json["events"] == []
        assert command_row.result_json["events_storage"] == {
            "kind": "world_event_rows",
            "version": 1,
            "branch_id": str(branch_id),
            "first_seq": committed.first_seq,
            "last_seq": committed.last_seq,
            "count": len(committed.events),
        }
        assert all(set(item.payload_json) == {"payload_storage"} for item in outbox_rows)
        assert all(item.payload_json["payload_storage"]["kind"] == "world_event_record" for item in outbox_rows)

    receipt = world_repo.read_command_receipt(
        scope, world_id, branch_id, command.command_id, command.actor_id, command.idempotency_key
    )
    assert receipt == committed
    duplicate = world_repo.commit_command(scope, world_id, branch_id, command, events)
    assert duplicate.duplicate is True
    assert duplicate.events == committed.events
    outbox = world_repo.pending_outbox()
    assert {item.event_id: item.payload for item in outbox} == expected_payloads

    # Simulate rows written before payload compaction: command receipts retain
    # their inline event array and outbox payloads retain full EventRecord JSON.
    # Keep one compact outbox row to exercise mixed-format reads in one page.
    with world_repo._transaction(write=True) as session:
        command_row = session.get(CommandRow, command.command_id)
        command_row.result_json = committed.model_dump(mode="json")
        outbox_rows = session.scalars(
            select(OutboxRow).where(OutboxRow.branch_id == branch_id).order_by(OutboxRow.event_id)
        ).all()
        outbox_rows[0].payload_json = expected_payloads[outbox_rows[0].event_id]

    legacy_receipt = world_repo.read_command_receipt(
        scope, world_id, branch_id, command.command_id, command.actor_id, command.idempotency_key
    )
    assert legacy_receipt == committed
    mixed_outbox = world_repo.pending_outbox()
    assert {item.event_id: item.payload for item in mixed_outbox} == expected_payloads


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
    world_repo.authorize_actor_control(player_scope, created.world.world_id, player_id)
    with pytest.raises(AccessDenied):
        world_repo.authorize_actor_control(player_scope, created.world.world_id, npc_id)
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


def test_author_revision_requires_owner_editor_authenticated_author_identity(world_repo: WorldRepository) -> None:
    tenant_id, owner_id, character_id = uuid4(), uuid4(), uuid4()
    owner_scope = AccessScope(tenant_id=tenant_id, user_id=owner_id)
    created = world_repo.create_world(
        owner_scope,
        NewWorld(
            title="Authoring",
            state=WorldState(
                entities={
                    character_id: WorldEntity(
                        id=character_id,
                        kind="character",
                        name="Mira",
                        components={"public_description": "Old profile."},
                    )
                }
            ),
        ),
    )

    def revision(author_id, replacement="Updated profile."):
        return AuthorComponentRevisedEvent(
            entity_id=character_id,
            author_id=author_id,
            component_name="public_description",
            expected_previous_hash=author_value_hash("Old profile."),
            replacement=replacement,
            revision_note="Update the NPC profile.",
            provenance=[ProvenanceReference(kind="pack", reference_id="authoring-pack")],
        )

    wrong_command = CommandEnvelope(
        actor_id=owner_id,
        idempotency_key="author-event-wrong-command",
        expected_state_version=0,
        command_type="core.rename",
    )
    with pytest.raises(AccessDenied, match="command_type"):
        world_repo.commit_command(
            owner_scope,
            created.world.world_id,
            created.branch.branch_id,
            wrong_command,
            [revision(owner_id)],
        )

    wrong_actor = CommandEnvelope(
        actor_id=character_id,
        idempotency_key="author-event-wrong-actor",
        expected_state_version=0,
        command_type="author.revise_component",
    )
    with pytest.raises(AccessDenied, match="authenticated user"):
        world_repo.commit_command(
            owner_scope,
            created.world.world_id,
            created.branch.branch_id,
            wrong_actor,
            [revision(owner_id)],
        )

    allowed = CommandEnvelope(
        actor_id=owner_id,
        idempotency_key="author-event-owner",
        expected_state_version=0,
        command_type="author.revise_component",
    )
    world_repo.commit_command(
        owner_scope,
        created.world.world_id,
        created.branch.branch_id,
        allowed,
        [revision(owner_id)],
    )
    assert (
        world_repo.read_world(owner_scope, created.world.world_id)
        .state.entities[character_id]
        .components["public_description"]
        == "Updated profile."
    )


def test_replay_rejects_branch_head_integrity_mismatch(world_repo: WorldRepository) -> None:
    scope, created, actor, _, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    world_repo.commit_command(scope, world_id, branch_id, CommandEnvelope(
        actor_id=actor, idempotency_key="tick", expected_state_version=0, command_type="core.time",
    ), [TimeAdvancedEvent(to_tick=60)])
    with world_repo._transaction(write=True) as session:
        session.get(BranchRow, branch_id).state_hash = "0" * 64
    assert world_repo.replay(scope, world_id, branch_id, through_seq=0).state.current_tick == 0
    with pytest.raises(Conflict, match="differs from its event replay"):
        world_repo.replay(scope, world_id, branch_id)


def test_branch_projection_reads_isolate_callers_and_check_database_payload(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    first = world_repo.read_world(scope, world_id, branch_id)
    first.state.entities[player_id].components["public_description"] = "Caller edit"
    second = world_repo.read_world(scope, world_id, branch_id)
    assert "public_description" not in second.state.entities[player_id].components
    assert world_repo._branch_state_cache.cache_info().hits == 1

    with world_repo._transaction(write=True) as session:
        branch = session.get(BranchRow, branch_id)
        payload = branch.state_json
        payload["entities"][str(player_id)]["name"] = "Tampered name"
        session.execute(BranchRow.__table__.update().where(BranchRow.branch_id == branch_id).values(state_json=payload))

    with pytest.raises(Conflict, match="projection failed its integrity check"):
        world_repo.read_world(scope, world_id, branch_id)


def test_committed_head_is_reused_for_the_next_commit_and_replays_exactly(
    world_repo: WorldRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, created, player_id, *_ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    first = world_repo.commit_command(
        scope, world_id, branch_id,
        CommandEnvelope(
            actor_id=player_id, idempotency_key="cache-seed-1", expected_state_version=0,
            command_type="core.time",
        ),
        [TimeAdvancedEvent(to_tick=60)],
    )

    def unexpected_validation(_payload):
        raise AssertionError("the committed head should be authorized by its fresh DB fingerprint")

    with monkeypatch.context() as cache_hit:
        cache_hit.setattr(world_repo._branch_state_cache, "_validate_payload", unexpected_validation)
        second = world_repo.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=player_id, idempotency_key="cache-seed-2", expected_state_version=1,
                command_type="core.time",
            ),
            [TimeAdvancedEvent(to_tick=120)],
        )

    assert first.state_version == 1
    assert second.state_version == 2
    assert world_repo.replay(scope, world_id, branch_id).state_hash == second.state_hash

    public_view = world_repo.read_world(scope, world_id, branch_id)
    public_view.state.metadata["public_mutation"] = True
    assert "public_mutation" not in world_repo.read_world(scope, world_id, branch_id).state.metadata


def test_commit_cache_hit_still_rejects_a_freshly_tampered_database_projection(
    world_repo: WorldRepository,
) -> None:
    scope, created, player_id, *_ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    world_repo.commit_command(
        scope, world_id, branch_id,
        CommandEnvelope(
            actor_id=player_id, idempotency_key="cache-tamper-seed", expected_state_version=0,
            command_type="core.time",
        ),
        [TimeAdvancedEvent(to_tick=60)],
    )
    with world_repo._transaction(write=True) as session:
        branch = session.get(BranchRow, branch_id)
        tampered_payload = {**branch.state_json, "current_tick": 61}
        session.execute(
            BranchRow.__table__.update().where(BranchRow.branch_id == branch_id)
            .values(state_json=tampered_payload),
        )

    with pytest.raises(Conflict, match="projection failed its integrity check"):
        world_repo.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=player_id, idempotency_key="cache-tamper-reject", expected_state_version=1,
                command_type="core.time",
            ),
            [TimeAdvancedEvent(to_tick=120)],
        )
    with world_repo._transaction() as session:
        branch = session.get(BranchRow, branch_id)
        assert branch.state_version == 1
        assert branch.head_seq == 1


def test_rolled_back_commit_does_not_publish_its_uncommitted_state(
    world_repo: WorldRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from world_engine.storage import repository as repository_module

    scope, created, player_id, *_ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    world_repo.commit_command(
        scope, world_id, branch_id,
        CommandEnvelope(
            actor_id=player_id, idempotency_key="cache-rollback-seed", expected_state_version=0,
            command_type="core.time",
        ),
        [TimeAdvancedEvent(to_tick=60)],
    )

    def fail_event_append(*_args, **_kwargs):
        raise RuntimeError("injected failure before commit")

    monkeypatch.setattr(repository_module, "append_event_row", fail_event_append)
    with pytest.raises(RuntimeError, match="injected failure"):
        world_repo.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=player_id, idempotency_key="cache-rollback-failure", expected_state_version=1,
                command_type="core.time",
            ),
            [TimeAdvancedEvent(to_tick=120)],
        )

    cached = world_repo._branch_state_cache._entries[(world_id, branch_id)]
    assert cached.state_version == 1
    with world_repo._transaction() as session:
        branch = session.get(BranchRow, branch_id)
        assert branch.state_version == 1
        assert branch.current_tick == 60


def test_full_world_read_reuses_verified_text_head_and_returns_detached_state(
    world_repo: WorldRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, created, player_id, npc_id, *_ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    world_repo.read_verified_player_head(scope, world_id, branch_id)

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("a verified text hit should avoid JSON decode and canonical encoding")

    with monkeypatch.context() as fast_path:
        fast_path.setattr(world_repo._branch_state_cache, "_encode_payload", unexpected_decode)
        fast_path.setattr("world_engine.storage.validated_state_cache.json.loads", unexpected_decode)
        view = world_repo.read_world(scope, world_id, branch_id)
    assert view.state.entities[player_id].name == "Mira"
    view.state.metadata["caller_mutation"] = True
    next_view = world_repo.read_world(scope, world_id, branch_id)
    assert "caller_mutation" not in next_view.state.metadata
    _world, _player_id, names = world_repo.read_player_history_context(scope, world_id, branch_id)
    assert names[player_id] == "Mira" and names[npc_id] == "Sora"

    with world_repo._transaction(write=True) as session:
        branch = session.get(BranchRow, branch_id)
        payload = branch.state_json
        payload["entities"][str(npc_id)]["name"] = "Tampered in storage"
        session.execute(BranchRow.__table__.update().where(BranchRow.branch_id == branch_id).values(state_json=payload))

    with pytest.raises(Conflict, match="projection failed its integrity check"):
        world_repo.read_world(scope, world_id, branch_id)


def test_player_history_context_avoids_warm_state_copy_and_checks_hot_fingerprint(
    world_repo: WorldRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope, created, player_id, npc_id, *_ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    branch_selects: list[str] = []

    def capture_branch_select(connection, cursor, statement, parameters, context, executemany):
        normalized = statement.lower()
        if normalized.lstrip().startswith("select") and "world_branches" in normalized:
            branch_selects.append(normalized)

    event.listen(world_repo.database.engine, "before_cursor_execute", capture_branch_select)
    try:
        record, default_player_id, names = world_repo.read_player_history_context(scope, world_id, branch_id)
    finally:
        event.remove(world_repo.database.engine, "before_cursor_execute", capture_branch_select)
    assert record.world_id == world_id
    assert default_player_id == player_id
    assert names[player_id] == "Mira"
    assert names[npc_id] == "Sora"
    assert len(branch_selects) == 1
    assert "cast(world_branches.state_json as text)" in branch_selects[0]
    assert "world_branches.state_json," not in branch_selects[0]

    def unexpected_copy(_state):
        raise AssertionError("history context should not copy a warm WorldState graph")

    def unexpected_model_copy(_state, *args, **kwargs):
        raise AssertionError("history context should not clone a warm WorldState graph")

    world_repo._branch_state_cache._copy_state = unexpected_copy
    monkeypatch.setattr(WorldState, "model_copy", unexpected_model_copy)
    _record, warm_player_id, warm_names = world_repo.read_player_history_context(scope, world_id, branch_id)
    assert warm_player_id == default_player_id and warm_names == names

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("a bound player-head fingerprint should avoid JSON work")

    with monkeypatch.context() as fast_path:
        fast_path.setattr(world_repo._branch_state_cache, "_encode_payload", unexpected_decode)
        fast_path.setattr(world_repo._branch_state_cache, "_history_context", unexpected_decode)
        fast_path.setattr("world_engine.storage.validated_state_cache.json.loads", unexpected_decode)
        world_record, branch_record, head_player_id = world_repo.read_verified_player_head(scope, world_id)
    assert world_record.world_id == world_id
    assert branch_record.branch_id == branch_id
    assert head_player_id == default_player_id

    with world_repo._transaction(write=True) as session:
        branch = session.get(BranchRow, branch_id)
        payload = branch.state_json
        payload["entities"][str(npc_id)]["name"] = "Tampered in storage"
        session.execute(BranchRow.__table__.update().where(BranchRow.branch_id == branch_id).values(state_json=payload))

    with pytest.raises(Conflict, match="projection failed its integrity check"):
        world_repo.read_player_history_context(scope, world_id, branch_id)


def test_historical_snapshot_pins_exact_commit_tick(world_repo: WorldRepository) -> None:
    scope, created, player_id, _, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    for version, ticks in enumerate(((5, 10), (20,))):
        command = CommandEnvelope(
            actor_id=player_id,
            idempotency_key=f"advance-{version}",
            expected_state_version=version,
            command_type="core.tick",
        )
        world_repo.commit_command(scope, world_id, branch_id, command, [
            TimeAdvancedEvent(to_tick=tick) for tick in ticks
        ])
    checkpoint = world_repo.snapshot(scope, world_id, branch_id, at_tick=10)
    assert (checkpoint.current_tick, checkpoint.last_seq, checkpoint.state_version) == (10, 2, 1)
    assert world_repo.replay(scope, world_id, branch_id, 2, from_genesis=True).state_hash == checkpoint.state_hash
    assert world_repo.read_world(scope, world_id, branch_id).state.current_tick == 20
    with pytest.raises(Conflict, match="No branch-local commit boundary"):
        world_repo.snapshot(scope, world_id, branch_id, at_tick=5)


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
    inherited_event = inherited.events[0]
    assert world_repo.read_branch_event(
        scope, created.world.world_id, created.branch.branch_id, inherited_event.event_id,
    ) == inherited_event
    with pytest.raises(NotFound):
        world_repo.read_branch_event(scope, created.world.world_id, child.branch_id, inherited_event.event_id)
    commits = []
    audited = world_repo.replay(
        scope, created.world.world_id, child.branch_id, from_genesis=True,
        visit_commit=lambda sequence, state: commits.append((sequence, state.current_tick)),
    )
    assert audited.state_hash == replayed.state_hash
    assert [sequence for sequence, _ in commits] == [0, 2, 3]
    assert commits[-1] == (3, 12)
    with world_repo._transaction(write=True) as session:
        early_event = session.get(EventRow, (created.branch.branch_id, 1))
        early_event.payload_json = {**early_event.payload_json, "to_tick": 6}
    assert world_repo.replay(scope, created.world.world_id, child.branch_id).state_hash == replayed.state_hash
    with pytest.raises(Conflict, match="inconsistent tick"):
        world_repo.replay(scope, created.world.world_id, child.branch_id, from_genesis=True)

    first_page = world_repo.list_events(scope, created.world.world_id, child.branch_id, limit=2)
    assert [event.sequence for event in first_page.events] == [1, 2]
    assert first_page.has_more
    next_page = world_repo.list_events(
        scope, created.world.world_id, child.branch_id, after_seq=first_page.next_cursor, limit=2,
    )
    assert [event.command_id for event in next_page.events] == [child_command.command_id]
    assert not next_page.has_more


def test_template_fork_commits_revisions_atomically_and_retries_after_parent_advances(
    world_repo: WorldRepository,
) -> None:
    scope, created, player_id, npc_id, _, _ = create_sample_world(world_repo)
    world_id, source_id = created.world.world_id, created.branch.branch_id
    child_id = uuid4()
    source = world_repo.read_world(scope, world_id, source_id)
    command = CommandEnvelope(
        actor_id=scope.user_id,
        idempotency_key="template-fork",
        expected_state_version=source.branch.state_version,
        command_type="author.revise_component",
    )
    revisions = [
        AuthorComponentRevisedEvent(
            entity_id=entity_id,
            author_id=scope.user_id,
            component_name="public_description",
            expected_previous_hash=author_value_hash(None),
            replacement=description,
            revision_note="Apply the updated template profile.",
            provenance=[ProvenanceReference(kind="pack", reference_id="template-v2")],
        )
        for entity_id, description in [(player_id, "Mira's new profile."), (npc_id, "Sora's new profile.")]
    ]

    def commit(events, *, branch_id=child_id, source_hash=source.state_hash, label="Template v2"):
        return world_repo.commit_template_fork(
            scope, world_id, source_id, source.last_seq, source_hash, branch_id, label, command, events,
        )

    invalid_revision = revisions[1].model_copy(update={"expected_previous_hash": author_value_hash("wrong")})
    with pytest.raises(InvalidEvent, match="expected_previous_hash"):
        commit([revisions[0], invalid_revision])
    with pytest.raises(Conflict, match="reviewed template checkpoint"):
        commit(revisions, source_hash="0" * 64)
    assert [branch.branch_id for branch in world_repo.list_branches(scope, world_id)] == [source_id]

    result = commit(revisions)
    assert not result.duplicate
    assert result.branch_id == child_id
    assert result.state_version == source.branch.state_version + 1
    assert (result.first_seq, result.last_seq) == (source.last_seq + 1, source.last_seq + 2)
    assert world_repo.read_world(scope, world_id, source_id).state_hash == source.state_hash
    child = world_repo.replay(scope, world_id, child_id, from_genesis=True)
    assert child.state_hash == result.state_hash
    assert child.state.current_tick == source.state.current_tick
    for revision in revisions:
        assert child.state.entities[revision.entity_id].components[revision.component_name] == revision.replacement

    ordinary_branch = world_repo.fork(scope, world_id, source_id, source.last_seq, label="Ordinary fork")
    with pytest.raises(Conflict, match="without this template migration receipt"):
        commit(revisions, branch_id=ordinary_branch.branch_id, label=ordinary_branch.name)

    advancement = CommandEnvelope(
        actor_id=player_id, idempotency_key="parent-advance", expected_state_version=source.branch.state_version,
        command_type="core.tick",
    )
    world_repo.commit_command(scope, world_id, source_id, advancement, [TimeAdvancedEvent(to_tick=5)])
    assert commit(revisions) == result.model_copy(update={"duplicate": True})
    with pytest.raises(IdempotencyConflict):
        commit(revisions[::-1])
    with pytest.raises(Conflict, match="different branch"):
        commit(revisions, label="Another template")
    with pytest.raises(StaleVersion):
        commit(revisions, branch_id=uuid4(), label="Stale template preview")
    assert {branch.branch_id for branch in world_repo.list_branches(scope, world_id)} == {
        source_id, child_id, ordinary_branch.branch_id,
    }


def test_media_job_fences_publication_without_holding_branch_lease(world_repo: WorldRepository) -> None:
    scope, created, _, _, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    media = world_repo.enqueue_job(scope, NewJob(
        world_id=world_id, branch_id=branch_id, job_type="media.generate", idempotency_key="media-job",
        payload={"submitted_by": str(scope.user_id)},
    ))
    other = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    world_repo.grant_membership(scope, world_id, other.user_id, MembershipRole.EDITOR)
    assert world_repo.list_jobs(scope, world_id, branch_id) == [media]
    assert world_repo.list_jobs(other, world_id, branch_id) == []
    assert world_repo.find_job_by_key(other, world_id, branch_id, "media-job") is None
    with pytest.raises(NotFound):
        world_repo.read_job(other, media.job_id)
    with pytest.raises(NotFound):
        world_repo.control_job(other, media.job_id, "cancel")
    assert world_repo.claim_job("normal-worker") is None
    world_repo.enqueue_job(scope, NewJob(
        world_id=world_id, branch_id=branch_id, job_type="simulation.world_advance", idempotency_key="simulation-job",
    ))
    started = utc_now()
    first = world_repo.claim_media_job("first-worker", lease_seconds=2, now=started)
    assert first.job.job_id == media.job_id
    assert world_repo.claim_job("normal-worker", now=started) is not None
    later = started + timedelta(seconds=3)
    second = world_repo.claim_media_job("second-worker", now=later)
    assert second.lease.fencing_token == first.lease.fencing_token + 1
    published = []

    def publish():
        published.append(True)
        return {"asset": "verified"}

    with pytest.raises(InvalidLease):
        world_repo.finish_media_job(first.lease, JobStatus.SUCCEEDED, publish=publish, now=later)
    assert not published
    refreshed = world_repo.renew_media_job(second.lease, now=later)
    result = world_repo.finish_media_job(refreshed, JobStatus.SUCCEEDED, publish=publish, now=later)
    assert result.result == {"asset": "verified"}
    assert world_repo.finish_media_job(refreshed, JobStatus.SUCCEEDED, publish=publish, now=later) == result
    assert published == [True]


def test_media_publication_uses_actual_source_policy_and_branch(world_repo: WorldRepository) -> None:
    scope, created, actor_id, _, _, _ = create_sample_world(world_repo)
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    source = TimeAdvancedEvent(to_tick=5, visibility={"policy": "author_only"})
    world_repo.commit_command(scope, world_id, branch_id, CommandEnvelope(
        actor_id=actor_id, idempotency_key="media-source", expected_state_version=0, command_type="core.tick",
    ), [source])
    request = MediaRequest(request_id=uuid4(), world_id=world_id, branch_id=branch_id,
                           recipient_id=scope.user_id, source_event_ids=(source.event_id,), kind="speech",
                           authorized_description="Time advanced.", visibility=AssetScope(policy="author_only"),
                           idempotency_key="media-authorization")
    plan = PlannedMediaRequest(plugin_id="qa.speech", manifest_version="1", request=request,
                               subscription=MediaSubscription(event_type=source.type, kind="speech"))
    authorizer = RepositoryMediaAuthorizer(world_repo)
    assert authorizer.authorize_plan(scope, world_id, branch_id, plan).scope == scope
    public_request = request.model_copy(update={"visibility": AssetScope(policy="public")})
    with pytest.raises(AccessDenied, match="audience"):
        authorizer.authorize_plan(scope, world_id, branch_id, plan.model_copy(update={"request": public_request}))
    child = world_repo.fork(scope, world_id, branch_id, 1, "Other branch")
    child_request = request.model_copy(update={"branch_id": child.branch_id})
    with pytest.raises(NotFound):
        authorizer.authorize_plan(scope, world_id, child.branch_id, plan.model_copy(update={"request": child_request}))


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

    unleased_action = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="blocked-by-live-writer",
        expected_state_version=0,
        command_type="core.tick",
    )
    with pytest.raises(Conflict, match="active writer lease"):
        world_repo.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            unleased_action,
            [TimeAdvancedEvent(to_tick=1)],
        )

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

    # A retry can still receive its durable receipt while a worker owns
    # the branch; only a new write must wait for the lease to finish.
    duplicate = world_repo.commit_command(
        scope,
        created.world.world_id,
        created.branch.branch_id,
        command,
        [TimeAdvancedEvent(to_tick=1)],
    )
    assert duplicate.duplicate is True

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

        # Distinct identities and keys cannot both commit from the same version.
        scope2, created2, player_id2, npc_id2, _, _ = create_sample_world(repository)
        other_scope = AccessScope(tenant_id=scope2.tenant_id, user_id=uuid4())
        repository.grant_membership(
            scope2, created2.world.world_id, other_scope.user_id, MembershipRole.PLAYER,
            controlled_actor_ids=[npc_id2],
        )
        participants = [(scope2, player_id2), (other_scope, npc_id2)]
        barrier2 = Barrier(2)

        def competing_version_request(index: int) -> str:
            participant, actor_id = participants[index]
            command = CommandEnvelope(
                actor_id=actor_id,
                idempotency_key=f"race-key-{index}",
                expected_state_version=0,
                command_type="core.tick",
            )
            barrier2.wait(timeout=5)
            try:
                repository.commit_command(
                    participant,
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
        events = repository.list_events(scope2, created2.world.world_id, created2.branch.branch_id).events
        assert [event.actor_id for event in events] == [participants[version_results.index("committed")][1]]
        assert repository.replay(scope2, created2.world.world_id, created2.branch.branch_id).state_hash == final.state_hash
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


def test_sqlite_write_lock_preserves_world_head_and_allows_same_command_retry(tmp_path):
    database_path = tmp_path / "busy.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    repository = WorldRepository(database)

    @event.listens_for(database.engine, "checkout")
    def use_short_busy_timeout(connection, _record, _proxy):
        with closing(connection.cursor()) as cursor:
            cursor.execute("PRAGMA busy_timeout=20")

    try:
        database.create_schema()
        scope, created, actor_id, *_ = create_sample_world(repository)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        before = repository.read_world(scope, world_id, branch_id)
        command = CommandEnvelope(
            actor_id=actor_id,
            idempotency_key="retry-after-write-lock",
            expected_state_version=before.branch.state_version,
            command_type="core.tick",
        )
        actions = [TimeAdvancedEvent(to_tick=60)]

        with closing(connect(database_path)) as blocker, blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(StorageBusy) as failure:
                repository.commit_command(scope, world_id, branch_id, command, actions)
            assert failure.value.__cause__.orig.sqlite_errorcode == SQLITE_BUSY
            after_failure = repository.read_world(scope, world_id, branch_id)
            assert after_failure.state_hash == before.state_hash
            assert after_failure.branch.state_version == before.branch.state_version
            assert after_failure.last_seq == before.last_seq
            assert repository.pending_outbox() == []
            assert repository.read_command_receipt(
                scope, world_id, branch_id, command.command_id, actor_id, command.idempotency_key,
            ) is None

        committed = repository.commit_command(scope, world_id, branch_id, command, actions)
        assert not committed.duplicate and committed.last_seq == before.last_seq + 1
        duplicate = repository.commit_command(scope, world_id, branch_id, command, actions)
        assert duplicate.duplicate and duplicate.last_seq == committed.last_seq
        assert [message.event_id for message in repository.pending_outbox()] == [actions[0].event_id]
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == committed.state_hash
    finally:
        database.dispose()


def test_sqlite_full_rolls_back_world_command_and_allows_same_command_retry(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'full.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope, created, actor_id, _, _, _ = create_sample_world(repository)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        before = repository.read_world(scope, world_id, branch_id)
        command = CommandEnvelope(
            actor_id=actor_id, idempotency_key="retry-after-space-restored",
            expected_state_version=0, command_type="core.tick",
            payload={"fixture_padding": "x" * (512 * 1024)},
        )
        actions = [TimeAdvancedEvent(to_tick=60)]

        def cap_database_pages(connection, _record):
            cursor = connection.cursor()
            try:
                pages = cursor.execute("PRAGMA page_count").fetchone()[0]
                cap = cursor.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
                assert cap == pages
            finally:
                cursor.close()

        database.dispose()
        event.listen(database.engine, "connect", cap_database_pages)
        try:
            with pytest.raises(InsufficientStorage) as failure:
                repository.commit_command(scope, world_id, branch_id, command, actions)
            assert failure.value.__cause__.orig.sqlite_errorcode == SQLITE_FULL
            after_failure = repository.read_world(scope, world_id, branch_id)
            assert after_failure.state_hash == before.state_hash and after_failure.last_seq == 0
            assert repository.pending_outbox() == []
            assert repository.read_command_receipt(
                scope, world_id, branch_id, command.command_id, actor_id, command.idempotency_key,
            ) is None
        finally:
            event.remove(database.engine, "connect", cap_database_pages)
            database.dispose()

        completed = repository.commit_command(scope, world_id, branch_id, command, actions)
        assert not completed.duplicate and completed.last_seq == 1
        assert repository.commit_command(scope, world_id, branch_id, command, actions).duplicate
        assert [message.event_id for message in repository.pending_outbox()] == [actions[0].event_id]
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == completed.state_hash
    finally:
        database.dispose()
