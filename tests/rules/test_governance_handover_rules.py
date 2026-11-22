from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.gameplay_events import GovernanceLeadershipHandedOverEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine, parse_action


def _appointment_world(*, policy: str = "appointment"):
    incumbent_id, successor_id, other_id, organization_id = (uuid4() for _ in range(4))
    offices = [
        OfficeRecord(
            office_id="office.leader", title="Steward", holder_id=incumbent_id,
            authority=["authority.coordinate", "authority.represent"],
        ),
        OfficeRecord(
            office_id="office.council_lead", title="Council Lead", holder_id=incumbent_id,
            authority=["organization.lead"],
        ),
        OfficeRecord(
            office_id="office.representative", title="Representative", holder_id=incumbent_id,
            authority=["authority.represent"],
        ),
        OfficeRecord(office_id="office.secretary", title="Secretary", holder_id=successor_id),
    ]
    governance = GovernanceComponent(
        revision=5,
        members=[incumbent_id, successor_id, other_id],
        leader_id=incumbent_id,
        offices=offices,
        succession_policy=policy,
    )
    state = WorldState(entities={
        incumbent_id: WorldEntity(id=incumbent_id, kind="character", name="Incumbent"),
        successor_id: WorldEntity(id=successor_id, kind="character", name="Successor"),
        other_id: WorldEntity(id=other_id, kind="character", name="Member"),
        organization_id: WorldEntity(
            id=organization_id,
            kind="organization",
            name="Appointment Guild",
            components={
                "governance": governance.model_dump(mode="json"),
                "treasury": TreasuryComponent(accounts={}).model_dump(mode="json"),
            },
        ),
    })
    context = RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(),
        actor_id=incumbent_id, ruleset_version="1.7.0",
    )
    return state, incumbent_id, successor_id, other_id, organization_id, context


def test_appointment_incumbent_hands_over_only_leadership_offices_then_can_resign():
    state, incumbent_id, successor_id, _other_id, organization_id, context = _appointment_world()
    action = {
        "kind": "handover_leadership",
        "organization_id": str(organization_id),
        "successor_id": str(successor_id),
    }
    parsed = parse_action(action)
    assert parsed.kind == "handover_leadership"
    assert parsed.successor_id == successor_id
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**action, "actor_id": str(incumbent_id)})

    result = RuleEngine().evaluate(state, action, context)
    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "leadership_handed_over"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, GovernanceLeadershipHandedOverEvent)
    assert event.visibility == {"policy": "public"}
    assert event.transferred_office_ids == ["office.council_lead", "office.leader"]

    after_handover = apply_event(state, event)
    governance = after_handover.entities[organization_id].components["governance"]
    assert governance["leader_id"] == str(successor_id)
    assert str(incumbent_id) in governance["members"]
    assert str(successor_id) in governance["members"]
    assert next(o for o in governance["offices"] if o["office_id"] == "office.leader")["holder_id"] == str(successor_id)
    assert next(o for o in governance["offices"] if o["office_id"] == "office.council_lead")["holder_id"] == str(successor_id)
    assert next(o for o in governance["offices"] if o["office_id"] == "office.representative")["holder_id"] == str(incumbent_id)
    assert next(o for o in governance["offices"] if o["office_id"] == "office.secretary")["holder_id"] == str(successor_id)
    assert len({o["office_id"] for o in governance["offices"]}) == len(governance["offices"])
    assert after_handover.current_tick == state.current_tick

    resignation = RuleEngine().evaluate(
        after_handover,
        {"kind": "leave_organization", "organization_id": str(organization_id)},
        context,
    )
    assert resignation.outcome.status == "succeeded"
    after_resignation = apply_event(after_handover, resignation.events[0])
    final_governance = after_resignation.entities[organization_id].components["governance"]
    assert str(incumbent_id) not in final_governance["members"]
    assert final_governance["leader_id"] == str(successor_id)


def test_only_current_incumbent_and_appointment_policy_can_handover():
    state, incumbent_id, successor_id, other_id, organization_id, context = _appointment_world()
    action = {"kind": "handover_leadership", "organization_id": str(organization_id), "successor_id": str(successor_id)}
    non_incumbent = RuleEngine().evaluate(state, action, context.model_copy(update={"actor_id": other_id}))
    assert non_incumbent.outcome.code == "leadership_handover_incumbent_required"
    assert non_incumbent.events == []

    council_state, _, _, _, council_org, council_context = _appointment_world(policy="council")
    council = RuleEngine().evaluate(
        council_state,
        {"kind": "handover_leadership", "organization_id": str(council_org), "successor_id": str(successor_id)},
        council_context,
    )
    assert council.outcome.code == "leadership_handover_requires_appointment"
    assert council.events == []


def test_handover_target_must_be_a_distinct_active_member():
    state, incumbent_id, _successor_id, other_id, organization_id, context = _appointment_world()
    engine = RuleEngine()
    for target_id in (incumbent_id, uuid4(), other_id):
        result = engine.evaluate(
            state,
            {"kind": "handover_leadership", "organization_id": str(organization_id), "successor_id": str(target_id)},
            context,
        )
        if target_id == other_id:
            assert result.outcome.status == "succeeded"
        else:
            assert result.outcome.code == "leadership_handover_target_unavailable"
            assert result.events == []
