"""Deterministic, finite promotion from anonymous age buckets into named seats."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid5

from .components import (
    CapabilitiesComponent,
    GovernanceComponent,
    HealthComponent,
    LifeComponent,
    MembershipRecruitmentPolicy,
    NeedsComponent,
    PersonalityComponent,
    PlaceComponent,
    PopulationComponent,
    PopulationSeatPolicy,
    ProgressionComponent,
    RenewableHarvestPolicy,
    RenewableHarvestRecipient,
    SkillsComponent,
    SkillValue,
    AttributesComponent,
)
from .gameplay_events import PopulationSeatAccount, PopulationSeatPromotion
from .models import ResourceAccount, WorldEntity, WorldState
from .organization_recruitment import (
    MembershipRecruitmentProjection,
    OrganizationRecruitmentSnapshot,
    RecruitmentPersonSnapshot,
    _eligible_candidate,
    _is_alive,
    project_membership_recruitment_facts,
    project_provisional_population_candidate,
)


@dataclass(frozen=True, slots=True)
class PopulationSeatPlan:
    promotions: tuple[PopulationSeatPromotion, ...]
    state: WorldState


@dataclass(frozen=True, slots=True)
class _RecruitmentPoolDemand:
    organization: OrganizationRecruitmentSnapshot
    policy: MembershipRecruitmentPolicy
    authored_roles: frozenset[str]

    @property
    def key(self) -> tuple[str, str]:
        return str(self.organization.organization_id), self.policy.policy_id


@dataclass(slots=True)
class _RecruitmentPoolTracker:
    projection: MembershipRecruitmentProjection
    region_id: UUID
    demands: tuple[_RecruitmentPoolDemand, ...]
    edges: dict[tuple[str, str], list[UUID]]
    candidate_to_demand: dict[UUID, tuple[str, str]]
    demand_to_candidate: dict[tuple[str, str], UUID]

    def priority_roles(self, age_years: int) -> frozenset[str]:
        return frozenset(
            role
            for demand in self.demands
            if demand.key not in self.demand_to_candidate
            and demand.policy.minimum_age_years <= age_years <= demand.policy.maximum_age_years
            for role in demand.authored_roles
        )

    def add_provisional_candidate(self, candidate: RecruitmentPersonSnapshot) -> None:
        for demand in self.demands:
            if _eligible_candidate(
                candidate,
                organization=demand.organization,
                policy=demand.policy,
                member_ids=set(demand.organization.member_ids),
                already_selected=set(),
                player_id=self.projection.player_id,
                current_tick=self.projection.current_tick,
                year_seconds=self.projection.year_seconds,
                allow_provisional_population_promotion=True,
            ):
                ids = self.edges[demand.key]
                if candidate.person_id not in ids:
                    ids.append(candidate.person_id)
                    ids.sort(key=str)
        for demand in self.demands:
            if demand.key not in self.demand_to_candidate:
                self._reserve(demand.key, set(), set())

    def _reserve(
        self,
        demand_key: tuple[str, str],
        seen_demands: set[tuple[str, str]],
        seen_candidates: set[UUID],
    ) -> bool:
        if demand_key in seen_demands:
            return False
        seen_demands.add(demand_key)
        for candidate_id in self.edges[demand_key]:
            if candidate_id in seen_candidates:
                continue
            seen_candidates.add(candidate_id)
            current_demand = self.candidate_to_demand.get(candidate_id)
            if current_demand is None or self._reserve(current_demand, seen_demands, seen_candidates):
                self.candidate_to_demand[candidate_id] = demand_key
                self.demand_to_candidate[demand_key] = candidate_id
                return True
        return False


def select_population_occupation_role(
    roles: Sequence[str],
    role_weights: Mapping[str, int] | None,
    *,
    seed_id: UUID,
    group_id: UUID,
    period_index: int,
    serial: int,
    field: str = "role",
) -> str:
    """Select a role deterministically, preserving the legacy uniform hash by default."""
    if not roles:
        raise ValueError("population occupation role list must not be empty")
    material = f"{field}:{seed_id}:{group_id}:{period_index}:{serial}".encode("utf-8")
    digest = int.from_bytes(sha256(material).digest(), "big")
    if not role_weights:
        return roles[digest % len(roles)]

    total = sum(role_weights.get(role, 1) for role in roles)
    offset = digest % total
    for role in roles:
        weight = role_weights.get(role, 1)
        if offset < weight:
            return role
        offset -= weight
    raise AssertionError("weighted population role offset exceeded its range")


def plan_population_seat_promotions(state: WorldState, period_index: int) -> PopulationSeatPlan:
    """Plan all safely supportable vacant-seat refills for this harvest period.

    This is pure and deterministic. It uses only already-counted anonymous age
    buckets, a finite authored seat roster, and the matching monthly food/water
    policies. With no seat policy or no complete finite ration source, it returns
    the original state and no promotions, leaving ordinary harvest behavior intact.
    """
    food_resource = _registered_resource(state, "food_resource_id")
    water_resource = _registered_resource(state, "water_resource_id")
    if food_resource is None or water_resource is None or food_resource == water_resource:
        return PopulationSeatPlan((), state)

    current_year = state.current_tick // (state.calendar.day_seconds * state.calendar.year_days)
    entities = dict(state.entities)
    resources = dict(state.resources)
    promotions: list[PopulationSeatPromotion] = []
    used_names = {
        entity.name.casefold()
        for entity in state.entities.values()
        if entity.name is not None
    }
    recruitment_projection: MembershipRecruitmentProjection | None = None
    recruitment_pool_trackers: dict[tuple[UUID, tuple[tuple[str, int], ...]], _RecruitmentPoolTracker] = {}
    projected_candidates_by_region: dict[UUID, list[WorldEntity]] = {}
    provisional_candidate_facts: dict[UUID, RecruitmentPersonSnapshot] = {}

    groups = sorted(
        (
            entity for entity in state.entities.values()
            if entity.kind in {"group", "population", "population_group", "cohort"}
            and isinstance(entity.components.get("population"), dict)
        ),
        key=lambda entity: str(entity.id),
    )
    for group in groups:
        try:
            population = PopulationComponent.model_validate(group.components.get("population"))
        except Exception:
            continue
        policy = population.seat_policy
        if policy is None or policy.non_player_seat_capacity <= 0:
            continue
        if (
            not population.age_cohorts
            or population.unmodeled_headcount is None
            or population.last_advanced_year is None
            or population.last_advanced_year != current_year
        ):
            continue
        region_id = group.location_id
        region = entities.get(region_id) if region_id is not None else None
        if region is None or region.kind not in {"place", "location"} or region.status != "active":
            continue
        try:
            place = PlaceComponent.model_validate(region.components.get("place"))
        except Exception:
            continue
        food_policies = [item for item in place.renewable_harvests if item.resource_id == food_resource]
        water_policies = [item for item in place.renewable_harvests if item.resource_id == water_resource]
        if len(food_policies) != 1 or len(water_policies) != 1:
            continue
        food_policy, water_policy = food_policies[0], water_policies[0]
        if (
            food_policy.period_ticks != state.calendar.day_seconds * 30
            or water_policy.period_ticks != state.calendar.day_seconds * 30
            or food_policy.issued_total >= food_policy.maximum_total
            or water_policy.issued_total >= water_policy.maximum_total
            or food_policy.last_issued_period != period_index - 1
            or water_policy.last_issued_period != period_index - 1
            or _valid_harvest_source(state, group.location_id, food_policy) is None
            or _valid_harvest_source(state, group.location_id, water_policy) is None
        ):
            continue
        food_by_member = {item.entity_id: item for item in food_policy.recipients}
        water_by_member = {item.entity_id: item for item in water_policy.recipients}
        if set(food_by_member) != set(water_by_member):
            continue

        seats: list[tuple[WorldEntity, LifeComponent]] = []
        roster_valid = True
        for member_id in sorted(food_by_member, key=str):
            if member_id == state.player_id:
                continue
            member = entities.get(member_id)
            if member is None:
                roster_valid = False
                break
            try:
                life = LifeComponent.model_validate(member.components.get("life"))
            except Exception:
                roster_valid = False
                break
            if life.population_group_id == group.id:
                seats.append((member, life))
        if not roster_valid or len(seats) != policy.non_player_seat_capacity:
            continue
        active_seats = sum(member.status == "active" for member, _life in seats)
        vacancies = [
            (member, life) for member, life in seats
            if member.status in {"dead", "retired"}
        ]
        if active_seats > policy.non_player_seat_capacity or not vacancies:
            continue

        eligible_counts = {
            cohort.age_years: cohort.headcount
            for cohort in population.age_cohorts
            if policy.eligible_min_age_years <= cohort.age_years <= policy.eligible_max_age_years
            and cohort.headcount > 0
        }
        if not eligible_counts:
            continue
        age_cohort_revisions = {item.age_years: item.revision for item in population.age_cohorts}
        year_seconds = state.calendar.day_seconds * state.calendar.year_days
        reference_tick = population.last_advanced_year * year_seconds
        demands = (
            _local_civic_recruitment_demands(state, region.id, policy)
            if policy.maintain_civic_occupations and not policy.maintain_civic_recruitment_pool
            else ()
        )
        local_occupations: list[tuple[str, int]] = []
        if demands:
            for member, life in seats:
                role = member.components.get("public_role")
                if (
                    member.status == "active" and isinstance(role, str)
                    and (life.is_immortal or life.lifespan_ticks is None
                         or life.birth_tick + life.lifespan_ticks > state.current_tick)
                    and member.location_id is not None
                    and _place_is_within_group(state, member.location_id, region.id)
                ):
                    local_occupations.append((role.casefold(), (state.current_tick - life.birth_tick) // year_seconds))
        pool_tracker: _RecruitmentPoolTracker | None = None
        if policy.maintain_civic_recruitment_pool:
            if recruitment_projection is None:
                recruitment_projection = project_membership_recruitment_facts(state)
            profile_key = tuple(sorted(policy.occupation_profile.role_weights.items()))
            tracker_key = (region.id, profile_key)
            pool_tracker = recruitment_pool_trackers.get(tracker_key)
            if pool_tracker is None:
                pool_tracker = _build_recruitment_pool_tracker(
                    recruitment_projection,
                    region.id,
                    policy,
                )
                recruitment_pool_trackers[tracker_key] = pool_tracker
                for prior_candidate in projected_candidates_by_region.get(region.id, ()):
                    candidate_facts = provisional_candidate_facts.get(prior_candidate.id)
                    if candidate_facts is None:
                        candidate_facts = project_provisional_population_candidate(state, prior_candidate)
                        provisional_candidate_facts[prior_candidate.id] = candidate_facts
                    pool_tracker.add_provisional_candidate(candidate_facts)
        group_promotions: list[PopulationSeatPromotion] = []
        for vacant_member, vacant_life in sorted(vacancies, key=lambda pair: str(pair[0].id)):
            if active_seats + len(group_promotions) >= policy.non_player_seat_capacity:
                break
            age_years = _choose_age(
                eligible_counts,
                policy=policy,
                group_id=group.id,
                period_index=period_index,
                vacant_member_id=vacant_member.id,
                serial=policy.promotion_serial + len(group_promotions),
            )
            if age_years is None:
                break
            serial = policy.promotion_serial + len(group_promotions)
            if serial >= 2**63 - 1:
                break
            new_id = uuid5(policy.seed_id, f"population-seat:{group.id}:{vacant_member.id}:{serial}")
            if new_id in entities:
                # A prior committed replacement owns this deterministic id; a
                # different vacancy or serial is needed before another draw.
                break
            account_records = [
                PopulationSeatAccount(
                    resource_id=resource_id,
                    account_id=uuid5(new_id, f"holder:{resource_id}"),
                )
                for resource_id in sorted((food_resource, water_resource))
            ]
            home_id = _choose_home(policy, group.id, period_index, serial)
            home = entities.get(home_id)
            if (
                home is None
                or home.kind not in {"place", "location"}
                or home.status != "active"
                or not _place_is_within_group(state, home_id, region.id)
            ):
                continue
            if policy.maintain_civic_recruitment_pool:
                prospective_birth_tick = reference_tick - age_years * year_seconds
                prospective_age_years = (state.current_tick - prospective_birth_tick) // year_seconds
                preferred_roles = (
                    pool_tracker.priority_roles(prospective_age_years)
                    if pool_tracker is not None else frozenset()
                )
            else:
                preferred_roles = next(
                    (roles for minimum_age, maximum_age, roles in demands
                     if minimum_age <= age_years <= maximum_age
                     and not any(role in roles and minimum_age <= age <= maximum_age
                                 for role, age in local_occupations)),
                    frozenset(),
                )
            character = _build_character(
                state,
                group=group,
                population=population,
                policy=policy,
                entity_id=new_id,
                home=home,
                age_years=age_years,
                reference_tick=reference_tick,
                year_seconds=year_seconds,
                period_index=period_index,
                serial=serial,
                used_names=used_names,
                preferred_roles=preferred_roles,
            )
            promotion = PopulationSeatPromotion(
                cohort_id=group.id,
                expected_population_revision=population.revision,
                expected_policy_revision=policy.revision,
                vacant_member_id=vacant_member.id,
                expected_vacant_life_revision=vacant_life.revision,
                age_years=age_years,
                expected_age_cohort_revision=age_cohort_revisions[age_years],
                promotion_serial=serial,
                character=character,
                holder_accounts=account_records,
            )
            group_promotions.append(promotion)
            if demands:
                local_occupations.append((character.components["public_role"].casefold(), age_years))
            used_names.add((character.name or "").casefold())
            eligible_counts[age_years] -= 1
            entities[character.id] = character
            projected_candidates_by_region.setdefault(region.id, []).append(character)
            matching_trackers = [
                tracker
                for tracker in recruitment_pool_trackers.values()
                if tracker.region_id == region.id
            ]
            if matching_trackers:
                candidate_facts = project_provisional_population_candidate(state, character)
                provisional_candidate_facts[character.id] = candidate_facts
                for tracker in matching_trackers:
                    tracker.add_provisional_candidate(candidate_facts)
            for account_record in account_records:
                resources[account_record.account_id] = ResourceAccount(
                    resource_id=account_record.resource_id,
                    balance="0",
                    owner_id=character.id,
                    account_type="holder",
                )

            account_by_resource = {item.resource_id: item for item in account_records}
            for policy_id, old_member, replacement_account in (
                (food_policy.policy_id, vacant_member.id, account_by_resource[food_resource]),
                (water_policy.policy_id, vacant_member.id, account_by_resource[water_resource]),
            ):
                # Preserve the per-seat amount and the policy revision here;
                # the combined harvest commit advances the policy and place CAS
                # exactly once after it issues this period's ration.
                current_region = entities.get(region.id, region)
                current_place = PlaceComponent.model_validate(current_region.components.get("place"))
                current_policies = list(current_place.renewable_harvests)
                policy_index = next(index for index, row in enumerate(current_policies) if row.policy_id == policy_id)
                current_policy = current_policies[policy_index]
                old_recipient = next(row for row in current_policy.recipients if row.entity_id == old_member)
                recipients = [
                    row for row in current_policy.recipients if row.entity_id != old_member
                ]
                recipients.append(
                    RenewableHarvestRecipient(
                        entity_id=character.id,
                        account_id=replacement_account.account_id,
                        per_period=old_recipient.per_period,
                    )
                )
                recipients.sort(key=lambda row: str(row.entity_id))
                current_policies[policy_index] = current_policy.model_copy(update={"recipients": recipients})
                next_place = current_place.model_copy(update={"renewable_harvests": current_policies})
                entities[region.id] = current_region.model_copy(
                    update={"components": {**current_region.components, "place": next_place.model_dump(mode="json")}}
                )

        if not group_promotions:
            continue
        updated_cohorts = list(population.age_cohorts)
        by_age = {item.age_years: index for index, item in enumerate(updated_cohorts)}
        for age_years in sorted({item.age_years for item in group_promotions}):
            index = by_age[age_years]
            old = updated_cohorts[index]
            number = sum(item.age_years == age_years for item in group_promotions)
            updated_cohorts[index] = old.model_copy(
                update={"headcount": old.headcount - number, "revision": old.revision + 1}
            )
        updated_policy = policy.model_copy(
            update={
                "promotion_serial": policy.promotion_serial + len(group_promotions),
                "revision": policy.revision + 1,
            }
        )
        updated_population = population.model_copy(
            update={
                "unmodeled_headcount": population.unmodeled_headcount - len(group_promotions),
                "age_cohorts": updated_cohorts,
                "seat_policy": updated_policy,
                "revision": population.revision + 1,
            }
        )
        entities[group.id] = group.model_copy(
            update={"components": {**group.components, "population": updated_population.model_dump(mode="json")}}
        )
        promotions.extend(group_promotions)

    promotions.sort(key=lambda item: (str(item.cohort_id), str(item.vacant_member_id)))
    if not promotions:
        return PopulationSeatPlan((), state)
    projected = state.model_copy(update={"entities": entities, "resources": resources})
    return PopulationSeatPlan(tuple(promotions), projected)


def _local_civic_recruitment_demands(
    state: WorldState, region_id: UUID, policy: PopulationSeatPolicy,
) -> tuple[tuple[int, int, frozenset[str]], ...]:
    assert policy.occupation_profile is not None
    authored = {role.casefold() for role in policy.occupation_profile.role_weights}
    demands = []
    for entity in state.entities.values():
        raw = entity.components.get("governance")
        if (
            entity.kind not in {"organization", "faction", "company"} or entity.status != "active"
            or entity.location_id != region_id or not isinstance(raw, dict)
        ):
            continue
        governance = GovernanceComponent.model_validate(raw)
        active_members = sum(
            member is not None and member.status == "active"
            for member in (state.entities.get(member_id) for member_id in governance.members)
        )
        for recruitment in governance.membership_recruitment:
            roles = authored.intersection(role.casefold() for role in recruitment.eligible_roles)
            if (
                roles and recruitment.region_id == region_id
                and recruitment.charter_clause_id in governance.charter_clause_ids
                and recruitment.authority_tag in governance.authority_tags
            ):
                demand = (recruitment.minimum_age_years, recruitment.maximum_age_years, frozenset(roles))
                demands.append((active_members, str(entity.id), recruitment.policy_id, demand))
    return tuple(entry[3] for entry in sorted(demands, key=lambda entry: entry[:3]))


def _build_recruitment_pool_tracker(
    projection: MembershipRecruitmentProjection,
    region_id: UUID,
    policy: PopulationSeatPolicy,
) -> _RecruitmentPoolTracker:
    assert policy.occupation_profile is not None
    authored_roles = {
        role.casefold(): role
        for role in policy.occupation_profile.role_weights
    }
    known_people = projection.people_by_id
    demands: list[_RecruitmentPoolDemand] = []
    for organization in projection.organizations:
        recruitment = organization.policy
        if (
            organization.kind not in {"organization", "faction", "company"}
            or organization.status != "active"
            or organization.region_id != region_id
            or recruitment is None
            or recruitment.region_id != region_id
            or recruitment.charter_clause_id not in organization.declared_charter_clause_ids
            or recruitment.authority_tag not in organization.authority_tags
            or not set(organization.member_ids).issubset(known_people)
            or any(
                office.holder_id is not None and office.holder_id not in known_people
                for office in organization.offices
            )
        ):
            continue
        active_members = sum(
            _is_alive(known_people[member_id], projection.current_tick)
            for member_id in organization.member_ids
        )
        matching_roles = frozenset(
            authored_roles[role.casefold()]
            for role in recruitment.eligible_roles
            if role.casefold() in authored_roles
        )
        if not matching_roles or active_members >= recruitment.active_member_capacity:
            continue
        demands.append(
            _RecruitmentPoolDemand(
                organization=organization,
                policy=recruitment,
                authored_roles=matching_roles,
            )
        )
    ordered_demands = tuple(sorted(demands, key=lambda item: item.key))
    regional_candidates = projection.people_by_population_region.get(region_id, ())
    edges: dict[tuple[str, str], list[UUID]] = {}
    for demand in ordered_demands:
        member_ids = set(demand.organization.member_ids)
        edges[demand.key] = [
            person.person_id
            for person in regional_candidates
            if _eligible_candidate(
                person,
                organization=demand.organization,
                policy=demand.policy,
                member_ids=member_ids,
                already_selected=set(),
                player_id=projection.player_id,
                current_tick=projection.current_tick,
                year_seconds=projection.year_seconds,
            )
        ]
    demand_to_candidate = _stable_recruitment_pool_matching(edges)
    candidate_to_demand = {
        candidate_id: demand_key
        for demand_key, candidate_id in demand_to_candidate.items()
    }
    return _RecruitmentPoolTracker(
        projection=projection,
        region_id=region_id,
        demands=ordered_demands,
        edges=edges,
        candidate_to_demand=candidate_to_demand,
        demand_to_candidate=demand_to_candidate,
    )


def _stable_recruitment_pool_matching(
    edges: Mapping[tuple[str, str], Sequence[UUID]],
) -> dict[tuple[str, str], UUID]:
    """Build one deterministic maximum-cardinality reservation per open policy."""
    candidate_to_demand: dict[UUID, tuple[str, str]] = {}

    def reserve(
        demand_key: tuple[str, str],
        seen_demands: set[tuple[str, str]],
        seen_candidates: set[UUID],
    ) -> bool:
        if demand_key in seen_demands:
            return False
        seen_demands.add(demand_key)
        for candidate_id in sorted(edges.get(demand_key, ()), key=str):
            if candidate_id in seen_candidates:
                continue
            seen_candidates.add(candidate_id)
            current_demand = candidate_to_demand.get(candidate_id)
            if current_demand is None or reserve(current_demand, seen_demands, seen_candidates):
                candidate_to_demand[candidate_id] = demand_key
                return True
        return False

    for demand_key in sorted(edges):
        reserve(demand_key, set(), set())
    return {
        demand_key: candidate_id
        for candidate_id, demand_key in candidate_to_demand.items()
    }


def _registered_resource(state: WorldState, key: str) -> str | None:
    roles = state.metadata.get("resource_roles")
    value = roles.get(key) if isinstance(roles, dict) else None
    return value if isinstance(value, str) else None


def _valid_harvest_source(state: WorldState, region_id: UUID, policy: RenewableHarvestPolicy) -> ResourceAccount | None:
    source = state.resources.get(policy.source_account_id)
    if (
        source is None
        or source.account_type != "environmental_source"
        or source.owner_id != region_id
        or source.resource_id != policy.resource_id
        or source.credit_limit is None
        or source.balance.copy_negate() != policy.issued_total
        or source.credit_limit != policy.issued_total
    ):
        return None
    return source


def _choose_age(
    eligible_counts: dict[int, int],
    *,
    policy: PopulationSeatPolicy,
    group_id: UUID,
    period_index: int,
    vacant_member_id: UUID,
    serial: int,
) -> int | None:
    total = sum(eligible_counts.values())
    if total <= 0:
        return None
    material = f"age:{policy.seed_id}:{group_id}:{period_index}:{vacant_member_id}:{serial}".encode("utf-8")
    offset = int.from_bytes(sha256(material).digest(), "big") % total
    for age_years in sorted(eligible_counts):
        count = eligible_counts[age_years]
        if offset < count:
            return age_years
        offset -= count
    return None


def _choose_home(policy: PopulationSeatPolicy, group_id: UUID, period_index: int, serial: int) -> UUID:
    material = f"home:{policy.seed_id}:{group_id}:{period_index}:{serial}".encode("utf-8")
    index = int.from_bytes(sha256(material).digest(), "big") % len(policy.home_place_ids)
    return policy.home_place_ids[index]


def _build_character(
    state: WorldState,
    *,
    group: WorldEntity,
    population: PopulationComponent,
    policy: PopulationSeatPolicy,
    entity_id: UUID,
    home: WorldEntity,
    age_years: int,
    reference_tick: int,
    year_seconds: int,
    period_index: int,
    serial: int,
    used_names: set[str],
    preferred_roles: frozenset[str] = frozenset(),
) -> WorldEntity:
    def pick(values: list[str], field: str) -> str:
        material = f"{field}:{policy.seed_id}:{group.id}:{period_index}:{serial}".encode("utf-8")
        return values[int.from_bytes(sha256(material).digest(), "big") % len(values)]

    given = pick(policy.extra_given_names, "given")
    family = pick(policy.extra_family_names, "family")
    occupation_profile = policy.occupation_profile
    roles = policy.extra_roles
    if preferred_roles and occupation_profile is not None:
        roles = [role for role in roles
                 if role in occupation_profile.role_weights and role.casefold() in preferred_roles]
    role = select_population_occupation_role(
        roles,
        occupation_profile.role_weights if occupation_profile is not None else None,
        seed_id=policy.seed_id,
        group_id=group.id,
        period_index=period_index,
        serial=serial,
    )
    base_name = f"{given} {family}"
    name = base_name
    collision = 0
    while name.casefold() in used_names:
        collision += 1
        name = f"{base_name} ({policy.seed_id.hex[:8]}-{serial + collision})"
    lifespan_material = f"lifespan:{policy.seed_id}:{group.id}:{period_index}:{serial}".encode("utf-8")
    lifespan_range = policy.lifespan_max_years - policy.lifespan_min_years + 1
    lifespan_years = policy.lifespan_min_years + int.from_bytes(sha256(lifespan_material).digest(), "big") % lifespan_range
    birth_tick = reference_tick - age_years * year_seconds
    if (
        birth_tick < -(2**63)
        or birth_tick > 2**63 - 1
        or lifespan_years * year_seconds > 2**63 - 1
        or birth_tick + lifespan_years * year_seconds > 2**63 - 1
    ):
        raise ValueError("named population life data exceeds the signed 64-bit world clock")
    region = state.entities.get(group.location_id) if group.location_id is not None else None
    region_name = region.name if region is not None and region.name else "the region"
    public_description = f"{name} is a {role} from {home.name or region_name}, drawn from the regional population."
    skills = SkillsComponent.model_validate({"skills": {
        "skill.local_knowledge": {"level": "10"},
        "skill.investigation": {"level": "5"},
        "skill.social": {"level": "5"},
    }})
    if occupation_profile is not None:
        role_skill_levels = occupation_profile.role_skill_levels.get(role, {})
        if role_skill_levels:
            merged_skills = dict(skills.skills)
            for skill_id, level in role_skill_levels.items():
                existing = merged_skills.get(skill_id, SkillValue())
                merged_skills[skill_id] = SkillValue(
                    level=level,
                    experience=existing.experience,
                    trained_ticks=existing.trained_ticks,
                    last_practiced_tick=existing.last_practiced_tick,
                )
            skills = SkillsComponent(skills=merged_skills)
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=home.id,
        status="active",
        components={
            "life": LifeComponent(
                species_id=population.species_id,
                birth_tick=birth_tick,
                lifespan_ticks=lifespan_years * year_seconds,
                maturity_ticks=18 * year_seconds,
                population_group_id=group.id,
                is_immortal=False,
                life_stage="adult" if age_years < 65 else "elder",
            ).model_dump(mode="json"),
            "health": HealthComponent(current=100, maximum=100).model_dump(mode="json"),
            "needs": NeedsComponent.model_validate({
                "food": {"value": 100, "desired": 80, "critical_below": 20},
                "water": {"value": 100, "desired": 80, "critical_below": 20},
                "rest": {"value": 75, "desired": 80, "critical_below": 20},
                "shelter": {"value": 70, "desired": 80, "critical_below": 20},
                "safety": {"value": 70, "desired": 80, "critical_below": 20},
                "health": {"value": 100, "desired": 80, "critical_below": 20},
                "social": {"value": 60, "desired": 80, "critical_below": 20},
                "belonging": {"value": 60, "desired": 80, "critical_below": 20},
            }).model_dump(mode="json"),
            "skills": skills.model_dump(mode="json"),
            "personality": PersonalityComponent().model_dump(mode="json"),
            "attributes": AttributesComponent(values={
                "attribute.reliability": "50",
                "attribute.practical_judgment": "50",
                "attribute.social_influence": "50",
                "attribute.physical_endurance": "50",
            }).model_dump(mode="json"),
            "capabilities": CapabilitiesComponent(grants=[], restrictions=[]).model_dump(mode="json"),
            "progression": ProgressionComponent(experience=0, level=1, milestones=[]).model_dump(mode="json"),
            "inventory": {"lots": []},
            "known_places": [str(home.id), str(group.location_id)] if group.location_id is not None else [str(home.id)],
            "hidden": False,
            "public_description": public_description,
            "public_role": role,
        },
    )


def _place_is_within_group(state: WorldState, place_id: UUID, region_id: UUID) -> bool:
    current_id: UUID | None = place_id
    seen: set[UUID] = set()
    while current_id is not None and current_id not in seen:
        if current_id == region_id:
            return True
        seen.add(current_id)
        entity = state.entities.get(current_id)
        if entity is None:
            return False
        raw_place = entity.components.get("place")
        next_id: UUID | None = None
        if raw_place is not None:
            try:
                place = PlaceComponent.model_validate(raw_place)
            except Exception:
                return False
            if place.region_id == region_id:
                return True
            next_id = place.parent_id or place.region_id
        if next_id is None:
            parent = entity.components.get("parent_id")
            try:
                next_id = UUID(str(parent)) if parent is not None else None
            except ValueError:
                return False
        current_id = next_id
    return False
