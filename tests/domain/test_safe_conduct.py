from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord, PlaceComponent, PlaceRoute
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.safe_conduct import (
    SAFE_CONDUCT_TAG,
    SafeConductError,
    SafeConductTreaty,
    propose_safe_conduct,
    registered_safe_conduct_route,
    safe_conduct_allows_route,
    transition_safe_conduct,
)


def _world(*, recipient_representation=True, shared_leader=False):
    source_region_id, recipient_region_id, origin_id, destination_id = (uuid4() for _ in range(4))
    source_leader_id, source_member_id, recipient_leader_id, recipient_member_id = (uuid4() for _ in range(4))
    if shared_leader:
        recipient_leader_id = source_leader_id
    source_org_id, recipient_org_id = uuid4(), uuid4()
    route = PlaceRoute(
        route_id=uuid4(),
        from_place_id=origin_id,
        to_place_id=destination_id,
        travel_ticks=300,
        access_tags=[SAFE_CONDUCT_TAG, "route.passport"],
        travel_modes=["travel.walk"],
        is_open=True,
    )
    entities = {
        source_region_id: WorldEntity(
            id=source_region_id, kind="place", name="Northern Region",
            components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
        ),
        recipient_region_id: WorldEntity(
            id=recipient_region_id, kind="place", name="Southern Region",
            components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
        ),
        origin_id: WorldEntity(
            id=origin_id, kind="place", name="North Gate",
            components={"place": PlaceComponent(
                place_type="settlement", region_id=source_region_id, routes=[route],
            ).model_dump(mode="json")},
        ),
        destination_id: WorldEntity(
            id=destination_id, kind="place", name="South Gate",
            components={"place": PlaceComponent(
                place_type="settlement", region_id=recipient_region_id,
            ).model_dump(mode="json")},
        ),
        source_leader_id: WorldEntity(
            id=source_leader_id, kind="character", name="North Steward", status="active",
        ),
        source_member_id: WorldEntity(
            id=source_member_id, kind="character", name="North Envoy", status="active",
        ),
        recipient_leader_id: WorldEntity(
            id=recipient_leader_id, kind="character", name="South Steward", status="active",
        ),
        recipient_member_id: WorldEntity(
            id=recipient_member_id, kind="character", name="South Envoy", status="active",
        ),
    }
    source_gov = GovernanceComponent(
        revision=4,
        members=[source_leader_id, source_member_id],
        leader_id=source_leader_id,
        offices=[OfficeRecord(
            office_id="office.leader", title="North Steward", holder_id=source_leader_id,
            authority=["authority.represent"],
        )],
        decision_threshold=Decimal("0.5"),
    )
    recipient_offices = [OfficeRecord(
        office_id="office.leader", title="South Steward", holder_id=recipient_leader_id,
        authority=["authority.represent"] if recipient_representation else [],
    )]
    recipient_gov = GovernanceComponent(
        revision=8,
        members=[recipient_leader_id, recipient_member_id],
        leader_id=recipient_leader_id,
        offices=recipient_offices,
        decision_threshold=Decimal("0.5"),
    )
    entities[source_org_id] = WorldEntity(
        id=source_org_id, kind="organization", name="North League", status="active",
        location_id=source_region_id,
        components={"governance": source_gov.model_dump(mode="json")},
    )
    entities[recipient_org_id] = WorldEntity(
        id=recipient_org_id, kind="organization", name="South League", status="active",
        location_id=recipient_region_id,
        components={"governance": recipient_gov.model_dump(mode="json")},
    )
    return WorldState(current_tick=1_000, entities=entities), {
        "source_region_id": source_region_id,
        "recipient_region_id": recipient_region_id,
        "origin_id": origin_id,
        "destination_id": destination_id,
        "source_leader_id": source_leader_id,
        "source_member_id": source_member_id,
        "recipient_leader_id": recipient_leader_id,
        "recipient_member_id": recipient_member_id,
        "source_org_id": source_org_id,
        "recipient_org_id": recipient_org_id,
        "route": route,
    }


def _propose(state, ids, *, treaty_id=None, expires_at_tick=2_000, existing_treaties=()):
    return propose_safe_conduct(
        state,
        treaty_id=treaty_id or uuid4(),
        proposer_organization_id=ids["source_org_id"],
        recipient_organization_id=ids["recipient_org_id"],
        proposer_actor_id=ids["source_leader_id"],
        route=ids["route"],
        expires_at_tick=expires_at_tick,
        existing_treaties=existing_treaties,
    )


def _with_tick(state, tick):
    return WorldState.model_validate({**state.model_dump(mode="python"), "current_tick": tick})


def _replace_governance(state, organization_id, replacement):
    entities = dict(state.entities)
    organization = entities[organization_id]
    entities[organization_id] = organization.model_copy(update={
        "components": {**organization.components, "governance": replacement.model_dump(mode="json")},
    })
    return WorldState.model_validate({**state.model_dump(mode="python"), "entities": entities})


def test_safe_conduct_is_bilateral_route_scoped_and_survives_leader_change():
    state, ids = _world()
    treaty = _propose(state, ids)
    assert treaty.status == "pending"
    assert treaty.revision == 1
    assert treaty.opened_tick == treaty.updated_tick == state.current_tick
    assert treaty.recipient_actor_id == ids["recipient_leader_id"]
    assert treaty.proposer_governance_revision == 4
    assert treaty.recipient_governance_revision == 8
    assert treaty.route_id == ids["route"].route_id
    assert treaty.from_place_id == ids["origin_id"]
    assert treaty.to_place_id == ids["destination_id"]

    active = transition_safe_conduct(
        state, treaty, actor_id=ids["recipient_leader_id"], operation="ratify", expected_revision=1,
    )
    assert active.status == "active"
    assert active.revision == 2
    assert active.updated_tick == state.current_tick
    assert safe_conduct_allows_route(
        state, state.entities[ids["source_member_id"]], ids["route"], [active],
    )
    assert not safe_conduct_allows_route(
        state, state.entities[ids["recipient_member_id"]], ids["route"], [active],
    )
    assert not safe_conduct_allows_route(
        _with_tick(state, active.expires_at_tick),
        state.entities[ids["source_member_id"]],
        ids["route"],
        [active],
    )
    outsider = WorldEntity(id=uuid4(), kind="character", name="Outsider", status="active")
    assert not safe_conduct_allows_route(state, outsider, ids["route"], [active])
    assert ids["route"].access_tags == [SAFE_CONDUCT_TAG, "route.passport"]

    # A duly represented new incumbent may revoke; the ratified treaty itself
    # remains useful to the source's active members through routine succession.
    old_source_governance = GovernanceComponent.model_validate(
        state.entities[ids["source_org_id"]].components["governance"],
    )
    new_leader_id = uuid4()
    new_leader = WorldEntity(id=new_leader_id, kind="character", name="New Steward", status="active")
    entities = dict(state.entities)
    entities[new_leader_id] = new_leader
    next_governance = old_source_governance.model_copy(update={
        "revision": old_source_governance.revision + 1,
        "members": [*old_source_governance.members, new_leader_id],
        "leader_id": new_leader_id,
        "offices": [OfficeRecord(
            office_id="office.leader", title="New Steward", holder_id=new_leader_id,
            authority=["authority.represent"],
        )],
    })
    entities[ids["source_org_id"]] = entities[ids["source_org_id"]].model_copy(update={
        "components": {"governance": next_governance.model_dump(mode="json")},
    })
    succeeded_by_succession = WorldState.model_validate({
        **state.model_dump(mode="python"), "entities": entities,
    })
    assert safe_conduct_allows_route(
        succeeded_by_succession,
        succeeded_by_succession.entities[ids["source_member_id"]],
        ids["route"],
        [active],
    )
    revoked = transition_safe_conduct(
        succeeded_by_succession,
        active,
        actor_id=new_leader_id,
        operation="revoke",
        expected_revision=active.revision,
    )
    assert revoked.status == "revoked"
    assert revoked.revision == active.revision + 1
    assert not safe_conduct_allows_route(
        succeeded_by_succession,
        succeeded_by_succession.entities[ids["source_member_id"]],
        ids["route"],
        [revoked],
    )


def test_safe_conduct_route_lookup_fails_closed_for_hidden_closed_or_ambiguous_routes():
    state, ids = _world()
    assert registered_safe_conduct_route(state, ids["origin_id"], ids["route"].route_id) == ids["route"]

    hidden_origin = state.entities[ids["origin_id"]].model_copy(update={
        "components": {
            **state.entities[ids["origin_id"]].components,
            "hidden": True,
        },
    })
    hidden_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ids["origin_id"]: hidden_origin},
    })
    assert registered_safe_conduct_route(hidden_state, ids["origin_id"], ids["route"].route_id) is None
    with pytest.raises(SafeConductError, match="registered safe-conduct route"):
        _propose(hidden_state, ids)

    closed_route = ids["route"].model_copy(update={"is_open": False})
    closed_origin = state.entities[ids["origin_id"]].model_copy(update={
        "components": {"place": PlaceComponent(
            place_type="settlement", region_id=ids["source_region_id"], routes=[closed_route],
        ).model_dump(mode="json")},
    })
    closed_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ids["origin_id"]: closed_origin},
    })
    assert registered_safe_conduct_route(closed_state, ids["origin_id"], ids["route"].route_id) is None

    hidden_destination = state.entities[ids["destination_id"]].model_copy(update={
        "components": {
            **state.entities[ids["destination_id"]].components,
            "hidden": True,
        },
    })
    hidden_destination_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ids["destination_id"]: hidden_destination},
    })
    assert registered_safe_conduct_route(
        hidden_destination_state, ids["origin_id"], ids["route"].route_id,
    ) is None

    untagged_route = ids["route"].model_copy(update={"access_tags": ["route.passport"]})
    untagged_origin = state.entities[ids["origin_id"]].model_copy(update={
        "components": {"place": PlaceComponent(
            place_type="settlement", region_id=ids["source_region_id"], routes=[untagged_route],
        ).model_dump(mode="json")},
    })
    untagged_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ids["origin_id"]: untagged_origin},
    })
    assert registered_safe_conduct_route(untagged_state, ids["origin_id"], ids["route"].route_id) is None

    second_origin_id = uuid4()
    second_route = ids["route"].model_copy(update={"from_place_id": second_origin_id})
    duplicate_origin = WorldEntity(
        id=second_origin_id,
        kind="place",
        name="Duplicate North Gate",
        components={"place": PlaceComponent(
            place_type="settlement", region_id=ids["source_region_id"], routes=[second_route],
        ).model_dump(mode="json")},
    )
    duplicate_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, second_origin_id: duplicate_origin},
    })
    assert registered_safe_conduct_route(duplicate_state, ids["origin_id"], ids["route"].route_id) is None


def test_safe_conduct_proposal_requires_authorized_distinct_party_representatives_and_region_pair():
    state, ids = _world()
    with pytest.raises(SafeConductError, match="source organization's represented incumbent"):
        propose_safe_conduct(
            state,
            treaty_id=uuid4(),
            proposer_organization_id=ids["source_org_id"],
            recipient_organization_id=ids["recipient_org_id"],
            proposer_actor_id=ids["source_member_id"],
            route=ids["route"],
            expires_at_tick=2_000,
        )

    no_recipient_rep, no_rep_ids = _world(recipient_representation=False)
    with pytest.raises(SafeConductError, match="authority.represent"):
        _propose(no_recipient_rep, no_rep_ids)

    shared_leader_state, shared_ids = _world(shared_leader=True)
    with pytest.raises(SafeConductError, match="distinct source and recipient representatives"):
        _propose(shared_leader_state, shared_ids)

    wrong_location = state.entities[ids["recipient_org_id"]].model_copy(update={
        "location_id": ids["source_region_id"],
    })
    wrong_location_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ids["recipient_org_id"]: wrong_location},
    })
    with pytest.raises(SafeConductError, match="endpoint regions"):
        _propose(wrong_location_state, ids)

    same_region_entities = dict(state.entities)
    destination = same_region_entities[ids["destination_id"]]
    destination_place = PlaceComponent.model_validate(destination.components["place"])
    same_region_entities[ids["destination_id"]] = destination.model_copy(update={
        "components": {"place": destination_place.model_copy(update={
            "region_id": ids["source_region_id"],
        }).model_dump(mode="json")},
    })
    recipient_org = same_region_entities[ids["recipient_org_id"]]
    same_region_entities[ids["recipient_org_id"]] = recipient_org.model_copy(update={
        "location_id": ids["source_region_id"],
    })
    same_region_state = WorldState.model_validate({
        **state.model_dump(mode="python"), "entities": same_region_entities,
    })
    with pytest.raises(SafeConductError, match="distinct source and destination regions"):
        _propose(same_region_state, ids)

    source_governance = GovernanceComponent.model_validate(
        state.entities[ids["source_org_id"]].components["governance"],
    )
    expired_office = source_governance.offices[0].model_copy(update={
        "term_end_tick": state.current_tick,
    })
    expired_mandate_state = _replace_governance(
        state,
        ids["source_org_id"],
        source_governance.model_copy(update={"offices": [expired_office]}),
    )
    with pytest.raises(SafeConductError, match="authority.represent"):
        _propose(expired_mandate_state, ids)

    with pytest.raises(SafeConductError, match="future"):
        _propose(state, ids, expires_at_tick=state.current_tick)


def test_safe_conduct_prevents_duplicate_live_terms_and_allows_expired_replacement():
    state, ids = _world()
    original = _propose(state, ids)
    with pytest.raises(SafeConductError, match="live safe-conduct term"):
        _propose(state, ids, existing_treaties=[original])

    expired_state = _with_tick(state, original.expires_at_tick)
    replacement = _propose(expired_state, ids, expires_at_tick=3_000, existing_treaties=[original])
    assert replacement.treaty_id != original.treaty_id


def test_countered_safe_conduct_is_closed_until_captured_proposer_accepts_shorter_term():
    state, ids = _world()
    treaty = _propose(state, ids, expires_at_tick=2_000)

    countered = transition_safe_conduct(
        state,
        treaty,
        actor_id=ids["recipient_leader_id"],
        operation="counter",
        expected_revision=1,
        counter_expires_at_tick=1_500,
    )
    assert countered.status == "countered"
    assert countered.revision == 2
    assert countered.expires_at_tick == 2_000
    assert countered.counter_expires_at_tick == 1_500
    assert not safe_conduct_allows_route(
        state, state.entities[ids["source_member_id"]], ids["route"], [countered],
    )
    with pytest.raises(SafeConductError, match="pending"):
        transition_safe_conduct(
            state, countered, actor_id=ids["recipient_leader_id"],
            operation="ratify", expected_revision=2,
        )
    with pytest.raises(SafeConductError, match="live safe-conduct term"):
        _propose(state, ids, expires_at_tick=2_500, existing_treaties=[countered])
    with pytest.raises(SafeConductError, match="captured proposer"):
        transition_safe_conduct(
            state, countered, actor_id=ids["recipient_leader_id"],
            operation="accept_counter", expected_revision=2,
        )

    before_deadline = _with_tick(state, 1_499)
    active = transition_safe_conduct(
        before_deadline,
        countered,
        actor_id=ids["source_leader_id"],
        operation="accept_counter",
        expected_revision=2,
    )
    assert active.status == "active"
    assert active.revision == 3
    assert active.expires_at_tick == 1_500
    assert active.counter_expires_at_tick == 1_500
    assert safe_conduct_allows_route(
        before_deadline, before_deadline.entities[ids["source_member_id"]], ids["route"], [active],
    )
    assert not safe_conduct_allows_route(
        _with_tick(before_deadline, 1_500),
        state.entities[ids["source_member_id"]],
        ids["route"],
        [active],
    )
    with pytest.raises(SafeConductError, match="expired safe-conduct counter"):
        transition_safe_conduct(
            _with_tick(state, 1_500), countered, actor_id=ids["source_leader_id"],
            operation="accept_counter", expected_revision=2,
        )


def test_counter_deadline_is_strict_pinned_and_releases_duplicate_route_at_expiry():
    state, ids = _world()
    treaty = _propose(state, ids, expires_at_tick=2_000)
    for invalid_deadline in (state.current_tick, treaty.expires_at_tick):
        with pytest.raises(SafeConductError, match="counter expiry"):
            transition_safe_conduct(
                state,
                treaty,
                actor_id=ids["recipient_leader_id"],
                operation="counter",
                expected_revision=1,
                counter_expires_at_tick=invalid_deadline,
            )
    with pytest.raises(SafeConductError, match="recipient representative"):
        transition_safe_conduct(
            state,
            treaty,
            actor_id=ids["recipient_member_id"],
            operation="counter",
            expected_revision=1,
            counter_expires_at_tick=1_500,
        )

    countered = transition_safe_conduct(
        state, treaty, actor_id=ids["recipient_leader_id"], operation="counter",
        expected_revision=1, counter_expires_at_tick=1_500,
    )
    changed_governance = GovernanceComponent.model_validate(
        state.entities[ids["recipient_org_id"]].components["governance"],
    ).model_copy(update={"revision": 9})
    stale_state = _replace_governance(state, ids["recipient_org_id"], changed_governance)
    with pytest.raises(SafeConductError, match="governance changed"):
        transition_safe_conduct(
            stale_state, countered, actor_id=ids["source_leader_id"],
            operation="accept_counter", expected_revision=2,
        )

    # The captured proposer may withdraw the live counter. Otherwise it blocks
    # a second term strictly before its deadline, then releases the route at it.
    withdrawn = transition_safe_conduct(
        state, countered, actor_id=ids["source_leader_id"],
        operation="withdraw", expected_revision=2,
    )
    assert withdrawn.status == "withdrawn" and withdrawn.revision == 3
    assert withdrawn.counter_expires_at_tick == 1_500
    with pytest.raises(SafeConductError, match="live safe-conduct term"):
        _propose(_with_tick(state, 1_499), ids, expires_at_tick=2_500, existing_treaties=[countered])
    replacement = _propose(
        _with_tick(state, 1_500), ids, expires_at_tick=2_500, existing_treaties=[countered],
    )
    assert replacement.treaty_id != countered.treaty_id

    # Like pending housekeeping, withdrawal uses the current proposer-side
    # representative and remains available after a counter's expiry.
    successor_id = uuid4()
    successor = WorldEntity(id=successor_id, kind="character", name="New North Steward", status="active")
    source_governance = GovernanceComponent.model_validate(
        state.entities[ids["source_org_id"]].components["governance"],
    )
    next_source_governance = source_governance.model_copy(update={
        "revision": source_governance.revision + 1,
        "members": [*source_governance.members, successor_id],
        "leader_id": successor_id,
        "offices": [OfficeRecord(
            office_id="office.leader", title="New North Steward", holder_id=successor_id,
            authority=["authority.represent"],
        )],
    })
    entities = dict(state.entities)
    entities[successor_id] = successor
    source_org = entities[ids["source_org_id"]]
    entities[ids["source_org_id"]] = source_org.model_copy(update={
        "components": {"governance": next_source_governance.model_dump(mode="json")},
    })
    current_source_state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "current_tick": 1_600,
        "entities": entities,
    })
    stale_counter_withdrawn = transition_safe_conduct(
        current_source_state, countered, actor_id=successor_id,
        operation="withdraw", expected_revision=2,
    )
    assert stale_counter_withdrawn.status == "withdrawn"
    assert stale_counter_withdrawn.updated_tick == 1_600


def test_counter_expiry_none_is_omitted_for_legacy_treaty_payloads():
    state, ids = _world()
    treaty = _propose(state, ids)
    payload = treaty.model_dump(mode="json")
    assert "counter_expires_at_tick" not in payload
    legacy_loaded = SafeConductTreaty.model_validate(payload)
    assert legacy_loaded == treaty
    assert "counter_expires_at_tick" not in legacy_loaded.model_dump(mode="json")


def test_safe_conduct_ratification_pins_both_governance_revisions_and_expiry():
    state, ids = _world()
    treaty = _propose(state, ids)
    with pytest.raises(SafeConductError, match="expected_revision"):
        transition_safe_conduct(
            state, treaty, actor_id=ids["recipient_leader_id"], operation="ratify", expected_revision=0,
        )
    with pytest.raises(SafeConductError, match="recipient representative"):
        transition_safe_conduct(
            state, treaty, actor_id=ids["recipient_member_id"], operation="ratify", expected_revision=1,
        )

    for organization_id in (ids["source_org_id"], ids["recipient_org_id"]):
        governance = GovernanceComponent.model_validate(
            state.entities[organization_id].components["governance"],
        )
        changed = _replace_governance(
            state,
            organization_id,
            governance.model_copy(update={"revision": governance.revision + 1}),
        )
        with pytest.raises(SafeConductError, match="governance changed"):
            transition_safe_conduct(
                changed, treaty, actor_id=ids["recipient_leader_id"], operation="ratify", expected_revision=1,
            )

    expired_state = _with_tick(state, treaty.expires_at_tick)
    with pytest.raises(SafeConductError, match="expired"):
        transition_safe_conduct(
            expired_state, treaty, actor_id=ids["recipient_leader_id"], operation="ratify", expected_revision=1,
        )


def test_safe_conduct_pending_decline_withdraw_and_active_revocation_have_party_scope():
    state, ids = _world()
    treaty = _propose(state, ids)
    with pytest.raises(SafeConductError, match="current party representative"):
        transition_safe_conduct(
            state, treaty, actor_id=ids["source_leader_id"], operation="decline", expected_revision=1,
        )
    declined = transition_safe_conduct(
        state, treaty, actor_id=ids["recipient_leader_id"], operation="decline", expected_revision=1,
    )
    assert declined.status == "declined" and declined.revision == 2
    with pytest.raises(SafeConductError, match="pending"):
        transition_safe_conduct(
            state, declined, actor_id=ids["recipient_leader_id"], operation="withdraw", expected_revision=2,
        )

    successor_id = uuid4()
    successor = WorldEntity(id=successor_id, kind="character", name="New South Steward", status="active")
    recipient_governance = GovernanceComponent.model_validate(
        state.entities[ids["recipient_org_id"]].components["governance"],
    )
    next_recipient_governance = recipient_governance.model_copy(update={
        "revision": recipient_governance.revision + 1,
        "members": [*recipient_governance.members, successor_id],
        "leader_id": successor_id,
        "offices": [OfficeRecord(
            office_id="office.leader", title="New South Steward", holder_id=successor_id,
            authority=["authority.represent"],
        )],
    })
    recipient_entities = dict(state.entities)
    recipient_entities[successor_id] = successor
    recipient_entities[ids["recipient_org_id"]] = recipient_entities[ids["recipient_org_id"]].model_copy(
        update={"components": {"governance": next_recipient_governance.model_dump(mode="json")}},
    )
    current_recipient_state = WorldState.model_validate({
        **state.model_dump(mode="python"), "entities": recipient_entities,
    })
    declined_by_current_leader = transition_safe_conduct(
        current_recipient_state,
        treaty,
        actor_id=successor_id,
        operation="decline",
        expected_revision=1,
    )
    assert declined_by_current_leader.status == "declined"
    assert declined_by_current_leader.recipient_actor_id == ids["recipient_leader_id"]

    withdrawn = transition_safe_conduct(
        state, treaty, actor_id=ids["source_leader_id"], operation="withdraw", expected_revision=1,
    )
    assert withdrawn.status == "withdrawn" and withdrawn.revision == 2
    active = transition_safe_conduct(
        state, treaty, actor_id=ids["recipient_leader_id"], operation="ratify", expected_revision=1,
    )
    revoked_by_recipient = transition_safe_conduct(
        state, active, actor_id=ids["recipient_leader_id"], operation="revoke", expected_revision=2,
    )
    assert revoked_by_recipient.status == "revoked" and revoked_by_recipient.revision == 3


def test_region_endpoints_use_the_region_itself_as_the_party_location():
    state, ids = _world()
    source_region = state.entities[ids["source_region_id"]]
    source_region_place = PlaceComponent.model_validate(source_region.components["place"])
    region_route = PlaceRoute(
        route_id=uuid4(),
        from_place_id=ids["source_region_id"],
        to_place_id=ids["recipient_region_id"],
        travel_ticks=900,
        access_tags=[SAFE_CONDUCT_TAG],
        is_open=True,
    )
    entities = dict(state.entities)
    entities[ids["source_region_id"]] = source_region.model_copy(update={
        "components": {"place": source_region_place.model_copy(update={
            "routes": [region_route],
        }).model_dump(mode="json")},
    })
    region_state = WorldState.model_validate({**state.model_dump(mode="python"), "entities": entities})
    proposed = propose_safe_conduct(
        region_state,
        treaty_id=uuid4(),
        proposer_organization_id=ids["source_org_id"],
        recipient_organization_id=ids["recipient_org_id"],
        proposer_actor_id=ids["source_leader_id"],
        route=region_route,
        expires_at_tick=2_000,
    )
    assert proposed.from_place_id == ids["source_region_id"]
    assert proposed.to_place_id == ids["recipient_region_id"]

