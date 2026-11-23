from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.content import ContentCatalog
from world_engine.domain.components import GovernanceComponent, OfficeRecord, PlaceComponent, PlaceRoute, SkillsComponent
from world_engine.domain.leadership_ballots import open_leadership_ballot
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import RuleContext
from world_engine.simulation.npc_capabilities import (
    DuplicateNpcCapabilityError,
    NpcCapabilityRegistry,
    build_default_npc_capability_registry,
    capability_id_for_action,
    preflight_npc_plan_step,
)
from world_engine.domain.npc_plans import NpcPlanStep
from world_engine.rules.contracts import RequestRelationshipReplyIntent


@pytest.fixture(scope="module")
def full_state() -> WorldState:
    return ContentCatalog().build_world("modern_city", "NPC capability tests", "Test Player").state


def _context(actor_id: UUID, *, command_id: UUID | None = None) -> RuleContext:
    return RuleContext(
        world_id=UUID("10000000-0000-0000-0000-000000000001"),
        branch_id=UUID("20000000-0000-0000-0000-000000000002"),
        command_id=command_id or uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _step(action: str, arguments: dict[str, object]) -> NpcPlanStep:
    return NpcPlanStep(capability_id=capability_id_for_action(action), arguments=arguments)


def _active_people(state: WorldState) -> list[WorldEntity]:
    return [
        entity
        for entity in state.entities.values()
        if entity.kind in {"character", "person"}
        and entity.status == "active"
        and entity.id != state.player_id
    ]


def _actor_with_route(state: WorldState) -> tuple[WorldEntity, UUID]:
    for actor in _active_people(state):
        origin = state.entities.get(actor.location_id) if actor.location_id else None
        if origin is None:
            continue
        try:
            place = PlaceComponent.model_validate(origin.components.get("place"))
        except Exception:
            continue
        for route in place.routes:
            destination = state.entities.get(route.to_place_id)
            if destination is not None and destination.status == "active":
                return actor, destination.id
    raise AssertionError("the catalog fixture must contain an active person with an active route")


def _state_with_hidden_route(state: WorldState, actor_id: UUID) -> tuple[WorldState, UUID]:
    actor = state.entities[actor_id]
    origin = state.entities[actor.location_id]
    place = PlaceComponent.model_validate(origin.components["place"])
    hidden_id = uuid4()
    route = PlaceRoute(
        route_id=uuid4(),
        from_place_id=origin.id,
        to_place_id=hidden_id,
        travel_ticks=600,
    )
    hidden_place = WorldEntity(
        id=hidden_id,
        kind="place",
        name="Author-only waypoint",
        components={"place": PlaceComponent(place_type="site").model_dump(mode="json"), "hidden": True},
    )
    updated_origin = origin.model_copy(update={
        "components": {
            **origin.components,
            "place": place.model_copy(update={"routes": [*place.routes, route]}).model_dump(mode="json"),
        },
    })
    raw = state.model_dump(mode="json")
    raw["entities"][str(origin.id)] = updated_origin.model_dump(mode="json")
    raw["entities"][str(hidden_id)] = hidden_place.model_dump(mode="json")
    return WorldState.model_validate(raw), hidden_id


def test_registry_has_explicit_v1_capabilities_and_rejects_duplicate_or_request_adapters():
    registry = build_default_npc_capability_registry()
    assert set(registry._capabilities) == {
        capability_id_for_action(kind)
        for kind in (
            "move",
            "explore",
            "train",
            "trade",
            "produce",
            "speak",
            "fulfill_return_promise",
            "propose_relationship_invitation",
            "resolve_relationship_invitation",
            "cast_leadership_ballot_vote",
        )
    }
    assert capability_id_for_action("move") == "world.action.move.v1"

    registry.register("test.move.alias.v1", registry.get("world.action.move.v1").intent_model)
    with pytest.raises(DuplicateNpcCapabilityError):
        registry.register("test.move.alias.v1", registry.get("world.action.move.v1").intent_model)
    with pytest.raises(ValueError, match="request/host-solicitation"):
        registry.register("test.request.v1", RequestRelationshipReplyIntent)


@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        ({"destination_id": str(uuid4()), "extra": "forbidden"}, "invalid_npc_action_arguments"),
        ({"kind": "explore", "destination_id": str(uuid4())}, "invalid_npc_action_arguments"),
    ],
)
def test_unknown_or_invalid_action_parameters_are_rejected_without_state_change(
    full_state: WorldState, arguments: dict[str, object], expected_code: str,
):
    actor, _ = _actor_with_route(full_state)
    before = state_hash(full_state)
    result = preflight_npc_plan_step(
        full_state,
        actor.id,
        _step("move", arguments),
        _context(actor.id),
    )
    assert result.status == "rejected"
    assert result.resolution.outcome.code == expected_code
    assert result.state_hash_before == result.state_hash_after == before
    assert result.proposed_events == ()
    assert result.committed is False and result.world_mutated is False


def test_unknown_capability_and_player_or_externally_controlled_actor_are_rejected(full_state: WorldState):
    actor, destination_id = _actor_with_route(full_state)
    unknown = NpcPlanStep(capability_id="world.action.teleport.v1", arguments={"destination_id": str(destination_id)})
    assert preflight_npc_plan_step(full_state, actor.id, unknown, _context(actor.id)).resolution.outcome.code == "unsupported_npc_capability"

    controlled = preflight_npc_plan_step(
        full_state,
        actor.id,
        _step("move", {"destination_id": str(destination_id)}),
        _context(actor.id),
        externally_controlled_actor_ids={actor.id},
    )
    assert controlled.status == "rejected"
    assert controlled.resolution.outcome.code == "npc_actor_externally_controlled"

    if full_state.player_id is not None:
        player_context = _context(full_state.player_id)
        player = preflight_npc_plan_step(
            full_state,
            full_state.player_id,
            _step("move", {"destination_id": str(destination_id)}),
            player_context,
        )
        assert player.resolution.outcome.code == "npc_actor_is_player"


def test_hidden_unobserved_route_destination_is_not_a_known_reference(full_state: WorldState):
    actor, _ = _actor_with_route(full_state)
    hidden_state, hidden_id = _state_with_hidden_route(full_state, actor.id)
    before = state_hash(hidden_state)
    result = preflight_npc_plan_step(
        hidden_state,
        actor.id,
        _step("move", {"destination_id": str(hidden_id)}),
        _context(actor.id),
    )
    assert result.status == "rejected"
    assert result.resolution.outcome.code == "npc_reference_not_known"
    assert result.state_hash_before == result.state_hash_after == before
    assert result.proposed_events == ()


def test_move_uses_rule_route_order_and_returns_departure_without_advancing_time(full_state: WorldState):
    actor, destination_id = _actor_with_route(full_state)
    origin = full_state.entities[actor.location_id]
    place = PlaceComponent.model_validate(origin.components["place"])
    candidates = [route for route in place.routes if route.to_place_id == destination_id]
    baseline = min(candidates, key=lambda route: (route.travel_ticks, str(route.route_id)))
    # Add an equal-duration route with the smallest possible UUID. The action
    # evaluator must retain the rules' tie-break semantics, not select a route
    # independently by insertion order.
    tie_route = PlaceRoute(
        route_id=UUID(int=0),
        from_place_id=origin.id,
        to_place_id=destination_id,
        travel_ticks=baseline.travel_ticks,
    )
    assert all(route.route_id != tie_route.route_id for route in place.routes)
    updated_origin = origin.model_copy(update={
        "components": {
            **origin.components,
            "place": place.model_copy(update={"routes": [*place.routes, tie_route]}).model_dump(mode="json"),
        },
    })
    raw = full_state.model_dump(mode="json")
    raw["entities"][str(origin.id)] = updated_origin.model_dump(mode="json")
    state = WorldState.model_validate(raw)
    selected = min(
        (route for route in PlaceComponent.model_validate(state.entities[origin.id].components["place"]).routes
         if route.to_place_id == destination_id),
        key=lambda route: (route.travel_ticks, str(route.route_id)),
    )
    before = state_hash(state)
    result = preflight_npc_plan_step(
        state,
        actor.id,
        _step("move", {"destination_id": str(destination_id)}),
        _context(actor.id),
    )
    assert result.status == "journey_start"
    assert result.required_ticks == selected.travel_ticks
    assert selected.route_id == tie_route.route_id
    assert result.resolution.elapsed_ticks == 0
    assert len(result.proposed_events) == 1
    event = result.proposed_events[0]
    assert event.operation == "depart"
    assert event.journey.route_id == selected.route_id
    assert result.state_hash_before == result.state_hash_after == before
    assert result.world_mutated is False


def test_training_returns_an_interval_proposal_and_does_not_apply_training(full_state: WorldState):
    actor = next(
        person
        for person in _active_people(full_state)
        if (skills := person.components.get("skills"))
        and SkillsComponent.model_validate(skills).skills
    )
    skill_id = next(iter(SkillsComponent.model_validate(actor.components["skills"]).skills))
    before = state_hash(full_state)
    result = preflight_npc_plan_step(
        full_state,
        actor.id,
        _step("train", {"skill_id": skill_id, "duration_ticks": 600}),
        _context(actor.id),
    )
    assert result.status == "interval_required"
    assert result.required_ticks == 600
    assert result.resolution.elapsed_ticks == 600
    assert result.proposed_events
    assert result.state_hash_before == result.state_hash_after == before
    assert state_hash(full_state) == before


def test_relationship_invitation_cannot_be_accepted_by_proposer_but_invitee_can(full_state: WorldState):
    active = _active_people(full_state)
    pair = next(
        (first, second)
        for first in active
        for second in active
        if first.id != second.id
        and first.location_id is not None
        and first.location_id == second.location_id
        and first.components.get("life", {}).get("life_stage") in {"adult", "elder"}
        and second.components.get("life", {}).get("life_stage") in {"adult", "elder"}
    )
    proposer, invitee = pair
    proposed = preflight_npc_plan_step(
        full_state,
        proposer.id,
        _step("propose_relationship_invitation", {"target_id": str(invitee.id)}),
        _context(proposer.id),
    )
    assert proposed.status == "instant"
    assert proposed.resolution.elapsed_ticks == 0
    assert len(proposed.proposed_events) == 1
    after_proposal = apply_event(full_state, proposed.proposed_events[0])
    invitation = after_proposal.relationship_invitations[0]

    proposer_cannot_accept = preflight_npc_plan_step(
        after_proposal,
        proposer.id,
        _step("resolve_relationship_invitation", {
            "invitation_id": str(invitation.invitation_id), "operation": "accept",
        }),
        _context(proposer.id),
    )
    assert proposer_cannot_accept.status == "rejected"
    assert proposer_cannot_accept.resolution.outcome.code == "relationship_invitation_unavailable"

    invitee_accepts = preflight_npc_plan_step(
        after_proposal,
        invitee.id,
        _step("resolve_relationship_invitation", {
            "invitation_id": str(invitation.invitation_id), "operation": "accept",
        }),
        _context(invitee.id),
    )
    assert invitee_accepts.status == "instant"
    assert invitee_accepts.resolution.elapsed_ticks == 0
    assert len(invitee_accepts.proposed_events) == 1


def test_ballot_vote_requires_fixed_electorate_and_is_zero_tick(full_state: WorldState):
    actor, nonmember = _active_people(full_state)[:2]
    # Build a minimal valid governance group around real active people. This
    # exercises the actual ballot planner/rule authorization, not a fake event.
    incumbent, nominee, voter = actor, nonmember, _active_people(full_state)[2]
    organization_id = uuid4()
    governance = GovernanceComponent(
        revision=1,
        members=sorted((incumbent.id, nominee.id, voter.id), key=str),
        leader_id=incumbent.id,
        succession_policy="election",
        decision_threshold=Decimal("0.5"),
        offices=[OfficeRecord(office_id="office.leader", title="Leader", holder_id=incumbent.id)],
    )
    organization = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Test Council",
        components={"governance": governance.model_dump(mode="json")},
    )
    raw = full_state.model_dump(mode="json")
    raw["entities"][str(organization_id)] = organization.model_dump(mode="json")
    state = WorldState.model_validate(raw)
    ballot = open_leadership_ballot(
        state,
        ballot_id=uuid4(),
        organization_id=organization_id,
        nominee_id=nominee.id,
        actor_id=incumbent.id,
        existing_ballots=(),
    )
    state_raw = state.model_dump(mode="json")
    state_raw["leadership_ballots"] = [ballot.model_dump(mode="json")]
    state = WorldState.model_validate(state_raw)

    valid_vote = preflight_npc_plan_step(
        state,
        voter.id,
        _step("cast_leadership_ballot_vote", {
            "ballot_id": str(ballot.ballot_id), "expected_ballot_revision": 1, "choice": "yes",
        }),
        _context(voter.id),
    )
    assert valid_vote.status == "instant"
    assert valid_vote.resolution.elapsed_ticks == 0
    assert len(valid_vote.proposed_events) == 1

    non_electorate_vote = preflight_npc_plan_step(
        state,
        incumbent.id,
        _step("cast_leadership_ballot_vote", {
            "ballot_id": str(ballot.ballot_id), "expected_ballot_revision": 1, "choice": "yes",
        }),
        _context(incumbent.id),
    )
    assert non_electorate_vote.status == "rejected"
    assert non_electorate_vote.resolution.outcome.code == "npc_reference_not_known"


def test_relationship_and_ballot_capabilities_require_typed_schema(full_state: WorldState):
    actor = _active_people(full_state)[0]
    invalid = preflight_npc_plan_step(
        full_state,
        actor.id,
        _step("cast_leadership_ballot_vote", {
            "ballot_id": str(uuid4()), "expected_ballot_revision": 1, "choice": "approve",
        }),
        _context(actor.id),
    )
    assert invalid.status == "rejected"
    assert invalid.resolution.outcome.code == "invalid_npc_action_arguments"
