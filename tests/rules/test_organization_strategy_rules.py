from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.organization_strategy import (
    OrganizationalStrategy,
    OrganizationalStrategyGoal,
    StrategyCondition,
    StrategyTransferAction,
    install_organizational_strategy,
    organization_strategy_schedule_id,
)
from world_engine.domain.reducers import apply_event, ledger_postings, state_hash
from world_engine.rules import RuleContext, RuleEngine


def test_scheduled_strategy_resolution_is_deterministic_and_pure_until_commit():
    organization_id, member_id, source_id, reserve_id, project_id = (uuid4() for _ in range(5))
    authority = "authority.strategy_execute"
    clause = "charter.strategy_review"
    governance = GovernanceComponent(
        members=[member_id],
        authority_tags=[authority],
        charter_clause_ids=[clause],
        offices=[OfficeRecord(
            office_id="office.treasurer",
            title="Treasurer",
            holder_id=member_id,
            authority=[authority],
        )],
    )
    state = WorldState(
        entities={
            organization_id: WorldEntity(
                id=organization_id,
                kind="polity",
                name="North Province",
                components={"governance": governance.model_dump(mode="json")},
            ),
            member_id: WorldEntity(id=member_id, kind="character", name="Mira"),
        },
        resources={
            source_id: ResourceAccount(resource_id="core.grain", balance="10", owner_id=organization_id),
            reserve_id: ResourceAccount(resource_id="core.grain", balance="0", owner_id=organization_id),
            project_id: ResourceAccount(resource_id="core.grain", balance="0", owner_id=organization_id),
        },
    )
    profile = OrganizationalStrategy(
        strategy_id="province_reserves",
        organization_id=organization_id,
        mission="Preserve a reliable grain reserve for public operations.",
        charter_clause_id=clause,
        authority_tag=authority,
        first_review_tick=3,
        review_interval_ticks=10,
        goals=[
            OrganizationalStrategyGoal(
                goal_id="grain.reserve",
                title="Maintain reserve stock",
                priority=100,
                conditions=[StrategyCondition(
                    condition_id="reserve_below_five",
                    kind="account_balance_below",
                    account_id=reserve_id,
                    threshold="5",
                )],
                action=StrategyTransferAction(
                    from_account_id=source_id,
                    to_account_id=reserve_id,
                    resource_id="core.grain",
                    quantity="4",
                    source_reserve_floor="2",
                ),
            ),
            OrganizationalStrategyGoal(
                goal_id="grain.projects",
                title="Fund district projects",
                priority=50,
                conditions=[StrategyCondition(
                    condition_id="projects_below_one",
                    kind="account_balance_below",
                    account_id=project_id,
                    threshold="1",
                )],
                action=StrategyTransferAction(
                    from_account_id=source_id,
                    to_account_id=project_id,
                    resource_id="core.grain",
                    quantity="1",
                ),
            ),
        ],
    )
    state = install_organizational_strategy(state, profile)
    state_hash_before = state_hash(state)
    schedule = next(
        row for row in state.scheduled_events
        if row.id == organization_strategy_schedule_id(profile)
    )
    state = apply_event(state, TimeAdvancedEvent(to_tick=3))
    schedule = next(row for row in state.scheduled_events if row.id == schedule.id)
    state_hash_before_resolution = state_hash(state)
    engine = RuleEngine()
    context = RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=uuid4(),
        actor_id=member_id,
        ruleset_version="1.0.0",
    )

    resolution = engine.resolve_scheduled(state, schedule, context)
    retried = engine.resolve_scheduled(state, schedule, context.model_copy(update={"command_id": uuid4()}))
    assert resolution.events == retried.events
    assert resolution.outcome.code == "organization_strategy_action_committed"
    assert resolution.events[0].review.authorized_actor_id == member_id
    assert resolution.events[0].review.goal_evaluations[0].conditions[0].observed_balance == Decimal("0")
    # Resolving produces a proposal; only the reducer changes authoritative state.
    assert state_hash(state) == state_hash_before_resolution
    assert state_hash_before != state_hash_before_resolution

    committed = apply_event(state, resolution.events[0])
    assert committed.resources[source_id].balance == Decimal("6")
    assert committed.resources[reserve_id].balance == Decimal("4")
    assert sum((posting.delta for posting in ledger_postings(resolution.events[0])), Decimal("0")) == 0
