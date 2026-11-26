from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
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


def _strategy_world(*, organization_kind: str = "company", authorized: bool = True):
    organization_id, member_id, source_id, reserve_id, projects_id = (uuid4() for _ in range(5))
    authority = "authority.strategy_execute"
    clause = "charter.strategy_review"
    governance = GovernanceComponent(
        members=[member_id],
        authority_tags=[authority] if authorized else [],
        charter_clause_ids=[clause] if authorized else [],
        offices=[OfficeRecord(
            office_id="office.steward",
            title="Steward",
            holder_id=member_id,
            authority=[authority] if authorized else [],
        )],
    )
    state = WorldState(
        entities={
            organization_id: WorldEntity(
                id=organization_id,
                kind=organization_kind,
                name="Example organization",
                components={"governance": governance.model_dump(mode="json")},
            ),
            member_id: WorldEntity(id=member_id, kind="person", name="Authorized steward"),
        },
        resources={
            source_id: ResourceAccount(resource_id="core.credit", balance="20", owner_id=organization_id),
            reserve_id: ResourceAccount(resource_id="core.credit", balance="0", owner_id=organization_id),
            projects_id: ResourceAccount(resource_id="core.credit", balance="0", owner_id=organization_id),
        },
    )
    strategy = OrganizationalStrategy(
        strategy_id="internal_reserves",
        organization_id=organization_id,
        mission="Keep essential organizational reserves available.",
        charter_clause_id=clause,
        authority_tag=authority,
        first_review_tick=0,
        review_interval_ticks=10,
        max_reviews=3,
        goals=[
            OrganizationalStrategyGoal(
                goal_id="reserve.operations",
                title="Maintain the operations reserve",
                priority=90,
                conditions=[
                    StrategyCondition(
                        condition_id="reserve_low",
                        kind="account_balance_below",
                        account_id=reserve_id,
                        threshold="5",
                    ),
                    StrategyCondition(
                        condition_id="minimum_staffing",
                        kind="active_member_count_at_least",
                        threshold="1",
                    ),
                ],
                action=StrategyTransferAction(
                    from_account_id=source_id,
                    to_account_id=reserve_id,
                    resource_id="core.credit",
                    quantity="5",
                    source_reserve_floor="2",
                ),
            ),
            OrganizationalStrategyGoal(
                goal_id="reserve.projects",
                title="Maintain project funds",
                priority=40,
                conditions=[
                    StrategyCondition(
                        condition_id="projects_low",
                        kind="account_balance_below",
                        account_id=projects_id,
                        threshold="2",
                    ),
                ],
                action=StrategyTransferAction(
                    from_account_id=source_id,
                    to_account_id=projects_id,
                    resource_id="core.credit",
                    quantity="2",
                ),
            ),
        ],
    )
    return install_organizational_strategy(state, strategy), strategy


def _rule_context(*, command_id=None):
    return RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=command_id or uuid4(),
        actor_id=uuid4(),
        ruleset_version="1.0.0",
    )


@pytest.mark.parametrize("organization_kind", ["organization", "polity", "government", "faction", "company"])
def test_strategy_profile_is_reusable_across_governed_organization_kinds(organization_kind):
    state, strategy = _strategy_world(organization_kind=organization_kind)

    assert state.organizational_strategies[strategy.organization_id] == strategy
    assert len(strategy.goals) == 2
    schedule = next(row for row in state.scheduled_events if row.id == organization_strategy_schedule_id(strategy))
    assert schedule.handler_id == "organization.strategy_review"
    assert schedule.repeat_ticks == 10


def test_two_organizations_with_the_same_local_strategy_id_resolve_and_replay_independently():
    first_state, first = _strategy_world(organization_kind="company")
    second_base, second = _strategy_world(organization_kind="polity")
    second = second.model_copy(update={"strategy_id": first.strategy_id})
    # Build both profiles into a single world, preserving the same local policy
    # name while keeping their organization and schedule IDs distinct.
    organizations = {**first_state.entities, **second_base.entities}
    resources = {**first_state.resources, **second_base.resources}
    combined = WorldState(entities=organizations, resources=resources)
    combined = install_organizational_strategy(combined, first)
    combined = install_organizational_strategy(combined, second)

    engine = RuleEngine()
    events = []
    projected = combined
    for schedule in sorted(projected.scheduled_events, key=lambda row: str(row.id)):
        resolution = engine.resolve_scheduled(projected, schedule, _rule_context())
        assert resolution.outcome.status == "succeeded"
        assert len(resolution.events) == 1
        event = parse_event(resolution.events[0].model_dump(mode="json"))
        assert event.review.organization_id == UUID(schedule.payload["organization_id"])
        events.append(event)
        projected = apply_event(projected, event)

    assert len(projected.organizational_strategy_reviews) == 2
    assert {record.reason for record in projected.organizational_strategy_reviews.values()} == {"action_executed"}
    assert sum((posting.delta for event in events for posting in ledger_postings(event)), Decimal("0")) == 0
    assert len({event.schedule_id for event in events}) == 2
    replayed = combined
    for event in events:
        replayed = apply_event(replayed, event)
    assert state_hash(replayed) == state_hash(projected)


def test_review_event_abstains_without_current_charter_authority_and_leaves_resources_untouched():
    state, strategy = _strategy_world(authorized=False)
    schedule = next(row for row in state.scheduled_events if row.id == organization_strategy_schedule_id(strategy))
    resolution = RuleEngine().resolve_scheduled(state, schedule, _rule_context())
    event = resolution.events[0]
    assert event.review.reason == "no_current_authority"
    assert not event.review.charter_clause_present
    assert not event.review.organization_authority_present
    assert event.review.authorized_actor_id is None

    updated = apply_event(state, event)
    assert updated.resources == state.resources
    assert updated.organizational_strategy_reviews[strategy.organization_id] == event.review
    assert ledger_postings(event) == ()
    next_schedule = next(row for row in updated.scheduled_events if row.id == schedule.id)
    assert (next_schedule.due_tick, next_schedule.completed_runs) == (10, 1)


def test_review_abstains_when_no_goal_action_preserves_the_required_reserve_floor():
    state, strategy = _strategy_world()
    source_id = strategy.goals[0].action.from_account_id
    resources = dict(state.resources)
    resources[source_id] = resources[source_id].model_copy(update={"balance": Decimal("1")})
    state = WorldState.model_validate(state.model_dump(mode="python") | {"resources": resources})
    schedule = next(row for row in state.scheduled_events if row.id == organization_strategy_schedule_id(strategy))
    event = RuleEngine().resolve_scheduled(state, schedule, _rule_context()).events[0]

    assert event.review.reason == "insufficient_resources"
    assert event.review.selected_goal_id is None
    assert all(not item.action_fundable for item in event.review.goal_evaluations)
    updated = apply_event(state, event)
    assert updated.resources[source_id].balance == Decimal("1")
    assert updated.resources[strategy.goals[0].action.to_account_id].balance == Decimal("0")


def test_recurring_review_reconsiders_next_goal_then_abstains_when_conditions_are_met():
    state, strategy = _strategy_world()
    engine = RuleEngine()
    context = _rule_context()
    schedule_id = organization_strategy_schedule_id(strategy)

    first_schedule = next(row for row in state.scheduled_events if row.id == schedule_id)
    first = engine.resolve_scheduled(state, first_schedule, context).events[0]
    assert first.review.selected_goal_id == "reserve.operations"
    assert first.review.reason == "action_executed"
    state = apply_event(state, first)
    state = apply_event(state, TimeAdvancedEvent(to_tick=10))

    second_schedule = next(row for row in state.scheduled_events if row.id == schedule_id)
    second = engine.resolve_scheduled(state, second_schedule, _rule_context()).events[0]
    assert second.review.review_index == 2
    assert second.review.selected_goal_id == "reserve.projects"
    state = apply_event(state, second)
    state = apply_event(state, TimeAdvancedEvent(to_tick=20))

    third_schedule = next(row for row in state.scheduled_events if row.id == schedule_id)
    third = engine.resolve_scheduled(state, third_schedule, _rule_context()).events[0]
    assert third.review.review_index == 3
    assert third.review.reason == "no_conditions_met"
    assert third.review.transfer is None
    state = apply_event(state, third)

    next_schedule = next(row for row in state.scheduled_events if row.id == schedule_id)
    assert next_schedule.status == "completed"
    assert next_schedule.completed_runs == strategy.max_reviews
    assert sum((account.balance for account in state.resources.values()), Decimal("0")) == Decimal("20")


def test_reducer_rejects_tampered_condition_evidence_even_when_event_schema_is_valid():
    state, strategy = _strategy_world()
    schedule = next(row for row in state.scheduled_events if row.id == organization_strategy_schedule_id(strategy))
    event = RuleEngine().resolve_scheduled(state, schedule, _rule_context()).events[0]
    first_goal = event.review.goal_evaluations[0]
    first_condition = first_goal.conditions[0].model_copy(update={"observed_balance": Decimal("99")})
    forged_goal = first_goal.model_copy(update={"conditions": (first_condition, *first_goal.conditions[1:])})
    forged_review = event.review.model_copy(update={"goal_evaluations": (forged_goal, *event.review.goal_evaluations[1:])})
    forged_event = event.model_copy(update={"review": forged_review})

    with pytest.raises(InvalidEvent, match="does not match current state"):
        apply_event(state, forged_event)


def test_strategy_rejects_unbalanced_or_foreign_accounts_at_install():
    state, strategy = _strategy_world()
    foreign_id = uuid4()
    foreign_owner = uuid4()
    foreign_entities = {**state.entities, foreign_owner: WorldEntity(id=foreign_owner, kind="company")}
    foreign_resources = {
        **state.resources,
        foreign_id: ResourceAccount(resource_id="core.credit", balance="0", owner_id=foreign_owner),
    }
    state = WorldState(entities=foreign_entities, resources=foreign_resources)
    altered_goal = strategy.goals[0].model_copy(update={
        "action": strategy.goals[0].action.model_copy(update={"to_account_id": foreign_id}),
    })
    altered = strategy.model_copy(update={"goals": (altered_goal, *strategy.goals[1:])})
    with pytest.raises(ValueError, match="owned by the organization"):
        install_organizational_strategy(state, altered)


def test_strategy_profile_requires_multiple_goals():
    _, strategy = _strategy_world()
    with pytest.raises(ValidationError, match="at least 2"):
        OrganizationalStrategy.model_validate(
            strategy.model_dump(mode="python") | {"goals": [strategy.goals[0]]}
        )
