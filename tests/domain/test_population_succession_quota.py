from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.components import (
    GovernanceComponent,
    HealthComponent,
    LifeComponent,
    PopulationComponent,
    ProductionComponent,
    ProductionQuotaPolicy,
    ProvenanceReference,
    ScheduledEventRecord,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    CohortMemberPromotedEvent,
    GovernanceSuccessionEvent,
    LifeEndedEvent,
    ProductionQuotaRenewedEvent,
    ScheduledEventCompletedEvent,
)
from world_engine.domain.models import EntityFieldChangedEvent, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event


def _character(*, entity_id, name, location_id, cohort_id=None, status="active", birth_tick=0, lifespan=None):
    components = {
        "life": LifeComponent(
            birth_tick=birth_tick,
            lifespan_ticks=lifespan,
            population_group_id=cohort_id,
        ),
        "health": HealthComponent(current=0 if status == "dead" else 100, maximum=100),
    }
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=location_id,
        status=status,
        components=components,
    )


def _cohort_state(*, current_tick=0):
    region_id = uuid4()
    cohort_id = uuid4()
    member_id = uuid4()
    region = WorldEntity(id=region_id, kind="place", name="North region")
    cohort = WorldEntity(
        id=cohort_id,
        kind="group",
        name="North residents",
        location_id=region_id,
        components={
            "population": PopulationComponent(
                species_id="core.human",
                headcount=2,
                unmodeled_headcount=1,
            )
        },
    )
    member = _character(
        entity_id=member_id,
        name="Mira",
        location_id=region_id,
        cohort_id=cohort_id,
        birth_tick=0,
        lifespan=10,
    )
    return WorldState(
        current_tick=current_tick,
        entities={region_id: region, cohort_id: cohort, member_id: member},
    ), cohort_id, member_id, region_id


def test_cohort_promotion_moves_one_person_from_anonymous_pool_atomically() -> None:
    state, cohort_id, _member_id, region_id = _cohort_state()
    newborn_id = uuid4()
    newborn = _character(
        entity_id=newborn_id,
        name="Niko",
        location_id=region_id,
        cohort_id=cohort_id,
        birth_tick=0,
    )
    event = CohortMemberPromotedEvent(
        cohort_id=cohort_id,
        expected_population_revision=1,
        character=newborn,
        provenance=[ProvenanceReference(kind="rule", reference_id="population.cohort_promotion.v1")],
    )

    next_state = apply_event(state, event)

    population = PopulationComponent.model_validate(next_state.entities[cohort_id].components["population"])
    assert population.headcount == 2
    assert population.unmodeled_headcount == 0
    assert population.revision == 2
    assert next_state.entities[newborn_id].components["life"]["population_group_id"] == str(cohort_id)
    assert len([entity for entity in next_state.entities.values() if entity.components.get("life", {}).get("population_group_id")]) == 2

    with pytest.raises(InvalidEvent, match="no unmodeled members"):
        apply_event(next_state, event.model_copy(update={"event_id": uuid4(), "expected_population_revision": 2, "character": _character(entity_id=uuid4(), name="Una", location_id=region_id, cohort_id=cohort_id)}))


def test_linked_life_expiry_decrements_total_but_not_unmodeled_pool_once() -> None:
    state, cohort_id, member_id, _region_id = _cohort_state(current_tick=10)
    member = state.entities[member_id]
    event = LifeEndedEvent(
        entity_id=member_id,
        expected_life_revision=member.components["life"]["revision"],
        expected_population_revision=1,
    )

    next_state = apply_event(state, event)
    population = PopulationComponent.model_validate(next_state.entities[cohort_id].components["population"])
    assert next_state.entities[member_id].status == "dead"
    assert population.headcount == 1
    assert population.unmodeled_headcount == 1
    assert population.revision == 2
    with pytest.raises(InvalidEvent, match="terminal character"):
        apply_event(next_state, event.model_copy(update={"event_id": uuid4(), "expected_population_revision": 2}))


def test_explicit_terminal_status_change_also_updates_population_and_health():
    state, cohort_id, member_id, _region_id = _cohort_state()

    next_state = apply_event(
        state,
        EntityFieldChangedEvent(
            entity_id=member_id,
            field_name="status",
            value="dead",
            expected_population_revision=1,
        ),
    )

    assert next_state.entities[member_id].status == "dead"
    assert next_state.entities[member_id].components["health"]["current"] == 0
    assert next_state.entities[cohort_id].components["population"]["headcount"] == 1


def test_succession_requires_terminal_predecessor_and_cas_replaces_current_holder() -> None:
    predecessor_id, successor_id, organization_id, death_event_id = (uuid4() for _ in range(4))
    predecessor = _character(
        entity_id=predecessor_id,
        name="Ari",
        location_id=None,
        status="dead",
    )
    successor = _character(
        entity_id=successor_id,
        name="Bea",
        location_id=None,
    )
    organization = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Harbor council",
        components={
            "governance": GovernanceComponent(
                members=[predecessor_id, successor_id],
                leader_id=predecessor_id,
                succession_policy="election",
            )
        },
    )
    state = WorldState(entities={predecessor_id: predecessor, successor_id: successor, organization_id: organization})
    event = GovernanceSuccessionEvent(
        organization_id=organization_id,
        slot="leader",
        predecessor_id=predecessor_id,
        successor_id=successor_id,
        expected_governance_revision=1,
        decision_basis="election",
        decision_request_id=uuid4(),
        decision_option_id=f"leader_holder_{successor_id.hex}",
        source_death_event_id=death_event_id,
        evidence_refs=[predecessor_id, death_event_id],
        rationale="The council elected the available member.",
    )

    next_state = apply_event(state, event)
    governance = GovernanceComponent.model_validate(next_state.entities[organization_id].components["governance"])
    assert governance.leader_id == successor_id
    assert governance.revision == 2
    with pytest.raises(InvalidEvent, match="expected_governance_revision"):
        apply_event(next_state, event.model_copy(update={"event_id": uuid4()}))


def test_quota_renewal_is_finite_period_bound_and_finishes_its_schedule() -> None:
    producer_id, account_id, schedule_id = uuid4(), uuid4(), uuid4()
    policy = ProductionQuotaPolicy(
        quota_id="regional_yield",
        resource_id="grain",
        source_account_id=account_id,
        period_ticks=10,
        per_period=Decimal("2"),
        maximum_renewal=Decimal("5"),
        base_credit_limit=Decimal("2"),
    )
    producer = WorldEntity(
        id=producer_id,
        kind="organization",
        name="Regional growers",
        components={"production": ProductionComponent(quotas=[policy])},
    )
    schedule = ScheduledEventRecord(
        id=schedule_id,
        due_tick=10,
        phase="environment",
        handler_id="production.quota_renewal",
        handler_version="1.0.0",
        payload={"producer_id": str(producer_id), "quota_id": "regional_yield"},
    )
    account = ResourceAccount(
        resource_id="grain",
        balance="0",
        owner_id=producer_id,
        account_type="production_source",
        credit_limit="2",
    )
    state = WorldState(
        current_tick=10,
        entities={producer_id: producer},
        resources={account_id: account},
        scheduled_events=[schedule],
    )
    renewal = ProductionQuotaRenewedEvent(
        producer_id=producer_id,
        quota_id="regional_yield",
        expected_production_revision=1,
        period_index=1,
        scheduled_event_id=schedule_id,
        provenance=[ProvenanceReference(kind="rule", reference_id="production.quota_renewal.v1")],
    )

    renewed = apply_event(state, renewal)
    assert renewed.resources[account_id].credit_limit == Decimal("4")
    renewed_policy = ProductionComponent.model_validate(renewed.entities[producer_id].components["production"]).quotas[0]
    assert renewed_policy.issued_total == Decimal("2")
    assert renewed_policy.last_issued_period == 1
    completed = apply_event(
        renewed,
        ScheduledEventCompletedEvent(scheduled_event_id=schedule_id, expected_revision=1),
    )
    assert completed.scheduled_events[0].status == "completed"
    with pytest.raises(InvalidEvent, match="expected_production_revision"):
        apply_event(renewed, renewal.model_copy(update={"event_id": uuid4()}))
