"""Full-pack Ask-GM context regression for routes, diplomacy, and freight.

The isolated fixture uses the shipped low_magic_feudal 2.3.0 pack (seed 0).
Its three explicit author revisions mirror the persistent freeplay setup:
enable safe-conduct permission on the existing Greyhaven–Nine Bridges route,
make the player the Greyhaven representative, and disclose the Grain Factors'
preference for a narrow trial. The last revision is narrative context only;
it does not change any executable rule. The player is placed at Ashen March
before genesis as a fixture convenience. The actual query path is the normal
ConversationApplication.send -> _answer path, with only ModelOperations.generate
replaced by a spy; no network request is made.
"""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import ModelRequest, RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.safe_conduct_context import safe_conduct_closure_matches
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.components import KnowledgeRecord, VisibilityScope
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG
from world_engine.domain.safe_conduct_state import (
    PROPOSE_SAFE_CONDUCT_COMMAND,
    RESOLVE_SAFE_CONDUCT_COMMAND,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


PACK_ID = "low_magic_feudal"
PACK_VERSION = "2.3.0"
GRAIN = "feudal.grain"


def test_treaty_closure_binds_the_player_reference_to_the_selected_proposal():
    first, second = uuid4(), uuid4()
    treaties = [
        {"treaty_id": str(first), "status": "pending", "recipient_name": "灰谷公国", "to_place_name": "九桥谷"},
        {"treaty_id": str(second), "status": "countered", "recipient_name": "灰谷公国", "to_place_name": "北河口"},
    ]

    assert not safe_conduct_closure_matches("把昨天聊的那份撤了。", first, "withdraw", treaties[:1])
    assert not safe_conduct_closure_matches("撤回给灰谷公国的通行提案。", first, "withdraw", treaties)
    assert safe_conduct_closure_matches("撤回给灰谷公国的九桥谷提案。", first, "withdraw", treaties)
    assert not safe_conduct_closure_matches("撤回给灰谷公国的九桥谷提案。", second, "withdraw", treaties)
    assert safe_conduct_closure_matches(f"撤回协定 {second}。", second, "withdraw", treaties)


def _commit_action(repository, scope, world_id, branch_id, actor_id, payload, *, key):
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
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    command_type = (
        PROPOSE_SAFE_CONDUCT_COMMAND if normalized["kind"] == "propose_safe_conduct"
        else RESOLVE_SAFE_CONDUCT_COMMAND
    )
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=key,
        expected_state_version=view.branch.state_version,
        command_type=command_type,
        payload=normalized,
    )
    committed = repository.commit_command(scope, world_id, branch_id, command, resolution.events)
    return committed, resolution


def _read_context(model_request: object) -> dict:
    assert isinstance(model_request, ModelRequest)
    assert model_request.role_task is RoleTask.GM_ANSWER
    assert len(model_request.messages) == 2
    return json.loads(model_request.messages[-1].content)["context"]


def _grain_balance(action_context: dict) -> str:
    return next(row["balance"] for row in action_context["held_resource_balances"]
                if row["resource_id"] == GRAIN)


@pytest.mark.asyncio
async def test_full_feudal_gm_context_tracks_safe_conduct_and_shippable_grain(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'feudal-world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    author_operations = OperationsStore(tmp_path / "author-operations.db")
    message_operations = OperationsStore(tmp_path / "message-operations.db")
    catalog = ContentCatalog()
    draft = catalog.build_world(PACK_ID, "GM action context", "Traveller", seed=0, pack_version=PACK_VERSION)
    state = draft.state

    assert sum(entity.kind == "polity" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "organization" for entity in state.entities.values()) == 24
    assert sum(entity.kind == "place" for entity in state.entities.values()) == 120
    assert sum(entity.kind == "character" for entity in state.entities.values()) == 273

    actor_id = state.player_id
    actor = state.entities[actor_id]
    other_character_id = next(
        entity.id for entity in state.entities.values()
        if entity.kind == "character" and entity.id != actor_id
    )
    visible_knowledge_id = uuid4()
    author_only_knowledge_id = uuid4()
    future_knowledge_id = uuid4()
    unavailable_knowledge_id = uuid4()
    state = state.model_copy(update={"knowledge": [
        KnowledgeRecord(
            id=visible_knowledge_id, owner_id=actor_id,
            claim="A private note already available to the player.", received_tick=0,
        ),
        KnowledgeRecord(
            id=author_only_knowledge_id, owner_id=actor_id,
            claim="An author-only note.",
            visibility=VisibilityScope(policy="author_only"),
        ),
        KnowledgeRecord(
            id=future_knowledge_id, holder_id=actor_id,
            claim="A future note.", valid_from_tick=1,
        ),
        KnowledgeRecord(
            id=unavailable_knowledge_id, owner_id=other_character_id,
            claim="Another character's private note.",
        ),
    ]})
    source = next(entity for entity in state.entities.values() if entity.name == "Duchy of Greyhaven")
    recipient = next(entity for entity in state.entities.values() if entity.name == "Rookfield Grain Factors")
    origin = state.entities[source.location_id]
    destination = state.entities[recipient.location_id]
    route = next(row for row in origin.components["place"]["routes"] if row["to_place_id"] == str(destination.id))
    route_id = UUID(route["route_id"])
    assert route["travel_ticks"] == 172800
    assert sum((Decimal(lot["quantity"]) for lot in actor.components["inventory"]["lots"]
                if lot["resource_id"] == GRAIN), Decimal("0")) == Decimal("54")

    # An unrelated, existing organization-held grain account is the privacy
    # sentinel. It is far from the player and is not either treaty party.
    organizations = {
        entity.id: entity for entity in state.entities.values()
        if entity.kind in {"organization", "polity", "faction", "company", "government"}
    }
    private_account_id, private_owner_id = next(
        (account_id, account.owner_id)
        for account_id, account in state.resources.items()
        if account.owner_id in organizations
        and account.owner_id not in {source.id, recipient.id}
        and account.resource_id == GRAIN and account.account_type == "holder" and account.balance > 0
        and organizations[account.owner_id].location_id not in {origin.id, destination.id}
    )
    hidden_destination = next(
        entity for entity in state.entities.values() if entity.components.get("hidden")
        and entity.kind in {"place", "location"}
    )
    hidden_destination_id = str(hidden_destination.id)
    # Relocation is a fixture convenience performed before the first commit.
    actor = actor.model_copy(update={"location_id": origin.id})
    draft = draft.model_copy(update={"state": state.model_copy(update={"entities": {**state.entities, actor_id: actor}})})
    created = repository.create_world(scope, draft)
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    authoring = AuthoringApplication(repository, author_operations)

    def author_component(entity_id, component_name, replacement, reason):
        preview = authoring.preview(
            scope, world_id, branch_id, entity_id, component_name, replacement, reason,
        )
        authoring.commit(scope, world_id, UUID(preview["preview_id"]), preview["expected_version"])

    origin_place = deepcopy(origin.components["place"])
    selected_route = next(row for row in origin_place["routes"] if row["route_id"] == str(route_id))
    assert SAFE_CONDUCT_TAG not in selected_route["access_tags"]
    selected_route["access_tags"].append(SAFE_CONDUCT_TAG)
    author_component(
        origin.id, "place", origin_place,
        "Isolated fixture: expose the existing Greyhaven–Nine Bridges route to safe-conduct rules.",
    )

    source_governance = deepcopy(source.components["governance"])
    if str(actor_id) not in source_governance["members"]:
        source_governance["members"].append(str(actor_id))
    source_governance["leader_id"] = str(actor_id)
    next(row for row in source_governance["offices"] if row["office_id"] == "office.leader")["holder_id"] = str(actor_id)
    author_component(
        source.id, "governance", source_governance,
        "Isolated fixture: grant the player representative authority in Greyhaven.",
    )

    public_description = recipient.components.get("public_description", "")
    trial_clause = " The factors will consider one tightly scoped grain passage as a trial, but reserve longer authorizations for routes with an established record."
    author_component(
        recipient.id, "public_description", public_description.rstrip() + trial_clause,
        "Isolated narrative context only: disclose the Grain Factors' preference; this is not an executable rule.",
    )

    seq3 = repository.read_world(scope, world_id, branch_id)
    assert seq3.last_seq == 3
    assert seq3.state.current_tick == 0

    captured: list[object] = []
    next_references: list[str] = [
        str(route_id), str(origin.id), str(destination.id), str(visible_knowledge_id),
    ]
    queued_gm_outputs: list[dict[str, object]] = []

    class ModelSpy:
        async def generate(self, _scope, model_request):
            captured.append(model_request)
            if model_request.role_task is RoleTask.AUTHOR_ASSIST:
                return SimpleNamespace(normalized_output={"answer": "建议仅供作者参考。", "references": []})
            assert model_request.role_task is RoleTask.GM_ANSWER
            if queued_gm_outputs:
                return SimpleNamespace(normalized_output=queued_gm_outputs.pop(0))
            return SimpleNamespace(normalized_output={
                "answer": "已根据当前路线与通行记录回答。",
                "references": list(next_references),
            })

    worlds = WorldApplication(repository, catalog)
    service = ConversationApplication(worlds, ModelSpy(), ConversationStore(message_operations))

    def query_request(view, *, text, mode="ask_gm"):
        return MessageRequest(
            request_id=uuid4(), world_id=world_id, branch_id=view.branch.branch_id,
            session_id=uuid4(), actor_id=actor_id, expected_state_version=str(view.branch.state_version),
            mode=mode, text=text,
        )

    opening_request = query_request(
        seq3,
        text="我有一些粮，想到九桥谷看看通路与许可。只告诉我选择，不要提案、发货或推进时间。",
    )
    opening_result = await service.send(scope, opening_request)
    assert opening_result["kind"] == "query" and opening_result["world_effect"] is False
    assert len(captured) == 1
    gm_answer_instructions = captured[-1].messages[0].content
    assert "印章、序号和引号内原文是来源内容" in gm_answer_instructions
    assert "逐字照录并引用该来源" in gm_answer_instructions
    opening_context = _read_context(captured[-1])
    action_context = opening_context["action_context"]
    assert opening_context["current_tick"] == action_context["snapshot_tick"] == 0
    assert "inventory" not in opening_context["state"]["entities"][str(actor_id)]["components"]
    legal_option = next(
        row for row in action_context["safe_conduct_proposal_options"]
        if row["route_id"] == str(route_id) and row["to_place_id"] == str(destination.id)
        and row["recipient_organization_id"] == str(recipient.id)
    )
    assert legal_option["travel_ticks"] == "172800"
    assert legal_option["travel_permitted_now"] is False
    assert {str(route_id), str(origin.id), str(destination.id), str(visible_knowledge_id)} <= set(
        opening_context["reference_ids"]
    )
    assert opening_result["references"] == next_references
    projected_knowledge_ids = {row["id"] for row in opening_context["state"]["knowledge"]}
    assert str(visible_knowledge_id) in projected_knowledge_ids
    assert not {
        str(author_only_knowledge_id), str(future_knowledge_id), str(unavailable_knowledge_id),
    } & projected_knowledge_ids
    assert not {
        str(author_only_knowledge_id), str(future_knowledge_id), str(unavailable_knowledge_id),
    } & set(opening_context["reference_ids"])
    assert _grain_balance(action_context) == "54"
    assert not any(row["route_id"] == str(route_id) and row["resource_id"] == GRAIN
                   for row in action_context["shipment_dispatch_options"])

    opening_payload = json.dumps(opening_context, ensure_ascii=False)
    hidden_route = next(
        row for row in origin.components["place"]["routes"]
        if row["to_place_id"] == hidden_destination_id
    )
    assert hidden_destination_id not in opening_payload
    assert hidden_route["route_id"] not in opening_payload
    assert hidden_destination.name not in opening_payload
    assert str(private_account_id) not in opening_payload
    assert str(private_owner_id) not in opening_payload
    assert opening_context["state"]["resources"]  # only the player's own account projection
    assert all(row.get("owner_id") == str(actor_id) for row in opening_context["state"]["resources"].values())

    after_opening = repository.read_world(scope, world_id, branch_id)
    assert (after_opening.last_seq, after_opening.state_hash, after_opening.state.current_tick) == (
        seq3.last_seq, seq3.state_hash, seq3.state.current_tick,
    )

    proposal, proposal_resolution = _commit_action(
        repository, scope, world_id, branch_id, actor_id,
        {"kind": "propose_safe_conduct", "proposer_organization_id": str(source.id),
         "recipient_organization_id": str(recipient.id), "from_place_id": str(origin.id),
         "route_id": str(route_id), "expires_at_tick": 345600},
        key="gm-context-test-proposal",
    )
    treaty_id = proposal_resolution.events[0].resulting_treaty.treaty_id
    seq4 = repository.read_world(scope, world_id, branch_id)
    next_references = [str(treaty_id), str(route_id), str(destination.id)]

    # A separately controlled player can read their own pending proposal but
    # cannot request a hosted NPC response under the world owner's authority.
    guest_scope = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    repository.grant_membership(
        scope, world_id, guest_scope.user_id, MembershipRole.PLAYER,
        controlled_actor_ids=[actor_id],
    )
    guest_pending = await service.send(
        guest_scope, query_request(seq4, text="这份提案是否还可请对方回应？"),
    )
    assert guest_pending["world_effect"] is False
    guest_context = _read_context(captured[-1])
    guest_treaty = next(
        row for row in guest_context["action_context"]["own_safe_conduct_treaties"]
        if row["treaty_id"] == str(treaty_id)
    )
    guest_state_treaty = next(
        row for row in guest_context["state"]["own_safe_conduct_treaties"]
        if row["treaty_id"] == str(treaty_id)
    )
    assert guest_treaty["can_request_response"] is False
    assert guest_state_treaty["can_request_response"] is False
    after_guest_query = repository.read_world(scope, world_id, branch_id)
    assert (after_guest_query.last_seq, after_guest_query.state_hash, after_guest_query.state.current_tick) == (
        seq4.last_seq, seq4.state_hash, seq4.state.current_tick,
    )

    missing_owner_view = SimpleNamespace(
        state=seq4.state,
        world=SimpleNamespace(world_id=world_id, ruleset_version=seq4.world.ruleset_version),
        branch=seq4.branch,
        last_seq=seq4.last_seq,
    )
    missing_owner_request = query_request(seq4, text="未知所有者的视图不能授予托管权限。")
    await service._answer(scope, missing_owner_view, missing_owner_request, "query")
    missing_owner_context = _read_context(captured[-1])
    missing_owner_treaty = next(
        row for row in missing_owner_context["action_context"]["own_safe_conduct_treaties"]
        if row["treaty_id"] == str(treaty_id)
    )
    assert missing_owner_treaty["can_request_response"] is False

    recipient_leader = UUID(recipient.components["governance"]["leader_id"])
    counter_commit, counter_resolution = _commit_action(
        repository, scope, world_id, branch_id, recipient_leader,
        {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
         "expected_revision": 1, "operation": "counter", "counter_expires_at_tick": 172800},
        key="gm-context-test-counter",
    )
    assert proposal.last_seq == 4 and counter_commit.last_seq == 5
    seq5 = repository.read_world(scope, world_id, branch_id)

    counter_request = query_request(seq5, text="對方的期限還剩多久？我現在能接受嗎？我手上有多少糧，能否發運？")
    counter_result = await service.send(scope, counter_request)
    assert counter_result["kind"] == "query" and counter_result["world_effect"] is False
    counter_context = _read_context(captured[-1])
    counter_action_context = counter_context["action_context"]
    treaty = next(row for row in counter_action_context["own_safe_conduct_treaties"]
                  if row["treaty_id"] == str(treaty_id))
    assert treaty["status"] == "countered"
    assert treaty["counter_expires_at_tick"] == 172800
    assert treaty["can_accept_counter"] is True
    assert treaty["can_ratify"] is False
    assert _grain_balance(counter_action_context) == "54"
    assert not any(row["route_id"] == str(route_id) and row["resource_id"] == GRAIN
                   for row in counter_action_context["shipment_dispatch_options"])
    assert {str(treaty_id), str(route_id), str(origin.id), str(destination.id)} <= set(counter_context["reference_ids"])
    assert counter_result["references"] == next_references

    after_counter_query = repository.read_world(scope, world_id, branch_id)
    assert (after_counter_query.last_seq, after_counter_query.state_hash, after_counter_query.state.current_tick) == (
        seq5.last_seq, seq5.state_hash, seq5.state.current_tick,
    )

    # A fresh child accepting the counter turns the legal route into a usable
    # shipment option. Held balance and actually shippable quantity stay distinct.
    accepted_branch = repository.fork(scope, world_id, branch_id, seq5.last_seq, "accepted counter context").branch_id
    _commit_action(
        repository, scope, world_id, accepted_branch, actor_id,
        {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
         "expected_revision": 2, "operation": "accept_counter"},
        key="gm-context-test-accept-counter",
    )
    accepted = repository.read_world(scope, world_id, accepted_branch)
    accepted_result = await service.send(
        scope, query_request(accepted, text="现在有粮能走这条线路吗？"),
    )
    accepted_context = _read_context(captured[-1])
    accepted_action_context = accepted_context["action_context"]
    assert accepted_action_context["own_safe_conduct_treaties"][0]["status"] == "active"
    assert _grain_balance(accepted_action_context) == "54"
    grain_options = [row for row in accepted_action_context["shipment_dispatch_options"]
                     if row["route_id"] == str(route_id) and row["resource_id"] == GRAIN]
    assert len(grain_options) == 1
    assert grain_options[0]["available_quantity"] == "54"
    assert "source_account_id" not in grain_options[0] and "destination_account_id" not in grain_options[0]
    assert accepted_result["world_effect"] is False
    after_accepted_query = repository.read_world(scope, world_id, accepted_branch)
    assert (after_accepted_query.last_seq, after_accepted_query.state_hash, after_accepted_query.state.current_tick) == (
        accepted.last_seq, accepted.state_hash, accepted.state.current_tick,
    )

    blocked_citations = (
        str(private_owner_id), str(author_only_knowledge_id), str(future_knowledge_id),
        str(unavailable_knowledge_id), str(uuid4()),
    )
    for blocked_citation in blocked_citations:
        before_unknown_citation = repository.read_world(scope, world_id, accepted_branch)
        rejected_marker = f"rejected-answer-{uuid4()}"
        queued_gm_outputs.extend([
            {"answer": rejected_marker, "references": [blocked_citation]},
            {"answer": "已按可用资料修复回答。", "references": []},
        ])
        request = query_request(accepted, text="能引用一个未提供给我的记录吗？")
        call_start = len(captured)
        repaired_result = await service.send(scope, request)
        assert repaired_result["content"] == "已按可用资料修复回答。"
        assert repaired_result["references"] == []
        first_request, repair_request = captured[call_start:]
        assert first_request.role_task is repair_request.role_task is RoleTask.GM_ANSWER
        first_payload = json.loads(first_request.messages[-1].content)
        repair_payload = json.loads(repair_request.messages[-1].content)
        assert repair_payload["intent"] == first_payload["intent"]
        assert repair_payload["context"] == first_payload["context"]
        assert repair_payload["discussion"] == first_payload["discussion"]
        assert repair_payload["question"] == first_payload["question"]
        assert "citation_repair" in repair_payload
        repair_prompt = "\n".join(message.content for message in repair_request.messages)
        assert blocked_citation not in repair_prompt
        assert rejected_marker not in repair_prompt
        after_unknown_citation = repository.read_world(scope, world_id, accepted_branch)
        assert (after_unknown_citation.last_seq, after_unknown_citation.state_hash,
                after_unknown_citation.state.current_tick) == (
            before_unknown_citation.last_seq, before_unknown_citation.state_hash,
            before_unknown_citation.state.current_tick,
        )

    before_second_rejection = repository.read_world(scope, world_id, accepted_branch)
    rejected_marker = f"rejected-answer-{uuid4()}"
    still_unknown = str(uuid4())
    call_start = len(captured)
    queued_gm_outputs.extend([
        {"answer": rejected_marker, "references": [still_unknown]},
        {"answer": "The second draft must also be rejected.", "references": [still_unknown]},
    ])
    with pytest.raises(InvalidEvent, match="authorized references"):
        await service.send(scope, query_request(accepted, text="请回答并引用资料。"))
    assert len(captured) == call_start + 2
    repair_payload = json.loads(captured[-1].messages[-1].content)
    assert "citation_repair" in repair_payload
    repair_prompt = "\n".join(message.content for message in captured[-1].messages)
    assert still_unknown not in repair_prompt
    assert rejected_marker not in repair_prompt
    after_second_rejection = repository.read_world(scope, world_id, accepted_branch)
    assert (after_second_rejection.last_seq, after_second_rejection.state_hash,
            after_second_rejection.state.current_tick) == (
        before_second_rejection.last_seq, before_second_rejection.state_hash,
        before_second_rejection.state.current_tick,
    )

    # Author advice stays on its separate projected path and does not receive
    # the action-options context used for player GM queries.
    next_references.clear()
    author_request = query_request(accepted, text="给我一个设定建议。", mode="author")
    await service.send(scope, author_request)
    after_author_advice = repository.read_world(scope, world_id, accepted_branch)
    assert (after_author_advice.last_seq, after_author_advice.state_hash, after_author_advice.state.current_tick) == (
        accepted.last_seq, accepted.state_hash, accepted.state.current_tick,
    )
    author_payload = json.loads(captured[-1].messages[-1].content)
    assert captured[-1].role_task is RoleTask.AUTHOR_ASSIST
    assert "action_context" not in author_payload["context"]
    assert str(private_account_id) not in json.dumps(author_payload, ensure_ascii=False)

    assert repository.read_world(scope, world_id, branch_id).last_seq == 5
    assert repository.read_world(scope, world_id, branch_id).state.safe_conduct_treaties[0].status == "countered"
    assert repository.read_world(scope, world_id, accepted_branch).state.safe_conduct_treaties[0].status == "active"
    assert all(request.role_task in {RoleTask.GM_ANSWER, RoleTask.AUTHOR_ASSIST} for request in captured)
    database.dispose()
