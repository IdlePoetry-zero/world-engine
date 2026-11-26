from uuid import uuid4

import pytest

from world_engine.domain.components import RelationRecord
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.occupations import (
    OccupationSource,
    WorkAssignment,
    WorkPosting,
    occupation_assignment_id,
)
from world_engine.domain.social_contacts import (
    EstablishedRelationProof,
    SharedDutyProof,
    SharedInstitutionProof,
    SocialContactOpportunity,
    plan_social_contact_candidates,
)


def _base_state(*, extra_people=()):
    region_id, square_id = uuid4(), uuid4()
    entities = {
        region_id: WorldEntity(id=region_id, kind="place", name="Harbor District"),
        square_id: WorldEntity(id=square_id, kind="place", name="Harbor Square", location_id=region_id),
    }
    actor_ids = []
    for name, location_id, status in extra_people:
        actor_id = uuid4()
        actor_ids.append(actor_id)
        if location_id is not None and location_id not in entities:
            entities[location_id] = WorldEntity(
                id=location_id, kind="place", name="Other Place", location_id=region_id,
            )
        entities[actor_id] = WorldEntity(
            id=actor_id, kind="character", name=name, location_id=location_id, status=status,
        )
    state = WorldState(current_tick=86_400, entities=entities)
    return state, region_id, square_id, tuple(actor_ids)


def _candidate_pair(candidate: SocialContactOpportunity):
    return candidate.participant_ids


def test_existing_directed_relation_is_a_typed_proof_and_requires_actual_colocation():
    state, _region_id, square_id, (a_id, b_id) = _base_state(
        extra_people=(("Aki", uuid4(), "active"), ("Bo", uuid4(), "active")),
    )
    # Move both people to the same registered location after constructing it.
    entities = {
        **state.entities,
        a_id: state.entities[a_id].model_copy(update={"location_id": square_id}),
        b_id: state.entities[b_id].model_copy(update={"location_id": square_id}),
    }
    relation = RelationRecord(
        id=uuid4(), from_id=a_id, to_id=b_id,
        dimensions={"familiarity": 12}, label="neighbors", updated_tick=1,
    )
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": entities, "relations": [relation]})

    candidates = plan_social_contact_candidates(state, stable_seed="world-seed")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.participant_ids == tuple(sorted((a_id, b_id), key=str))
    assert candidate.location_id == square_id
    assert candidate.slot_tick == state.current_tick
    assert isinstance(candidate.proof, EstablishedRelationProof)
    assert candidate.proof.relation_id == relation.id
    assert (candidate.proof.from_id, candidate.proof.to_id) == (a_id, b_id)
    assert set(candidate.model_dump()) == {"participant_ids", "location_id", "slot_tick", "proof"}


def test_shared_governance_membership_emits_institution_proof_and_excludes_controlled_actor():
    state, _region_id, square_id, (a_id, b_id, c_id, distant_id, inactive_id) = _base_state(
        extra_people=(
            ("Aki", uuid4(), "active"), ("Bo", uuid4(), "active"),
            ("Cy", uuid4(), "active"), ("Dee", uuid4(), "active"),
            ("Inactive", uuid4(), "retired"),
        ),
    )
    entities = {
        **state.entities,
        a_id: state.entities[a_id].model_copy(update={"location_id": square_id}),
        b_id: state.entities[b_id].model_copy(update={"location_id": square_id}),
        c_id: state.entities[c_id].model_copy(update={"location_id": square_id}),
        inactive_id: state.entities[inactive_id].model_copy(update={"location_id": square_id}),
    }
    institution_id = uuid4()
    entities[institution_id] = WorldEntity(
        id=institution_id,
        kind="organization",
        name="Harbor Guild",
        components={"governance": {"members": [a_id, b_id, c_id, distant_id, inactive_id]}},
    )
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": entities})

    candidates = plan_social_contact_candidates(
        state, stable_seed="world-seed", controlled_actor_ids=frozenset({b_id}),
        scope_place_id=_region_id,
    )

    assert candidates
    assert all(isinstance(item.proof, SharedInstitutionProof) for item in candidates)
    assert all(b_id not in item.participant_ids for item in candidates)
    assert all(distant_id not in item.participant_ids for item in candidates)
    assert all(inactive_id not in item.participant_ids for item in candidates)
    assert all(item.proof.institution_id == institution_id for item in candidates)
    assert all(item.location_id == square_id for item in candidates)
    assert _candidate_pair(candidates[0]) == tuple(sorted((a_id, c_id), key=str))


def test_active_shared_work_posting_emits_assignment_ids_in_participant_order():
    state, _region_id, square_id, (a_id, b_id) = _base_state(
        extra_people=(("Aki", uuid4(), "active"), ("Bo", uuid4(), "active")),
    )
    entities = {
        **state.entities,
        a_id: state.entities[a_id].model_copy(update={"location_id": square_id}),
        b_id: state.entities[b_id].model_copy(update={"location_id": square_id}),
    }
    employer_id, posting_id = uuid4(), uuid4()
    entities[employer_id] = WorldEntity(id=employer_id, kind="organization", name="Harbor Works")
    posting = WorkPosting(
        posting_id=posting_id, employer_id=employer_id, location_id=square_id,
        title="Dock Steward", slots=2, valid_from_tick=0, status="closed",
    )
    source = OccupationSource(kind="genesis", reference_id="harbor-work-fixture")
    assignment_by_actor = {
        actor_id: WorkAssignment(
            assignment_id=occupation_assignment_id(source, actor_id, posting_id),
            actor_id=actor_id, posting_id=posting_id, employer_id=employer_id,
            location_id=square_id, title="Dock Steward", started_tick=0, source=source,
        )
        for actor_id in (a_id, b_id)
    }
    state = WorldState.model_validate(state.model_dump(mode="python") | {
        "entities": entities,
        "work_postings": [posting],
        "work_assignments": list(assignment_by_actor.values()),
    })

    candidates = plan_social_contact_candidates(state, stable_seed="world-seed")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate.proof, SharedDutyProof)
    assert candidate.proof.posting_id == posting_id
    assert candidate.proof.employer_id == employer_id
    assert candidate.proof.location_id == candidate.location_id == square_id
    expected_assignment_ids = tuple(assignment_by_actor[actor_id].assignment_id for actor_id in candidate.participant_ids)
    assert candidate.proof.assignment_ids == expected_assignment_ids
    assert candidate.proof.title == "Dock Steward"


def test_no_tie_no_contact_and_no_future_or_stale_calendar_slot():
    state, _region_id, square_id, (a_id, b_id) = _base_state(
        extra_people=(("Aki", uuid4(), "active"), ("Bo", uuid4(), "active")),
    )
    entities = {
        **state.entities,
        a_id: state.entities[a_id].model_copy(update={"location_id": square_id}),
        b_id: state.entities[b_id].model_copy(update={"location_id": square_id}),
    }
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": entities})

    assert plan_social_contact_candidates(state, stable_seed="world-seed") == ()
    with pytest.raises(ValueError, match="current world tick"):
        plan_social_contact_candidates(state, stable_seed="world-seed", slot_tick=state.current_tick + 1)


def test_player_and_unlocated_people_are_never_contact_candidates():
    state, _region_id, square_id, (a_id, b_id, player_id, traveller_id) = _base_state(
        extra_people=(
            ("Aki", uuid4(), "active"), ("Bo", uuid4(), "active"),
            ("Player", uuid4(), "active"), ("Traveller", None, "active"),
        ),
    )
    entities = {
        **state.entities,
        a_id: state.entities[a_id].model_copy(update={"location_id": square_id}),
        b_id: state.entities[b_id].model_copy(update={"location_id": square_id}),
        player_id: state.entities[player_id].model_copy(update={"location_id": square_id}),
    }
    institution_id = uuid4()
    entities[institution_id] = WorldEntity(
        id=institution_id, kind="organization", name="Guild",
        components={"governance": {"members": [a_id, b_id, player_id, traveller_id]}},
    )
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": entities, "player_id": player_id})

    candidates = plan_social_contact_candidates(state, stable_seed="world-seed")

    assert len(candidates) == 1
    assert set(candidates[0].participant_ids) == {a_id, b_id}


def test_large_membership_group_uses_bounded_seeded_candidates_not_all_actor_pairs():
    state, _region_id, square_id, _ = _base_state()
    actor_ids = []
    entities = dict(state.entities)
    for index in range(300):
        actor_id = uuid4()
        actor_ids.append(actor_id)
        entities[actor_id] = WorldEntity(
            id=actor_id, kind="character", name=f"Harbor member {index}", location_id=square_id,
        )
    institution_id = uuid4()
    entities[institution_id] = WorldEntity(
        id=institution_id,
        kind="organization",
        name="Large Harbor Guild",
        components={"governance": {"members": actor_ids}},
    )
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": entities})

    first = plan_social_contact_candidates(state, stable_seed="same-seed")
    second = plan_social_contact_candidates(state, stable_seed="same-seed")

    assert first == second
    assert 0 < len(first) <= 16
    assert len({_candidate_pair(item) for item in first}) == len(first)
    assert all(isinstance(item.proof, SharedInstitutionProof) for item in first)
    assert len(first) < len(actor_ids) * (len(actor_ids) - 1) // 2


def test_max_candidates_is_a_hard_output_bound_and_can_disable_planning():
    state, _region_id, square_id, (a_id, b_id, c_id) = _base_state(
        extra_people=(("Aki", uuid4(), "active"), ("Bo", uuid4(), "active"), ("Cy", uuid4(), "active")),
    )
    entities = {
        **state.entities,
        a_id: state.entities[a_id].model_copy(update={"location_id": square_id}),
        b_id: state.entities[b_id].model_copy(update={"location_id": square_id}),
        c_id: state.entities[c_id].model_copy(update={"location_id": square_id}),
    }
    institution_id = uuid4()
    entities[institution_id] = WorldEntity(
        id=institution_id, kind="organization", name="Guild",
        components={"governance": {"members": [a_id, b_id, c_id]}},
    )
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": entities})

    assert len(plan_social_contact_candidates(state, stable_seed="seed", max_candidates=1)) == 1
    assert plan_social_contact_candidates(state, stable_seed="seed", max_candidates=0) == ()
    with pytest.raises(ValueError, match="max_candidates"):
        plan_social_contact_candidates(state, stable_seed="seed", max_candidates=65)
