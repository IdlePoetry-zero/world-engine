from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from world_engine.content import ContentCatalog
from world_engine.content.definitions import BACKGROUNDS
from world_engine.domain.components import GovernanceComponent, MarketComponent, ScheduledEventRecord
from world_engine.domain.market_pricing import (
    PRICE_REVIEW_CONFIG_KEY,
    PRICE_REVIEW_DAYS,
    PRICE_REVIEW_HANDLER_ID,
    PRICE_REVIEW_HANDLER_VERSION,
    plan_market_price_review,
    validate_market_pricing_config,
)
from world_engine.domain.models import WorldState


@pytest.fixture(scope="module")
def catalog() -> ContentCatalog:
    return ContentCatalog()


def _opening_anchor_ids(state: WorldState) -> list[UUID]:
    return [
        entity.id
        for entity in state.entities.values()
        if "investigation_case" in entity.components
    ]


def _market_review_schedule(state: WorldState) -> ScheduledEventRecord:
    schedules = [
        scheduled
        for scheduled in state.scheduled_events
        if scheduled.handler_id == PRICE_REVIEW_HANDLER_ID
    ]
    assert len(schedules) == 1
    return schedules[0]


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda item: item.pack_id)
def test_2_1_catalog_adds_216_market_policies_and_plans_first_review(catalog, pack):
    draft = catalog.build_world(
        pack.pack_id,
        "Market pricing compatibility",
        "Avery",
        seed=2_100,
        pack_version="2.1.0",
    )
    state = draft.state
    assert draft.pack_version == state.metadata["pack_version"] == "2.1.0"

    config = validate_market_pricing_config(state)
    assert config is not None
    assert config.schema_version == "world.market_pricing.v1"
    assert config.review_period_days == PRICE_REVIEW_DAYS == 30
    assert len(config.policies) == 216

    market_bindings = set()
    for entity in state.entities.values():
        if "market" not in entity.components:
            continue
        market = MarketComponent.model_validate(entity.components["market"])
        market_bindings.update((entity.id, offer.listing_id) for offer in market.offers)
    policy_bindings = {
        (policy.market_id, policy.listing_id)
        for policy in config.policies
    }
    assert len(market_bindings) == 216
    assert policy_bindings == market_bindings
    assert len({policy.policy_id for policy in config.policies}) == 216

    schedule = _market_review_schedule(state)
    period_ticks = PRICE_REVIEW_DAYS * state.calendar.day_seconds
    assert schedule.handler_version == PRICE_REVIEW_HANDLER_VERSION
    assert schedule.phase == "maintenance"
    assert schedule.due_tick == schedule.repeat_ticks == period_ticks
    assert schedule.max_runs == 600 * state.calendar.year_days // PRICE_REVIEW_DAYS
    assert schedule.completed_runs == 0 and schedule.status == "queued"
    assert schedule.due_tick + schedule.repeat_ticks * (schedule.max_runs - 1) == (
        600 * state.calendar.year_days * state.calendar.day_seconds
    )

    due_payload = state.model_dump(mode="python")
    due_payload["current_tick"] = schedule.due_tick
    due_state = WorldState.model_validate(due_payload)
    event = plan_market_price_review(
        due_state,
        schedule,
        world_id=draft.world_id,
        branch_id=draft.branch_id,
        command_id=uuid5(draft.world_id, "test:first-market-price-review"),
        ruleset_version=draft.ruleset_version,
    )
    assert event.schedule_id == schedule.id
    assert event.expected_schedule_revision == schedule.revision
    assert event.period_index == 1
    assert event.due_tick == schedule.due_tick
    assert event.assessment_count == 216
    assert event.changed_count + event.unchanged_count + event.skipped_count == 216


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda item: item.pack_id)
@pytest.mark.parametrize("population_mode", ("natural", "sustained"))
def test_2_1_seeded_profiles_preserve_opening_case_and_recruitment_mode(
    catalog, pack, population_mode,
):
    draft = catalog.build_world(
        pack.pack_id,
        "Market seeded profile",
        "Avery",
        seed=21_000,
        population_mode=population_mode,
        pack_version="2.1.0",
    )

    assert draft.pack_version == draft.state.metadata["pack_version"] == "2.1.0"
    assert draft.state.metadata["generation_seed"] == 21_000
    assert draft.state.metadata["population_mode"] == population_mode
    assert len(_opening_anchor_ids(draft.state)) == 1

    governance = [
        entity for entity in draft.state.entities.values()
        if "governance" in entity.components
    ]
    if population_mode == "sustained":
        resident_groups = draft.state.metadata["content_index"]["other_resident_population_groups"]
        assert len(resident_groups) == 240
        assert len(governance) == 48
        assert all(
            GovernanceComponent.model_validate(entity.components["governance"]).membership_recruitment
            for entity in governance
        )
    else:
        assert "other_resident_population_groups" not in draft.state.metadata["content_index"]
        assert all(
            not GovernanceComponent.model_validate(entity.components["governance"]).membership_recruitment
            for entity in governance
        )


@pytest.mark.parametrize("pack", BACKGROUNDS, ids=lambda item: item.pack_id)
@pytest.mark.parametrize("population_mode", ("natural", "sustained"))
def test_explicit_2_0_catalog_remains_deterministic_without_market_pricing(
    catalog, pack, population_mode,
):
    args = {
        "seed": 2_000,
        "population_mode": population_mode,
        "pack_version": "2.0.0",
    }
    first = catalog.build_world(pack.pack_id, "Legacy market compatibility", "Avery", **args)
    second = catalog.build_world(pack.pack_id, "Legacy market compatibility", "Avery", **args)

    assert first.pack_version == second.pack_version == "2.0.0"
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert PRICE_REVIEW_CONFIG_KEY not in first.state.metadata
    assert not any(
        scheduled.handler_id == PRICE_REVIEW_HANDLER_ID
        for scheduled in first.state.scheduled_events
    )
    assert len(_opening_anchor_ids(first.state)) == 1
    if population_mode == "sustained":
        assert first.state.metadata["content_index"]["other_resident_population_groups"]

