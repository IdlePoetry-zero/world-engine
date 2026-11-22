"""SQLite lifecycle, route access, replay, and restore for safe-conduct terms."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import (
    CapabilitiesComponent,
    GovernanceComponent,
    OfficeRecord,
    PlaceComponent,
    PlaceRoute,
)
from world_engine.domain.events import parse_event
from world_engine.domain.errors import AccessDenied, InvalidEvent
from world_engine.domain.gameplay_events import MovementCompletedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import state_hash
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG
from world_engine.domain.safe_conduct_events import SafeConductChangedEvent, safe_conduct_event_id
from world_engine.domain.safe_conduct_state import (
    PROPOSE_SAFE_CONDUCT_COMMAND,
    RESOLVE_SAFE_CONDUCT_COMMAND,
)
from world_engine.rules import ACTION_INTENT_ADAPTER, RuleContext, RuleEngine, parse_action
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _character(entity_id, name, location_id, *, can_travel=False):
    components = {}
    if can_travel:
        components["capabilities"] = CapabilitiesComponent(grants=["world.travel"]).model_dump(mode="json")
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=location_id,
        components=components,
    )


def _organization(entity_id, name, location_id, leader_id, members):
    governance = GovernanceComponent(
        revision=1,
        members=members,
        leader_id=leader_id,
        offices=[OfficeRecord(
            office_id="office.leader",
            title="Leader",
            holder_id=leader_id,
            authority=["organization.lead", "authority.represent"],
        )],
        succession_policy="appointment",
    )
    return WorldEntity(
        id=entity_id,
        kind="organization",
        name=name,
        location_id=location_id,
        components={"governance": governance.model_dump(mode="json")},
    )


def _fixture_state():
    proposer_region_id, recipient_region_id, proposer_id, proposer_traveler_id, second_traveler_id, recipient_id, proposer_org_id, recipient_org_id, route_id = (
        uuid4() for _ in range(9)
    )
    route = PlaceRoute(
        route_id=route_id,
        from_place_id=proposer_region_id,
        to_place_id=recipient_region_id,
        travel_ticks=60,
        access_tags=[SAFE_CONDUCT_TAG],
        travel_modes=["travel.walk"],
    )
    proposer_region = WorldEntity(
        id=proposer_region_id,
        kind="place",
        name="Greyhaven Reach",
        components={"place": PlaceComponent(place_type="region", routes=[route]).model_dump(mode="json")},
    )
    recipient_region = WorldEntity(
        id=recipient_region_id,
        kind="place",
        name="Northbank March",
        components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
    )
    proposer = _character(proposer_id, "Mira Vale", proposer_region_id, can_travel=True)
    proposer_traveler = _character(proposer_traveler_id, "Tomas Reed", proposer_region_id, can_travel=True)
    second_traveler = _character(second_traveler_id, "Elin Ward", proposer_region_id, can_travel=True)
    recipient = _character(recipient_id, "Sera North", recipient_region_id)
    proposer_org = _organization(
        proposer_org_id, "Greyhaven Compact", proposer_region_id,
        proposer_id, [proposer_id, proposer_traveler_id, second_traveler_id],
    )
    recipient_org = _organization(
        recipient_org_id, "Northbank Assembly", recipient_region_id, recipient_id, [recipient_id],
    )
    state = WorldState(
        player_id=proposer_id,
        entities={entity.id: entity for entity in (
            proposer_region, recipient_region, proposer, proposer_traveler, second_traveler,
            recipient, proposer_org, recipient_org,
        )},
    )
    return (
        state, proposer_id, proposer_traveler_id, second_traveler_id, recipient_id,
        proposer_org_id, recipient_org_id, proposer_region_id, recipient_region_id, route_id,
    )


def _evaluate(repository, scope, world_id, branch_id, actor_id, payload):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    normalized = parse_action(payload).model_dump(mode="json")
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=view.world.ruleset_version,
    )
    resolution = RuleEngine().evaluate(view.state, normalized, context)
    return view, command_id, normalized, resolution


def _commit_action(repository, scope, world_id, branch_id, actor_id, payload, *, idempotency_key):
    view, command_id, normalized, resolution = _evaluate(
        repository, scope, world_id, branch_id, actor_id, payload,
    )
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    if normalized["kind"] == "propose_safe_conduct":
        command_type = PROPOSE_SAFE_CONDUCT_COMMAND
    elif normalized["kind"] == "resolve_safe_conduct":
        command_type = RESOLVE_SAFE_CONDUCT_COMMAND
    else:
        command_type = f"action.{normalized['kind']}"
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_state_version=view.branch.state_version,
        command_type=command_type,
        payload=normalized,
    )
    committed = repository.commit_command(scope, world_id, branch_id, command, resolution.events)
    return committed, resolution


def _assert_move_blocked(repository, scope, world_id, branch_id, actor_id, destination_id, route_id):
    before = repository.read_world(scope, world_id, branch_id)
    _view, _command_id, _payload, resolution = _evaluate(
        repository,
        scope,
        world_id,
        branch_id,
        actor_id,
        {"kind": "move", "destination_id": str(destination_id), "route_id": str(route_id)},
    )
    assert resolution.outcome.status == "rejected"
    assert resolution.outcome.code == "route_access_denied"
    assert resolution.events == []
    assert resolution.elapsed_ticks == 0
    after = repository.read_world(scope, world_id, branch_id)
    assert after.branch.state_version == before.branch.state_version
    assert after.state_hash == before.state_hash
    assert after.state.current_tick == before.state.current_tick
    assert after.state.entities[actor_id].location_id == before.state.entities[actor_id].location_id


def _typed_safe_conduct_events(repository, scope, world_id, branch_id):
    rows = repository.list_events(scope, world_id, branch_id, limit=100).events
    return [
        parse_event({
            "type": row.type,
            "event_id": str(row.event_id),
            "visibility": row.visibility,
            **row.payload,
        })
        for row in rows
        if row.type == "core.safe_conduct_changed.v1"
    ]


def test_bilateral_safe_conduct_controls_route_access_across_branches_and_restore(tmp_path):
    source_db = Database(f"sqlite:///{(tmp_path / 'safe-conduct-source.sqlite').as_posix()}")
    source_db.create_schema()
    restored_db = Database(f"sqlite:///{(tmp_path / 'safe-conduct-restored.sqlite').as_posix()}")
    restored_db.create_schema()
    repository = WorldRepository(source_db)
    restored_repository = WorldRepository(restored_db)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        (
            state, proposer_id, proposer_traveler_id, second_traveler_id, recipient_id,
            proposer_org_id, recipient_org_id, proposer_region_id, recipient_region_id, route_id,
        ) = _fixture_state()
        created = repository.create_world(
            scope,
            NewWorld(title="Greyhaven safe conduct", ruleset_version="safe-conduct-storage.v1", state=state),
        )
        world_id, main_branch = created.world.world_id, created.branch.branch_id

        # Without a ratified treaty, the ordinary route rule denies passage.
        _assert_move_blocked(
            repository, scope, world_id, main_branch, proposer_traveler_id, recipient_region_id, route_id,
        )

        proposal, proposal_resolution = _commit_action(
            repository,
            scope,
            world_id,
            main_branch,
            proposer_id,
            {"kind": "propose_safe_conduct", "proposer_organization_id": str(proposer_org_id),
             "recipient_organization_id": str(recipient_org_id), "from_place_id": str(proposer_region_id),
             "route_id": str(route_id), "expires_at_tick": 3_600},
            idempotency_key="propose-greyhaven-northbank-safe-conduct",
        )
        proposal_event = proposal_resolution.events[0]
        assert isinstance(proposal_event, SafeConductChangedEvent)
        assert proposal_event.operation == "propose"
        treaty_id = proposal_event.resulting_treaty.treaty_id
        assert proposal_event.resulting_treaty.status == "pending"
        assert proposal_event.resulting_treaty.proposer_actor_id == proposer_id
        assert proposal_event.resulting_treaty.recipient_actor_id == recipient_id
        _assert_move_blocked(
            repository, scope, world_id, main_branch, proposer_traveler_id, recipient_region_id, route_id,
        )

        # Fork before recipient choice so refusal and ratification remain separate histories.
        declined_fork = repository.fork(
            scope, world_id, main_branch, proposal.last_seq, "Safe conduct declined",
        )
        decline_commit, decline_resolution = _commit_action(
            repository,
            scope,
            world_id,
            declined_fork.branch_id,
            recipient_id,
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
             "expected_revision": 1, "operation": "decline"},
            idempotency_key="decline-greyhaven-northbank-safe-conduct",
        )
        decline_event = decline_resolution.events[0]
        assert isinstance(decline_event, SafeConductChangedEvent)
        assert decline_event.resulting_treaty.status == "declined"
        assert decline_event.resulting_treaty.revision == 2
        _assert_move_blocked(
            repository, scope, world_id, declined_fork.branch_id,
            proposer_traveler_id, recipient_region_id, route_id,
        )

        ratify_commit, ratify_resolution = _commit_action(
            repository,
            scope,
            world_id,
            main_branch,
            recipient_id,
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
             "expected_revision": 1, "operation": "ratify"},
            idempotency_key="ratify-greyhaven-northbank-safe-conduct",
        )
        ratify_event = ratify_resolution.events[0]
        assert isinstance(ratify_event, SafeConductChangedEvent)
        assert ratify_event.resulting_treaty.status == "active"
        assert ratify_event.resulting_treaty.revision == 2

        move_commit, move_resolution = _commit_action(
            repository,
            scope,
            world_id,
            main_branch,
            proposer_traveler_id,
            {"kind": "move", "destination_id": str(recipient_region_id), "route_id": str(route_id)},
            idempotency_key="move-under-active-safe-conduct",
        )
        movement = next(event for event in move_resolution.events if isinstance(event, MovementCompletedEvent))
        moved = repository.read_world(scope, world_id, main_branch)
        assert movement.route_id == route_id
        assert moved.state.current_tick == 60
        assert moved.state.entities[proposer_traveler_id].location_id == recipient_region_id

        denied_head = repository.read_world(scope, world_id, declined_fork.branch_id)
        forged_command = CommandEnvelope(
            actor_id=proposer_traveler_id,
            idempotency_key="reuse-authorized-movement-on-denied-branch",
            expected_state_version=denied_head.branch.state_version,
            command_type="action.move",
            payload={"kind": "move", "destination_id": str(recipient_region_id), "route_id": str(route_id)},
        )
        with pytest.raises(InvalidEvent, match="no valid safe conduct at departure"):
            repository.commit_command(scope, world_id, declined_fork.branch_id, forged_command, move_resolution.events)
        assert repository.read_world(scope, world_id, declined_fork.branch_id).state_hash == denied_head.state_hash

        # A child that reaches the declared expiry cannot reuse the old authorization.
        expiry_fork = repository.fork(
            scope, world_id, main_branch, move_commit.last_seq, "Safe conduct expired",
        )
        expiry_head = repository.read_world(scope, world_id, expiry_fork.branch_id)
        time_command = CommandEnvelope(
            actor_id=scope.user_id,
            idempotency_key="advance-expiry-test-clock",
            expected_state_version=expiry_head.branch.state_version,
            command_type="simulation.safe-conduct-expiry",
            payload={"to_tick": 3_600},
        )
        repository.commit_command(
            scope,
            world_id,
            expiry_fork.branch_id,
            time_command,
            [TimeAdvancedEvent(to_tick=3_600)],
        )
        expired = repository.read_world(scope, world_id, expiry_fork.branch_id)
        assert expired.state.current_tick == 3_600
        assert expired.state.safe_conduct_treaties[0].status == "active"
        _assert_move_blocked(
            repository, scope, world_id, expiry_fork.branch_id,
            second_traveler_id, recipient_region_id, route_id,
        )

        revoke_commit, revoke_resolution = _commit_action(
            repository,
            scope,
            world_id,
            main_branch,
            recipient_id,
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
             "expected_revision": 2, "operation": "revoke"},
            idempotency_key="revoke-greyhaven-northbank-safe-conduct",
        )
        revoke_event = revoke_resolution.events[0]
        assert isinstance(revoke_event, SafeConductChangedEvent)
        assert revoke_event.resulting_treaty.status == "revoked"
        assert revoke_event.resulting_treaty.revision == 3
        assert repository.read_world(scope, world_id, main_branch).state.current_tick == 60
        _assert_move_blocked(
            repository, scope, world_id, main_branch,
            second_traveler_id, recipient_region_id, route_id,
        )

        source_head = repository.read_world(scope, world_id, main_branch)
        declined_head = repository.read_world(scope, world_id, declined_fork.branch_id)
        expired_head = repository.read_world(scope, world_id, expiry_fork.branch_id)
        for branch_id, head in (
            (main_branch, source_head),
            (declined_fork.branch_id, declined_head),
            (expiry_fork.branch_id, expired_head),
        ):
            replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
            assert replayed.state_hash == head.state_hash == state_hash(head.state)

        source_events = _typed_safe_conduct_events(repository, scope, world_id, main_branch)
        assert [event.operation for event in source_events] == ["propose", "ratify", "revoke"]
        assert all(event.visibility == {"policy": "author_only"} for event in source_events)
        assert [event.type for event in source_events] == ["core.safe_conduct_changed.v1"] * 3
        assert [event.resulting_treaty.status for event in source_events] == ["pending", "active", "revoked"]

        backup = BackupApplication(repository)
        archive = backup.export_world(scope, world_id)
        with backup.validate_archive(archive) as verified:
            imported = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
        assert imported.world_id == world_id
        assert imported.branch_count == 3
        for branch_id, source in (
            (main_branch, source_head),
            (declined_fork.branch_id, declined_head),
            (expiry_fork.branch_id, expired_head),
        ):
            restored = restored_repository.read_world(scope, world_id, branch_id)
            assert restored.state_hash == source.state_hash == state_hash(source.state)
            assert restored_repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == source.state_hash
        assert restored_repository.read_world(scope, world_id, main_branch).state.safe_conduct_treaties[0].status == "revoked"
        assert restored_repository.read_world(scope, world_id, declined_fork.branch_id).state.safe_conduct_treaties[0].status == "declined"
        assert restored_repository.read_world(scope, world_id, expiry_fork.branch_id).state.safe_conduct_treaties[0].status == "active"
    finally:
        restored_db.dispose()
        source_db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,expected", [
    ("ratify", "active"), ("decline", "declined"), ("counter_half", "countered"),
    ("defer", "pending"), ("controlled", "pending"),
])
async def test_hosted_safe_conduct_response(tmp_path, choice, expected):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    state, proposer, _, _, recipient, source_org, target_org, origin, _, route = _fixture_state()
    database = Database(f"sqlite:///{(tmp_path / 'hosted-treaty.sqlite').as_posix()}")
    database.create_schema()
    repo = WorldRepository(database)
    try:
        created = repo.create_world(scope, NewWorld(title="Hosted treaty", state=state))
        wid, bid = created.world.world_id, created.branch.branch_id
        _, resolution = _commit_action(
            repo, scope, wid, bid, proposer,
            {"kind": "propose_safe_conduct", "proposer_organization_id": str(source_org),
             "recipient_organization_id": str(target_org), "from_place_id": str(origin),
             "route_id": str(route), "expires_at_tick": 3600}, idempotency_key="propose-for-hosted-response",
        )
        treaty_id = resolution.events[0].resulting_treaty.treaty_id
        proposer_view = WorldApplication(repo, None).view_world(scope, wid, bid)["state"]
        treaty_projection, = proposer_view["own_safe_conduct_treaties"]
        assert treaty_projection["can_request_response"] and not treaty_projection["can_ratify"]
        assert treaty_projection["from_place_name"] == "Greyhaven Reach"
        assert treaty_projection["to_place_name"] == "Northbank March"
        calls = []

        class Models:
            async def generate(self, scope, request):
                calls.append(request.role_task)
                if request.role_task is RoleTask.INTENT:
                    return SimpleNamespace(normalized_output={"action": {
                        "kind": "request_safe_conduct_response", "treaty_id": str(treaty_id), "expected_revision": 1,
                    }, "clarification": ""})
                return SimpleNamespace(normalized_output={"choice": choice})

        before = repo.read_world(scope, wid, bid)
        request = MessageRequest(request_id=uuid4(), world_id=wid, branch_id=bid, actor_id=proposer,
            session_id=uuid4(), mode="action", expected_state_version=str(before.branch.state_version),
            text="请这份通行协定的对方代表独立回应。")
        dispatcher = RulesActionDispatcher(repo, Models(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        if choice == "controlled":
            repo.grant_membership(scope, wid, uuid4(), MembershipRole.PLAYER, controlled_actor_ids=[recipient])
            with pytest.raises(AccessDenied):
                await dispatcher.execute(scope, before, request, "action")
            assert calls == [RoleTask.INTENT]
            assert repo.read_world(scope, wid, bid).state_hash == before.state_hash
            return
        response = await dispatcher.execute(scope, before, request, "action")
        after = repo.read_world(scope, wid, bid)
        assert calls == [RoleTask.INTENT, RoleTask.NPC_DECISION]
        assert after.state.safe_conduct_treaties[0].status == expected
        if choice == "defer":
            assert not response["world_effect"] and before.state_hash == after.state_hash
            return
        assert after.state.current_tick == before.state.current_tick
        receipt = repo.read_command_receipt(scope, wid, bid, request.request_id, proposer, f"message:{request.request_id}")
        event, = receipt.events
        assert event.actor_id == proposer and event.payload["actor_id"] == str(recipient)
        assert event.payload["requested_by"] == str(proposer)
        if expected == "countered":
            assert event.payload["operation"] == "counter"
            assert event.payload["resulting_treaty"]["counter_expires_at_tick"] == 1800
            assert "1800" in response["content"]
            _assert_move_blocked(repo, scope, wid, bid, proposer, state.entities[recipient].location_id, route)
            alternate = repo.fork(scope, wid, bid, before.last_seq, "Unauthorized model deadline").branch_id
            untouched = repo.read_world(scope, wid, alternate)
            typed = parse_event({"type": event.type, "event_id": str(event.event_id), **event.payload})
            command_id = uuid4()
            forged = typed.model_copy(update={
                "branch_id": alternate, "command_id": command_id,
                "event_id": safe_conduct_event_id(command_id, before.world.ruleset_version, "counter", treaty_id),
                "resulting_treaty": typed.resulting_treaty.model_copy(update={"counter_expires_at_tick": 1799}),
            })
            command = CommandEnvelope(
                command_id=command_id, actor_id=proposer, command_type="action.request_safe_conduct_response",
                expected_state_version=untouched.branch.state_version, idempotency_key="unauthorized-counter-deadline",
                payload={"kind": "request_safe_conduct_response", "treaty_id": str(treaty_id), "expected_revision": 1},
            )
            with pytest.raises(InvalidEvent, match="available shorter expiry"):
                repo.commit_command(scope, wid, alternate, command, [forged])
            assert repo.read_world(scope, wid, alternate).state_hash == untouched.state_hash
        assert repo.replay(scope, wid, bid, from_genesis=True).state_hash == after.state_hash
        service = ConversationApplication(SimpleNamespace(repository=repo), Models(), None)
        assert service._committed_status(scope, wid, bid, request.request_id, proposer)["result"]["content"] == response["content"]
    finally:
        database.dispose()


def test_counteroffer_requires_second_acceptance_and_replays_branch_locally(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    state, proposer, traveler, _, recipient, source_org, target_org, origin, destination, route = _fixture_state()
    database = Database(f"sqlite:///{(tmp_path / 'counter-treaty.sqlite').as_posix()}")
    database.create_schema()
    repo = WorldRepository(database)
    try:
        created = repo.create_world(scope, NewWorld(title="Countered treaty", state=state))
        wid, bid = created.world.world_id, created.branch.branch_id
        _, original = _commit_action(
            repo, scope, wid, bid, proposer,
            {"kind": "propose_safe_conduct", "proposer_organization_id": str(source_org),
             "recipient_organization_id": str(target_org), "from_place_id": str(origin),
             "route_id": str(route), "expires_at_tick": 3600}, idempotency_key="original-treaty",
        )
        treaty_id = original.events[0].resulting_treaty.treaty_id
        counter_command, counter = _commit_action(
            repo, scope, wid, bid, recipient,
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id), "expected_revision": 1,
             "operation": "counter", "counter_expires_at_tick": 120}, idempotency_key="shorter-counteroffer",
        )
        assert counter.events[0].resulting_treaty.status == "countered"
        assert counter.events[0].resulting_treaty.expires_at_tick == 3600
        _assert_move_blocked(repo, scope, wid, bid, traveler, destination, route)
        projected = WorldApplication(repo, None).view_world(scope, wid, bid)["state"]["own_safe_conduct_treaties"][0]
        assert projected["can_accept_counter"] and projected["counter_expires_at_tick"] == 120
        other = repo.fork(scope, wid, bid, counter_command.last_seq, "Withdraw counteroffer").branch_id
        accept = {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id), "expected_revision": 2,
                  "operation": "accept_counter"}
        _, _, _, rejected = _evaluate(repo, scope, wid, bid, recipient, accept)
        assert rejected.outcome.status == "rejected" and not rejected.events
        _, agreement = _commit_action(repo, scope, wid, bid, proposer, accept, idempotency_key="accept-shorter-terms")
        assert agreement.events[0].resulting_treaty.expires_at_tick == 120
        _commit_action(repo, scope, wid, bid, traveler,
            {"kind": "move", "destination_id": str(destination), "route_id": str(route)},
            idempotency_key="move-after-counter-acceptance")
        _commit_action(repo, scope, wid, other, proposer, accept | {"operation": "withdraw"},
                       idempotency_key="withdraw-instead-of-accepting")
        _assert_move_blocked(repo, scope, wid, other, traveler, destination, route)
        for branch_id in (bid, other):
            assert repo.replay(scope, wid, branch_id, from_genesis=True).state_hash == repo.read_world(scope, wid, branch_id).state_hash
        restored_db = Database(f"sqlite:///{(tmp_path / 'counter-treaty-restored.sqlite').as_posix()}")
        restored_db.create_schema()
        try:
            backup = BackupApplication(repo)
            restored = WorldRepository(restored_db)
            with backup.validate_archive(backup.export_world(scope, wid)) as verified:
                WorldBackupImporter(restored).import_verified_backup(scope, verified)
            for branch_id in (bid, other):
                assert restored.replay(scope, wid, branch_id, from_genesis=True).state_hash == repo.read_world(scope, wid, branch_id).state_hash
        finally:
            restored_db.dispose()
    finally:
        database.dispose()
