"""Authored operating policies with no required story outcome."""

from decimal import Decimal
from uuid import UUID, uuid5

from world_engine.domain.models import ResourceAccount, WorldState


def reserve_strategy(
    state: WorldState, organization_id: UUID, source_id: UUID, *, mission: str,
    review_days: int = 30, scale: int = 1, officer_id: UUID | None = None, first_review_days: int = 2,
    allow_current_leader: bool = False,
) -> WorldState:
    from world_engine.domain.organization_strategy import (
        OrganizationalStrategy, OrganizationalStrategyGoal, StrategyCondition,
        StrategyTransferAction, install_organizational_strategy,
    )

    organization = state.entities[organization_id]
    governance = organization.components["governance"]
    officer_id = str(officer_id) if officer_id is not None else governance.get("leader_id")
    if not officer_id or officer_id not in governance.get("members", ()):
        if not allow_current_leader:
            return state
        officer_id = None
    authority = "authority.internal_reserves"
    clause = "charter.internal_reserves"
    source = state.resources[source_id]
    targets = {key: uuid5(organization_id, f"strategy-reserve:{key}:{source.resource_id}")
               for key in ("operations", "development")}
    resources = state.resources | {
        ident: ResourceAccount(resource_id=source.resource_id, owner_id=organization_id, balance=0)
        for ident in targets.values()
    }
    officers = [dict(office) for office in governance.get("offices", ())]
    if officer_id is not None:
        for office in officers:
            if office.get("holder_id") == officer_id:
                office["authority"] = list(dict.fromkeys([*office.get("authority", []), authority]))
                break
        else:
            officers.append({"office_id": "office.reserve_keeper", "title": "储备经办", "holder_id": officer_id,
                             "authority": [authority]})
    organization = organization.model_copy(update={"components": organization.components | {
        "governance": governance | {
            "offices": officers,
            "authority_tags": [*governance.get("authority_tags", []), authority],
            "charter_clause_ids": [*governance.get("charter_clause_ids", []), clause],
        },
    }})
    state = state.model_copy(update={"entities": state.entities | {organization_id: organization}, "resources": resources})

    def goal(key, title, priority, threshold, reserve_floor, members):
        return OrganizationalStrategyGoal(
            goal_id=f"reserve.{key}", title=title, priority=priority,
            conditions=[
                StrategyCondition(condition_id="target_below_minimum", kind="account_balance_below",
                                  account_id=targets[key], threshold=Decimal(scale * threshold)),
                StrategyCondition(condition_id="staffing", kind="active_member_count_at_least",
                                  threshold=Decimal(members)),
            ],
            action=StrategyTransferAction(
                from_account_id=source_id, to_account_id=targets[key], resource_id=source.resource_id,
                quantity=Decimal(scale * 2), source_reserve_floor=Decimal(scale * reserve_floor),
            ),
        )

    return install_organizational_strategy(state, OrganizationalStrategy(
        strategy_id="internal_reserves", organization_id=organization_id,
        mission=mission, charter_clause_id=clause, authority_tag=authority,
        first_review_tick=state.current_tick + first_review_days * state.calendar.day_seconds,
        review_interval_ticks=review_days * state.calendar.day_seconds,
        allow_current_leader=allow_current_leader,
        goals=[
            goal("operations", "维持日常事务储备", 90, 4, 2, 1),
            goal("development", "在有余力时积累发展储备", 50, 6, 6, 2),
        ],
    ))
