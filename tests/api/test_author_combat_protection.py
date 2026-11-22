"""An authored combat protection rule changes the actual fight and recovery."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.visibility import project_player_state
from world_engine.application.worlds import WorldApplication
from world_engine.domain.combat import COMBAT_ATTACK_ROLL_LABEL, combat_attack_chance, combat_roll_digest, combat_roll_value
from world_engine.domain.models import AccessScope, NewWorld, ResourceAccount, WorldEntity, WorldState
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER
from world_engine.simulation import LongActionPlan, SimulationContext, SimulationEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_protected_npc_is_incapacitated_then_returns_after_actual_treatment(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    healer, npc, place, medicine, sink = (uuid4() for _ in range(5))
    state = WorldState(player_id=healer, entities={
        healer: WorldEntity(id=healer, kind="character", name="Aster", location_id=place, components={
            "skills": {"skills": {"skill.care": {"level": "10"}}},
            "inventory": {"lots": [{"lot_id": str(uuid4()), "account_id": str(medicine),
                                    "resource_id": "core.medicine", "quantity": "2"}]},
        }),
        npc: WorldEntity(id=npc, kind="character", name="米拉", location_id=place,
                         components={"health": {"current": 1, "maximum": 10}, "capabilities": {"grants": []}}),
        place: WorldEntity(id=place, kind="place", name="医馆", components={"place": {"place_type": "building"}}),
    }, resources={
        medicine: ResourceAccount(owner_id=healer, resource_id="core.medicine", balance="2"),
        sink: ResourceAccount(account_type="consumption_sink", resource_id="core.medicine", balance="0"),
    })
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        created = repository.create_world(scope, NewWorld(title="Combat protection", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        operations = OperationsStore(tmp_path / "ops.db")
        authoring = AuthoringApplication(repository, operations)
        worlds = WorldApplication(repository, None)

        class Proposal:
            def __init__(self):
                self.action = {"kind": "attack", "target_id": str(npc)}
                self.last_scene = None

            async def generate(self, _scope, request):
                self.last_scene = json.loads(request.messages[-1].content)["scene"]
                return SimpleNamespace(normalized_output={"action": self.action, "clarification": ""})

        proposal = Proposal()
        service = ConversationApplication(worlds, proposal, ConversationStore(operations), actions=RulesActionDispatcher(
            repository, proposal, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
        ))
        session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        origin = {"Origin": "http://testserver"}
        prefix = f"/api/v1/worlds/{world_id}"
        with TestClient(create_app(worlds, session, authoring=authoring, conversations=service)) as client:
            client.get("/api/v1/session").raise_for_status()

            def preview(entity):
                return client.post(f"{prefix}/interventions/preview", headers=origin, json={
                    "branch_id": str(branch_id), "patch_proposal": {"entity_id": str(entity), "component_name": "capabilities",
                    "replacement": {"combat_death_protection": True}, "reason": "保留受伤后的治疗分支"},
                })

            assert preview(healer).status_code == 422
            draft = preview(npc).raise_for_status().json()
            initial = client.post(f"{prefix}/interventions", headers=origin, json={"preview_id": draft["preview_id"],
                "expected_version": draft["expected_version"]}).raise_for_status().json()
            chance = combat_attack_chance(state.entities[healer], state.entities[npc])
            attack_id = next(command for command in map(lambda n: UUID(int=n), range(1, 1024))
                             if combat_roll_value(combat_roll_digest(world_id=world_id, branch_id=branch_id, command_id=command,
                                 ruleset_version=created.world.ruleset_version, label=COMBAT_ATTACK_ROLL_LABEL), 100) <= chance)
            conversation_id = str(uuid4())

            def send(command_id, version, text):
                return client.post(f"{prefix}/messages", headers=origin | {"Idempotency-Key": str(command_id)}, json={
                    "request_id": str(command_id), "world_id": str(world_id), "branch_id": str(branch_id),
                    "session_id": conversation_id, "actor_id": str(healer), "expected_state_version": str(version),
                    "mode": "action", "text": text,
                }).raise_for_status().json()

            hit = send(attack_id, initial["state_version"], "我攻击米拉。")
            assert hit["world_effect"] and "无法继续交战" in hit["content"]
            incapacitated = repository.read_world(scope, world_id, branch_id)
            victim = incapacitated.state.entities[npc]
            assert victim.status == "incapacitated" and victim.components["health"]["current"] == 1
            injury_id = victim.components["health"]["incapacitating_injury_id"]
            assert repository.find_death_events(scope, world_id, branch_id, {npc}) == {}
            own_status = project_player_state(incapacitated.state.model_dump(mode="json"), str(npc))["entities"][str(npc)]
            assert "combat_death_protection" not in own_status["components"]["capabilities"]
            clock = incapacitated.state.current_tick
            stopped = SimulationEngine().plan_window(incapacitated.state, LongActionPlan(
                actor_id=npc, started_tick=clock, target_tick=clock + 60, activity="wait",
            ), SimulationContext(world_id=world_id, branch_id=branch_id, command_id=uuid4(),
                                 world_seed="protected-patient", state_version=incapacitated.branch.state_version))
            assert stopped.outcome == "stopped" and stopped.interruption.reason_code == "actor_incapacitated"
            assert not stopped.events and stopped.next_tick == clock
            proposal.action = {"kind": "treat_injury", "target_id": str(npc), "injury_id": injury_id,
                               "medicine_resource_id": "core.medicine"}
            treated = send(uuid4(), incapacitated.branch.state_version, "用我的药处理米拉不能行动的那处伤。")
            assert treated["world_effect"] and treated["elapsed_ticks"] == "1800"
            scene = proposal.last_scene.get("nearby_incapacitated_treatment")
            assert scene and injury_id in json.dumps(scene)
        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.entities[npc].status == "active"
        assert final.state.entities[npc].components["health"]["current"] == 6
        assert final.state.resources[medicine].balance == 1
        assert repository.replay(scope, world_id, branch_id, through_seq=incapacitated.last_seq, from_genesis=True).state.entities[npc].status == "incapacitated"
        backups = BackupApplication(repository)
        with backups.validate_archive(backups.export_world(scope, world_id)) as archive:
            assert archive.load_head_state(branch_id) == final.state
    finally:
        database.dispose()
