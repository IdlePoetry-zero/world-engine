from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import GovernanceLeadershipHandedOverEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event


def _state_and_event():
    predecessor_id, successor_id, member_id, organization_id = (uuid4() for _ in range(4))
    governance = GovernanceComponent(
        revision=8,
        members=[predecessor_id, successor_id, member_id],
        leader_id=predecessor_id,
        succession_policy="appointment",
        offices=[
            OfficeRecord(office_id="office.leader", title="Leader", holder_id=predecessor_id),
            OfficeRecord(office_id="office.other", title="Other", holder_id=predecessor_id, authority=["authority.coordinate"]),
            OfficeRecord(office_id="office.staff", title="Staff", holder_id=successor_id),
        ],
    )
    state = WorldState(entities={
        predecessor_id: WorldEntity(id=predecessor_id, kind="character", name="Predecessor"),
        successor_id: WorldEntity(id=successor_id, kind="character", name="Successor"),
        member_id: WorldEntity(id=member_id, kind="character", name="Member"),
        organization_id: WorldEntity(id=organization_id, kind="organization", components={
            "governance": governance.model_dump(mode="json"),
        }),
    })
    event = GovernanceLeadershipHandedOverEvent(
        organization_id=organization_id,
        predecessor_id=predecessor_id,
        successor_id=successor_id,
        expected_governance_revision=8,
        transferred_office_ids=["office.leader"],
        provenance=[{"kind": "rule", "reference_id": "organization.leadership_handover.v1"}],
    )
    return state, event


def test_handover_event_is_registered_and_reducer_preserves_nonleadership_offices():
    state, event = _state_and_event()
    assert isinstance(parse_event(event.model_dump(mode="json")), GovernanceLeadershipHandedOverEvent)
    after = apply_event(state, event)
    governance = GovernanceComponent.model_validate(after.entities[event.organization_id].components["governance"])
    assert governance.leader_id == event.successor_id
    assert event.predecessor_id in governance.members
    assert event.successor_id in governance.members
    offices = {office.office_id: office for office in governance.offices}
    assert offices["office.leader"].holder_id == event.successor_id
    assert offices["office.leader"].revision == 2
    assert offices["office.other"].holder_id == event.predecessor_id
    assert offices["office.other"].revision == 1
    assert offices["office.staff"].holder_id == event.successor_id
    assert offices["office.staff"].revision == 1
    assert governance.revision == 9


def test_reducer_rejects_stale_policy_wrong_predecessor_and_incomplete_office_manifest():
    state, event = _state_and_event()
    with pytest.raises(InvalidEvent, match="expected_governance_revision"):
        apply_event(state, event.model_copy(update={"expected_governance_revision": 7}))
    with pytest.raises(InvalidEvent, match="exactly the incumbent's registered leadership offices"):
        apply_event(state, event.model_copy(update={"transferred_office_ids": ["office.other"]}))

    state_governance = GovernanceComponent.model_validate(state.entities[event.organization_id].components["governance"])
    council = state_governance.model_copy(update={"succession_policy": "council"})
    council_state = state.model_copy(update={
        "entities": {
            **state.entities,
            event.organization_id: state.entities[event.organization_id].model_copy(update={
                "components": {"governance": council.model_dump(mode="json")}
            }),
        }
    })
    with pytest.raises(InvalidEvent, match="appointment policy"):
        apply_event(council_state, event)

    wrong_predecessor = event.model_copy(update={"predecessor_id": uuid4()})
    with pytest.raises(InvalidEvent, match="not the current organization leader"):
        apply_event(state, wrong_predecessor)


def test_handover_event_requires_public_visibility_and_rule_provenance():
    state, event = _state_and_event()
    with pytest.raises(ValueError, match="distinct predecessor and successor"):
        GovernanceLeadershipHandedOverEvent.model_validate({
            **event.model_dump(mode="python"),
            "successor_id": event.predecessor_id,
        })
    with pytest.raises(ValueError, match="stable order"):
        GovernanceLeadershipHandedOverEvent.model_validate({
            **event.model_dump(mode="python"),
            "transferred_office_ids": ["zeta", "alpha"],
        })
    with pytest.raises(ValueError, match="rule provenance"):
        GovernanceLeadershipHandedOverEvent.model_validate({
            **event.model_dump(mode="python"),
            "provenance": [{"kind": "event", "reference_id": str(uuid4())}],
        })
    with pytest.raises(ValueError, match="public governance event"):
        GovernanceLeadershipHandedOverEvent.model_validate({
            **event.model_dump(mode="python"),
            "visibility": {"policy": "author_only"},
        })
