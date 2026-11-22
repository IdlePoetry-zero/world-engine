"""SQLite integration coverage for explicit, private relationship partnerships."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

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
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class PartnershipModels:
    """Return queued action intents and separate NPC choices by model role."""

    def __init__(self, intents, choices):
        self.intents = list(intents)
        self.choices = list(choices)
        self.calls = []
        self.decision_contexts = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if request.role_task is RoleTask.INTENT:
            return SimpleNamespace(normalized_output={"action": self.intents.pop(0), "clarification": ""})
        if request.role_task is RoleTask.NPC_DECISION:
            context = json.loads(request.messages[-1].content)
            self.decision_contexts.append(context)
            choice = self.choices.pop(0)
            return SimpleNamespace(normalized_output=choice if isinstance(choice, dict) else {"choice": choice})
        raise AssertionError(f"Unexpected model task: {request.role_task}")


def _make_world(repository, scope):
    player_id, first_partner_id, second_partner_id, bystander_id, place_id = (uuid4() for _ in range(5))
    year_ticks = 86_400 * 360

    def person(entity_id, name):
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            status="active",
            location_id=place_id,
            components={
                "life": LifeComponent(
                    birth_tick=-(25 * year_ticks),
                    lifespan_ticks=80 * year_ticks,
                    maturity_ticks=18 * year_ticks,
                    life_stage="adult",
                ).model_dump(mode="json"),
                "personality": PersonalityComponent(traits=["patient"], values=["honesty"]).model_dump(mode="json"),
            },
        )

    created = repository.create_world(scope, NewWorld(
        title="Relationship partnership flow",
        ruleset_version="relationship-partnership-flow.v1",
        state=WorldState(player_id=player_id, entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Courtyard", status="active"),
            player_id: person(player_id, "Player"),
            first_partner_id: person(first_partner_id, "Rae"),
            second_partner_id: person(second_partner_id, "Sol"),
            bystander_id: person(bystander_id, "Mika"),
        }),
    ))
    return created, player_id, first_partner_id, second_partner_id, bystander_id


def _build_service(tmp_path, repository, models):
    operations = OperationsStore(tmp_path / "operations.sqlite")
    worlds = WorldApplication(repository, None)
    service = ConversationApplication(worlds, models, ConversationStore(operations))
    service.actions = RulesActionDispatcher(repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
    return service


def _request(created, actor_id, text, *, request_id, expected_state_version):
    return MessageRequest(
        request_id=request_id,
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        session_id=uuid4(),
        actor_id=actor_id,
        expected_state_version=str(expected_state_version),
        mode="action",
        text=text,
        profile=QualityProfile(speed="fast", depth="standard"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("second_choice", "second_status"), [
    ("accept", "active"), ("decline", "declined"), ("defer", "pending"),
])
async def test_two_partnerships_are_independent_private_replayable_and_restorable(
    tmp_path, second_choice, second_status,
):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    restored_database = Database(f"sqlite:///{(tmp_path / 'restored.sqlite').as_posix()}")
    restored_database.create_schema()
    restored_repository = WorldRepository(restored_database)
    try:
        created, player_id, first_partner_id, second_partner_id, bystander_id = _make_world(repository, scope)

        first_proposal_request_id = uuid4()
        second_proposal_request_id = uuid4()
        first_partnership_id = uuid5(first_proposal_request_id, "relationship-partnership")
        second_partnership_id = uuid5(second_proposal_request_id, "relationship-partnership")
        first_response_request_id = uuid4()
        second_response_request_id = uuid4()
        end_request_id = uuid4()
        first_terms = ["双方都可以和别人约会", "先把关系留在我们两人之间"]
        models = PartnershipModels(
            intents=[
                {"kind": "propose_relationship_partnership", "partner_id": str(first_partner_id),
                 "proposal_terms": ["只可以和我约会"]},
                {"kind": "propose_relationship_partnership", "partner_id": str(first_partner_id),
                 "proposal_terms": first_terms},
                {"kind": "propose_relationship_partnership", "partner_id": str(second_partner_id)},
                {"kind": "request_relationship_partnership_response", "partnership_id": str(first_partnership_id), "expected_revision": 1},
                {"kind": "request_relationship_partnership_response", "partnership_id": str(second_partnership_id), "expected_revision": 1},
                {"kind": "resolve_relationship_partnership", "partnership_id": str(first_partnership_id), "operation": "end", "expected_revision": 2},
            ],
            choices=["accept", second_choice],
        )
        service = _build_service(tmp_path, repository, models)

        async def send(request_id, text):
            head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
            request = _request(
                created, player_id, text, request_id=request_id,
                expected_state_version=head.branch.state_version,
            )
            return request, await service.send(scope, request)

        opening = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        _, invented_terms = await send(uuid4(), "明确向 Rae 提议成为伴侣。双方都可以和别人约会。")
        assert invented_terms["kind"] == "clarification" and not invented_terms["world_effect"]
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state_hash == opening.state_hash
        first_proposal_request, first_proposal_result = await send(
            first_proposal_request_id, "明确向 Rae 提议成为伴侣。双方都可以和别人约会；先把关系留在我们两人之间。",
        )
        second_proposal_request, second_proposal_result = await send(
            second_proposal_request_id, "明确向 Sol 提议成为伴侣。FLOW_PROPOSE_SECOND",
        )
        assert first_proposal_result["action_status"] == second_proposal_result["action_status"] == "succeeded"
        assert all(term in first_proposal_result["content"] for term in first_terms)

        first_response_request, first_response_result = await send(
            first_response_request_id, f"请让 Rae 自行回应我的伴侣提议 {first_partnership_id}。FLOW_REPLY_FIRST",
        )
        second_response_request, second_response_result = await send(
            second_response_request_id, f"请让 Sol 自行回应我的伴侣提议 {second_partnership_id}。FLOW_REPLY_SECOND",
        )
        assert first_response_result["action_status"] == "succeeded" and first_response_result["world_effect"]
        if second_choice != "defer":
            assert second_response_result["action_status"] == "succeeded" and second_response_result["world_effect"]
        else:
            assert second_response_result["action_status"] == "partial" and not second_response_result["world_effect"]

        concurrent = repository.read_world(scope, created.world.world_id, created.branch.branch_id).state
        assert [row.status for row in concurrent.relationship_partnerships] == ["active", second_status]
        end_request, end_result = await send(
            end_request_id, f"明确结束我和 Rae 的伴侣关系 {first_partnership_id}。FLOW_END_FIRST",
        )
        assert end_result["action_status"] == "succeeded" and end_result["world_effect"]

        head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        after = head.state
        records = {item.partnership_id: item for item in after.relationship_partnerships}
        assert records[first_partnership_id].status == "ended"
        assert records[first_partnership_id].revision == 3
        assert list(records[first_partnership_id].proposal_terms) == first_terms
        assert records[second_partnership_id].status == second_status
        assert records[second_partnership_id].revision == (1 if second_status == "pending" else 2)
        assert after.current_tick == 0

        projections = {
            actor_id: project_player_state(after.model_dump(mode="json"), str(actor_id))
            for actor_id in (player_id, first_partner_id, second_partner_id, bystander_id)
        }
        assert {row["partnership_id"] for row in projections[player_id]["own_relationship_partnerships"]} == {
            str(first_partnership_id), str(second_partnership_id),
        }
        assert [row["partnership_id"] for row in projections[first_partner_id]["own_relationship_partnerships"]] == [
            str(first_partnership_id),
        ]
        assert [row["partnership_id"] for row in projections[second_partner_id]["own_relationship_partnerships"]] == [
            str(second_partnership_id),
        ]
        assert projections[bystander_id]["own_relationship_partnerships"] == []
        assert records[first_partnership_id].status == projections[first_partner_id]["own_relationship_partnerships"][0]["status"] == "ended"
        assert records[second_partnership_id].status == projections[second_partner_id]["own_relationship_partnerships"][0]["status"] == second_status

        event_page = repository.list_events(scope, created.world.world_id, created.branch.branch_id, limit=100)
        partnership_events = [event for event in event_page.events if event.type == "core.relationship_partnership_changed.v1"]
        expected_statuses = ["pending", "pending", "active"]
        if second_choice != "defer":
            expected_statuses.append(second_status)
        assert [event.payload["resulting_partnership"]["status"] for event in partnership_events] == [
            *expected_statuses, "ended",
        ]
        for event in partnership_events:
            partnership = event.payload["resulting_partnership"]
            participant_ids = {partnership["proposer_id"], partnership["partner_id"]}
            for actor_id in (player_id, first_partner_id, second_partner_id, bystander_id):
                projected = project_event(event.model_dump(mode="json"), str(actor_id))
                assert (projected is not None) is (str(actor_id) in participant_ids)
        assert len(models.decision_contexts) == 2
        assert {context["recipient_name"]: context["proposal_terms"] for context in models.decision_contexts} == {
            "Rae": first_terms, "Sol": [],
        }
        assert projections[first_partner_id]["own_relationship_partnerships"][0]["proposal_terms"] == first_terms
        assert "proposal_terms" not in projections[second_partner_id]["own_relationship_partnerships"][0]

        response_receipt = repository.read_command_receipt(
            scope, created.world.world_id, created.branch.branch_id,
            first_response_request.request_id, player_id, f"message:{first_response_request.request_id}",
        )
        assert response_receipt is not None
        response_event = next(event for event in response_receipt.events
                              if event.type == "core.relationship_partnership_changed.v1")
        assert response_event.payload["requested_by"] == str(player_id)
        assert response_event.payload["actor_id"] == str(first_partner_id)
        assert response_event.payload["resulting_partnership"]["status"] == "active"

        replayed = repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True)
        assert replayed.state_hash == head.state_hash
        archive_app = BackupApplication(repository)
        archive = archive_app.export_world(scope, created.world.world_id, format_version="2.0")
        with archive_app.validate_archive(archive) as verified:
            WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
        restored_head = restored_repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert restored_head.state_hash == head.state_hash
        restored_replay = restored_repository.replay(
            scope, created.world.world_id, created.branch.branch_id, from_genesis=True,
        )
        assert restored_replay.state_hash == head.state_hash
        assert restored_head.state.relationship_partnerships == after.relationship_partnerships

        # The direct end command also has a durable receipt for idempotent recovery.
        end_receipt = repository.read_command_receipt(
            scope, created.world.world_id, created.branch.branch_id,
            end_request.request_id, player_id, f"message:{end_request.request_id}",
        )
        assert end_receipt is not None
        end_event = next(event for event in end_receipt.events
                         if event.type == "core.relationship_partnership_changed.v1")
        assert end_event.payload["operation"] == "end"
        assert end_event.payload["resulting_partnership"]["partnership_id"] == str(first_partnership_id)
        assert await service.send(scope, end_request) == end_result
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state_hash == head.state_hash
    finally:
        restored_database.dispose()
        database.dispose()


@pytest.mark.asyncio
async def test_revised_offer_and_npc_counter_require_acceptance_of_the_current_version(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, player_id, partner_id, _, bystander_id = _make_world(repository, scope)
        world_id = created.world.world_id
        main_id = created.branch.branch_id
        initial_request_id = uuid4()
        partnership_id = uuid5(initial_request_id, "relationship-partnership")
        original_terms = ["每周一起吃一次饭"]
        revised_terms = ["各自可以和别人约会；约会安排提前告知"]
        counter_terms = ["安排变更当天告知", "不向别人公开伴侣关系"]

        def intent(kind, revision, **fields):
            return {"kind": kind, "partnership_id": str(partnership_id), "expected_revision": revision, **fields}

        models = PartnershipModels(
            intents=[
                {"kind": "propose_relationship_partnership", "partner_id": str(partner_id),
                 "proposal_terms": original_terms},
                intent("update_relationship_partnership_proposal", 1, operation="revise", proposal_terms=revised_terms),
                intent("request_relationship_partnership_response", 1),
                intent("request_relationship_partnership_response", 1),
                intent("request_relationship_partnership_response", 2),
                intent("resolve_relationship_partnership", 3, operation="accept"),
                intent("resolve_relationship_partnership", 3, operation="accept"),
            ],
            choices=["accept", {"choice": "counter", "proposal_terms": counter_terms}],
        )
        service = _build_service(tmp_path, repository, models)

        async def send(branch_id, text, request_id=None):
            head = repository.read_world(scope, world_id, branch_id)
            request = _request(
                created, player_id, text, request_id=request_id or uuid4(),
                expected_state_version=head.branch.state_version,
            ).model_copy(update={"branch_id": branch_id})
            return request, await service.send(scope, request)

        _, proposed = await send(main_id, "向 Rae 提议成为伴侣。\n提议条件：每周一起吃一次饭", initial_request_id)
        assert proposed["action_status"] == "succeeded"
        original = repository.read_world(scope, world_id, main_id)
        control = repository.fork(scope, world_id, main_id, original.branch.head_seq, "Original terms")

        _, revised = await send(main_id, "修改伴侣提议的完整条件。\n提议条件：各自可以和别人约会；约会安排提前告知")
        assert revised["action_status"] == "succeeded"
        assert all(term in revised["content"] for term in revised_terms)
        _, stale = await send(main_id, "请 Rae 回应最初那份提议。")
        assert stale["kind"] == "clarification" and not stale["world_effect"]
        assert not models.decision_contexts

        _, accepted_original = await send(control.branch_id, "请 Rae 自己决定是否接受最初的提议。")
        assert accepted_original["action_status"] == "succeeded"
        control_head = repository.read_world(scope, world_id, control.branch_id)
        accepted_on_control = control_head.state.relationship_partnerships[0]
        assert accepted_on_control.status == "active"
        assert list(accepted_on_control.proposal_terms) == original_terms

        response_request, counter = await send(main_id, "请 Rae 自行回应修改后的提议。")
        assert counter["action_status"] == "succeeded"
        assert all(term in counter["content"] for term in counter_terms)
        assert [context["proposal_terms"] for context in models.decision_contexts] == [original_terms, revised_terms]
        pending = repository.read_world(scope, world_id, main_id).state
        player_rows = project_player_state(pending.model_dump(mode="json"), str(player_id))["own_relationship_partnerships"]
        assert player_rows[0]["can_counter"] and not player_rows[0]["can_revise"]
        assert player_rows[0]["current_proposal_author_id"] == str(partner_id)
        assert player_rows[0]["current_proposal_recipient_id"] == str(player_id)
        assert pending.relationship_partnerships[0].status == "pending"
        assert await service.send(scope, response_request) == counter

        _, obsolete_acceptance = await send(main_id, f"我明确接受伴侣关系提议 {partnership_id}，版本 2。")
        assert obsolete_acceptance["kind"] == "clarification" and not obsolete_acceptance["world_effect"]
        _, accepted_counter = await send(main_id, "接受 Rae 的第三版伴侣提议。")
        assert accepted_counter["action_status"] == "succeeded"

        head = repository.read_world(scope, world_id, main_id)
        record = head.state.relationship_partnerships[0]
        assert (record.status, record.revision, head.state.current_tick) == ("active", 4, 0)
        assert list(record.proposal_terms) == counter_terms
        assert project_player_state(head.state.model_dump(mode="json"), str(bystander_id))["own_relationship_partnerships"] == []
        history = repository.list_events(scope, world_id, main_id, limit=100).events
        events = [event for event in history if event.type == "core.relationship_partnership_changed.v1"]
        assert [event.payload["operation"] for event in events] == ["propose", "revise", "counter", "accept"]
        assert [event.payload["resulting_partnership"]["proposal_terms"] for event in events] == [
            original_terms, revised_terms, counter_terms, counter_terms,
        ]
        assert all(project_event(event.model_dump(mode="json"), str(bystander_id)) is None for event in events)
        assert repository.replay(scope, world_id, main_id, from_genesis=True).state_hash == head.state_hash
        assert repository.replay(scope, world_id, control.branch_id, from_genesis=True).state_hash == control_head.state_hash

        restored_database = Database(f"sqlite:///{(tmp_path / 'restored-negotiation.sqlite').as_posix()}")
        try:
            restored_database.create_schema()
            restored = WorldRepository(restored_database)
            backup = BackupApplication(repository)
            archive = backup.export_world(scope, world_id, format_version="2.0")
            with backup.validate_archive(archive) as verified:
                WorldBackupImporter(restored).import_verified_backup(scope, verified)
            assert restored.replay(scope, world_id, main_id, from_genesis=True).state_hash == head.state_hash
            assert restored.replay(scope, world_id, control.branch_id, from_genesis=True).state_hash == control_head.state_hash
        finally:
            restored_database.dispose()
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_host_cannot_solicit_a_partner_controlled_by_another_player(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    other_player = uuid4()
    try:
        created, player_id, partner_id, _, _ = _make_world(repository, scope)
        proposal_request_id = uuid4()
        partnership_id = uuid5(proposal_request_id, "relationship-partnership")
        response_request_id = uuid4()
        models = PartnershipModels(
            intents=[
                {"kind": "propose_relationship_partnership", "partner_id": str(partner_id)},
                {"kind": "request_relationship_partnership_response", "partnership_id": str(partnership_id), "expected_revision": 1},
            ],
            choices=["accept"],
        )
        service = _build_service(tmp_path, repository, models)
        initial_head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        proposal = _request(
            created, player_id, "向 Rae 提议成为伴侣。\n提议条件：每次安排变化都互相说一声。",
            request_id=proposal_request_id, expected_state_version=initial_head.branch.state_version,
        )
        assert (await service.send(scope, proposal))["action_status"] == "succeeded"
        repository.grant_membership(
            scope, created.world.world_id, other_player, MembershipRole.PLAYER,
            controlled_actor_ids=[partner_id],
        )
        head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert head.state.relationship_partnerships[0].proposal_terms == ("每次安排变化都互相说一声。",)
        request = _request(
            created, player_id, f"请 Rae 自行回应伴侣提议 {partnership_id}。HOST_CONTROL_REQUEST",
            request_id=response_request_id, expected_state_version=head.branch.state_version,
        )

        with pytest.raises(AccessDenied):
            await service.send(scope, request)

        assert not models.decision_contexts
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert after.state_hash == head.state_hash
        assert after.state.relationship_partnerships[0].status == "pending"
    finally:
        database.dispose()
