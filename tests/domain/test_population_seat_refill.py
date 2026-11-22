from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import (
    GovernanceComponent,
    HealthComponent,
    InventoryComponent,
    LifeComponent,
    MembershipRecruitmentPolicy,
    PlaceComponent,
    PopulationAgeCohort,
    PopulationAgeRate,
    PopulationComponent,
    PopulationOccupationProfile,
    PopulationSeatPolicy,
    RenewableHarvestPolicy,
    RenewableHarvestRecipient,
    ScheduledEventRecord,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    OrganizationMembershipReviewedEvent,
    RenewableResourceHarvestedEvent,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.population_seats import (
    _build_recruitment_pool_tracker,
    _stable_recruitment_pool_matching,
    plan_population_seat_promotions,
    select_population_occupation_role,
)
from world_engine.domain.organization_recruitment import (
    project_membership_recruitment_facts,
    project_provisional_population_candidate,
)
from world_engine.domain.reducers import apply_event, ledger_postings
from world_engine.rules import RuleContext, RuleEngine


PERIOD_TICKS = 30 * 86_400


def _fixture(
    *, with_seat_policy: bool = True, include_unrostered_npc: bool = True,
    membership_recruitment_enabled: bool = False, anonymous_count: int = 2,
    occupation_profile: PopulationOccupationProfile | None = None,
    maintain_civic_occupations: bool = False, maintain_civic_recruitment_pool: bool = False,
    authorized_recruitment: bool = False,
):
    region_id, home_id, group_id, player_id, dead_id, schedule_id = (uuid4() for _ in range(6))
    custom_id = uuid4()
    food_source_id, water_source_id = uuid4(), uuid4()
    player_food, player_water, dead_food, dead_water = (uuid4() for _ in range(4))
    food_policy = RenewableHarvestPolicy(
        policy_id="monthly.food",
        resource_id="core.food",
        source_account_id=food_source_id,
        period_ticks=PERIOD_TICKS,
        per_period=Decimal("2"),
        maximum_total=Decimal("20"),
        recipients=sorted(
            [
                RenewableHarvestRecipient(entity_id=player_id, account_id=player_food, per_period=Decimal("1")),
                RenewableHarvestRecipient(entity_id=dead_id, account_id=dead_food, per_period=Decimal("1")),
            ],
            key=lambda item: str(item.entity_id),
        ),
    )
    water_policy = RenewableHarvestPolicy(
        policy_id="monthly.water",
        resource_id="core.water",
        source_account_id=water_source_id,
        period_ticks=PERIOD_TICKS,
        per_period=Decimal("2"),
        maximum_total=Decimal("20"),
        recipients=sorted(
            [
                RenewableHarvestRecipient(entity_id=player_id, account_id=player_water, per_period=Decimal("1")),
                RenewableHarvestRecipient(entity_id=dead_id, account_id=dead_water, per_period=Decimal("1")),
            ],
            key=lambda item: str(item.entity_id),
        ),
    )
    region = WorldEntity(
        id=region_id,
        kind="place",
        name="North region",
        components={"place": PlaceComponent(place_type="region", renewable_harvests=[food_policy, water_policy])},
    )
    home = WorldEntity(
        id=home_id,
        kind="place",
        name="North settlement",
        location_id=region_id,
        components={"place": PlaceComponent(place_type="settlement", region_id=region_id)},
    )
    cohorts = [
        PopulationAgeCohort(age_years=age, headcount=anonymous_count if age == 20 else 0)
        for age in range(201)
    ]
    rates = [PopulationAgeRate(age_years=age) for age in range(201)]
    group = WorldEntity(
        id=group_id,
        kind="group",
        name="North residents",
        location_id=region_id,
        components={
            "population": PopulationComponent(
                species_id="core.human",
                headcount=anonymous_count + (2 if include_unrostered_npc else 1),
                unmodeled_headcount=anonymous_count,
                age_rates=rates,
                age_cohorts=cohorts,
                last_advanced_year=0,
                seat_policy=(
                    PopulationSeatPolicy(
                        non_player_seat_capacity=1,
                        home_place_ids=[home_id],
                        extra_given_names=["Tala", "Rin"],
                        extra_family_names=["North", "Vale"],
                        extra_roles=["artisan", "farmer"],
                        occupation_profile=occupation_profile,
                        maintain_civic_occupations=maintain_civic_occupations,
                        maintain_civic_recruitment_pool=maintain_civic_recruitment_pool,
                        lifespan_min_years=82,
                        lifespan_max_years=100,
                        seed_id=uuid4(),
                    )
                    if with_seat_policy
                    else None
                ),
            )
        },
    )

    def character(entity_id, name, status, *, player=False):
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            location_id=home_id,
            status=status,
            components={
                "life": LifeComponent(
                    birth_tick=0,
                    lifespan_ticks=90 * 360 * 86_400,
                    population_group_id=group_id,
                    life_stage="adult",
                ),
                "health": HealthComponent(current=0 if status == "dead" else 100, maximum=100),
                "inventory": InventoryComponent(),
            },
        )

    player = character(player_id, "Ari", "active", player=True)
    dead = character(dead_id, "Mira", "dead")
    entities = {item.id: item for item in (region, home, group, player, dead)}
    if include_unrostered_npc:
        entities[custom_id] = character(custom_id, "Custom resident", "active")
    organization_id = None
    if membership_recruitment_enabled:
        organization_id = uuid4()
        entities[organization_id] = WorldEntity(
            id=organization_id,
            kind="organization",
            name="North Guild",
            location_id=region_id,
            components={
                "governance": GovernanceComponent(
                    charter_clause_ids=["charter.local_membership"] if authorized_recruitment else [],
                    authority_tags=["authority.local_membership"] if authorized_recruitment else [],
                    membership_recruitment=[
                        MembershipRecruitmentPolicy(
                            policy_id="membership.local.test",
                            charter_clause_id="charter.local_membership",
                            admission_rule_id="organization.local_role_match.v1",
                            authority_tag="authority.local_membership",
                            region_id=region_id,
                            eligible_roles=["artisan"],
                            active_member_capacity=10,
                            max_recruits_per_period=1,
                            selection_seed_id=uuid4(),
                        )
                    ]
                )
            },
        )
    sources_and_holders = {
        food_source_id: ResourceAccount(
            resource_id="core.food", balance="0", owner_id=region_id,
            account_type="environmental_source", credit_limit="0",
        ),
        water_source_id: ResourceAccount(
            resource_id="core.water", balance="0", owner_id=region_id,
            account_type="environmental_source", credit_limit="0",
        ),
        player_food: ResourceAccount(resource_id="core.food", balance="0", owner_id=player_id),
        player_water: ResourceAccount(resource_id="core.water", balance="0", owner_id=player_id),
        dead_food: ResourceAccount(resource_id="core.food", balance="0", owner_id=dead_id),
        dead_water: ResourceAccount(resource_id="core.water", balance="0", owner_id=dead_id),
    }
    schedule = ScheduledEventRecord(
        id=schedule_id,
        due_tick=PERIOD_TICKS,
        phase="environment",
        handler_id="environmental.harvest",
        handler_version="1.0.0",
        repeat_ticks=PERIOD_TICKS,
        max_runs=3,
    )
    state = WorldState(
        current_tick=PERIOD_TICKS,
        player_id=player_id,
        entities=entities,
        resources=sources_and_holders,
        scheduled_events=[schedule],
        metadata={"resource_roles": {"food_resource_id": "core.food", "water_resource_id": "core.water"}},
    )
    context = RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(), actor_id=player_id, ruleset_version="1.0.0"
    )
    return state, context, {
        "region": region_id,
        "home": home_id,
        "group": group_id,
        "player": player_id,
        "dead": dead_id,
        "custom": custom_id,
        "schedule": schedule_id,
        "food_source": food_source_id,
        "water_source": water_source_id,
        "player_food": player_food,
        "player_water": player_water,
        "organization": organization_id,
    }


def _harvest(state, context):
    return RuleEngine().resolve_scheduled(state, state.scheduled_events[0], context)


def _apply_all(state, resolution):
    for event in resolution.events:
        state = apply_event(state, event)
    return state


@pytest.mark.parametrize("authorized", [False, True])
def test_civic_seat_preference_obeys_the_local_charter_and_keeps_harvest_and_membership_separate(authorized):
    profile = PopulationOccupationProfile(
        role_weights={"artisan": 1, "farmer": 1000},
        role_skill_levels={"artisan": {"skill.repair": "72"}},
    )
    state, context, ids = _fixture(
        membership_recruitment_enabled=True, occupation_profile=profile,
        maintain_civic_occupations=True, authorized_recruitment=authorized,
    )
    group = state.entities[ids["group"]]
    population = PopulationComponent.model_validate(group.components["population"])
    policy = population.seat_policy
    ordinary_role = select_population_occupation_role(
        policy.extra_roles, profile.role_weights, seed_id=policy.seed_id, group_id=group.id,
        period_index=1, serial=0,
    )
    resolution = _harvest(state, context)
    event, = [event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent)]
    promotion, = event.promotions
    assert promotion.character.components["public_role"] == ("artisan" if authorized else ordinary_role)
    if authorized:
        assert promotion.character.components["skills"]["skills"]["skill.repair"]["level"] == "72"
    result = _apply_all(state, resolution)
    assert result.entities[ids["group"]].components["population"]["unmodeled_headcount"] == 1
    assert result.entities[ids["organization"]].components["governance"]["members"] == []
    assert len(promotion.holder_accounts) == 2


def test_monthly_harvest_refills_only_a_vacant_named_seat_from_anonymous_population():
    state, context, ids = _fixture()
    resolution = _harvest(state, context)
    assert resolution.outcome.status == "succeeded"
    event = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    assert len(event.promotions) == 1
    promotion = event.promotions[0]
    assert promotion.cohort_id == ids["group"]
    assert promotion.vacant_member_id == ids["dead"]
    assert promotion.character.id != ids["player"]
    assert promotion.character.id != ids["custom"]
    assert promotion.character.components["life"]["population_group_id"] == str(ids["group"])
    assert promotion.character.components["life"]["lifespan_ticks"] <= 100 * 360 * 86_400
    assert promotion.character.components["life"]["lifespan_ticks"] >= 82 * 360 * 86_400

    # Replaying a forged character record cannot bypass the deterministic
    # naming, age-bucket, roster, or ration policy.
    forged_promotion = promotion.model_copy(
        update={"character": promotion.character.model_copy(update={"name": "Model-invented resident"})}
    )
    with pytest.raises(InvalidEvent, match="promotions must match"):
        apply_event(state, event.model_copy(update={"promotions": [forged_promotion]}))

    result = _apply_all(state, resolution)
    population = result.entities[ids["group"]].components["population"]
    assert population["headcount"] == 4
    assert population["unmodeled_headcount"] == 1
    assert population["seat_policy"]["promotion_serial"] == 1
    assert population["age_cohorts"][20]["headcount"] == 1
    assert result.entities[ids["player"]].status == "active"
    assert result.entities[ids["custom"]].status == "active"
    place = PlaceComponent.model_validate(result.entities[ids["region"]].components["place"])
    food = next(item for item in place.renewable_harvests if item.resource_id == "core.food")
    water = next(item for item in place.renewable_harvests if item.resource_id == "core.water")
    assert {row.entity_id for row in food.recipients} == {ids["player"], promotion.character.id}
    assert {row.entity_id for row in water.recipients} == {ids["player"], promotion.character.id}
    assert result.resources[ids["player_food"]].balance == Decimal("1")
    assert result.resources[ids["player_water"]].balance == Decimal("1")
    for account in promotion.holder_accounts:
        assert result.resources[account.account_id].balance == Decimal("1")
    assert result.resources[ids["food_source"]].balance == Decimal("-2")
    assert result.resources[ids["water_source"]].balance == Decimal("-2")
    assert sum((posting.delta for posting in ledger_postings(event) if posting.resource_id == "core.food"), Decimal("0")) == 0
    assert sum((posting.delta for posting in ledger_postings(event) if posting.resource_id == "core.water"), Decimal("0")) == 0


def test_sustained_harvest_lineage_survives_inventory_reconciliation():
    state, context, _ids = _fixture(membership_recruitment_enabled=True)
    resolution = _harvest(state, context)
    event = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    assert len(event.promotions) == 1

    result = _apply_all(state, resolution)
    life = result.entities[event.promotions[0].character.id].components["life"]
    assert life["population_origin"] == {
        "source_event_id": str(event.event_id),
        "source_event_type": event.type,
        "population_group_id": str(event.promotions[0].cohort_id),
    }


def test_missing_seat_policy_keeps_ordinary_harvest_without_named_refill():
    state, context, ids = _fixture(with_seat_policy=False)
    resolution = _harvest(state, context)
    assert resolution.outcome.status == "succeeded"
    event = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    assert event.promotions == []
    result = _apply_all(state, resolution)
    assert result.resources[ids["player_food"]].balance == Decimal("1")
    assert result.entities[ids["dead"]].status == "dead"
    assert result.entities[ids["group"]].components["population"]["unmodeled_headcount"] == 2


def test_empty_eligible_cohorts_do_not_mint_a_named_character():
    state, context, ids = _fixture(anonymous_count=0)
    resolution = _harvest(state, context)
    assert resolution.outcome.status == "succeeded"
    event = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    assert event.promotions == []
    result = _apply_all(state, resolution)
    assert set(result.entities) == set(state.entities)
    assert result.resources[ids["player_food"]].balance == Decimal("1")
    assert result.entities[ids["group"]].components["population"]["headcount"] == 2
    assert result.entities[ids["group"]].components["population"]["unmodeled_headcount"] == 0


def test_occupation_profile_is_optional_and_role_selection_keeps_legacy_hash():
    seed_id, group_id = uuid4(), uuid4()
    roles = ["care worker", "farmer", "nurse"]
    selection_inputs = {
        "seed_id": seed_id,
        "group_id": group_id,
        "period_index": 4,
        "serial": 2,
    }
    policy = PopulationSeatPolicy(
        non_player_seat_capacity=1,
        home_place_ids=[uuid4()],
        extra_given_names=["Tala"],
        extra_family_names=["North"],
        extra_roles=roles,
        lifespan_min_years=82,
        lifespan_max_years=100,
        seed_id=seed_id,
    )
    assert "occupation_profile" not in policy.model_dump(mode="json")

    material = f"role:{seed_id}:{group_id}:4:2".encode("utf-8")
    legacy_index = int.from_bytes(sha256(material).digest(), "big") % len(roles)
    assert select_population_occupation_role(roles, None, **selection_inputs) == roles[legacy_index]
    assert select_population_occupation_role(roles, {}, **selection_inputs) == roles[legacy_index]

    weight_profile = {"nurse": 32}
    changed_serial = next(
        serial
        for serial in range(100)
        if select_population_occupation_role(
            roles, weight_profile, **(selection_inputs | {"serial": serial})
        ) != select_population_occupation_role(
            roles, None, **(selection_inputs | {"serial": serial})
        )
    )
    weighted = select_population_occupation_role(
        roles, weight_profile, **(selection_inputs | {"serial": changed_serial})
    )
    assert weighted != select_population_occupation_role(
        roles, None, **(selection_inputs | {"serial": changed_serial})
    )
    assert weighted in roles
    assert any(
        select_population_occupation_role(
            roles,
            weight_profile,
            **(selection_inputs | {"serial": serial}),
        )
        in {"care worker", "farmer"}
        for serial in range(100)
    )


def test_occupation_profile_bounds_and_authored_role_references():
    with pytest.raises(ValidationError):
        PopulationOccupationProfile(role_weights={"nurse": 0})
    with pytest.raises(ValidationError):
        PopulationOccupationProfile(role_weights={"nurse": True})
    with pytest.raises(ValidationError):
        PopulationOccupationProfile(role_weights={"nurse": 1001})
    with pytest.raises(ValidationError):
        PopulationOccupationProfile(role_skill_levels={"nurse": {"skill.care": "100.1"}})

    state, _context, ids = _fixture()
    group = state.entities[ids["group"]]
    population = PopulationComponent.model_validate(group.components["population"])
    policy = population.seat_policy.model_copy(
        update={"occupation_profile": PopulationOccupationProfile(role_weights={"nurse": 4})}
    )
    with pytest.raises(ValidationError, match="authored roles"):
        PopulationSeatPolicy.model_validate(policy.model_dump(mode="json"))


def test_profiled_refill_uses_role_profile_without_changing_headcount_or_membership():
    profile = PopulationOccupationProfile(
        role_weights={"farmer": 32},
        role_skill_levels={
            "artisan": {"skill.local_knowledge": "18", "skill.care": "21"},
            "farmer": {"skill.local_knowledge": "18", "skill.farming": "44"},
        },
    )
    state, context, ids = _fixture(
        membership_recruitment_enabled=True,
        occupation_profile=profile,
    )
    original_org = state.entities[ids["organization"]]
    original_population = state.entities[ids["group"]].components["population"]
    resolution = _harvest(state, context)
    event = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    assert len(event.promotions) == 1
    promotion = event.promotions[0]
    expected_role = select_population_occupation_role(
        ["artisan", "farmer"],
        profile.role_weights,
        seed_id=PopulationComponent.model_validate(original_population).seat_policy.seed_id,
        group_id=ids["group"],
        period_index=1,
        serial=0,
    )
    assert promotion.character.components["public_role"] == expected_role
    skills = promotion.character.components["skills"]["skills"]
    assert skills["skill.local_knowledge"]["level"] == "18"
    selected_specialty = "skill.care" if expected_role == "artisan" else "skill.farming"
    assert skills[selected_specialty]["level"] == ("21" if expected_role == "artisan" else "44")
    assert "skill.investigation" in skills
    assert "skill.social" in skills

    result = _apply_all(state, resolution)
    resulting_population = result.entities[ids["group"]].components["population"]
    assert resulting_population["headcount"] == original_population["headcount"]
    assert resulting_population["unmodeled_headcount"] == original_population["unmodeled_headcount"] - 1
    assert len(set(result.entities) - set(state.entities)) == 1
    assert result.entities[ids["organization"]].components == original_org.components
    assert not any(isinstance(item, OrganizationMembershipReviewedEvent) for item in resolution.events)


def _competing_care_pool_state():
    state, _context, ids = _fixture(include_unrostered_npc=False, anonymous_count=2)
    group = state.entities[ids["group"]]
    home = state.entities[ids["home"]]
    profile = PopulationOccupationProfile(role_weights={"care worker": 1, "farmer": 1000})
    population = PopulationComponent.model_validate(group.components["population"])

    care_worker_ids = [uuid4(), uuid4()]
    care_workers = [
        WorldEntity(
            id=person_id,
            kind="character",
            name=f"Care Worker {index}",
            location_id=home.id,
            status="active",
            components={
                "life": LifeComponent(
                    birth_tick=state.current_tick - 30 * 360 * 86_400,
                    lifespan_ticks=90 * 360 * 86_400,
                    population_group_id=group.id,
                    life_stage="adult",
                ).model_dump(mode="json"),
                "health": HealthComponent(current=100, maximum=100).model_dump(mode="json"),
                "inventory": InventoryComponent().model_dump(mode="json"),
                "public_role": "care worker",
            },
        )
        for index, person_id in enumerate(care_worker_ids, start=1)
    ]
    second_dead_id = uuid4()
    second_dead = WorldEntity(
        id=second_dead_id,
        kind="character",
        name="Another deceased resident",
        location_id=home.id,
        status="dead",
        components={
            "life": LifeComponent(
                birth_tick=0,
                lifespan_ticks=90 * 360 * 86_400,
                population_group_id=group.id,
                life_stage="adult",
            ).model_dump(mode="json"),
            "health": HealthComponent(current=0, maximum=100).model_dump(mode="json"),
            "inventory": InventoryComponent().model_dump(mode="json"),
        },
    )

    seed_id = next(
        UUID(int=value)
        for value in range(1, 50_000)
        if all(
            select_population_occupation_role(
                ["care worker", "farmer"],
                profile.role_weights,
                seed_id=UUID(int=value),
                group_id=group.id,
                period_index=1,
                serial=serial,
            ) == "farmer"
            for serial in (0, 1)
        )
    )
    old_policy = population.seat_policy
    assert old_policy is not None
    seat_policy = PopulationSeatPolicy.model_validate(
        old_policy.model_dump(mode="python")
        | {
            "non_player_seat_capacity": 4,
            "extra_roles": ["care worker", "farmer"],
            "occupation_profile": profile,
            "maintain_civic_occupations": True,
            "maintain_civic_recruitment_pool": True,
            "seed_id": seed_id,
        }
    )
    population = population.model_copy(
        update={"headcount": population.headcount + 3, "seat_policy": seat_policy}
    )
    updated_group = group.model_copy(
        update={"components": {**group.components, "population": population.model_dump(mode="json")}}
    )

    entities = dict(state.entities)
    entities[group.id] = updated_group
    entities[second_dead.id] = second_dead
    entities.update({person.id: person for person in care_workers})
    organization_ids = [uuid4(), uuid4()]
    organizations = []
    for index, organization_id in enumerate(organization_ids, start=1):
        recruitment = MembershipRecruitmentPolicy(
            policy_id=f"membership.local.care_{index}",
            charter_clause_id="charter.local_care",
            admission_rule_id="organization.local_role_match.v1",
            authority_tag="authority.local_care",
            region_id=ids["region"],
            eligible_roles=["care worker"],
            minimum_age_years=18,
            maximum_age_years=64,
            active_member_capacity=4,
            max_recruits_per_period=1,
            selection_seed_id=uuid4(),
        )
        organization = WorldEntity(
            id=organization_id,
            kind="organization",
            name=f"Local Care Organization {index}",
            location_id=ids["region"],
            components={
                "governance": GovernanceComponent(
                    members=care_worker_ids,
                    charter_clause_ids=["charter.local_care"],
                    authority_tags=["authority.local_care"],
                    membership_recruitment=[recruitment],
                ).model_dump(mode="json")
            },
        )
        entities[organization_id] = organization
        organizations.append(organization)

    region = entities[ids["region"]]
    place = PlaceComponent.model_validate(region.components["place"])
    holder_additions: dict[str, list[RenewableHarvestRecipient]] = {
        "core.food": [],
        "core.water": [],
    }
    new_resources: dict = {}
    for person in [*care_workers, second_dead]:
        for resource_id in ("core.food", "core.water"):
            account_id = uuid4()
            holder_additions[resource_id].append(
                RenewableHarvestRecipient(
                    entity_id=person.id,
                    account_id=account_id,
                    per_period=Decimal("1"),
                )
            )
            new_resources[account_id] = ResourceAccount(
                resource_id=resource_id,
                balance="0",
                owner_id=person.id,
                account_type="holder",
            )
    harvests = []
    for harvest in place.renewable_harvests:
        recipients = sorted(
            [*harvest.recipients, *holder_additions[harvest.resource_id]],
            key=lambda item: str(item.entity_id),
        )
        harvests.append(
            harvest.model_copy(update={"per_period": Decimal("5"), "recipients": recipients})
        )
    updated_region = region.model_copy(
        update={
            "components": {
                **region.components,
                "place": place.model_copy(update={"renewable_harvests": harvests}).model_dump(mode="json"),
            }
        }
    )
    entities[region.id] = updated_region
    resident_groups = {str(person_id): str(group.id) for person_id in care_worker_ids}
    metadata = {
        **state.metadata,
        "pack_id": "modern_city",
        "pack_version": "2.7.0",
        "content_index": {
            "key_npc_ids": [],
            "other_resident_ids": [str(person_id) for person_id in care_worker_ids],
            "other_resident_population_groups": resident_groups,
        },
    }
    resources = {**state.resources, **new_resources}
    return (
        state.model_copy(update={"entities": entities, "resources": resources, "metadata": metadata}),
        {
            **ids,
            "care_workers": care_worker_ids,
            "second_dead": second_dead_id,
            "organizations": [item.id for item in organizations],
        },
    )


def test_opt_in_civic_refill_preserves_a_distinct_candidate_for_each_open_care_policy():
    state, ids = _competing_care_pool_state()
    group = state.entities[ids["group"]]
    population = PopulationComponent.model_validate(group.components["population"])
    policy = population.seat_policy
    assert policy is not None
    opted_in_policy_dump = policy.model_dump(mode="json")
    assert opted_in_policy_dump["maintain_civic_recruitment_pool"] is True
    # The explicit flag is the only semantic difference between the new and legacy planners.
    legacy_policy = policy.model_copy(update={"maintain_civic_recruitment_pool": False})
    legacy_population = population.model_copy(update={"seat_policy": legacy_policy})
    legacy_group = group.model_copy(
        update={"components": {**group.components, "population": legacy_population.model_dump(mode="json")}}
    )
    legacy_entities = dict(state.entities)
    legacy_entities[group.id] = legacy_group
    legacy_state = state.model_copy(update={"entities": legacy_entities})

    planned = plan_population_seat_promotions(state, period_index=1)
    legacy_planned = plan_population_seat_promotions(legacy_state, period_index=1)
    new_roles = [item.character.components["public_role"] for item in planned.promotions]
    old_roles = [item.character.components["public_role"] for item in legacy_planned.promotions]

    assert len(planned.promotions) == 2
    assert new_roles == ["care worker", "care worker"]
    assert old_roles == ["farmer", "farmer"]
    assert [item.character.id for item in planned.promotions] == [
        item.character.id for item in legacy_planned.promotions
    ]
    assert len({item.character.id for item in planned.promotions}) == 2
    assert planned.state.current_tick == state.current_tick
    assert planned.state.entities[ids["player"]].status == "active"
    assert all(
        planned.state.entities[org_id].components == state.entities[org_id].components
        for org_id in ids["organizations"]
    )

    group_population = PopulationComponent.model_validate(
        planned.state.entities[ids["group"]].components["population"]
    )
    final_policy = group_population.seat_policy
    assert final_policy is not None
    tracker = _build_recruitment_pool_tracker(
        project_membership_recruitment_facts(state),
        ids["region"],
        final_policy,
    )
    tracker.add_provisional_candidate(
        project_provisional_population_candidate(state, planned.promotions[0].character)
    )
    assert tracker.priority_roles(20) == frozenset({"care worker"})
    tracker.add_provisional_candidate(
        project_provisional_population_candidate(state, planned.promotions[1].character)
    )
    assert tracker.priority_roles(20) == frozenset()


def test_recruitment_pool_reservation_uses_distinct_candidates_for_overlapping_policies():
    first_policy = ("org-1", "membership.care")
    second_policy = ("org-2", "membership.care")
    shared_candidate, alternate_candidate = uuid4(), uuid4()
    matched = _stable_recruitment_pool_matching(
        {
            first_policy: [shared_candidate, alternate_candidate],
            second_policy: [shared_candidate],
        }
    )
    assert set(matched) == {first_policy, second_policy}
    assert set(matched.values()) == {shared_candidate, alternate_candidate}


def test_recruitment_pool_flag_requires_civic_occupation_authorship_and_is_omitted_when_off():
    state, _context, ids = _fixture()
    population = PopulationComponent.model_validate(state.entities[ids["group"]].components["population"])
    policy = population.seat_policy
    assert policy is not None
    assert "maintain_civic_recruitment_pool" not in policy.model_dump(mode="json")

    with pytest.raises(ValidationError, match="requires civic occupation maintenance"):
        PopulationSeatPolicy.model_validate(
            policy.model_dump(mode="python") | {"maintain_civic_recruitment_pool": True}
        )
    with pytest.raises(ValidationError, match="authored regional role weights"):
        PopulationSeatPolicy.model_validate(
            policy.model_dump(mode="python")
            | {
                "maintain_civic_occupations": True,
                "maintain_civic_recruitment_pool": True,
            }
        )
