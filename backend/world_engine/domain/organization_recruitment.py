"""Pure, finite planning for scheduled organization membership recruitment.

This is deliberately separate from the event registry, reducers, and catalog
builder until those shared boundaries can be changed together. It consumes
host-extracted organization/person snapshots; it reads neither the repository nor
event tables and never creates entities or mutates a world.

Catalog strategy for the follow-up integration:

* Content 1.8.0 may opt in sustained worlds by adding an explicit structured
  admission clause to each eligible organization's governance policy. The clause
  names a registered rule, charter-clause ID, authority tag, local region, allowed
  roles, adult age range, active-member capacity, and finite annual enrollment cap.
* The policy is absent in natural worlds. Free-form charter prose is never parsed
  to infer membership authority or applicant eligibility.
* Keep `content_index.key_npc_ids` as the immutable opening-era identities. Report
  those original identities' living count separately from recruited residents and
  office holders; recruitment does not promote a resident to a “key NPC”.
* Do not add policy defaults to existing 1.7 saves. For current long-run acceptance,
  prefer a fresh 1.8 sustained world. If an existing 1.7 history must continue, add
  a separately authorized, logged policy-adoption command at a committed boundary.

Example structured policy input (IDs shown symbolically)::

    {
      "policy_id": "membership.local.mill.v1",
      "charter_clause_id": "charter.membership.local_service",
      "admission_rule_id": "organization.local_role_match.v1",
      "authority_tag": "authority.membership_recruitment",
      "region_id": "<region UUID>",
      "eligible_roles": ["miller", "tenant farmer"],
      "minimum_age_years": 18,
      "maximum_age_years": 64,
      "active_member_capacity": 4,
      "max_recruits_per_period": 1,
      "selection_seed_id": "<world/policy UUID>"
    }

The caller must resolve candidate lineage from the immutable world/event history:
initial named residents carry their actual pack genesis reference, while later
residents carry the actual event ID and type that introduced them. The planner
preserves those references and never manufactures source IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .components import (
    GovernanceComponent,
    LifeComponent,
    MembershipRecruitmentPolicy,
    PlaceComponent,
    PopulationComponent,
    PopulationOrigin,
)


Tick = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
BoundedName = Annotated[str, Field(min_length=1, max_length=160)]
PolicyId = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]


class RecruitmentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CandidatePopulationLineage(RecruitmentContract):
    """Actual genesis or event provenance resolved by the host/repository."""

    source_kind: Literal["genesis", "population_event"]
    population_group_id: UUID
    pack_id: PolicyId | None = None
    pack_version: BoundedName | None = None
    source_event_id: UUID | None = None
    source_event_type: Literal[
        "core.cohort_member_promoted.v1",
        "core.renewable_resource_harvested.v1",
    ] | None = None

    @model_validator(mode="after")
    def exact_source_shape(self) -> CandidatePopulationLineage:
        if self.source_kind == "genesis":
            if (
                self.pack_id is None
                or self.pack_version is None
                or self.source_event_id is not None
                or self.source_event_type is not None
            ):
                raise ValueError("genesis lineage must name its pack and cannot invent an event ID")
        elif (
            self.source_event_id is None
            or self.source_event_type is None
            or self.pack_id is not None
            or self.pack_version is not None
        ):
            raise ValueError("population event lineage must carry its actual event ID and type")
        return self


class RecruitmentLifeSnapshot(RecruitmentContract):
    population_group_id: UUID
    birth_tick: Tick
    lifespan_ticks: int | None = Field(default=None, gt=0, le=2**63 - 1)
    is_immortal: bool = False
    revision: int = Field(ge=1, le=2**63 - 1)


OrganizationRecruitmentPolicy = MembershipRecruitmentPolicy


class RecruitmentPersonSnapshot(RecruitmentContract):
    """Authoritative facts needed for selection; never supplied by a model."""

    person_id: UUID
    kind: Literal["character", "person"]
    status: BoundedName
    identity_class: Literal["original_key_person", "resident", "other"]
    life: RecruitmentLifeSnapshot | None = None
    population_region_id: UUID | None = None
    current_region_id: UUID | None = None
    expected_population_revision: int = Field(default=1, ge=1, le=2**63 - 1)
    public_role: BoundedName | None = None
    lineage: CandidatePopulationLineage | None = None

    @model_validator(mode="after")
    def lineage_matches_life(self) -> RecruitmentPersonSnapshot:
        if self.lineage is not None and (
            self.life is None or self.lineage.population_group_id != self.life.population_group_id
        ):
            raise ValueError("candidate lineage must identify the life component's population group")
        return self


class OrganizationOfficeSnapshot(RecruitmentContract):
    office_id: PolicyId
    holder_id: UUID | None = None


class OrganizationRecruitmentSnapshot(RecruitmentContract):
    organization_id: UUID
    kind: Literal["organization", "polity", "faction", "company", "government"]
    status: BoundedName
    region_id: UUID | None = None
    governance_revision: int = Field(ge=1, le=2**63 - 1)
    member_ids: tuple[UUID, ...] = Field(default=(), max_length=100_000)
    authority_tags: tuple[PolicyId, ...] = Field(default=(), max_length=256)
    declared_charter_clause_ids: tuple[PolicyId, ...] = Field(default=(), max_length=4096)
    offices: tuple[OrganizationOfficeSnapshot, ...] = Field(default=(), max_length=4096)
    policy: OrganizationRecruitmentPolicy | None = None

    @model_validator(mode="after")
    def unique_roster_and_authority(self) -> OrganizationRecruitmentSnapshot:
        if len(self.member_ids) != len(set(self.member_ids)):
            raise ValueError("organization roster IDs must be unique")
        if len(self.authority_tags) != len(set(self.authority_tags)):
            raise ValueError("organization authority tags must be unique")
        if len(self.declared_charter_clause_ids) != len(set(self.declared_charter_clause_ids)):
            raise ValueError("declared charter clause IDs must be unique")
        office_ids = [office.office_id for office in self.offices]
        if len(office_ids) != len(set(office_ids)):
            raise ValueError("organization office IDs must be unique")
        return self


class MembershipRecruitmentReview(RecruitmentContract):
    year_index: int = Field(ge=1, le=2**63 - 1)
    current_tick: Tick
    day_seconds: int = Field(ge=1, le=2**63 - 1)
    year_days: int = Field(ge=1, le=100_000)
    player_id: UUID | None = None
    original_key_person_ids: tuple[UUID, ...] = Field(default=(), max_length=100_000)
    organizations: tuple[OrganizationRecruitmentSnapshot, ...] = Field(max_length=100_000)
    people: tuple[RecruitmentPersonSnapshot, ...] = Field(max_length=1_000_000)

    @model_validator(mode="after")
    def exact_boundary_and_complete_membership_snapshot(self) -> MembershipRecruitmentReview:
        year_seconds = self.day_seconds * self.year_days
        if self.current_tick != self.year_index * year_seconds:
            raise ValueError("membership review must run at its exact registered world-year boundary")
        organization_ids = [item.organization_id for item in self.organizations]
        person_ids = [item.person_id for item in self.people]
        if len(organization_ids) != len(set(organization_ids)):
            raise ValueError("membership review cannot repeat an organization")
        if len(person_ids) != len(set(person_ids)):
            raise ValueError("membership review cannot repeat a person")
        if len(self.original_key_person_ids) != len(set(self.original_key_person_ids)):
            raise ValueError("original key-person identities must be unique")
        known_people = set(person_ids)
        if not set(self.original_key_person_ids).issubset(known_people):
            raise ValueError("original key-person identities must remain in the world roster")
        referenced_members = {
            member_id
            for organization in self.organizations
            for member_id in (
                *organization.member_ids,
                *(office.holder_id for office in organization.offices if office.holder_id is not None),
            )
        }
        if not referenced_members.issubset(known_people):
            raise ValueError("membership review requires a complete person snapshot for every member and office holder")
        return self


@dataclass(frozen=True, slots=True)
class MembershipRecruitmentProjection:
    """Read-only recruitment facts projected at any committed planning tick.

    Unlike the public annual review contract, this internal projection has no
    exact-year-boundary requirement. It shares the same source lineage and
    locality extraction with annual recruitment.
    """

    current_tick: int
    year_seconds: int
    player_id: UUID | None
    original_key_person_ids: tuple[UUID, ...]
    organizations: tuple[OrganizationRecruitmentSnapshot, ...]
    people: tuple[RecruitmentPersonSnapshot, ...]
    people_by_population_region: dict[UUID, tuple[RecruitmentPersonSnapshot, ...]]
    people_by_id: dict[UUID, RecruitmentPersonSnapshot]


class PlannedMembershipEnrollment(RecruitmentContract):
    """One append-only membership addition; contains no leadership mutation."""

    organization_id: UUID
    policy_id: PolicyId
    expected_governance_revision: int = Field(ge=1, le=2**63 - 1)
    year_index: int = Field(ge=1, le=2**63 - 1)
    member_id: UUID
    expected_life_revision: int = Field(ge=1, le=2**63 - 1)
    population_group_id: UUID
    expected_population_revision: int = Field(ge=1, le=2**63 - 1)
    age_years: int = Field(ge=0, le=200)
    public_role: BoundedName
    lineage: CandidatePopulationLineage


class OrganizationOfficeStatus(RecruitmentContract):
    office_id: PolicyId
    holder_id: UUID | None
    status: Literal["filled_active", "vacant", "inactive_holder"]


class OrganizationRecruitmentReport(RecruitmentContract):
    organization_id: UUID
    reason: Literal[
        "policy_missing",
        "organization_inactive",
        "policy_not_authorized",
        "capacity_full",
        "no_eligible_candidates",
        "recruited",
    ]
    active_members_before: int = Field(ge=0)
    active_members_after: int = Field(ge=0)
    active_member_capacity: int | None = Field(default=None, ge=1)
    newly_enrolled_member_ids: tuple[UUID, ...] = ()
    offices: tuple[OrganizationOfficeStatus, ...] = ()


class OriginalKeyPeopleMetric(RecruitmentContract):
    original_key_people_seeded: int = Field(ge=0)
    original_key_people_alive: int = Field(ge=0)


class OrganizationReviewExpectation(RecruitmentContract):
    organization_id: UUID
    expected_governance_revision: int = Field(ge=1, le=2**63 - 1)
    policy_ids: tuple[PolicyId, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_policy_ids(self) -> OrganizationReviewExpectation:
        if self.policy_ids != tuple(sorted(set(self.policy_ids))):
            raise ValueError("organization review policy ids must be unique and stable")
        return self


class MembershipRecruitmentPlan(RecruitmentContract):
    year_index: int = Field(ge=1)
    expected_organizations: tuple[OrganizationReviewExpectation, ...]
    enrollments: tuple[PlannedMembershipEnrollment, ...]
    organizations: tuple[OrganizationRecruitmentReport, ...]
    original_key_people: OriginalKeyPeopleMetric


def plan_membership_recruitment(review: MembershipRecruitmentReview) -> MembershipRecruitmentPlan:
    """Return deterministic enrollments from the supplied committed-boundary snapshot.

    The caller resolves lineage from actual genesis metadata/event history before
    creating this input. Invalid policy references fail closed by producing a
    `policy_not_authorized` report with no enrollment. Original key people are
    measured but are never selected as replacement members by this resident rule.
    """

    review = _fully_validate_review(review)
    people = {person.person_id: person for person in review.people}
    original_key_ids = set(review.original_key_person_ids)
    key_metric = OriginalKeyPeopleMetric(
        original_key_people_seeded=len(original_key_ids),
        original_key_people_alive=sum(
            _is_alive(people[person_id], review.current_tick) for person_id in original_key_ids
        ),
    )
    year_seconds = review.day_seconds * review.year_days
    enrollments: list[PlannedMembershipEnrollment] = []
    reports: list[OrganizationRecruitmentReport] = []
    expected_organizations: list[OrganizationReviewExpectation] = []
    already_selected: set[UUID] = set()

    for organization in sorted(review.organizations, key=lambda item: str(item.organization_id)):
        active_members = {
            member_id for member_id in organization.member_ids
            if _is_alive(people[member_id], review.current_tick)
        }
        offices = _office_statuses(organization, people, review.current_tick)
        policy = organization.policy
        if policy is not None:
            expected_organizations.append(
                OrganizationReviewExpectation(
                    organization_id=organization.organization_id,
                    expected_governance_revision=organization.governance_revision,
                    policy_ids=(policy.policy_id,),
                )
            )
        if organization.status != "active":
            reports.append(_report(organization, "organization_inactive", active_members, None, (), offices))
            continue
        if policy is None:
            reports.append(_report(organization, "policy_missing", active_members, None, (), offices))
            continue
        if (
            organization.region_id != policy.region_id
            or policy.charter_clause_id not in organization.declared_charter_clause_ids
            or policy.authority_tag not in organization.authority_tags
        ):
            reports.append(
                _report(organization, "policy_not_authorized", active_members, policy.active_member_capacity, (), offices)
            )
            continue
        if len(active_members) >= policy.active_member_capacity:
            reports.append(
                _report(organization, "capacity_full", active_members, policy.active_member_capacity, (), offices)
            )
            continue

        candidates = [
            person for person in review.people
            if _eligible_candidate(
                person,
                organization=organization,
                policy=policy,
                member_ids=set(organization.member_ids),
                already_selected=already_selected,
                player_id=review.player_id,
                current_tick=review.current_tick,
                year_seconds=year_seconds,
            )
        ]
        candidates.sort(key=lambda person: _selection_rank(policy, review.year_index, person.person_id))
        slots = min(
            policy.active_member_capacity - len(active_members),
            policy.max_recruits_per_period,
        )
        selected = candidates[:slots]
        if not selected:
            reports.append(
                _report(organization, "no_eligible_candidates", active_members, policy.active_member_capacity, (), offices)
            )
            continue

        selected_ids = tuple(person.person_id for person in selected)
        already_selected.update(selected_ids)
        for person in selected:
            life = person.life
            lineage = person.lineage
            # _eligible_candidate guarantees both records and their matching group.
            assert life is not None and lineage is not None and person.public_role is not None
            age_years = (review.current_tick - life.birth_tick) // year_seconds
            enrollments.append(
                PlannedMembershipEnrollment(
                    organization_id=organization.organization_id,
                    policy_id=policy.policy_id,
                    expected_governance_revision=organization.governance_revision,
                    year_index=review.year_index,
                    member_id=person.person_id,
                    expected_life_revision=life.revision,
                    population_group_id=life.population_group_id,
                    expected_population_revision=person.expected_population_revision,
                    age_years=age_years,
                    public_role=person.public_role,
                    lineage=lineage,
                )
            )
        updated_members = active_members | set(selected_ids)
        reports.append(
            _report(
                organization,
                "recruited",
                active_members,
                policy.active_member_capacity,
                selected_ids,
                offices,
                active_after=len(updated_members),
            )
        )

    enrollments.sort(key=lambda item: (str(item.organization_id), str(item.member_id)))
    return MembershipRecruitmentPlan(
        year_index=review.year_index,
        expected_organizations=tuple(expected_organizations),
        enrollments=tuple(enrollments),
        organizations=tuple(reports),
        original_key_people=key_metric,
    )


def build_membership_recruitment_review(
    state: object,
    *,
    census_population_revision_increment: int = 0,
) -> MembershipRecruitmentReview:
    """Project a trusted world state into the recruitment planner's closed input.

    Initial residents are eligible only when their IDs and source cohorts match
    the immutable content index. Later residents require a reducer-authored
    ``LifeComponent.population_origin`` marker. The optional revision increment
    lets the scheduled-rule builder plan the immediately-following census event;
    the reducer rebuilds the review after that event with increment zero.
    """
    from .models import WorldState

    if census_population_revision_increment not in {0, 1}:
        raise ValueError("recruitment review may anticipate at most one census revision")
    if not isinstance(state, WorldState):
        state = WorldState.model_validate(state)

    year_seconds = state.calendar.day_seconds * state.calendar.year_days
    if year_seconds <= 0 or state.current_tick % year_seconds:
        raise ValueError("membership review requires an exact world-year boundary")
    year_index = state.current_tick // year_seconds
    projection = project_membership_recruitment_facts(
        state,
        census_population_revision_increment=census_population_revision_increment,
    )
    return MembershipRecruitmentReview(
        year_index=year_index,
        current_tick=state.current_tick,
        day_seconds=state.calendar.day_seconds,
        year_days=state.calendar.year_days,
        player_id=projection.player_id,
        original_key_person_ids=projection.original_key_person_ids,
        organizations=projection.organizations,
        people=projection.people,
    )


def project_membership_recruitment_facts(
    state: object,
    *,
    census_population_revision_increment: int = 0,
) -> MembershipRecruitmentProjection:
    """Project trusted recruitment facts without requiring an annual boundary.

    Annual review construction calls this same projection after its historical
    boundary check. Seat refill planning uses it once to inspect potential local
    candidates between annual reviews; it never creates or persists lineage.
    """
    from .models import WorldState

    if census_population_revision_increment not in {0, 1}:
        raise ValueError("recruitment review may anticipate at most one census revision")
    if not isinstance(state, WorldState):
        state = WorldState.model_validate(state)

    year_seconds = state.calendar.day_seconds * state.calendar.year_days
    metadata = state.metadata
    content_index = metadata.get("content_index") if isinstance(metadata, dict) else None
    if not isinstance(content_index, dict):
        content_index = {}
    original_key_ids = _uuid_index(content_index.get("key_npc_ids"))
    resident_ids = set(_uuid_index(content_index.get("other_resident_ids")))
    resident_groups = _uuid_mapping(content_index.get("other_resident_population_groups"))
    player_id = state.player_id
    pack_id = metadata.get("pack_id") if isinstance(metadata.get("pack_id"), str) else None
    pack_version = metadata.get("pack_version") if isinstance(metadata.get("pack_version"), str) else None

    population_by_id: dict[UUID, PopulationComponent] = {}
    for entity in state.entities.values():
        raw = entity.components.get("population")
        if entity.kind in {"group", "population", "population_group", "cohort"} and isinstance(raw, dict):
            try:
                population_by_id[entity.id] = PopulationComponent.model_validate(raw)
            except Exception as exc:
                raise ValueError("recruitment state contains an invalid population component") from exc

    organizations: list[OrganizationRecruitmentSnapshot] = []
    for entity in state.entities.values():
        if entity.kind not in {"organization", "polity", "faction", "company", "government"}:
            continue
        raw = entity.components.get("governance")
        if not isinstance(raw, dict):
            continue
        try:
            governance = GovernanceComponent.model_validate(raw)
        except Exception as exc:
            raise ValueError("recruitment state contains an invalid governance component") from exc
        if not governance.membership_recruitment:
            continue
        policy = governance.membership_recruitment[0] if governance.membership_recruitment else None
        if len(governance.membership_recruitment) > 1:
            raise ValueError("only one registered recruitment policy per organization is currently supported")
        organizations.append(
            OrganizationRecruitmentSnapshot(
                organization_id=entity.id,
                kind=entity.kind,
                status=entity.status,
                region_id=entity.location_id,
                governance_revision=governance.revision,
                member_ids=tuple(governance.members),
                authority_tags=tuple(governance.authority_tags),
                declared_charter_clause_ids=tuple(governance.charter_clause_ids),
                offices=tuple(
                    OrganizationOfficeSnapshot(office_id=item.office_id, holder_id=item.holder_id)
                    for item in governance.offices
                ),
                policy=policy,
            )
        )

    if any(item.policy is not None for item in organizations) and resident_ids:
        if (
            pack_id is None
            or pack_version is None
            or set(resident_groups) != resident_ids
        ):
            raise ValueError("opted-in recruitment requires a complete immutable genesis resident-to-population index")
        for resident_id, expected_group_id in resident_groups.items():
            resident = state.entities.get(resident_id)
            if resident is None or resident.kind not in {"character", "person"}:
                raise ValueError("genesis resident index must reference existing character entities")
            raw_life = resident.components.get("life")
            if not isinstance(raw_life, dict):
                raise ValueError("genesis resident index requires a typed life component")
            life = LifeComponent.model_validate(raw_life)
            if life.population_group_id != expected_group_id:
                raise ValueError("genesis resident life cohort does not match the immutable content index")

    people: list[RecruitmentPersonSnapshot] = []
    for entity in state.entities.values():
        if entity.kind not in {"character", "person"}:
            continue
        raw_life = entity.components.get("life")
        life_component: LifeComponent | None = None
        if isinstance(raw_life, dict):
            try:
                life_component = LifeComponent.model_validate(raw_life)
            except Exception as exc:
                raise ValueError("recruitment state contains an invalid life component") from exc
        life_snapshot: RecruitmentLifeSnapshot | None = None
        group: PopulationComponent | None = None
        population_region_id: UUID | None = None
        expected_population_revision = 1
        lineage: CandidatePopulationLineage | None = None
        if life_component is not None and life_component.population_group_id is not None:
            group = population_by_id.get(life_component.population_group_id)
            if group is not None:
                population_region_id = state.entities[life_component.population_group_id].location_id
                expected_population_revision = group.revision + (
                    census_population_revision_increment if group.age_cohorts else 0
                )
                if expected_population_revision > 2**63 - 1:
                    raise ValueError("recruitment population revision exceeds the signed 64-bit range")
                life_snapshot = RecruitmentLifeSnapshot(
                    population_group_id=life_component.population_group_id,
                    birth_tick=life_component.birth_tick,
                    lifespan_ticks=life_component.lifespan_ticks,
                    is_immortal=life_component.is_immortal,
                    revision=life_component.revision,
                )
                if entity.id in resident_ids:
                    if (
                        resident_groups.get(entity.id) == life_component.population_group_id
                        and pack_id is not None
                        and pack_version is not None
                    ):
                        lineage = CandidatePopulationLineage(
                            source_kind="genesis",
                            population_group_id=life_component.population_group_id,
                            pack_id=pack_id,
                            pack_version=pack_version,
                        )
                elif life_component.population_origin is not None:
                    origin = life_component.population_origin
                    if origin.population_group_id == life_component.population_group_id:
                        lineage = CandidatePopulationLineage(
                            source_kind="population_event",
                            population_group_id=origin.population_group_id,
                            source_event_id=origin.source_event_id,
                            source_event_type=origin.source_event_type,
                        )

        if entity.id in original_key_ids:
            identity_class: Literal["original_key_person", "resident", "other"] = "original_key_person"
        elif entity.id in resident_ids or lineage is not None:
            identity_class = "resident"
        else:
            identity_class = "other"
        raw_role = entity.components.get("public_role")
        role = raw_role if isinstance(raw_role, str) else None
        people.append(
            RecruitmentPersonSnapshot(
                person_id=entity.id,
                kind="character" if entity.kind == "character" else "person",
                status=entity.status,
                identity_class=identity_class,
                life=life_snapshot,
                population_region_id=population_region_id,
                current_region_id=_region_for_location(state, entity.location_id),
                expected_population_revision=expected_population_revision,
                public_role=role,
                lineage=lineage,
            )
        )

    people_by_region: dict[UUID, list[RecruitmentPersonSnapshot]] = {}
    people_by_id: dict[UUID, RecruitmentPersonSnapshot] = {}
    for person in people:
        people_by_id[person.person_id] = person
        if person.population_region_id is not None:
            people_by_region.setdefault(person.population_region_id, []).append(person)
    frozen_people_by_region = {
        region_id: tuple(sorted(rows, key=lambda item: str(item.person_id)))
        for region_id, rows in people_by_region.items()
    }
    return MembershipRecruitmentProjection(
        current_tick=state.current_tick,
        year_seconds=year_seconds,
        player_id=player_id,
        original_key_person_ids=tuple(sorted(original_key_ids, key=str)),
        organizations=tuple(organizations),
        people=tuple(people),
        people_by_population_region=frozen_people_by_region,
        people_by_id=people_by_id,
    )


def project_provisional_population_candidate(
    state: object,
    entity: object,
    *,
    expected_population_revision: int | None = None,
) -> RecruitmentPersonSnapshot:
    """Describe an uncommitted real seat promotion without inventing lineage IDs.

    The returned resident has no durable lineage yet. Callers may pass it to
    `_eligible_candidate` only with `allow_provisional_population_promotion=True`
    while planning the same atomic promotion batch.
    """
    from .models import WorldEntity, WorldState

    if not isinstance(state, WorldState):
        state = WorldState.model_validate(state)
    if not isinstance(entity, WorldEntity) or entity.kind not in {"character", "person"}:
        raise ValueError("provisional recruitment candidate must be a named person entity")
    raw_life = entity.components.get("life")
    if not isinstance(raw_life, dict):
        raise ValueError("provisional recruitment candidate requires a typed life component")
    life = LifeComponent.model_validate(raw_life)
    group_id = life.population_group_id
    group = state.entities.get(group_id) if group_id is not None else None
    if group is None or group.kind not in {"group", "population", "population_group", "cohort"}:
        raise ValueError("provisional recruitment candidate requires a population group")
    raw_population = group.components.get("population")
    if not isinstance(raw_population, dict):
        raise ValueError("provisional recruitment candidate requires an authored population group")
    population = PopulationComponent.model_validate(raw_population)
    role = entity.components.get("public_role")
    return RecruitmentPersonSnapshot(
        person_id=entity.id,
        kind="character" if entity.kind == "character" else "person",
        status=entity.status,
        identity_class="resident",
        life=RecruitmentLifeSnapshot(
            population_group_id=group_id,
            birth_tick=life.birth_tick,
            lifespan_ticks=life.lifespan_ticks,
            is_immortal=life.is_immortal,
            revision=life.revision,
        ),
        population_region_id=group.location_id,
        current_region_id=_region_for_location(state, entity.location_id),
        expected_population_revision=(
            expected_population_revision
            if expected_population_revision is not None
            else population.revision + 1
        ),
        public_role=role if isinstance(role, str) else None,
        lineage=None,
    )


def _uuid_index(value: object) -> list[UUID]:
    if not isinstance(value, list):
        return []
    result: list[UUID] = []
    for item in value:
        try:
            result.append(UUID(str(item)))
        except (TypeError, ValueError):
            raise ValueError("content identity index contains an invalid UUID") from None
    if len(result) != len(set(result)):
        raise ValueError("content identity index contains duplicate UUIDs")
    return result


def _uuid_mapping(value: object) -> dict[UUID, UUID]:
    if not isinstance(value, dict):
        return {}
    result: dict[UUID, UUID] = {}
    for key, item in value.items():
        try:
            result[UUID(str(key))] = UUID(str(item))
        except (TypeError, ValueError):
            raise ValueError("content population lineage index contains an invalid UUID") from None
    return result


def _region_for_location(state: object, location_id: UUID | None) -> UUID | None:
    if location_id is None:
        return None
    current_id = location_id
    seen: set[UUID] = set()
    while current_id not in seen:
        seen.add(current_id)
        current = state.entities.get(current_id)
        if current is None:
            return None
        raw_place = current.components.get("place")
        if not isinstance(raw_place, dict):
            return None
        try:
            place = PlaceComponent.model_validate(raw_place)
        except Exception:
            return None
        if place.place_type == "region":
            return current.id
        next_id = place.region_id or place.parent_id
        if next_id is None:
            return None
        current_id = next_id
    return None


def _fully_validate_review(review: MembershipRecruitmentReview) -> MembershipRecruitmentReview:
    data = review.model_dump(mode="python") if isinstance(review, MembershipRecruitmentReview) else review
    return MembershipRecruitmentReview.model_validate(data)


def _is_alive(person: RecruitmentPersonSnapshot, current_tick: int) -> bool:
    if person.status != "active":
        return False
    life = person.life
    return (
        life is None
        or life.is_immortal
        or life.lifespan_ticks is None
        or life.birth_tick + life.lifespan_ticks > current_tick
    )


def _eligible_candidate(
    person: RecruitmentPersonSnapshot,
    *,
    organization: OrganizationRecruitmentSnapshot,
    policy: OrganizationRecruitmentPolicy,
    member_ids: set[UUID],
    already_selected: set[UUID],
    player_id: UUID | None,
    current_tick: int,
    year_seconds: int,
    allow_provisional_population_promotion: bool = False,
) -> bool:
    life = person.life
    lineage = person.lineage
    if (
        person.identity_class != "resident"
        or person.kind not in {"character", "person"}
        or person.status != "active"
        or person.person_id == player_id
        or person.person_id in member_ids
        or person.person_id in already_selected
        or life is None
        or (lineage is None and not allow_provisional_population_promotion)
        or person.public_role is None
        or not _is_alive(person, current_tick)
        or (
            lineage is not None
            and life.population_group_id != lineage.population_group_id
        )
        or person.population_region_id != policy.region_id
        or person.current_region_id != policy.region_id
    ):
        return False
    age_years = (current_tick - life.birth_tick) // year_seconds
    if not policy.minimum_age_years <= age_years <= policy.maximum_age_years:
        return False
    if person.public_role.casefold() not in {role.casefold() for role in policy.eligible_roles}:
        return False
    # Population membership is tied to the organization's declared region, not
    # merely to a matching job title in another region.
    return (
        organization.region_id == policy.region_id
        and (
            (lineage is not None and life.population_group_id == lineage.population_group_id)
            or (lineage is None and allow_provisional_population_promotion)
        )
    )


def _selection_rank(
    policy: OrganizationRecruitmentPolicy,
    year_index: int,
    person_id: UUID,
) -> tuple[bytes, str]:
    material = f"{policy.selection_seed_id}:{policy.policy_id}:{year_index}:{person_id}".encode("utf-8")
    return sha256(material).digest(), str(person_id)


def _office_statuses(
    organization: OrganizationRecruitmentSnapshot,
    people: dict[UUID, RecruitmentPersonSnapshot],
    current_tick: int,
) -> tuple[OrganizationOfficeStatus, ...]:
    result = []
    for office in sorted(organization.offices, key=lambda item: item.office_id):
        if office.holder_id is None:
            status: Literal["filled_active", "vacant", "inactive_holder"] = "vacant"
        elif _is_alive(people[office.holder_id], current_tick):
            status = "filled_active"
        else:
            status = "inactive_holder"
        result.append(OrganizationOfficeStatus(office_id=office.office_id, holder_id=office.holder_id, status=status))
    return tuple(result)


def _report(
    organization: OrganizationRecruitmentSnapshot,
    reason: Literal[
        "policy_missing",
        "organization_inactive",
        "policy_not_authorized",
        "capacity_full",
        "no_eligible_candidates",
        "recruited",
    ],
    active_members: set[UUID],
    capacity: int | None,
    newly_enrolled: tuple[UUID, ...],
    offices: tuple[OrganizationOfficeStatus, ...],
    *,
    active_after: int | None = None,
) -> OrganizationRecruitmentReport:
    return OrganizationRecruitmentReport(
        organization_id=organization.organization_id,
        reason=reason,
        active_members_before=len(active_members),
        active_members_after=len(active_members) if active_after is None else active_after,
        active_member_capacity=capacity,
        newly_enrolled_member_ids=newly_enrolled,
        offices=offices,
    )
