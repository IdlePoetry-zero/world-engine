import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.relationship_decisions import choose_invitation_response
from world_engine.domain.components import (
    GoalCondition,
    GoalRecord,
    KnowledgeRecord,
    LifeComponent,
    PersonalityComponent,
    RelationObligation,
    RelationRecord,
    VisibilityScope,
)
from world_engine.domain.models import AccessScope, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.relationship_invitations import InvitationMeeting, RelationshipInvitation
from world_engine.domain.relationship_promises import (
    ReturnItemTerms,
    build_promise_deadline_schedule,
    create_return_promise,
)


WORLD_ID = UUID("d1000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("d2000000-0000-4000-8000-000000000001")
PLACE_ID = UUID("d3000000-0000-4000-8000-000000000001")
PROPOSER_ID = UUID("d4000000-0000-4000-8000-000000000001")
INVITEE_ID = UUID("d4000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("d4000000-0000-4000-8000-000000000003")
OUTSIDER_ID = UUID("d4000000-0000-4000-8000-000000000004")
INVITATION_ID = UUID("d5000000-0000-4000-8000-000000000001")
APPOINTMENT_ID = UUID("d5000000-0000-4000-8000-000000000002")
YEAR_TICKS = 86_400 * 360
CURRENT_TICK = YEAR_TICKS * 20
RESOURCE_ID = "cultivation.herb"
INVITEE_ACCOUNT_ID = UUID("d6000000-0000-4000-8000-000000000001")
OTHER_ACCOUNT_ID = UUID("d6000000-0000-4000-8000-000000000002")


class CapturingModels:
    def __init__(self, choice="defer"):
        self.choice = choice
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        return SimpleNamespace(normalized_output={"choice": self.choice})


def _goal(
    goal_id: int,
    owner_id: UUID,
    *,
    title: str,
    priority: int = 50,
    deadline: int | None = CURRENT_TICK + 500,
    description: str = "",
    status: str = "active",
    created_tick: int = CURRENT_TICK - 30,
    conditions: list[GoalCondition] | None = None,
) -> GoalRecord:
    return GoalRecord(
        id=UUID(int=goal_id),
        owner_id=owner_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        deadline=deadline,
        created_tick=created_tick,
        updated_tick=CURRENT_TICK - 1,
        conditions=conditions or [],
    )


def _promise(*, promise_id: int, promisor_id: UUID, promisee_id: UUID, account_id: UUID, text: str, deadline: int):
    return create_return_promise(
        promise_id=UUID(int=promise_id),
        promisor_id=promisor_id,
        promisee_id=promisee_id,
        private_text=text,
        terms=ReturnItemTerms(
            resource_id=RESOURCE_ID,
            quantity="2.5",
            promisee_account_id=account_id,
        ),
        created_tick=CURRENT_TICK - 20,
        deadline_tick=deadline,
    )


def _state(*, goals=None, include_commitments=False, relations=None):
    entities = {
        PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Atrium"),
    }
    for actor_id, name, age in (
        (PROPOSER_ID, "Morgan", 30),
        (INVITEE_ID, "Rae", 28),
        (OTHER_ID, "Kai", 29),
        (OUTSIDER_ID, "Tess", 31),
    ):
        entities[actor_id] = WorldEntity(
            id=actor_id,
            kind="character",
            name=name,
            location_id=PLACE_ID,
            components={
                "life": LifeComponent(
                    birth_tick=CURRENT_TICK - age * YEAR_TICKS,
                    life_stage="adult",
                ).model_dump(mode="json"),
                "personality": PersonalityComponent(traits=["patient"]).model_dump(mode="json"),
            },
        )

    pending = RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        status="pending",
        proposed_tick=CURRENT_TICK - 10,
        updated_tick=CURRENT_TICK - 10,
        revision=1,
        meeting=InvitationMeeting(start_tick=CURRENT_TICK + 100, end_tick=CURRENT_TICK + 120),
    )
    accepted = RelationshipInvitation(
        invitation_id=APPOINTMENT_ID,
        proposer_id=INVITEE_ID,
        invitee_id=OTHER_ID,
        status="accepted",
        proposed_tick=CURRENT_TICK - 30,
        updated_tick=CURRENT_TICK - 20,
        revision=2,
        meeting=InvitationMeeting(start_tick=CURRENT_TICK + 110, end_tick=CURRENT_TICK + 140),
    )

    promises = []
    resources = {}
    schedules = []
    if include_commitments:
        own_promise = _promise(
            promise_id=501,
            promisor_id=INVITEE_ID,
            promisee_id=OTHER_ID,
            account_id=OTHER_ACCOUNT_ID,
            text="I promised Kai I would return the herb stock.",
            deadline=CURRENT_TICK + 50,
        )
        incoming_promise = _promise(
            promise_id=502,
            promisor_id=OTHER_ID,
            promisee_id=INVITEE_ID,
            account_id=INVITEE_ACCOUNT_ID,
            text="OUTSIDER_OWES_RAE_PRIVATE_MARKER",
            deadline=CURRENT_TICK + 40,
        )
        promises = [own_promise, incoming_promise]
        schedules = [build_promise_deadline_schedule(item) for item in promises]
        resources = {
            OTHER_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID, balance="0", owner_id=OTHER_ID,
            ),
            INVITEE_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID, balance="0", owner_id=INVITEE_ID,
            ),
        }

    state = WorldState(
        current_tick=CURRENT_TICK,
        player_id=PROPOSER_ID,
        calendar={"day_seconds": 86_400, "year_days": 360},
        entities=entities,
        resources=resources,
        relations=list(relations or []),
        goals=list(goals or []),
        knowledge=[
            KnowledgeRecord(
                id=UUID(int=601),
                owner_id=INVITEE_ID,
                subject_id=OTHER_ID,
                claim="FUTURE_KNOWLEDGE_MARKER",
                received_tick=CURRENT_TICK - 2,
                valid_from_tick=CURRENT_TICK + 1,
            ),
            KnowledgeRecord(
                id=UUID(int=602),
                holder_id=OTHER_ID,
                subject_id=INVITEE_ID,
                claim="OTHER_PRIVATE_KNOWLEDGE_MARKER",
                received_tick=CURRENT_TICK - 2,
            ),
        ],
        scheduled_events=schedules,
        relationship_promises=promises,
        relationship_invitations=[pending, accepted],
    )
    return state, pending


def _request():
    return MessageRequest(
        request_id=uuid4(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        session_id=uuid4(),
        actor_id=PROPOSER_ID,
        expected_state_version="2",
        text="PLAYER_TEXT_MUST_NOT_ENTER_NPC_DECISION",
        profile=QualityProfile(speed="patient", depth="exhaustive"),
    )


async def _captured_context(state, invitation):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = CapturingModels()
    choice = await choose_invitation_response(models, scope, _request(), state, invitation)
    assert choice == "defer"
    assert len(models.calls) == 1
    observed_scope, model_request = models.calls[0]
    assert observed_scope == scope
    assert model_request.role_task is RoleTask.NPC_DECISION
    return json.loads(model_request.messages[1].content), model_request


@pytest.mark.asyncio
async def test_model_request_contains_only_invitees_active_goals_and_excludes_private_or_future_knowledge():
    goals = [
        _goal(701, INVITEE_ID, title="Rae's work goal", priority=87, deadline=CURRENT_TICK + 12,
              description="Finish the late dispatch roster.", conditions=[GoalCondition(
                  kind="relationship_dimension_minimum", subject_id=INVITEE_ID, target_id=OTHER_ID,
                  dimension="trust", threshold=5,
              )]),
        _goal(702, OTHER_ID, title="OTHER_PRIVATE_GOAL_MARKER", description="Do not expose this goal."),
        _goal(703, INVITEE_ID, title="PAUSED_GOAL_MARKER", status="paused"),
    ]
    relations = [
        RelationRecord(
            id=UUID(int=801),
            from_id=INVITEE_ID,
            to_id=OTHER_ID,
            obligations=[RelationObligation(
                obligation_id=UUID(int=811),
                kind="keep_word",
                description="Rae agreed to help Kai finish the roster.",
                beneficiary_id=OTHER_ID,
                due_tick=CURRENT_TICK + 8,
            )],
            updated_tick=CURRENT_TICK - 1,
            visibility=VisibilityScope(policy="public"),
        ),
        RelationRecord(
            id=UUID(int=802),
            from_id=INVITEE_ID,
            to_id=PROPOSER_ID,
            obligations=[RelationObligation(
                obligation_id=UUID(int=812),
                kind="private_plan",
                description="PRIVATE_UNSHARED_OBLIGATION_MARKER",
            )],
            updated_tick=CURRENT_TICK - 1,
        ),
        RelationRecord(
            id=UUID(int=803),
            from_id=OTHER_ID,
            to_id=OUTSIDER_ID,
            obligations=[RelationObligation(
                obligation_id=UUID(int=813),
                kind="private_plan",
                description="OTHER_ACTOR_OBLIGATION_MARKER",
            )],
            updated_tick=CURRENT_TICK - 1,
            visibility=VisibilityScope(policy="public"),
        ),
    ]
    state, invitation = _state(goals=goals, relations=relations)
    before = state.model_dump(mode="json")

    context, model_request = await _captured_context(state, invitation)

    assert context["invitee_active_goals"] == [{
        "title": "Rae's work goal",
        "description": "Finish the late dispatch roster.",
        "status": "active",
        "priority": 87,
        "deadline_tick": CURRENT_TICK + 12,
        "progress": 0,
        "conditions": [{
            "kind": "relationship_dimension_minimum",
            "target_id": str(OTHER_ID),
            "dimension": "trust",
            "threshold": "5",
        }],
        "conditions_truncated": False,
    }]
    assert context["invitee_known_commitments"]["known_relation_obligations"] == [{
        "to": "Kai",
        "kind": "keep_word",
        "description": "Rae agreed to help Kai finish the roster.",
        "beneficiary": "Kai",
        "due_tick": CURRENT_TICK + 8,
        "status": "active",
    }]
    prompt = "\n".join(message.content for message in model_request.messages)
    for forbidden in (
        "OTHER_PRIVATE_GOAL_MARKER",
        "PAUSED_GOAL_MARKER",
        "FUTURE_KNOWLEDGE_MARKER",
        "OTHER_PRIVATE_KNOWLEDGE_MARKER",
        "PRIVATE_UNSHARED_OBLIGATION_MARKER",
        "OTHER_ACTOR_OBLIGATION_MARKER",
        "PLAYER_TEXT_MUST_NOT_ENTER_NPC_DECISION",
    ):
        assert forbidden not in prompt
    assert state.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_goals_are_stably_sorted_bounded_and_context_includes_conflicting_own_commitments():
    many_conditions = [
        GoalCondition(
            kind="relationship_dimension_minimum", subject_id=INVITEE_ID, target_id=OTHER_ID,
            dimension="trust", threshold=index,
        )
        for index in range(9)
    ]
    goals = [
        _goal(712, INVITEE_ID, title="Later due", priority=70, deadline=CURRENT_TICK + 500),
        _goal(711, INVITEE_ID, title="Same due higher id", priority=70, deadline=CURRENT_TICK + 100),
        _goal(710, INVITEE_ID, title="Same due lower id", priority=70, deadline=CURRENT_TICK + 100,
              description="長" * 1_000, conditions=many_conditions),
        _goal(709, INVITEE_ID, title="Lower priority", priority=60, deadline=CURRENT_TICK + 1),
        _goal(708, INVITEE_ID, title="Priority 5", priority=5),
        _goal(707, INVITEE_ID, title="Priority 4", priority=4),
        _goal(706, INVITEE_ID, title="Priority 3", priority=3),
        _goal(705, INVITEE_ID, title="Priority 2", priority=2),
        _goal(704, INVITEE_ID, title="Priority 1", priority=1),
        _goal(713, INVITEE_ID, title="Priority 0", priority=0),
        _goal(714, INVITEE_ID, title="Highest priority", priority=90, deadline=CURRENT_TICK + 900),
    ]
    state, invitation = _state(goals=goals, include_commitments=True)
    before = state.model_dump(mode="json")

    context, _ = await _captured_context(state, invitation)
    reversed_state = state.model_copy(update={"goals": list(reversed(state.goals))})
    repeated_context, _ = await _captured_context(reversed_state, invitation)

    projected_goals = context["invitee_active_goals"]
    assert [goal["title"] for goal in projected_goals] == [
        "Highest priority",
        "Same due lower id",
        "Same due higher id",
        "Later due",
        "Lower priority",
        "Priority 5",
        "Priority 4",
        "Priority 3",
    ]
    assert repeated_context["invitee_active_goals"] == projected_goals
    assert len(projected_goals) == 8
    long_goal = next(goal for goal in projected_goals if goal["title"] == "Same due lower id")
    assert len(long_goal["description"]) == 800
    assert long_goal["description"].endswith("…")
    assert len(long_goal["conditions"]) == 8
    assert long_goal["conditions_truncated"] is True

    commitments = context["invitee_known_commitments"]
    assert commitments["promises_made"] == [{
        "promise_to": "Kai",
        "description": "I promised Kai I would return the herb stock.",
        "status": "active",
        "created_tick": CURRENT_TICK - 20,
        "deadline_tick": CURRENT_TICK + 50,
        "terms": {
            "kind": "return_item",
            "resource_id": RESOURCE_ID,
            "quantity": "2.5",
        },
    }]
    assert commitments["known_relation_obligations"] == []
    assert commitments["accepted_appointments"] == [{
        "with": "Kai",
        "meeting": {"start_tick": CURRENT_TICK + 110, "end_tick": CURRENT_TICK + 140},
        "status": "accepted",
    }]
    assert commitments["accepted_appointments"][0]["meeting"]["start_tick"] < invitation.meeting.end_tick
    assert commitments["accepted_appointments"][0]["meeting"]["end_tick"] > invitation.meeting.start_tick
    serialized_context = json.dumps(context, ensure_ascii=False)
    assert "OUTSIDER_OWES_RAE_PRIVATE_MARKER" not in serialized_context
    assert state.model_dump(mode="json") == before
