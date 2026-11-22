from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.visibility import project_event, project_player_state
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.components import InventoryComponent, InventoryLot
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.private_gifts import plan_private_gift_resolution
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.mark.parametrize("archive_version", ["1.0", "2.0"])
def test_private_gift_action_forks_and_hosted_reply_remain_consent_bound(tmp_path, archive_version):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    giver, receiver, observer, square, source, origin = (uuid4() for _ in range(6))
    lot = InventoryLot(
        lot_id=uuid4(), account_id=source, resource_id="feudal.grain", quantity=Decimal("8"),
        quality=67, expiry_tick=80_000, origin_event_id=origin,
    )
    state = WorldState(player_id=giver, entities={
        square: WorldEntity(id=square, kind="place", name="九桥谷"),
        giver: WorldEntity(id=giver, kind="character", name="Tarek", location_id=square, components={
            "inventory": InventoryComponent(revision=3, lots=[lot]).model_dump(mode="json"),
        }),
        receiver: WorldEntity(id=receiver, kind="character", name="Yara", location_id=square, components={
            "personality": {"values": ["公平"]},
        }),
        observer: WorldEntity(id=observer, kind="character", name="见证人", location_id=square, components={
            "personality": {"values": ["UNRELATED-NPC-SECRET"]},
        }),
    }, resources={source: ResourceAccount(
        resource_id=lot.resource_id, account_type="holder", balance=Decimal("8"), owner_id=giver,
    )})
    db = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    restored_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    db.create_schema()
    restored_db.create_schema()
    repo = WorldRepository(db)

    class Models:
        action = {}
        choice = "defer"

        def __init__(self):
            self.calls = []

        async def generate(self, identity, model_request):
            self.calls.append(model_request)
            if model_request.role_task.value == "npc_decision":
                prompt = "".join(message.content for message in model_request.messages)
                assert "UNRELATED-NPC-SECRET" not in prompt and str(source) not in prompt
                assert "公平" in prompt and lot.resource_id in prompt
                return SimpleNamespace(normalized_output={"choice": self.choice})
            return SimpleNamespace(normalized_output={"action": self.action, "clarification": ""})

    models = Models()
    worlds = WorldApplication(repo, ContentCatalog())
    service = ConversationApplication(worlds, models, ConversationStore(OperationsStore(tmp_path / "ops.db")),
        actions=RulesActionDispatcher(repo, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext))
    try:
        created = repo.create_world(scope, NewWorld(title="私人赠与分支", state=state))
        world, base = created.world.world_id, created.branch.branch_id
        recipient_scope = scope.model_copy(update={"user_id": uuid4()})
        repo.grant_membership(scope, world, recipient_scope.user_id, MembershipRole.PLAYER, controlled_actor_ids=(receiver,))
        port_origin = frozenset({"http://testserver"})
        app = create_app(worlds, LocalSession(str(scope.user_id), str(scope.tenant_id), port_origin), conversations=service)
        recipient_app = create_app(worlds, LocalSession(str(recipient_scope.user_id), str(scope.tenant_id), port_origin), conversations=service)
        with TestClient(app) as owner, TestClient(recipient_app) as recipient:
            for client in (owner, recipient):
                client.get("/api/v1/session").raise_for_status()

            def submit(client, branch, actor, text, action):
                models.action = action
                current = repo.read_world(scope, world, branch)
                data = {"world_id":str(world), "branch_id":str(branch), "actor_id":str(actor),
                        "session_id":str(uuid4()), "request_id":str(uuid4()), "expected_state_version":str(current.branch.state_version),
                        "mode":"action", "text":text}
                result = client.post(f"/api/v1/worlds/{world}/messages", json=data)
                assert result.is_success, result.text
                return result.json(), data

            offered, request = submit(owner, base, giver, "我送 Yara 两份粮食。", {
                "kind":"offer_private_gift", "recipient_id":str(receiver), "resource_id":lot.resource_id, "quantity":"2",
            })
            assert offered["world_effect"] and offered["elapsed_ticks"]=="0"
            gift = UUID(offered["references"][0])
            pending = repo.read_world(scope,world,base)
            offer, = pending.state.private_gifts
            assert offer.status=="pending" and offer.gift_id==gift
            assert pending.state.resources[source].balance==8
            assert owner.post(f"/api/v1/worlds/{world}/messages",json=request).raise_for_status().json()==offered
            assert len(models.calls)==1
            for actor in (giver,receiver,observer):
                projection=project_player_state(pending.state.model_dump(mode="json"),str(actor))
                rows=projection["own_private_gifts"]
                if actor==observer:
                    assert not rows
                else:
                    assert len(rows)==1 and "source_account_id" not in rows[0]

            accepted_branch=repo.fork(scope,world,base,pending.last_seq,"收下").branch_id
            declined_branch=repo.fork(scope,world,base,pending.last_seq,"婉拒").branch_id
            hosted_branch=repo.fork(scope,world,base,pending.last_seq,"请 NPC 自行决定").branch_id
            resolve={"kind":"resolve_private_gift","gift_id":str(gift),"expected_revision":1}
            good,gift_request=submit(recipient,accepted_branch,receiver,"我收下 Tarek 的赠与。",resolve|{"operation":"accept"})
            bad,_=submit(recipient,declined_branch,receiver,"我拒绝这份赠与。",resolve|{"operation":"decline"})
            assert all(row["world_effect"] and row["elapsed_ticks"]=="0" for row in (good,bad))
            recovered=service._committed_status(scope,world,accepted_branch,UUID(gift_request["request_id"]),receiver)
            assert recovered["result"]==good

            kept=repo.read_world(scope,world,accepted_branch)
            declined=repo.read_world(scope,world,declined_branch)
            assert kept.state.private_gifts[0].status=="accepted"
            assert declined.state.private_gifts[0].status=="declined"
            assert kept.state.resources[source].balance==6 and declined.state.resources[source].balance==8
            received=InventoryComponent.model_validate(kept.state.entities[receiver].components["inventory"])
            received_lot,=received.lots
            assert (received_lot.quantity,received_lot.quality,received_lot.expiry_tick,received_lot.origin_event_id)==(2,67,80_000,origin)
            assert kept.state.resources[received_lot.account_id].owner_id==receiver
            assert kept.state.resources[received_lot.account_id].balance==2
            assert "inventory" not in declined.state.entities[receiver].components
            assert repo.read_world(scope,world,base).state_hash==pending.state_hash
            receipts=repo.list_events(scope,world,accepted_branch,after_seq=pending.last_seq,limit=10).events
            assert len(receipts)==1 and receipts[0].type=="core.private_gift_changed.v1"
            record=receipts[0].model_dump(mode="json")
            assert project_event(record,str(observer)) is None
            receipt_view=project_event(record,str(receiver))
            assert receipt_view is not None and "transfer" not in receipt_view["payload"]
            assert str(source) not in json.dumps(receipt_view)

            invalid_id=uuid4()
            invalid_event=plan_private_gift_resolution(
                pending.state,world_id=world,branch_id=hosted_branch,command_id=invalid_id,
                actor_id=receiver,ruleset_version=pending.world.ruleset_version,gift_id=gift,
                expected_revision=1,operation="accept",
            )
            with pytest.raises(InvalidEvent, match="Private gift resolution"):
                repo.commit_command(scope,world,hosted_branch,CommandEnvelope(
                    command_id=invalid_id,actor_id=receiver,idempotency_key="test-wrong-command",
                    command_type="action.wait",expected_state_version=pending.branch.state_version,
                ),[invalid_event])

            host_intent={"kind":"request_private_gift_reply","gift_id":str(gift),"expected_revision":1}
            models.action=host_intent
            data={"world_id":str(world), "branch_id":str(hosted_branch), "actor_id":str(giver),
                  "session_id":str(uuid4()), "request_id":str(uuid4()), "expected_state_version":str(pending.branch.state_version),
                  "mode":"action", "text":"请让 Yara 自己决定是否收下。"}
            guarded=owner.post(f"/api/v1/worlds/{world}/messages",json=data)
            assert guarded.status_code==403
            assert all(call.role_task.value!="npc_decision" for call in models.calls)

            repo.grant_membership(scope,world,recipient_scope.user_id,MembershipRole.PLAYER)
            defer,_=submit(owner,hosted_branch,giver,"请让她自行决定。",host_intent)
            assert defer["action_status"]=="partial" and not defer["world_effect"]
            assert repo.read_world(scope,world,hosted_branch).state_hash==pending.state_hash
            models.choice="accept"
            accept_hosted,host_request=submit(owner,hosted_branch,giver,"再请她自行选择。",host_intent)
            assert accept_hosted["world_effect"] and accept_hosted["elapsed_ticks"]=="0"
            hosted_event,=repo.list_events(scope,world,hosted_branch,after_seq=pending.last_seq,limit=10).events
            assert hosted_event.actor_id==giver
            assert hosted_event.payload["actor_id"]==str(receiver) and hosted_event.payload["requested_by"]==str(giver)
            assert service._committed_status(scope,world,hosted_branch,UUID(host_request["request_id"]),giver)["result"]==accept_hosted

            backup=BackupApplication(repo,scratch_directory=tmp_path/"scratch")
            archive=backup.validate_archive(backup.export_world(scope,world,format_version=archive_version))
            restored=WorldRepository(restored_db)
            WorldBackupImporter(restored).import_verified_backup(scope,archive)
            for fork in (base,accepted_branch,declined_branch,hosted_branch):
                final=repo.replay(scope,world,fork,from_genesis=True)
                assert final.state_hash==restored.replay(scope,world,fork,from_genesis=True).state_hash
                assert final.state.current_tick==0
    finally:
        db.dispose()
        restored_db.dispose()
