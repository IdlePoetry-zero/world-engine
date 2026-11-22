from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import OrganizationMemberResignedEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event


def _state_and_event():
    actor_id, leader_id, organization_id = (uuid4() for _ in range(3))
    governance = GovernanceComponent(
        revision=7,
        members=[leader_id, actor_id],
        leader_id=leader_id,
        offices=[OfficeRecord(office_id="treasurer", title="Treasurer", holder_id=actor_id)],
    )
    state = WorldState(entities={
        actor_id: WorldEntity(id=actor_id, kind="character", name="Member"),
        leader_id: WorldEntity(id=leader_id, kind="character", name="Leader"),
        organization_id: WorldEntity(
            id=organization_id,
            kind="organization",
            components={"governance": governance.model_dump(mode="json")},
        ),
    })
    event = OrganizationMemberResignedEvent(
        organization_id=organization_id,
        actor_id=actor_id,
        expected_governance_revision=7,
        cleared_office_ids=["treasurer"],
        provenance=[{"kind": "rule", "reference_id": "organization.membership_resignation.v1"}],
    )
    return state, event


def test_resignation_event_is_registered_and_reducer_checks_governance_cas_and_exact_offices():
    state, event = _state_and_event()
    assert isinstance(parse_event(event.model_dump(mode="json")), OrganizationMemberResignedEvent)

    after = apply_event(state, event)
    governance = GovernanceComponent.model_validate(after.entities[event.organization_id].components["governance"])
    assert event.actor_id not in governance.members
    assert governance.offices[0].holder_id is None
    assert governance.offices[0].revision == 2
    assert governance.revision == 8

    with pytest.raises(InvalidEvent, match="expected_governance_revision"):
        apply_event(state, event.model_copy(update={"expected_governance_revision": 6}))
    with pytest.raises(InvalidEvent, match="exactly the actor's current subordinate offices"):
        apply_event(state, event.model_copy(update={"cleared_office_ids": []}))


def test_event_requires_stable_office_manifest_and_rule_provenance():
    state, event = _state_and_event()
    with pytest.raises(ValueError, match="stable order"):
        OrganizationMemberResignedEvent.model_validate({
            **event.model_dump(mode="python"),
            "cleared_office_ids": ["zeta", "alpha"],
        })
    with pytest.raises(ValueError, match="rule provenance"):
        OrganizationMemberResignedEvent.model_validate({
            **event.model_dump(mode="python"),
            "provenance": [{"kind": "event", "reference_id": str(uuid4())}],
        })
    with pytest.raises(ValueError, match="public governance event"):
        OrganizationMemberResignedEvent.model_validate({
            **event.model_dump(mode="python"),
            "visibility": {"policy": "author_only"},
        })

    nonmember_state = state.model_copy(update={
        "entities": {
            **state.entities,
            event.organization_id: state.entities[event.organization_id].model_copy(update={
                "components": {"governance": GovernanceComponent(
                    revision=7,
                    members=[], offices=[OfficeRecord(office_id="treasurer", title="Treasurer", holder_id=None)],
                ).model_dump(mode="json")}
            }),
        }
    })
    with pytest.raises(InvalidEvent, match="not an organization member"):
        apply_event(nonmember_state, event)
