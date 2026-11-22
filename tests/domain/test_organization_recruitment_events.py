from uuid import uuid4

import pytest

from world_engine.domain.components import (
    GovernanceComponent,
    HealthComponent,
    LifeComponent,
    MembershipRecruitmentPolicy,
    OfficeRecord,
    PlaceComponent,
    PopulationAgeCohort,
    PopulationAgeRate,
    PopulationComponent,
    PopulationOrigin,
    ProvenanceReference,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import validate_population_membership_batch
from world_engine.domain.gameplay_events import (
    CohortMemberPromotedEvent,
    ComponentReplacedEvent,
    OrganizationMembershipReviewedEvent,
)
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.organization_recruitment import (
    CandidatePopulationLineage,
    build_membership_recruitment_review,
)
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RuleEngine


YEAR_SECONDS = 60


class _NoEntityScan(dict):
    def values(self):
        raise AssertionError("entity governance scan should be short-circuited")


def _base_state(*, recruitment_enabled: bool = True) -> tuple[WorldState, dict[str, object]]:
    region_id, group_id, resident_id, leader_id, organization_id, schedule_id = (
        uuid4() for _ in range(6)
    )
    policy = MembershipRecruitmentPolicy(
        policy_id="membership.local_service.test",
        charter_clause_id="charter.membership.local_service",
        admission_rule_id="organization.local_role_match.v1",
        authority_tag="authority.membership_recruitment",
        region_id=region_id,
        eligible_roles=["artisan"],
        minimum_age_years=18,
        maximum_age_years=64,
        active_member_capacity=3,
        max_recruits_per_period=2,
        selection_seed_id=uuid4(),
    )
    governance = GovernanceComponent(
        charter="Members serve the local settlement.",
        members=[leader_id],
        leader_id=leader_id,
        offices=[
            OfficeRecord(
                office_id="office.leader",
                title="Steward",
                holder_id=leader_id,
                authority=["authority.represent"],
            )
        ],
        authority_tags=["authority.membership_recruitment"],
        charter_clause_ids=["charter.membership.local_service"] if recruitment_enabled else [],
        membership_recruitment=[policy] if recruitment_enabled else [],
    )
    population = PopulationComponent(
        species_id="core.human",
        headcount=2,
        unmodeled_headcount=1,
        age_rates=[PopulationAgeRate(age_years=age) for age in range(201)],
        age_cohorts=[
            PopulationAgeCohort(age_years=age, headcount=1 if age == 20 else 0)
            for age in range(201)
        ],
        last_advanced_year=0,
    )
    region = WorldEntity(
        id=region_id,
        kind="place",
        name="North region",
        components={"place": PlaceComponent(place_type="region")},
    )
    group = WorldEntity(
        id=group_id,
        kind="group",
        name="North residents",
        location_id=region_id,
        components={"population": population},
    )
    resident = WorldEntity(
        id=resident_id,
        kind="character",
        name="Mira Chen",
        location_id=region_id,
        components={
            "life": LifeComponent(
                birth_tick=-20 * YEAR_SECONDS,
                lifespan_ticks=80 * YEAR_SECONDS,
                maturity_ticks=18 * YEAR_SECONDS,
                population_group_id=group_id,
                life_stage="adult",
            ),
            "health": HealthComponent(current=100, maximum=100),
            "public_role": "artisan",
        },
    )
    leader = WorldEntity(
        id=leader_id,
        kind="character",
        name="Sora Lee",
        location_id=region_id,
        components={
            "life": LifeComponent(
                birth_tick=-30 * YEAR_SECONDS,
                lifespan_ticks=80 * YEAR_SECONDS,
                maturity_ticks=18 * YEAR_SECONDS,
                life_stage="adult",
            ),
            "health": HealthComponent(current=100, maximum=100),
            "public_role": "steward",
        },
    )
    organization = WorldEntity(
        id=organization_id,
        kind="organization",
        name="North Guild",
        location_id=region_id,
        components={"governance": governance},
    )
    state = WorldState.model_validate({
        "entities": {
            str(region_id): region,
            str(group_id): group,
            str(resident_id): resident,
            str(leader_id): leader,
            str(organization_id): organization,
        },
        "scheduled_events": [{
            "id": str(schedule_id),
            "due_tick": YEAR_SECONDS,
            "phase": "environment",
            "handler_id": "population.year_advance",
            "handler_version": "1.0.0",
            "repeat_ticks": YEAR_SECONDS,
            "max_runs": 2,
        }],
        "calendar": {"day_seconds": YEAR_SECONDS, "year_days": 1},
        "metadata": {
            "pack_id": "test_pack",
            "pack_version": "1.8.0",
            "population_mode": "sustained",
            "content_index": {
                "key_npc_ids": [str(leader_id)],
                "other_resident_ids": [str(resident_id)],
                "other_resident_population_groups": {str(resident_id): str(group_id)},
            },
        },
    })
    return state, {
        "region_id": region_id,
        "group_id": group_id,
        "resident_id": resident_id,
        "leader_id": leader_id,
        "organization_id": organization_id,
        "schedule_id": schedule_id,
        "policy": policy,
    }


def _promote_one(state: WorldState, ids: dict[str, object]) -> tuple[WorldState, object]:
    group_id = ids["group_id"]
    region_id = ids["region_id"]
    promoted_id = uuid4()
    promoted = WorldEntity(
        id=promoted_id,
        kind="character",
        name="Niko Park",
        location_id=region_id,
        components={
            "life": LifeComponent(
                birth_tick=-20 * YEAR_SECONDS,
                lifespan_ticks=80 * YEAR_SECONDS,
                maturity_ticks=18 * YEAR_SECONDS,
                population_group_id=group_id,
                life_stage="adult",
            ),
            "health": HealthComponent(current=100, maximum=100),
            "public_role": "artisan",
        },
    )
    event = CohortMemberPromotedEvent(
        cohort_id=group_id,
        expected_population_revision=1,
        age_years=20,
        character=promoted,
        provenance=[ProvenanceReference(kind="rule", reference_id="population.cohort_promotion.v1")],
    )
    return apply_event(state, event), event


def _annual_resolution(state: WorldState):
    boundary_state = apply_event(state, TimeAdvancedEvent(to_tick=YEAR_SECONDS))
    scheduled = boundary_state.scheduled_events[0]
    context = RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=uuid4(),
        actor_id=uuid4(),
        ruleset_version="test.rules.v1",
    )
    return boundary_state, RuleEngine().resolve_scheduled(boundary_state, scheduled, context)


def test_annual_review_enrolls_real_residents_after_census_without_assigning_offices():
    state, ids = _base_state()
    state, promotion = _promote_one(state, ids)
    promoted = state.entities[promotion.character.id]
    origin = LifeComponent.model_validate(promoted.components["life"]).population_origin
    assert origin.source_event_id == promotion.event_id
    assert origin.source_event_type == promotion.type

    boundary_state, resolution = _annual_resolution(state)

    assert resolution.outcome.code == "population_year_advanced"
    assert [event.type for event in resolution.events] == [
        "core.population_year_advanced.v1",
        "core.organization_membership_reviewed.v1",
        "core.scheduled_event_completed.v1",
    ]
    census, review, _completion = resolution.events
    assert isinstance(review, OrganizationMembershipReviewedEvent)
    assert review.census_event_id == census.event_id
    assert review.year_index == 1
    assert {item.member_id for item in review.review.enrollments} == {
        ids["resident_id"], promotion.character.id,
    }
    assert {item.lineage.source_kind for item in review.review.enrollments} == {
        "genesis", "population_event",
    }
    assert review.review.original_key_people.original_key_people_seeded == 1
    assert review.review.original_key_people.original_key_people_alive == 1
    assert review.review.organizations[0].offices[0].status == "filled_active"
    validate_population_membership_batch(boundary_state, resolution.events)

    projected = boundary_state
    for event in resolution.events:
        projected = apply_event(projected, event)
    governance = GovernanceComponent.model_validate(
        projected.entities[ids["organization_id"]].components["governance"]
    )
    assert set(governance.members) == {
        ids["leader_id"], ids["resident_id"], promotion.character.id,
    }
    assert governance.leader_id == ids["leader_id"]
    assert governance.offices[0].holder_id == ids["leader_id"]
    assert governance.revision == 2
    assert projected.scheduled_events[0].completed_runs == 1


def test_unrelated_and_orphan_review_batches_skip_full_governance_scan():
    state, ids = _base_state()
    state, _promotion = _promote_one(state, ids)
    boundary_state, resolution = _annual_resolution(state)
    review = resolution.events[1]
    assert isinstance(review, OrganizationMembershipReviewedEvent)
    guarded_state = boundary_state.model_copy(update={
        "entities": _NoEntityScan(boundary_state.entities),
    })

    validate_population_membership_batch(guarded_state, [TimeAdvancedEvent(to_tick=1)])
    with pytest.raises(InvalidEvent, match="requires its annual population census"):
        validate_population_membership_batch(guarded_state, [review])


def test_membership_review_rejects_forged_population_origin_and_wrong_event_order():
    state, ids = _base_state()
    state, promotion = _promote_one(state, ids)
    boundary_state, resolution = _annual_resolution(state)
    census, review, completion = resolution.events
    assert isinstance(review, OrganizationMembershipReviewedEvent)
    post_census = apply_event(boundary_state, census)

    altered_enrollments = tuple(
        item.model_copy(update={
            "lineage": CandidatePopulationLineage(
                source_kind="population_event",
                population_group_id=item.population_group_id,
                source_event_id=uuid4(),
                source_event_type="core.cohort_member_promoted.v1",
            )
        })
        if item.member_id == promotion.character.id else item
        for item in review.review.enrollments
    )
    altered_plan = review.review.model_copy(update={"enrollments": altered_enrollments})
    forged = review.model_copy(update={"review": altered_plan})
    with pytest.raises(InvalidEvent, match="deterministic policy, roster, life and lineage"):
        apply_event(post_census, forged)

    with pytest.raises(InvalidEvent, match="immediately follow"):
        validate_population_membership_batch(boundary_state, [census, completion, review])


def test_population_origin_cannot_be_injected_by_generic_component_replacement():
    state, ids = _base_state(recruitment_enabled=False)
    resident_id = ids["resident_id"]
    current_life = LifeComponent.model_validate(state.entities[resident_id].components["life"])
    forged_life = current_life.model_copy(update={
        "revision": current_life.revision + 1,
        "population_origin": PopulationOrigin(
            source_event_id=uuid4(),
            source_event_type="core.cohort_member_promoted.v1",
            population_group_id=ids["group_id"],
        ),
    })
    event = ComponentReplacedEvent(
        entity_id=resident_id,
        component_name="life",
        expected_revision=current_life.revision,
        replacement=forged_life.model_dump(mode="json"),
    )
    with pytest.raises(InvalidEvent, match="reducer-owned"):
        apply_event(state, event)


def test_genesis_lineage_requires_complete_immutable_resident_to_cohort_map():
    state, ids = _base_state()
    raw = state.model_dump(mode="json")
    raw["metadata"]["content_index"].pop("other_resident_population_groups")
    missing_map = WorldState.model_validate(raw)
    with pytest.raises(ValueError, match="complete immutable genesis"):
        build_membership_recruitment_review(missing_map)

    raw["metadata"]["content_index"]["other_resident_population_groups"] = {
        str(ids["resident_id"]): str(ids["region_id"]),
    }
    mismatched_map = WorldState.model_validate(raw)
    with pytest.raises(ValueError, match="does not match the immutable content index"):
        build_membership_recruitment_review(mismatched_map)


def test_pre_policy_cohort_promotions_do_not_gain_new_lineage_markers():
    state, ids = _base_state(recruitment_enabled=False)
    promoted_state, promotion = _promote_one(state, ids)
    origin = LifeComponent.model_validate(
        promoted_state.entities[promotion.character.id].components["life"]
    ).population_origin
    assert origin is None


def test_new_empty_origin_and_policy_defaults_preserve_legacy_state_hashes():
    state, ids = _base_state(recruitment_enabled=False)
    old_payload = state.model_dump(mode="json")
    for entity in old_payload["entities"].values():
        components = entity["components"]
        if "life" in components:
            components["life"].pop("population_origin", None)
        if "governance" in components:
            components["governance"].pop("charter_clause_ids", None)
            components["governance"].pop("membership_recruitment", None)
    legacy = WorldState.model_validate(old_payload)
    expanded = WorldState.model_validate(state.model_dump(mode="json"))
    assert state_hash(legacy) == state_hash(expanded)
