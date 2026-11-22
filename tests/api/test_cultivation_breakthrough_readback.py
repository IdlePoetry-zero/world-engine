import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.errors import AccessDenied
from world_engine.domain.models import AccessScope, MembershipRole, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.rules import ACTION_INTENT_ADAPTER, RuleContext, RuleEngine
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.mark.asyncio
async def test_gm_cites_only_inherited_breakthrough_receipts_of_the_querying_actor(tmp_path):
    owner = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    observer_scope = owner.model_copy(update={"user_id": uuid4()})
    actor, observer, place, holder, sink, lot = (uuid4() for _ in range(6))
    policy = "cultivation.qi_gathering.v1"
    database = Database(f"sqlite:///{(tmp_path / 'cultivation.db').as_posix()}")
    restored = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    contexts = []

    class Models:
        async def generate(self, scope, request):
            if request.role_task is RoleTask.GM_ANSWER:
                context = json.loads(request.messages[-1].content)["context"]
                receipts = context["own_completed_breakthroughs"]
                contexts.append(context)
                return SimpleNamespace(normalized_output={"answer": "按正式结算核对。", "references": [
                    row["event_id"] for row in receipts
                ]})
            assert request.role_task is RoleTask.INTENT
            return SimpleNamespace(normalized_output={
                "action": {"kind": "attempt_cultivation_breakthrough", "policy_id": policy},
                "clarification": "",
            })

    try:
        database.create_schema()
        repository = WorldRepository(database)
        state = WorldState(player_id=actor, metadata={"pack_id": "long_life_cultivation"}, entities={
            actor: WorldEntity(id=actor, kind="character", name="Practitioner", location_id=place, components={
                "capabilities": {"grants": ["cultivation.practice"]},
                "skills": {"skills": {"skill.cultivation_practice": {"level": "100"}}},
                "progression": {}, "health": {"current": 50, "maximum": 100},
                "inventory": {"lots": [{
                    "lot_id": str(lot), "account_id": str(holder), "resource_id": "cultivation.qi", "quantity": "2",
                }]},
            }),
            observer: WorldEntity(id=observer, kind="character", name="Observer", location_id=place),
            place: WorldEntity(id=place, kind="place", name="Hill"),
        }, resources={
            holder: ResourceAccount(resource_id="cultivation.qi", balance="2", owner_id=actor),
            sink: ResourceAccount(resource_id="cultivation.qi", balance="0", account_type="consumption_sink"),
        })
        created = repository.create_world(owner, NewWorld(title="Breakthrough receipt", state=state))
        world, branch = created.world.world_id, created.branch.branch_id
        repository.grant_membership(owner, world, observer_scope.user_id, MembershipRole.PLAYER,
                                    controlled_actor_ids=(observer,))
        before = repository.read_world(owner, world, branch)
        earlier = repository.fork(owner, world, branch, before.last_seq, "Before breakthrough").branch_id
        models = Models()
        chat = ConversationApplication(
            WorldApplication(repository, None), models, ConversationStore(OperationsStore(tmp_path / "ops.db")),
            actions=RulesActionDispatcher(repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext),
        )

        async def send(scope, branch_id, actor_id, mode):
            start = repository.read_world(scope, world, branch_id)
            result = await chat.send(scope, MessageRequest(
                request_id=uuid4(), world_id=world, branch_id=branch_id, session_id=branch_id, actor_id=actor_id,
                mode=mode, expected_state_version=str(start.branch.state_version),
                text="现在尝试聚气突破。" if mode == "action" else "上一次突破实际花了多少灵气？",
            ))
            if mode == "ask_gm":
                assert not result["world_effect"]
                assert repository.read_world(scope, world, branch_id).state_hash == start.state_hash
            return result

        assert (await send(owner, branch, actor, "action"))["world_effect"]
        head = repository.read_world(owner, world, branch)
        event, = repository.list_actor_breakthrough_history(owner, world, branch, actor, through_seq=head.last_seq)
        plan = event.payload["plan"]
        assert head.state.resources[holder].balance == head.state.resources[sink].balance == 1
        inherited = repository.fork(owner, world, branch, head.last_seq, "After breakthrough").branch_id
        own = await send(owner, inherited, actor, "ask_gm")
        receipt, = contexts[-1]["own_completed_breakthroughs"]
        assert receipt == {"event_id": str(event.event_id), "resolved_tick": 3600,
                           "duration_ticks": 3600, "qi_cost": "1", "source_realm": "mortal",
                           "target_realm": "qi_gathering", "outcome": plan["outcome"],
                           "failure_damage": plan["failure_damage"]}
        assert own["references"] == [str(event.event_id)]
        assert not (await send(owner, earlier, actor, "ask_gm"))["references"]
        assert not (await send(observer_scope, branch, observer, "ask_gm"))["references"]
        assert str(event.event_id) not in contexts[-1]["reference_ids"]
        assert repository.list_actor_breakthrough_history(owner, world, branch, actor, through_seq=before.last_seq) == []
        with pytest.raises(AccessDenied):
            repository.list_actor_breakthrough_history(observer_scope, world, branch, actor, through_seq=head.last_seq)

        backup = BackupApplication(repository, scratch_directory=tmp_path / "scratch")
        archive = backup.validate_archive(backup.export_world(owner, world, format_version="2.0"))
        restored.create_schema()
        restored_repository = WorldRepository(restored)
        WorldBackupImporter(restored_repository).import_verified_backup(owner, archive)
        receipts = restored_repository.list_actor_breakthrough_history(owner, world, inherited, actor, through_seq=head.last_seq)
        assert [row.event_id for row in receipts] == [event.event_id]
        assert restored_repository.replay(owner, world, inherited, from_genesis=True).state_hash == head.state_hash
    finally:
        restored.dispose()
        database.dispose()
