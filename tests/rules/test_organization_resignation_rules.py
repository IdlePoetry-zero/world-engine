from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord, RelationRecord, TreasuryComponent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine, parse_action


def _world(*, leader_is_actor: bool = False, actor_is_member: bool = True):
    actor_id, leader_id, organization_id, account_id, relation_id = (uuid4() for _ in range(5))
    members = [leader_id]
    if actor_is_member and actor_id not in members:
        members.append(actor_id)
    offices = [
        OfficeRecord(office_id="steward", title="Steward", holder_id=actor_id if actor_is_member else None),
        OfficeRecord(office_id="scribe", title="Scribe", holder_id=leader_id),
        OfficeRecord(office_id="vacant", title="Vacant"),
    ]
    governance = GovernanceComponent(
        revision=4,
        charter="Private charter text must not appear in player feedback.",
        members=members,
        leader_id=actor_id if leader_is_actor else leader_id,
        offices=offices,
        succession_policy="council",
    )
    organization = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Copper Assembly",
        components={
            "governance": governance.model_dump(mode="json"),
            "treasury": TreasuryComponent(accounts={"core.silver": account_id}).model_dump(mode="json"),
        },
    )
    actor = WorldEntity(id=actor_id, kind="character", name="Player")
    leader = WorldEntity(id=leader_id, kind="character", name="Chair")
    relation = RelationRecord(id=relation_id, from_id=actor_id, to_id=organization_id, label="former ally")
    state = WorldState(
        current_tick=321,
        entities={actor_id: actor, leader_id: leader, organization_id: organization},
        resources={account_id: ResourceAccount(resource_id="core.silver", balance="12.50", owner_id=organization_id)},
        relations=[relation],
        player_id=actor_id,
    )
    context = RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(),
        actor_id=actor_id, ruleset_version="1.7.0",
    )
    return state, actor_id, leader_id, organization_id, account_id, context


def test_resignation_clears_membership_and_held_offices_but_preserves_assets_and_history():
    state, actor_id, leader_id, organization_id, account_id, context = _world()
    action = {"kind": "leave_organization", "organization_id": str(organization_id)}

    assert parse_action(action).kind == "leave_organization"
    assert parse_action(action).organization_id == organization_id
    assert "actor_id" not in parse_action(action).model_dump()
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**action, "actor_id": str(actor_id)})

    result = RuleEngine().evaluate(state, action, context)
    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "organization_member_resigned"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert event.type == "core.organization_member_resigned.v1"
    assert event.visibility == {"policy": "public"}
    assert "Private charter text" not in result.outcome.message
    assert "Private charter text" not in str(result.outcome.details)

    after = apply_event(state, event)
    before_governance = state.entities[organization_id].components["governance"]
    after_governance = after.entities[organization_id].components["governance"]
    assert str(actor_id) not in after_governance["members"]
    assert after_governance["leader_id"] == str(leader_id)
    assert after_governance["revision"] == before_governance["revision"] + 1
    assert next(office for office in after_governance["offices"] if office["office_id"] == "steward")["holder_id"] is None
    assert next(office for office in after_governance["offices"] if office["office_id"] == "scribe")["holder_id"] == str(leader_id)
    assert next(office for office in after_governance["offices"] if office["office_id"] == "vacant")["holder_id"] is None
    assert after.resources[account_id] == state.resources[account_id]
    assert after.entities[organization_id].components["treasury"] == state.entities[organization_id].components["treasury"]
    assert after.relations == state.relations
    assert after.current_tick == state.current_tick


def test_leader_resignation_is_rejected_without_private_details_or_mutation():
    state, actor_id, _leader_id, organization_id, _account_id, context = _world(leader_is_actor=True)
    result = RuleEngine().evaluate(
        state,
        {"kind": "leave_organization", "organization_id": str(organization_id)},
        context,
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "leader_handover_required"
    assert result.events == []
    assert "handover" in result.outcome.message
    assert "Private charter text" not in result.outcome.message
    assert str(actor_id) in state.entities[organization_id].components["governance"]["members"]


def test_nonmember_and_unavailable_organization_share_a_non_enumerating_rejection():
    state, _actor_id, _leader_id, organization_id, _account_id, context = _world(actor_is_member=False)
    engine = RuleEngine()
    nonmember = engine.evaluate(
        state,
        {"kind": "leave_organization", "organization_id": str(organization_id)},
        context,
    )
    unavailable = engine.evaluate(
        state,
        {"kind": "leave_organization", "organization_id": str(uuid4())},
        context,
    )

    assert nonmember.outcome.code == unavailable.outcome.code == "membership_unavailable"
    assert nonmember.outcome.message == unavailable.outcome.message
    assert nonmember.events == unavailable.events == []
