"""A deterministic integration scenario for several private NPC romances.

The model gateway below is a stub: it exercises intent routing and the NPC
decision pipeline but does not assess real model judgment or prose quality.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.visibility import project_event, project_player_state
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import LifeComponent, PersonalityComponent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, WorldEntity, WorldState
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


class ScriptedRomanceModels:
    """Route each host request and return the named NPC's scripted choice."""

    def __init__(self, invitation_ids, choices):
        self.invitation_ids = list(invitation_ids)
        self.choices = choices
        self.calls = []
        self.decision_contexts = []
        self.gm_context = None

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if request.role_task is RoleTask.INTENT:
            invitation_id = self.invitation_ids.pop(0)
            output = {
                "action": {
                    "kind": "request_relationship_reply",
                    "invitation_id": str(invitation_id),
                },
                "clarification": "",
            }
        elif request.role_task is RoleTask.NPC_DECISION:
            context = json.loads(request.messages[-1].content)
            self.decision_contexts.append(context)
            output = {"choice": self.choices[context["invitee"]["name"]]}
        elif request.role_task is RoleTask.GM_ANSWER:
            self.gm_context = json.loads(request.messages[-1].content)["context"]
            output = {"answer": "后天 18 点到 19 点。", "references": []}
        else:
            raise AssertionError(f"Unexpected model task: {request.role_task}")
        return SimpleNamespace(normalized_output=output)


def _person(entity_id, name, place_id, trait):
    year_ticks = 86_400 * 360
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
            "personality": PersonalityComponent(
                traits=[trait],
                values=["honesty"],
            ).model_dump(mode="json"),
        },
    )


def _seed_invitation(repository, scope, created, proposer_id, invitee_id, index):
    head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    command_id = uuid4()
    payload = {"kind": "propose_relationship_invitation", "target_id": str(invitee_id)}
    if index in {0, 2}:
        payload["meeting"] = {"start_tick": 237600, "end_tick": 241200}
    result = RuleEngine().evaluate(head.state, payload, RuleContext(
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        command_id=command_id,
        actor_id=proposer_id,
        ruleset_version=head.world.ruleset_version,
    ))
    assert result.outcome.status == "succeeded", result.outcome.model_dump(mode="json")
    assert len(result.events) == 1
    repository.commit_command(scope, created.world.world_id, created.branch.branch_id, CommandEnvelope(
        command_id=command_id,
        actor_id=proposer_id,
        idempotency_key=f"romance-invitation-{index}",
        expected_state_version=head.branch.state_version,
        command_type="action.propose_relationship_invitation",
        payload=payload,
    ), result.events)
    return result.events[0].resulting_invitation


def _request(created, proposer_id, state_version, request_id):
    return MessageRequest(
        request_id=request_id,
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        session_id=uuid4(),
        actor_id=proposer_id,
        expected_state_version=str(state_version),
        mode="action",
        text="请让这位角色自行决定如何回应我的私人恋爱邀约。",
        profile=QualityProfile(speed="fast", depth="standard"),
    )


@pytest.mark.asyncio
async def test_three_npcs_make_independent_private_romance_choices(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        player_id, place_id = uuid4(), uuid4()
        npc_ids = {name: uuid4() for name in ("Rae", "Sol", "Mika")}
        bystander_id = uuid4()
        private_traits = {
            "Rae": "RAE_PRIVATE_MOTIVE",
            "Sol": "SOL_PRIVATE_MOTIVE",
            "Mika": "MIKA_PRIVATE_MOTIVE",
        }
        entities = {
            place_id: WorldEntity(id=place_id, kind="place", name="Station Cafe", status="active"),
            player_id: _person(player_id, "Morgan", place_id, "PLAYER_PRIVATE_MARKER"),
            **{
                npc_ids[name]: _person(npc_ids[name], name, place_id, private_traits[name])
                for name in npc_ids
            },
            bystander_id: _person(bystander_id, "Jun", place_id, "BYSTANDER_PRIVATE_MARKER"),
        }
        created = repository.create_world(scope, NewWorld(
            title="Station Cafe Invitations",
            ruleset_version="modern-multi-invitation.v1",
            state=WorldState(player_id=player_id, entities=entities),
        ))
        invitations = {
            name: _seed_invitation(repository, scope, created, player_id, npc_id, index)
            for index, (name, npc_id) in enumerate(npc_ids.items())
        }
        models = ScriptedRomanceModels(
            invitation_ids=[invitations[name].invitation_id for name in npc_ids],
            choices={"Rae": "accept", "Sol": "decline", "Mika": "defer"},
        )
        operations = OperationsStore(tmp_path / "operations.sqlite")
        service = ConversationApplication(
            WorldApplication(repository, None),
            models,
            ConversationStore(operations),
        )
        service.actions = RulesActionDispatcher(
            repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
        )

        results = {}
        for name in npc_ids:
            head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
            request = _request(created, player_id, head.branch.state_version, uuid4())
            results[name] = await service.send(scope, request)

        assert {name: result["action_status"] for name, result in results.items()} == {
            "Rae": "succeeded",
            "Sol": "succeeded",
            "Mika": "partial",
        }
        assert {name: result["world_effect"] for name, result in results.items()} == {
            "Rae": True,
            "Sol": True,
            "Mika": False,
        }

        head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        final_invitations = {item.invitation_id: item for item in head.state.relationship_invitations}
        assert {
            name: final_invitations[row.invitation_id].status
            for name, row in invitations.items()
        } == {"Rae": "accepted", "Sol": "declined", "Mika": "pending"}
        assert head.state.current_tick == 0

        assert [context["invitee"]["name"] for context in models.decision_contexts] == ["Rae", "Sol", "Mika"]
        for name, context in zip(npc_ids, models.decision_contexts, strict=True):
            prompt = json.dumps(context, ensure_ascii=False)
            assert context["invitee"]["personality"]["traits"] == [private_traits[name]]
            for other_name, marker in private_traits.items():
                if other_name != name:
                    assert marker not in prompt
            assert "PLAYER_PRIVATE_MARKER" not in prompt
            assert "BYSTANDER_PRIVATE_MARKER" not in prompt
            assert str(invitations[name].invitation_id) in prompt

        state_json = head.state.model_dump(mode="json")
        projected = {
            actor_id: project_player_state(state_json, str(actor_id))
            for actor_id in [player_id, *npc_ids.values(), bystander_id]
        }
        assert {row["invitation_id"] for row in projected[player_id]["own_relationship_invitations"]} == {
            str(row.invitation_id) for row in invitations.values()
        }
        for name, actor_id in npc_ids.items():
            assert [row["invitation_id"] for row in projected[actor_id]["own_relationship_invitations"]] == [
                str(invitations[name].invitation_id)
            ]
        assert projected[bystander_id]["own_relationship_invitations"] == []

        gm_request = _request(created, player_id, head.branch.state_version, uuid4()).model_copy(
            update={"mode": "ask_gm", "text": "和 Rae 约到了几点？"},
        )
        gm_result = await service.send(scope, gm_request)
        assert not gm_result["world_effect"]
        gm_invitations = {
            row["invitation_id"]: row for row in models.gm_context["state"]["own_relationship_invitations"]
        }
        meeting = gm_invitations[str(invitations["Rae"].invitation_id)]["meeting"]
        assert meeting == {
            "start_tick": 237600, "end_tick": 241200,
            "local_start": {"day_offset_from_now": 2, "clock": "18:00:00"},
            "local_end": {"day_offset_from_now": 2, "clock": "19:00:00"},
        }
        pending_meeting = gm_invitations[str(invitations["Mika"].invitation_id)]["meeting"]
        assert pending_meeting == meeting | {"response_deadline_passed": False}
        assert repository.read_world(scope, created.world.world_id, created.branch.branch_id).state_hash == head.state_hash

        invitation_events = [
            event for event in repository.list_events(
                scope, created.world.world_id, created.branch.branch_id, limit=100,
            ).events
            if event.type == "core.relationship_invitation_changed.v1"
        ]
        assert [event.payload["resulting_invitation"]["status"] for event in invitation_events] == [
            "pending", "pending", "pending", "accepted", "declined",
        ]
        viewers = [player_id, *npc_ids.values(), bystander_id]
        for event in invitation_events:
            invitation = event.payload["resulting_invitation"]
            participants = {invitation["proposer_id"], invitation["invitee_id"]}
            for actor_id in viewers:
                assert (project_event(event.model_dump(mode="json"), str(actor_id)) is not None) == (
                    str(actor_id) in participants
                )

        replayed = repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True)
        assert replayed.state_hash == head.state_hash
    finally:
        database.dispose()
