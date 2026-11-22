"""Integration coverage for recipient-scoped partnership self-disclosure."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.visibility import (
    project_event,
    project_player_state,
    project_sent_relationship_disclosures,
)
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import LifeComponent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, WorldEntity, WorldState
from world_engine.rules import ACTION_INTENT_ADAPTER, RuleContext, RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _make_world(repository, scope):
    player, partner, recipient, bystander, remote, square, studio = (uuid4() for _ in range(7))
    year_ticks = 86_400 * 360

    def person(entity_id, name, location_id):
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            status="active",
            location_id=location_id,
            components={"life": LifeComponent(
                birth_tick=-(25 * year_ticks), life_stage="adult",
            ).model_dump(mode="json")},
        )

    created = repository.create_world(scope, NewWorld(
        title="Relationship disclosure",
        ruleset_version="relationship-disclosure-test.v1",
        state=WorldState(
            player_id=player,
            current_tick=60,
            entities={
                square: WorldEntity(id=square, kind="place", name="Harbor Square"),
                studio: WorldEntity(id=studio, kind="place", name="Workshop"),
                player: person(player, "Traveller", square),
                partner: person(partner, "Abena Quinn", square),
                recipient: person(recipient, "Aiko Valdez", square),
                bystander: person(bystander, "Mika Ito", square),
                remote: person(remote, "Sol Reyes", studio),
            },
        ),
    ))
    return created, {
        "player": player,
        "partner": partner,
        "recipient": recipient,
        "bystander": bystander,
        "remote": remote,
        "square": square,
    }


def _seed_active_partnership(repository, scope, created, people):
    engine = RuleEngine()
    player, partner = people["player"], people["partner"]
    head = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    proposal_id = uuid4()
    proposal_action = ACTION_INTENT_ADAPTER.validate_python({
        "kind": "propose_relationship_partnership", "partner_id": str(partner),
    })
    proposal_context = RuleContext(
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        command_id=proposal_id,
        actor_id=player,
        ruleset_version=created.world.ruleset_version,
    )
    proposal = engine.evaluate(head.state, proposal_action, proposal_context)
    repository.commit_command(
        scope, created.world.world_id, created.branch.branch_id,
        CommandEnvelope(
            command_id=proposal_id,
            actor_id=player,
            idempotency_key=f"seed-partnership-proposal:{proposal_id}",
            expected_state_version=head.branch.state_version,
            command_type="action.propose_relationship_partnership",
            payload=proposal_action.model_dump(mode="json"),
        ),
        proposal.events,
    )
    proposed = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    partnership = proposed.state.relationship_partnerships[0]
    accept_id = uuid4()
    accept_action = ACTION_INTENT_ADAPTER.validate_python({
        "kind": "resolve_relationship_partnership",
        "partnership_id": str(partnership.partnership_id),
        "operation": "accept",
        "expected_revision": partnership.revision,
    })
    accept_context = RuleContext(
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        command_id=accept_id,
        actor_id=partner,
        ruleset_version=created.world.ruleset_version,
    )
    acceptance = engine.evaluate(proposed.state, accept_action, accept_context)
    repository.commit_command(
        scope, created.world.world_id, created.branch.branch_id,
        CommandEnvelope(
            command_id=accept_id,
            actor_id=partner,
            idempotency_key=f"seed-partnership-accept:{accept_id}",
            expected_state_version=proposed.branch.state_version,
            command_type="action.resolve_relationship_partnership",
            payload=accept_action.model_dump(mode="json"),
        ),
        acceptance.events,
    )
    return repository.read_world(scope, created.world.world_id, created.branch.branch_id).state.relationship_partnerships[0]


class DisclosureModels:
    def __init__(self, people, *, action_choice="disclose"):
        self.people = people
        self.action_choice = action_choice
        self.action_contexts = []
        self.dialogue_contexts = []
        self.gm_contexts = []

    async def generate(self, _scope, request):
        if request.role_task is RoleTask.INTENT:
            payload = json.loads(request.messages[-1].content)
            if "speech" in payload:
                return SimpleNamespace(normalized_output={
                    "target_id": str(self.people["recipient"]),
                    "purpose": "dialogue",
                    "subject_id": None,
                    "predicate": None,
                    "effective_tick": None,
                    "clarification": "",
                })

            self.action_contexts.append(payload)
            scene = payload["scene"]
            option = next(
                item for item in scene["relationship_disclosure_options"]
                if item["opposite_participant_name"] == "Abena Quinn"
            )
            recipient = next(
                item for item in option["recipient_options"]
                if item["recipient_name"] == "Aiko Valdez"
            )
            if self.action_choice == "none":
                action = None
            elif self.action_choice == "outside_scene":
                action = {
                    "kind": "disclose_relationship_partnership",
                    "partnership_id": option["partnership_id"],
                    "expected_revision": option["expected_revision"],
                    "recipient_id": str(self.people["remote"]),
                }
            else:
                action = {
                    "kind": "disclose_relationship_partnership",
                    "partnership_id": option["partnership_id"],
                    "expected_revision": option["expected_revision"],
                    "recipient_id": recipient["recipient_id"],
                }
            return SimpleNamespace(normalized_output={"action": action, "clarification": ""})

        if request.role_task is RoleTask.SOCIAL_ADJUDICATION:
            self.dialogue_contexts.append(json.loads(request.messages[-1].content))
            return SimpleNamespace(normalized_output={"utterance": "我记得你当时这么告诉过我。"})
        if request.role_task is RoleTask.GM_ANSWER:
            context = json.loads(request.messages[-1].content)["context"]
            self.gm_contexts.append(context)
            references = [item["record_id"] for item in context["sent_relationship_disclosures"]]
            return SimpleNamespace(normalized_output={
                "answer": "你当时告诉 Aiko Valdez，你和 Abena Quinn 已成为伴侣。",
                "references": references,
            })
        raise AssertionError(f"Unexpected model task: {request.role_task}")


def _service(tmp_path, repository, models):
    tmp_path.mkdir(parents=True, exist_ok=True)
    operations = OperationsStore(tmp_path / "operations.db")
    service = ConversationApplication(WorldApplication(repository, None), models, ConversationStore(operations))
    service.actions = RulesActionDispatcher(repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
    return service


def _request(created, actor_id, text, *, mode="action", expected_state_version="0"):
    return MessageRequest(
        request_id=uuid4(),
        session_id=uuid4(),
        world_id=created.world.world_id,
        branch_id=created.branch.branch_id,
        actor_id=actor_id,
        expected_state_version=expected_state_version,
        mode=mode,
        text=text,
    )


@pytest.mark.asyncio
async def test_named_relationship_disclosure_is_private_and_dialogue_receives_only_a_past_statement(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, people = _make_world(repository, scope)
        people["partnership"] = _seed_active_partnership(repository, scope, created, people)
        models = DisclosureModels(people)
        service = _service(tmp_path, repository, models)
        opening = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        opening_projection = project_player_state(opening.state.model_dump(mode="json"), str(people["player"]))
        option, = opening_projection["relationship_disclosure_options"]
        assert option["partnership_id"] == str(people["partnership"].partnership_id)
        assert option["expected_revision"] == people["partnership"].revision
        assert option["opposite_participant_name"] == "Abena Quinn"
        recipient_ids = {row["recipient_id"] for row in option["recipient_options"]}
        assert recipient_ids == {str(people["recipient"]), str(people["bystander"])}
        assert str(people["remote"]) not in recipient_ids

        before_query = _request(
            created, people["player"], "我刚才告诉 Aiko 什么了？",
            mode="ask_gm", expected_state_version=str(opening.branch.state_version),
        )
        before_answer = await service.send(scope, before_query)
        assert before_answer["references"] == []
        before_context = models.gm_contexts[-1]
        assert before_context["sent_relationship_disclosures"] == []

        request = _request(
            created, people["player"],
            "我只私下告诉 Aiko Valdez，我和 Abena Quinn 已经是伴侣。",
            expected_state_version=str(opening.branch.state_version),
        )
        response = await service.send(scope, request)
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert response["action_status"] == "succeeded"
        assert response["content"] == "已将这段关系告诉指定人物。"
        assert response["references"] == [str(people["recipient"])]
        assert response["elapsed_ticks"] == "0"
        assert after.state.current_tick == opening.state.current_tick == 60
        assert after.state.relationship_partnerships == opening.state.relationship_partnerships
        assert len(models.action_contexts) == 1
        intent_context = models.action_contexts[0]["scene"]["relationship_disclosure_options"]
        assert intent_context == opening_projection["relationship_disclosure_options"]

        record, = [
            record for record in after.state.knowledge
            if record.holder_id == people["recipient"] and record.relationship_disclosure is not None
        ]
        receipt = record.relationship_disclosure
        assert receipt is not None
        assert (receipt.discloser_id, receipt.opposite_participant_id, receipt.recipient_id) == (
            people["player"], people["partner"], people["recipient"],
        )
        assert receipt.source_partnership_id == people["partnership"].partnership_id
        assert receipt.source_revision == people["partnership"].revision

        after_query = _request(
            created, people["player"], "我刚才告诉 Aiko 什么了？",
            mode="ask_gm", expected_state_version=str(after.branch.state_version),
        )
        after_answer = await service.send(scope, after_query)
        assert after_answer["references"] == [str(record.id)]
        sender_context = models.gm_contexts[-1]
        sent, = sender_context["sent_relationship_disclosures"]
        assert sent["record_id"] == str(record.id)
        assert sent["discloser_id"] == str(people["player"])
        assert sent["discloser_name"] == "Traveller"
        assert sent["recipient_id"] == str(people["recipient"])
        assert sent["recipient_name"] == "Aiko Valdez"
        assert sent["disclosed_tick"] == receipt.disclosed_tick == opening.state.current_tick
        assert sent["source"] == "past_statement_by_discloser"
        assert "Abena Quinn" in sent["statement"]
        assert {str(record.id), str(people["recipient"]), str(people["partner"])} <= set(
            sender_context["reference_ids"]
        )
        assert set(sent) == {
            "record_id", "statement", "source", "discloser_id", "discloser_name",
            "recipient_id", "recipient_name", "opposite_participant_id",
            "opposite_participant_name", "disclosed_tick",
        }
        sent_json = json.dumps(sent, ensure_ascii=False)
        assert str(people["partnership"].partnership_id) not in sent_json
        assert "source_partnership_id" not in sent_json and "source_revision" not in sent_json
        assert record.claim not in sent_json

        raw_after = after.state.model_dump(mode="json")
        sender_projection = project_sent_relationship_disclosures(
            raw_after, str(people["player"]), current_tick=after.state.current_tick,
        )
        assert sender_projection == [sent]
        for observer in (people["recipient"], people["partner"], people["bystander"]):
            assert project_sent_relationship_disclosures(
                raw_after, str(observer), current_tick=after.state.current_tick,
            ) == []

        recipient_view = project_player_state(after.state.model_dump(mode="json"), str(people["recipient"]))
        disclosure, = recipient_view["own_relationship_disclosures"]
        assert disclosure["record_id"] == str(record.id)
        assert disclosure["source"] == "past_statement_by_discloser"
        assert "Abena Quinn" in disclosure["statement"]
        assert disclosure["opposite_participant_name"] == "Abena Quinn"
        recipient_view_json = json.dumps(recipient_view, ensure_ascii=False)
        assert str(people["partnership"].partnership_id) not in recipient_view_json
        assert "source_partnership_id" not in recipient_view_json and "source_revision" not in recipient_view_json
        assert all(item["id"] != str(record.id) for item in recipient_view["knowledge"])
        assert record.claim not in json.dumps(recipient_view["knowledge"], ensure_ascii=False)

        for observer in (people["player"], people["partner"], people["bystander"]):
            projection = project_player_state(after.state.model_dump(mode="json"), str(observer))
            assert projection["own_relationship_disclosures"] == []
        partner_view = project_player_state(after.state.model_dump(mode="json"), str(people["partner"]))
        assert [row["partnership_id"] for row in partner_view["own_relationship_partnerships"]] == [
            str(people["partnership"].partnership_id),
        ]

        events = repository.list_events(
            scope, created.world.world_id, created.branch.branch_id, after_seq=opening.last_seq,
        ).events
        assert [event.type for event in events] == ["core.relationship_disclosure_recorded.v1"]
        safe_event = project_event(
            events[0].model_dump(mode="json"), str(people["recipient"]), entities=after.state.entities,
        )
        assert safe_event is not None
        _, default_player, history_names = repository.read_player_history_context(
            scope, created.world.world_id, created.branch.branch_id,
        )
        assert default_player == people["player"]
        assert project_event(
            events[0].model_dump(mode="json"), str(people["recipient"]), entities=history_names,
        ) == safe_event
        safe_event_json = json.dumps(safe_event, ensure_ascii=False)
        assert safe_event["payload"]["record_id"] == str(record.id)
        assert "Abena Quinn" in safe_event["payload"]["statement"]
        assert str(people["partnership"].partnership_id) not in safe_event_json
        assert "source_partnership_id" not in safe_event_json and "source_revision" not in safe_event_json
        for observer in (people["player"], people["partner"], people["bystander"]):
            assert project_event(
                events[0].model_dump(mode="json"), str(observer), entities=after.state.entities,
            ) is None

        after_options = project_player_state(after.state.model_dump(mode="json"), str(people["player"]))[
            "relationship_disclosure_options"
        ]
        source_option = next(row for row in after_options if row["partnership_id"] == str(receipt.source_partnership_id))
        assert str(people["recipient"]) not in {row["recipient_id"] for row in source_option["recipient_options"]}

        dialogue_request = _request(
            created, people["player"], "Aiko Valdez，我想听听看，你还记得我当时特地告诉你的那件事吗？",
            mode="roleplay",
        ).model_copy(update={"expected_state_version": str(after.branch.state_version)})
        dialogue_response = await service.send(scope, dialogue_request)
        assert dialogue_response["kind"] == "dialogue"
        dialogue_context = models.dialogue_contexts[-1]
        past, = dialogue_context.get("past_relationship_disclosures_by_current_interlocutor", [])
        assert past["source"] == "past_statement_by_discloser"
        assert "Abena Quinn" in past["statement"]
        dialogue_json = json.dumps(dialogue_context, ensure_ascii=False)
        assert str(people["partnership"].partnership_id) not in dialogue_json
        assert "source_partnership_id" not in dialogue_json and "source_revision" not in dialogue_json
        assert record.claim not in dialogue_json
        final = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert final.state.current_tick == opening.state.current_tick
        disclosure_events = [
            event for event in repository.list_events(scope, created.world.world_id, created.branch.branch_id).events
            if event.type == "core.relationship_disclosure_recorded.v1"
        ]
        assert len(disclosure_events) == 1
        assert repository.replay(scope, created.world.world_id, created.branch.branch_id, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["outside_scene", "none"])
async def test_disclosure_requires_explicit_intent_and_current_scene_recipient(tmp_path, choice):
    database = Database(f"sqlite:///{(tmp_path / f'{choice}.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, people = _make_world(repository, scope)
        people["partnership"] = _seed_active_partnership(repository, scope, created, people)
        models = DisclosureModels(people, action_choice=choice)
        service = _service(tmp_path / choice, repository, models)
        before = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        request = _request(
            created, people["player"], "Abena Quinn and I are partners.",
            expected_state_version=str(before.branch.state_version),
        )
        response = await service.send(scope, request)
        after = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
        assert response["kind"] == "clarification"
        assert response["world_effect"] is False
        assert after.state_hash == before.state_hash
        assert after.last_seq == before.last_seq
        assert after.state.current_tick == before.state.current_tick
        assert not any(record.relationship_disclosure is not None for record in after.state.knowledge)
        assert not any(
            event.type == "core.relationship_disclosure_recorded.v1"
            for event in repository.list_events(scope, created.world.world_id, created.branch.branch_id).events
        )
    finally:
        database.dispose()
