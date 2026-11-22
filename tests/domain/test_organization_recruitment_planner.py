from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.organization_recruitment import (
    CandidatePopulationLineage,
    MembershipRecruitmentReview,
    OrganizationRecruitmentPolicy,
    OrganizationRecruitmentSnapshot,
    OrganizationOfficeSnapshot,
    RecruitmentLifeSnapshot,
    RecruitmentPersonSnapshot,
    plan_membership_recruitment,
)


YEAR_SECONDS = 360 * 86_400


def _policy(region_id, *, capacity=2, authority_tag="authority.membership_recruitment", eligible_roles=("scribe",)):
    return OrganizationRecruitmentPolicy(
        policy_id="membership.local.role.v1",
        charter_clause_id="charter.membership.local_service",
        admission_rule_id="organization.local_role_match.v1",
        authority_tag=authority_tag,
        region_id=region_id,
        eligible_roles=eligible_roles,
        minimum_age_years=18,
        maximum_age_years=64,
        active_member_capacity=capacity,
        max_recruits_per_period=1,
        selection_seed_id=uuid4(),
    )


def _person(
    person_id,
    *,
    current_tick,
    region_id=None,
    identity_class="resident",
    status="active",
    role="scribe",
    age=28,
    population_group_id=None,
    source_kind="population_event",
    source_event_id=None,
    lifespan_years=80,
):
    group_id = population_group_id or uuid4()
    birth_tick = current_tick - age * YEAR_SECONDS
    lineage = None
    if identity_class == "resident":
        if source_kind == "genesis":
            lineage = CandidatePopulationLineage(
                source_kind="genesis",
                population_group_id=group_id,
                pack_id="modern_city",
                pack_version="1.8.0",
            )
        else:
            lineage = CandidatePopulationLineage(
                source_kind="population_event",
                population_group_id=group_id,
                source_event_id=source_event_id or uuid4(),
                source_event_type="core.renewable_resource_harvested.v1",
            )
    return RecruitmentPersonSnapshot(
        person_id=person_id,
        kind="character",
        status=status,
        identity_class=identity_class,
        life=RecruitmentLifeSnapshot(
            population_group_id=group_id,
            birth_tick=birth_tick,
            lifespan_ticks=lifespan_years * YEAR_SECONDS,
            revision=1,
        ),
        population_region_id=region_id,
        current_region_id=region_id,
        public_role=role,
        lineage=lineage,
    )


def _review(*, organizations, people, key_ids=(), year_index=20):
    return MembershipRecruitmentReview(
        year_index=year_index,
        current_tick=year_index * YEAR_SECONDS,
        day_seconds=86_400,
        year_days=360,
        player_id=uuid4(),
        original_key_person_ids=tuple(key_ids),
        organizations=tuple(organizations),
        people=tuple(people),
    )


def test_planner_recruits_only_local_policy_eligible_residents_and_keeps_roles_separate():
    region_id, other_region = uuid4(), uuid4()
    dead_key_id, active_key_id, expired_key_id = uuid4(), uuid4(), uuid4()
    organization_id = uuid4()
    local_first, local_second, remote, wrong_role, child = (uuid4() for _ in range(5))
    year_index = 20
    current_tick = year_index * YEAR_SECONDS

    people = [
        _person(
            dead_key_id, current_tick=current_tick, region_id=region_id,
            identity_class="original_key_person", status="dead",
        ),
        _person(active_key_id, current_tick=current_tick, region_id=region_id, identity_class="original_key_person"),
        _person(
            expired_key_id, current_tick=current_tick, region_id=region_id,
            identity_class="original_key_person", lifespan_years=20,
        ),
        _person(local_first, current_tick=current_tick, region_id=region_id,
                source_event_id=uuid4()),
        _person(local_second, current_tick=current_tick, region_id=region_id,
                source_kind="genesis"),
        _person(remote, current_tick=current_tick, region_id=other_region),
        _person(wrong_role, current_tick=current_tick, region_id=region_id, role="wheelwright"),
        _person(child, current_tick=current_tick, region_id=region_id, age=17),
    ]
    original_keys = tuple(uuid4() for _ in range(29)) + (dead_key_id, active_key_id, expired_key_id)
    assert len(original_keys) == 32
    people.extend(
        _person(key_id, current_tick=current_tick, region_id=region_id, identity_class="original_key_person")
        for key_id in original_keys[:29]
    )

    policy = _policy(region_id)
    organization = OrganizationRecruitmentSnapshot(
        organization_id=organization_id,
        kind="organization",
        status="active",
        region_id=region_id,
        governance_revision=7,
        member_ids=(dead_key_id, active_key_id),
        authority_tags=("authority.membership_recruitment",),
        declared_charter_clause_ids=("charter.membership.local_service",),
        offices=(
            OrganizationOfficeSnapshot(office_id="office.leader", holder_id=dead_key_id),
            OrganizationOfficeSnapshot(office_id="office.clerk", holder_id=None),
            OrganizationOfficeSnapshot(office_id="office.records", holder_id=active_key_id),
        ),
        policy=policy,
    )
    review = _review(organizations=[organization], people=people, key_ids=original_keys, year_index=year_index)

    plan = plan_membership_recruitment(review)
    repeated = plan_membership_recruitment(review)

    assert plan == repeated
    assert len(plan.enrollments) == 1  # the typed policy has one opening per year
    enrollment = plan.enrollments[0]
    assert enrollment.member_id in {local_first, local_second}
    assert enrollment.organization_id == organization_id
    assert enrollment.policy_id == policy.policy_id
    assert enrollment.expected_governance_revision == 7
    assert enrollment.population_group_id == next(
        person.life.population_group_id for person in people if person.person_id == enrollment.member_id
    )
    assert enrollment.lineage == next(person.lineage for person in people if person.person_id == enrollment.member_id)
    assert plan.original_key_people.original_key_people_seeded == 32
    assert plan.original_key_people.original_key_people_alive == 30

    report = plan.organizations[0]
    assert report.active_members_before == 1
    assert report.active_members_after == 2
    assert report.active_member_capacity == 2
    assert report.newly_enrolled_member_ids == (enrollment.member_id,)
    assert [(office.office_id, office.status) for office in report.offices] == [
        ("office.clerk", "vacant"),
        ("office.leader", "inactive_holder"),
        ("office.records", "filled_active"),
    ]
    assert "leader_id" not in enrollment.model_dump()
    assert "office_id" not in enrollment.model_dump()
    assert review.organizations[0].member_ids == (dead_key_id, active_key_id)


def test_deterministic_selection_ignores_input_order_and_preserves_source_kind():
    region_id, organization_id = uuid4(), uuid4()
    year_index = 11
    current_tick = year_index * YEAR_SECONDS
    genesis_candidate, event_candidate = uuid4(), uuid4()
    people = [
        _person(genesis_candidate, current_tick=current_tick, region_id=region_id, source_kind="genesis"),
        _person(event_candidate, current_tick=current_tick, region_id=region_id, source_event_id=uuid4()),
    ]
    organization = OrganizationRecruitmentSnapshot(
        organization_id=organization_id,
        kind="polity",
        status="active",
        region_id=region_id,
        governance_revision=2,
        member_ids=(),
        authority_tags=("authority.membership_recruitment",),
        declared_charter_clause_ids=("charter.membership.local_service",),
        policy=_policy(region_id, capacity=1),
    )
    first = plan_membership_recruitment(_review(organizations=[organization], people=people, year_index=year_index))
    second = plan_membership_recruitment(_review(organizations=[organization], people=list(reversed(people)), year_index=year_index))

    assert first == second
    selected = first.enrollments[0]
    source_person = next(person for person in people if person.person_id == selected.member_id)
    assert selected.lineage == source_person.lineage
    assert (selected.lineage.source_kind == "genesis") == (source_person.person_id == genesis_candidate)


@pytest.mark.parametrize(
    "status,authority_tags,clauses,capacity,expected_reason",
    [
        ("dead", ("authority.membership_recruitment",), ("charter.membership.local_service",), 4, "organization_inactive"),
        ("active", (), ("charter.membership.local_service",), 4, "policy_not_authorized"),
        ("active", ("authority.membership_recruitment",), (), 4, "policy_not_authorized"),
        ("active", ("authority.membership_recruitment",), ("charter.membership.local_service",), 1, "capacity_full"),
    ],
)
def test_policy_authority_activity_and_capacity_fail_closed(
    status, authority_tags, clauses, capacity, expected_reason,
):
    region_id, organization_id, incumbent = uuid4(), uuid4(), uuid4()
    current_tick = 9 * YEAR_SECONDS
    person = _person(incumbent, current_tick=current_tick, region_id=region_id, identity_class="original_key_person")
    organization = OrganizationRecruitmentSnapshot(
        organization_id=organization_id,
        kind="organization",
        status=status,
        region_id=region_id,
        governance_revision=3,
        member_ids=(incumbent,),
        authority_tags=authority_tags,
        declared_charter_clause_ids=clauses,
        policy=_policy(region_id, capacity=capacity),
    )

    plan = plan_membership_recruitment(_review(organizations=[organization], people=[person], year_index=9))
    assert plan.enrollments == ()
    assert plan.organizations[0].reason == expected_reason
    assert plan.organizations[0].active_members_after == plan.organizations[0].active_members_before


def test_natural_mode_is_represented_by_absent_policy_and_no_candidates_do_not_revive_org():
    region_id, organization_id, old_member = uuid4(), uuid4(), uuid4()
    current_tick = 6 * YEAR_SECONDS
    person = _person(old_member, current_tick=current_tick, region_id=region_id,
                     identity_class="original_key_person", status="dead")
    natural_org = OrganizationRecruitmentSnapshot(
        organization_id=organization_id,
        kind="organization",
        status="active",
        region_id=region_id,
        governance_revision=3,
        member_ids=(old_member,),
        policy=None,
    )
    natural = plan_membership_recruitment(_review(organizations=[natural_org], people=[person], year_index=6))
    assert natural.enrollments == ()
    assert natural.organizations[0].reason == "policy_missing"

    policy_org = natural_org.model_copy(update={
        "authority_tags": ("authority.membership_recruitment",),
        "declared_charter_clause_ids": ("charter.membership.local_service",),
        "policy": _policy(region_id),
    })
    sustained = plan_membership_recruitment(_review(organizations=[policy_org], people=[person], year_index=6))
    assert sustained.enrollments == ()
    assert sustained.organizations[0].reason == "no_eligible_candidates"
    assert sustained.organizations[0].active_members_after == 0


def test_lineage_never_accepts_fabricated_or_ambiguous_source_ids():
    group_id = uuid4()
    with pytest.raises(ValidationError, match="genesis lineage"):
        CandidatePopulationLineage(
            source_kind="genesis",
            population_group_id=group_id,
            pack_id="modern_city",
            pack_version="1.8.0",
            source_event_id=uuid4(),
        )
    with pytest.raises(ValidationError, match="actual event ID and type"):
        CandidatePopulationLineage(
            source_kind="population_event",
            population_group_id=group_id,
            source_event_type="core.renewable_resource_harvested.v1",
        )


def test_review_is_only_valid_at_exact_annual_world_boundary():
    with pytest.raises(ValidationError, match="exact registered world-year boundary"):
        MembershipRecruitmentReview(
            year_index=2,
            current_tick=2 * YEAR_SECONDS + 1,
            day_seconds=86_400,
            year_days=360,
            organizations=(),
            people=(),
        )
