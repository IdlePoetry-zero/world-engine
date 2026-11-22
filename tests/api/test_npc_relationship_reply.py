"""SQLite integration coverage for a host-requested NPC invitation answer."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.visibility import project_event, project_player_state
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import LifeComponent, PersonalityComponent
from world_engine.domain.errors import AccessDenied
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.repository import WorldRepository


class FakeModels:
    def __init__(self, invitation_id, choice, *, intent_kind="request_relationship_reply"):
        self.invitation_id = invitation_id
        self.choice = choice
        self.intent_kind = intent_kind
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if request.role_task is RoleTask.INTENT:
            output = {
                "action": {"kind": self.intent_kind, "invitation_id": str(self.invitation_id)},
                "clarification": "",
            }
        elif request.role_task is RoleTask.NPC_DECISION:
            output = {"choice": self.choice}
        else:
            raise AssertionError(f"Unexpected task: {request.role_task}")
        return SimpleNamespace(normalized_output=output)


def _make_world(repository, scope, *, meeting=None):
    player_id, npc_id, place_id = uuid4(), uuid4(), uuid4()
    year_ticks = 86_400 * 360
    def person(entity_id, name, *, personality=False):
        components = {"life": LifeComponent(
            birth_tick=-(25 * year_ticks), lifespan_ticks=80 * year_ticks,
            maturity_ticks=18 * year_ticks, life_stage="adult",
        ).model_dump(mode="json")}
        if personality:
            components["personality"] = PersonalityComponent(
                traits=["PROPOSER_PRIVATE_MARKER"] if name == "Player" else ["patient"],
                values=["honesty"],
            ).model_dump(mode="json")
        return WorldEntity(
            id=entity_id, kind="character", name=name, status="active",
            location_id=place_id, components=components,
        )

    created = repository.create_world(scope, NewWorld(
        title="NPC invitation reply integration",
        ruleset_version="npc-invitation-reply-test.v1",
        state=WorldState(player_id=player_id, entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Courtyard", status="active"),
            player_id: person(player_id, "Player", personality=True),
            npc_id: person(npc_id, "Rae", personality=True),
        }),
    ))
    view = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    command_id = uuid4()
    payload = {"kind": "propose_relationship_invitation", "target_id": str(npc_id)}
    if meeting is not None:
        payload["meeting"] = meeting
    context = RuleContext(
        world_id=created.world.world_id, branch_id=created.branch.branch_id,
        command_id=command_id, actor_id=player_id, ruleset_version=view.world.ruleset_version,
    )
    proposal = RuleEngine().evaluate(view.state, payload, context)
    assert proposal.outcome.status == "succeeded", proposal.outcome.model_dump(mode="json")
    assert len(proposal.events) == 1
    repository.commit_command(scope, created.world.world_id, created.branch.branch_id, CommandEnvelope(
        command_id=command_id, actor_id=player_id, idempotency_key="invite-npc",
        expected_state_version=view.branch.state_version,
        command_type="action.propose_relationship_invitation", payload=payload,
    ), proposal.events)
    return created, player_id, npc_id, proposal.events[0].resulting_invitation


def _build_service(tmp_path, scope, repository, models):
    operations = OperationsStore(tmp_path / "operations.sqlite")
    worlds = WorldApplication(repository, None)
    service = ConversationApplication(worlds, models, ConversationStore(operations))
    service.actions = RulesActionDispatcher(
        repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    return service


def _request(created, player_id, invitation_id, *, request_id=None):
    return MessageRequest(
        request_id=request_id or uuid4(), world_id=created.world.world_id,
        branch_id=created.branch.branch_id, session_id=uuid4(), actor_id=player_id,
        expected_state_version="1", mode="action",
        text=f"请让对方自行回应我发出的这份待回复私人恋爱邀约 {invitation_id}。",
        profile=QualityProfile(speed="fast", depth="standard"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,expected", [("attend", "succeeded"), ("defer", "partial")])
async def test_hosted_npc_attendance_records_only_its_own_choice(tmp_path, choice, expected):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        meeting = {"start_tick": 60, "end_tick": 120}
        created, proposer_id, npc_id, invitation = _make_world(repository, scope, meeting=meeting)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        view = repository.read_world(scope, world_id, branch_id)
        command_id = uuid4()
        payload = {"kind": "resolve_relationship_invitation", "invitation_id": str(invitation.invitation_id), "operation": "accept"}
        resolution = RuleEngine().evaluate(view.state, payload, RuleContext(
            world_id=world_id, branch_id=branch_id, command_id=command_id, actor_id=npc_id,
            ruleset_version=view.world.ruleset_version,
        ))
        repository.commit_command(scope, world_id, branch_id, CommandEnvelope(
            command_id=command_id, actor_id=npc_id, expected_state_version=1,
            idempotency_key="accept-meeting", command_type="action.resolve_relationship_invitation", payload=payload,
        ), resolution.events)
        repository.commit_command(scope, world_id, branch_id, CommandEnvelope(
            command_id=uuid4(), actor_id=proposer_id, expected_state_version=2,
            idempotency_key="advance-to-meeting", command_type="fixture.clock",
        ), [TimeAdvancedEvent(to_tick=60)])
        before = repository.read_world(scope, world_id, branch_id)
        models = FakeModels(invitation.invitation_id, choice, intent_kind="request_relationship_meeting_attendance")
        service = _build_service(tmp_path, scope, repository, models)
        request = _request(created, proposer_id, invitation.invitation_id).model_copy(update={
            "expected_state_version": "3", "text": "请让对方自行决定是否登记赴约。TEST_REQUEST_PRIVATE_MARKER",
        })
        result = await service.send(scope, request)
        assert result["action_status"] == expected
        assert result["world_effect"] is (choice == "attend")
        model = next(call for _, call in models.calls if call.role_task is RoleTask.NPC_DECISION)
        context = json.loads(model.messages[-1].content)
        assert context["meeting"] == meeting and not context["other_participant_registered"]
        assert "TEST_REQUEST_PRIVATE_MARKER" not in model.messages[-1].content
        assert "PROPOSER_PRIVATE_MARKER" not in model.messages[-1].content
        after = repository.read_world(scope, world_id, branch_id)
        assert after.state.current_tick == before.state.current_tick == 60
        attendance = after.state.relationship_invitations[0].meeting_attendance
        assert [record.actor_id for record in attendance] == ([npc_id] if choice == "attend" else [])
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
        assert await service.send(scope, request) == result
        assert len(models.calls) == 2
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["uneasy", "defer"])
async def test_requested_meeting_reflection_records_only_the_npc_private_choice(tmp_path, choice):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, player_id, npc_id, invitation = _make_world(
            repository, scope, meeting={"start_tick": 60, "end_tick": 120},
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id

        def commit(actor_id, key, payload=None, *, to_tick=None):
            before = repository.read_world(scope, world_id, branch_id)
            command_id = uuid4()
            if payload is None:
                events = [TimeAdvancedEvent(to_tick=to_tick)]
                command_type = "fixture.clock"
            else:
                outcome = RuleEngine().evaluate(before.state, payload, RuleContext(
                    world_id=world_id, branch_id=branch_id, command_id=command_id,
                    actor_id=actor_id, ruleset_version=before.world.ruleset_version,
                ))
                assert outcome.outcome.status == "succeeded"
                events = outcome.events
                command_type = f"action.{payload['kind']}"
            repository.commit_command(scope, world_id, branch_id, CommandEnvelope(
                command_id=command_id, actor_id=actor_id, idempotency_key=key,
                expected_state_version=before.branch.state_version,
                command_type=command_type, payload=payload or {},
            ), events)

        id_payload = {"invitation_id": str(invitation.invitation_id)}
        commit(npc_id, "accept", {"kind": "resolve_relationship_invitation", "operation": "accept", **id_payload})
        commit(player_id, "to-start", to_tick=60)
        for actor_id in (player_id, npc_id):
            commit(actor_id, f"attend:{actor_id}", {"kind": "attend_relationship_meeting", **id_payload})
        models = FakeModels(invitation.invitation_id, choice, intent_kind="request_meeting_reflection")
        service = _build_service(tmp_path, scope, repository, models)

        async def ask():
            before = repository.read_world(scope, world_id, branch_id)
            request = _request(created, player_id, invitation.invitation_id).model_copy(update={
                "expected_state_version": str(before.branch.state_version),
                "text": "请让对方自行决定是否记录会后的私人感受。",
            })
            return request, await service.send(scope, request)

        _, early = await ask()
        assert early["action_status"] == "rejected" and len(models.calls) == 1
        commit(player_id, "to-end", to_tick=120)
        before = repository.read_world(scope, world_id, branch_id)
        request, result = await ask()
        after = repository.read_world(scope, world_id, branch_id)
        assert result["action_status"] == "succeeded" and "outlook" not in result
        assert len(models.calls) == 3
        assert after.state.current_tick == before.state.current_tick == 120
        assert after.state.relationship_invitations == before.state.relationship_invitations
        projected = after.state.model_dump(mode="json")
        assert project_player_state(projected, str(player_id))["own_meeting_reflections"] == []
        if choice == "defer":
            assert not result["world_effect"] and after.state_hash == before.state_hash
        else:
            assert result["world_effect"]
            event, = repository.list_events(scope, world_id, branch_id, after_seq=before.last_seq).events
            assert event.payload["requested_by"] == str(player_id)
            assert project_event(event.model_dump(mode="json"), str(player_id)) is None
            assert project_player_state(projected, str(npc_id))["own_meeting_reflections"][0]["outlook"] == choice
            _, repeat = await ask()
            assert repeat["content"] == result["content"] and not repeat["world_effect"]
            assert len(models.calls) == 4
            backup = BackupApplication(repository)
            restored_db = Database(f"sqlite:///{(tmp_path / 'restored.sqlite').as_posix()}")
            restored_db.create_schema()
            try:
                restored = WorldRepository(restored_db)
                archive = backup.export_world(scope, world_id, format_version="2.0")
                with backup.validate_archive(archive) as verified:
                    WorldBackupImporter(restored).import_verified_backup(scope, verified)
                assert restored.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
            finally:
                restored_db.dispose()
        assert await service.send(scope, request) == result
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_npc_reply_to_expired_meeting_leaves_invitation_pending_without_decision_call(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        meeting = {"start_tick": 60, "end_tick": 120}
        created, proposer_id, _, invitation = _make_world(repository, scope, meeting=meeting)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        repository.commit_command(scope, world_id, branch_id, CommandEnvelope(
            command_id=uuid4(), actor_id=proposer_id, expected_state_version=1,
            idempotency_key="advance-to-meeting-start", command_type="fixture.clock",
        ), [TimeAdvancedEvent(to_tick=meeting["start_tick"])])
        before = repository.read_world(scope, world_id, branch_id)
        models = FakeModels(invitation.invitation_id, "accept")
        service = _build_service(tmp_path, scope, repository, models)
        request = _request(created, proposer_id, invitation.invitation_id).model_copy(update={"expected_state_version": "2"})

        result = await service.send(scope, request)
        assert result["action_status"] == "rejected" and not result["world_effect"]
        assert "约会时间" in result["content"]
        assert [call.role_task for _, call in models.calls] == [RoleTask.INTENT]
        after = repository.read_world(scope, world_id, branch_id)
        assert after.state_hash == before.state_hash and after.last_seq == before.last_seq
        assert after.state.relationship_invitations[0] == invitation
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_npc_acceptance_uses_npc_actor_and_proposer_receipt_recovers(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    restored_database = Database(f"sqlite:///{(tmp_path / 'world-restored.sqlite').as_posix()}")
    restored_database.create_schema()
    repository = WorldRepository(database)
    restored_repository = WorldRepository(restored_database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, proposer_id, invitee_id, invitation = _make_world(repository, scope)
        models = FakeModels(invitation.invitation_id, "accept")
        service = _build_service(tmp_path, scope, repository, models)
        request = _request(created, proposer_id, invitation.invitation_id)

        result = await service.send(scope, request)

        assert result["world_effect"] is True
        assert result["action_status"] == "succeeded"
        assert result["content"] == "对方明确接受了这份邀约。"
        npc_decision = next(call for _, call in models.calls if call.role_task is RoleTask.NPC_DECISION)
        assert npc_decision.trace_context.parent_trace == str(request.request_id)
        current = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        resolved = current.state.relationship_invitations[0]
        assert resolved.status == "accepted"
        assert resolved.revision == 2
        receipt = repository.read_command_receipt(
            scope, created.world.world_id, created.branch.branch_id,
            request.request_id, proposer_id, f"message:{request.request_id}",
        )
        assert receipt is not None
        event = next(item for item in receipt.events if item.type == "core.relationship_invitation_changed.v1")
        assert event.actor_id == proposer_id
        assert event.payload["actor_id"] == str(invitee_id)
        assert event.payload["requested_by"] == str(proposer_id)
        assert event.payload["resulting_invitation"]["status"] == "accepted"

        # Simulate a lost conversation result after the world commit. The owner
        # recovers it from the exact NPC-reply command receipt without replay.
        with service.store.operations.connect() as connection:
            connection.execute(
                "UPDATE message_requests SET status='failed', result=? WHERE tenant=? AND principal=? AND id=?",
                (json.dumps({"error": "REQUEST_FAILED"}), str(scope.tenant_id), str(scope.user_id), str(request.request_id)),
            )
        recovered = service.status(scope, request.request_id)
        assert recovered["status"] == "succeeded"
        assert recovered["result"]["content"] == "对方明确接受了这份邀约。"
        source_head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert source_head.branch.state_version == 2

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, created.world.world_id)
        with backup_app.validate_archive(archive) as verified:
            imported = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
        assert imported.world_id == created.world.world_id
        restored_head = restored_repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert restored_head.state_hash == source_head.state_hash
        assert restored_head.state.relationship_invitations[0].status == "accepted"
        replayed = restored_repository.replay(
            scope, created.world.world_id, created.branch.branch_id, from_genesis=True,
        )
        assert replayed.state_hash == source_head.state_hash
        restored_events = restored_repository.list_events(
            scope, created.world.world_id, created.branch.branch_id, limit=20,
        ).events
        restored_event = next(
            item for item in restored_events if item.type == "core.relationship_invitation_changed.v1"
            and item.payload.get("requested_by") == str(proposer_id)
        )
        assert restored_event.actor_id == proposer_id
        assert restored_event.payload["actor_id"] == str(invitee_id)
        assert restored_event.payload["operation"] == "accept"
        assert restored_event.payload["resulting_invitation"]["status"] == "accepted"
        assert len([call for _, call in models.calls if call.role_task is RoleTask.NPC_DECISION]) == 1
    finally:
        restored_database.dispose()
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "expected_status"), [("decline", "declined"), ("defer", "pending")])
async def test_npc_decline_or_defer_is_scoped_and_does_not_read_other_private_data(
    tmp_path, choice, expected_status,
):
    database = Database(f"sqlite:///{(tmp_path / f'world-{choice}.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, proposer_id, invitee_id, invitation = _make_world(repository, scope)
        models = FakeModels(invitation.invitation_id, choice)
        service = _build_service(tmp_path / choice, scope, repository, models)
        request = _request(created, proposer_id, invitation.invitation_id)

        result = await service.send(scope, request)

        current = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert current.state.relationship_invitations[0].status == expected_status
        assert current.state.current_tick == 0
        if choice == "defer":
            assert result["world_effect"] is False
            assert result["action_status"] == "partial"
            assert current.branch.state_version == 1
            assert not any(item.type == "core.relationship_invitation_changed.v1"
                           for item in repository.list_events(scope, created.world.world_id, created.branch.branch_id).events[1:])
        else:
            assert result["world_effect"] is True
            assert result["action_status"] == "succeeded"
            assert current.branch.state_version == 2
            receipt = repository.read_command_receipt(
                scope, created.world.world_id, created.branch.branch_id,
                request.request_id, proposer_id, f"message:{request.request_id}",
            )
            event = next(item for item in receipt.events if item.type == "core.relationship_invitation_changed.v1")
            assert event.actor_id == proposer_id
            assert event.payload["actor_id"] == str(invitee_id)
            assert event.payload["requested_by"] == str(proposer_id)
            assert event.payload["resulting_invitation"]["status"] == "declined"

        decision = next(call for _, call in models.calls if call.role_task is RoleTask.NPC_DECISION)
        private_context = decision.messages[-1].content
        assert "Player" in private_context and "Rae" in private_context
        assert str(invitation.invitation_id) in private_context
        assert "PROPOSER_PRIVATE_MARKER" not in private_context
        assert "other invitation" not in private_context.lower()
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_host_cannot_solicit_a_character_controlled_by_another_player(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world-controlled.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    other_player = uuid4()
    try:
        created, proposer_id, invitee_id, invitation = _make_world(repository, scope)
        repository.grant_membership(scope, created.world.world_id, other_player, MembershipRole.PLAYER,
                                   controlled_actor_ids=[invitee_id])
        models = FakeModels(invitation.invitation_id, "accept")
        service = _build_service(tmp_path, scope, repository, models)
        request = _request(created, proposer_id, invitation.invitation_id)

        with pytest.raises(AccessDenied):
            await service.send(scope, request)

        assert not any(call.role_task is RoleTask.NPC_DECISION for _, call in models.calls)
        current = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert current.branch.state_version == 1
        assert current.state.relationship_invitations[0].status == "pending"
    finally:
        database.dispose()
