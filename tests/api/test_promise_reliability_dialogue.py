import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator

from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.visibility import project_player_state
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.promise_trust import PROMISE_TRUST_POLICY_ID, promise_trust_relation_id
from world_engine.domain.promise_trust_state import PromiseTrustBond, PromiseTrustProjection
from world_engine.domain.relationship_invitations import (
    InvitationMeeting, InvitationMeetingAttendance, RelationshipInvitation,
)
from world_engine.domain.relationship_reflections import MeetingReflection
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext


def _projection(world_id: UUID, speaker_id: UUID, npcs: dict[str, UUID]) -> PromiseTrustProjection:
    directed_values = {
        (npcs["positive"], speaker_id): "37",
        (speaker_id, npcs["positive"]): "-42",
        (npcs["negative"], speaker_id): "-23",
        (speaker_id, npcs["negative"]): "61",
        (npcs["zero"], speaker_id): "0",
        (speaker_id, npcs["zero"]): "73",
    }
    bonds = {}
    for (from_id, to_id), trust in directed_values.items():
        bond_id = promise_trust_relation_id(world_id, from_id, to_id)
        bond = PromiseTrustBond(
            id=bond_id,
            world_id=world_id,
            from_id=from_id,
            to_id=to_id,
            revision=1,
            trust=trust,
            policy_id=PROMISE_TRUST_POLICY_ID,
            success_budget_spent="0",
        )
        bonds[bond_id] = bond
    return PromiseTrustProjection(world_id=world_id, bonds=bonds)


def _keys_and_string_leaves(value):
    keys = set()
    strings = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            child_keys, child_strings = _keys_and_string_leaves(item)
            keys.update(child_keys)
            strings.extend(child_strings)
    elif isinstance(value, list):
        for item in value:
            child_keys, child_strings = _keys_and_string_leaves(item)
            keys.update(child_keys)
            strings.extend(child_strings)
    elif isinstance(value, str):
        strings.append(value)
    return keys, strings


@pytest.mark.asyncio
async def test_dialogue_uses_only_target_to_speaker_reliability_and_keeps_it_private(tmp_path):
    identity = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    world_id, branch_id = uuid4(), uuid4()
    speaker_id, place_id = uuid4(), uuid4()
    npc_ids = {name: uuid4() for name in ("positive", "negative", "zero")}
    completed_meeting_id = uuid4()
    entities = {
        place_id: WorldEntity(
            id=place_id,
            kind="place",
            name="Meeting Hall",
            components={"place": {"place_type": "settlement"}},
        ),
        speaker_id: WorldEntity(
            id=speaker_id, kind="character", name="Current Speaker", location_id=place_id,
        ),
        npc_ids["positive"]: WorldEntity(
            id=npc_ids["positive"], kind="character", name="Trusting NPC", location_id=place_id,
        ),
        npc_ids["negative"]: WorldEntity(
            id=npc_ids["negative"], kind="character", name="Doubtful NPC", location_id=place_id,
        ),
        npc_ids["zero"]: WorldEntity(
            id=npc_ids["zero"], kind="character", name="Neutral NPC", location_id=place_id,
        ),
    }
    state = WorldState(
        player_id=speaker_id,
        entities=entities,
        promise_trust=_projection(world_id, speaker_id, npc_ids).to_payload(),
        relationship_invitations=[
            RelationshipInvitation(
                invitation_id=completed_meeting_id,
                proposer_id=speaker_id,
                invitee_id=npc_ids["positive"],
                status="ended",
                proposed_tick=-200_000_000,
                updated_tick=-150_000_000,
                revision=5,
                meeting=InvitationMeeting(start_tick=-190_000_000, end_tick=-180_000_000),
                meeting_attendance=[
                    InvitationMeetingAttendance(actor_id=participant, attended_tick=-185_000_000)
                    for participant in (speaker_id, npc_ids["positive"])
                ],
            ),
            # The older proposals have later updated_tick values. The context
            # must follow canonical proposal insertion order, not timestamp.
            RelationshipInvitation(
                invitation_id=uuid4(), proposer_id=npc_ids["positive"], invitee_id=speaker_id,
                status="declined", proposed_tick=-123_456_789, updated_tick=-111_111_111, revision=2,
            ),
            RelationshipInvitation(
                invitation_id=uuid4(), proposer_id=speaker_id, invitee_id=npc_ids["positive"],
                status="pending", proposed_tick=-123_456_780, updated_tick=-123_456_780, revision=1,
            ),
            RelationshipInvitation(
                invitation_id=uuid4(), proposer_id=speaker_id, invitee_id=npc_ids["negative"],
                status="declined", proposed_tick=-234_567_891, updated_tick=-222_222_222, revision=2,
            ),
            RelationshipInvitation(
                invitation_id=uuid4(), proposer_id=npc_ids["negative"], invitee_id=speaker_id,
                status="accepted", proposed_tick=-234_567_890, updated_tick=-234_567_889, revision=2,
                meeting=InvitationMeeting(start_tick=-234_567_800, end_tick=-234_567_700),
            ),
        ],
        relationship_reflections=[
            MeetingReflection(invitation_id=completed_meeting_id, actor_id=npc_ids["positive"],
                              outlook="closer", recorded_tick=-175_000_000),
            MeetingReflection(invitation_id=completed_meeting_id, actor_id=speaker_id,
                              outlook="uneasy", recorded_tick=-174_000_000),
        ],
    )
    view = SimpleNamespace(
        state=state,
        world=SimpleNamespace(world_id=world_id, ruleset_version="1.0.0", owner_id=identity.user_id),
        branch=SimpleNamespace(branch_id=branch_id, current_tick=0, state_version=0),
        last_seq=0,
    )

    selected_target_id = {"value": str(npc_ids["positive"])}
    captured = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            captured.append(model_request)
            task = model_request.role_task.value
            if task == "intent":
                output = {
                    "target_id": selected_target_id["value"],
                    "purpose": "dialogue",
                    "clarification": "",
                }
            elif task == "social_adjudication":
                output = {"utterance": "I hear you."}
            elif task == "gm_answer":
                output = {"answer": "I can answer from public facts only.", "references": []}
            else:
                raise AssertionError(f"Unexpected model role: {task}")
            Draft202012Validator(dict(model_request.output_schema)).validate(output)
            return SimpleNamespace(normalized_output=output)

    models = ModelFixture()
    operations = OperationsStore(tmp_path / "operations.db")

    class RepositoryFixture:
        def assert_branch_can_accept_write(self, _scope, _world_id, _branch_id):
            return None

        def commit_command(self, _scope, _world_id, _branch_id, command, _events):
            return SimpleNamespace(command_id=command.command_id, state_version=1)

        def list_shipment_relief_history(self, _scope, _world_id, _branch_id, _actor_id, *, limit, through_seq):
            assert limit > 0
            assert through_seq == view.last_seq
            return []

        def list_actor_rest_history(self, _scope, _world_id, _branch_id, actor_id, *, through_seq):
            assert actor_id == speaker_id and through_seq == view.last_seq
            return []

        def list_actor_training_history(self, _scope, _world_id, _branch_id, actor_id, *, through_seq):
            assert actor_id == speaker_id and through_seq == view.last_seq
            return []

        def list_actor_breakthrough_history(self, _scope, _world_id, _branch_id, actor_id, *, through_seq):
            assert actor_id == speaker_id and through_seq == view.last_seq
            return []

        def list_actor_study_totals(self, _scope, _world_id, _branch_id, actor_id, *, through_seq):
            assert actor_id == speaker_id and through_seq == view.last_seq
            return []

        def list_actor_movement_history(self, _scope, _world_id, _branch_id, actor_id, *, through_seq):
            assert actor_id == speaker_id and through_seq == view.last_seq
            return []

        def list_jobs(self, _scope, _world_id, _branch_id):
            return []

        def list_waiting_waypoint_jobs(self, _scope, _world_id, _branch_id, _actor_id):
            return []

    repository = RepositoryFixture()
    dispatcher = RulesActionDispatcher(repository, models, None, ACTION_INTENT_ADAPTER, RuleContext)
    service = ConversationApplication(
        SimpleNamespace(repository=repository),
        models,
        ConversationStore(operations),
        dispatcher,
    )
    session_id = uuid4()

    def request_for(mode: str, text: str, version: int) -> MessageRequest:
        return MessageRequest(
            request_id=uuid4(),
            world_id=world_id,
            branch_id=branch_id,
            session_id=session_id,
            actor_id=speaker_id,
            expected_state_version=str(version),
            mode=mode,
            text=text,
        )

    def prompt_context(model_request):
        return json.loads(model_request.messages[-1].content)

    async def speak_to(target_key: str):
        selected_target_id["value"] = str(npc_ids[target_key])
        first_call = len(captured)
        await dispatcher._dialogue(
            identity,
            view,
            request_for("roleplay", f"I speak to the {target_key} NPC.", 0),
            0,
        )
        assert [call.role_task.value for call in captured[first_call:]] == ["intent", "social_adjudication"]
        return prompt_context(captured[first_call + 1])

    positive_prompt = await speak_to("positive")
    assert positive_prompt["personal_reliability_impression"] == "守诺更可信"
    assert positive_prompt["latest_pair_invitation"] == {"status": "pending", "initiated_by": "player"}
    assert positive_prompt["personal_meeting_manner"] == "待对方更亲近一些"

    negative_prompt = await speak_to("negative")
    assert negative_prompt["personal_reliability_impression"] == "对守诺有疑虑"
    assert negative_prompt["latest_pair_invitation"] == {
        "status": "accepted", "initiated_by": "npc",
        "meeting": {
            "start_tick": -234_567_800, "end_tick": -234_567_700, "has_ended": True,
            "local_start": {"day_offset_from_now": -2715, "clock": "02:16:40"},
            "local_end": {"day_offset_from_now": -2715, "clock": "02:18:20"},
        },
    }
    assert "personal_meeting_manner" not in negative_prompt

    zero_prompt = await speak_to("zero")
    assert "personal_reliability_impression" not in zero_prompt
    assert "latest_pair_invitation" not in zero_prompt
    assert "personal_meeting_manner" not in zero_prompt

    invitation_ids = {str(item.invitation_id) for item in state.relationship_invitations}
    private_ticks = {"-123456789", "-111111111", "-123456780", "-234567891", "-222222222", "-234567890", "-234567889"}
    for social_prompt in (positive_prompt, negative_prompt, zero_prompt):
        if "latest_pair_invitation" in social_prompt:
            assert set(social_prompt["latest_pair_invitation"]) <= {"status", "initiated_by", "meeting"}
        prompt_text = json.dumps(social_prompt, ensure_ascii=False)
        assert all(invitation_id not in prompt_text for invitation_id in invitation_ids)
        assert not any(tick in prompt_text for tick in private_ticks)

    # The private projection is not part of a player or GM query view.
    player_view = project_player_state(view.state.model_dump(mode="json"), str(speaker_id))
    assert "promise_trust" not in player_view
    player_keys, player_strings = _keys_and_string_leaves(player_view)
    assert not player_keys.intersection({"trust", "receipts", "evidence_refs", "audit"})
    assert not {"37", "-42", "-23", "61", "73"}.intersection(player_strings)

    first_call = len(captured)
    await service._answer(
        identity,
        view,
        request_for("ask_gm", "What does the public record say about these people?", 0),
        "query",
    )
    assert [call.role_task.value for call in captured[first_call:]] == ["gm_answer"]
    gm_request = captured[first_call]
    query_context = prompt_context(gm_request)["context"]
    assert query_context["past_shipment_relief"] == []
    gm_context = query_context["state"]
    assert "promise_trust" not in gm_context
    assert "relationship_invitations" not in gm_context
    assert "latest_pair_invitation" not in gm_context
    assert "personal_meeting_manner" not in gm_context
    gm_keys, gm_strings = _keys_and_string_leaves(gm_context)
    assert not gm_keys.intersection({
        "trust", "personal_reliability_impression", "receipts", "evidence_refs", "audit",
    })
    assert not {"37", "-42", "-23", "61", "73"}.intersection(gm_strings)

    # The social requests receive a qualitative signal only; the numeric
    # directed bond scores and projection/audit structure stay private.
    for social_prompt in (positive_prompt, negative_prompt, zero_prompt):
        social_keys, social_strings = _keys_and_string_leaves(social_prompt)
        assert not social_keys.intersection({"trust", "receipts", "evidence_refs", "audit"})
        assert not {"37", "-42", "-23", "61", "73"}.intersection(social_strings)
