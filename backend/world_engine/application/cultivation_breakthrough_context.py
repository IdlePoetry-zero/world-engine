"""Player-visible cultivation policy terms and action feedback."""

from uuid import UUID

from world_engine.domain.combat import active_combat_opponent
from world_engine.domain.components import ProgressionComponent, SkillsComponent
from world_engine.domain.cultivation_breakthrough import (
    CULTIVATION_PRACTICE_SKILL_ID,
    CULTIVATION_REALM_MILESTONE_PREFIX,
    CultivationBreakthroughError,
    cultivation_breakthrough_policies,
    plan_cultivation_breakthrough,
)
from world_engine.domain.models import WorldView
from world_engine.rules.versions import cultivation_disabled_by_world_rules


_REALM_NAMES = {"mortal": "未入境", "qi_gathering": "聚气", "foundation": "筑基"}
_BLOCKED_REASONS = {
    "cultivation_capability_missing": "当前角色尚未获得修行能力。",
    "capabilities_invalid": "当前角色尚未登记修行能力。",
    "practice_skill_missing": "当前角色尚未习得修行功法。",
    "practice_skill_insufficient": "功法熟练度未达到这次突破的门槛。",
    "qi_unavailable_at_completion": "可用灵气不足，或会在突破完成前失效。",
}


def cultivation_breakthrough_options(view: WorldView, actor_id: UUID) -> list[dict]:
    policies = cultivation_breakthrough_policies(view.state)
    actor = view.state.entities.get(actor_id)
    if not policies or actor is None or actor.status != "active":
        return []
    try:
        progression = ProgressionComponent.model_validate(actor.components.get("progression"))
    except (TypeError, ValueError):
        return []
    try:
        skills = SkillsComponent.model_validate(actor.components.get("skills"))
        practice = skills.skills.get(CULTIVATION_PRACTICE_SKILL_ID)
    except (TypeError, ValueError):
        practice = None
    current = next((
        name.removeprefix(CULTIVATION_REALM_MILESTONE_PREFIX)
        for name in reversed(progression.milestones)
        if name.startswith(CULTIVATION_REALM_MILESTONE_PREFIX)
    ), policies[0].initial_realm)
    unavailable = None
    if cultivation_disabled_by_world_rules(view.world.ruleset_version, view.state.metadata):
        unavailable = "此世界已禁用魔法，当前规则版本禁止突破。"
    elif active_combat_opponent(view.state, actor) is not None:
        unavailable = "当前仍在交战。"
    elif actor.location_id is None or any(
        journey.actor_id == actor_id and journey.status == "in_transit"
        for journey in view.state.travel_journeys
    ):
        unavailable = "到达可停留的地点后才能开始突破。"
    options = []
    for policy in policies:
        if policy.source_realm != current:
            continue
        option = {
            "policy_id": policy.policy_id,
            "source_realm_name": _REALM_NAMES.get(current, current),
            "target_realm_name": _REALM_NAMES.get(policy.target_realm, policy.target_realm),
            "duration_ticks": str(policy.duration_ticks),
            "qi_cost": str(policy.qi_cost),
            "minimum_skill_level": str(policy.minimum_practice_skill_level),
            "practice_skill_level": str(practice.level) if practice is not None else None,
            "failure_damage": policy.failure_damage,
        }
        if unavailable is not None:
            options.append(option | {"available": False, "chance_percent": None, "blocked_reason": unavailable})
            continue
        try:
            # This preview uses a fixed placeholder ID. Only policy terms and chance
            # are exposed. The committed command derives its own unpreviewed roll.
            plan = plan_cultivation_breakthrough(
                view.state, actor_id=actor_id, policy=policy, world_id=view.world.world_id,
                branch_id=view.branch.branch_id, command_id=UUID(int=0),
                ruleset_version=view.world.ruleset_version,
            )
        except CultivationBreakthroughError as error:
            options.append(option | {
                "available": False, "chance_percent": None,
                "blocked_reason": _BLOCKED_REASONS.get(error.code, "当前条件无法开始突破。"),
            })
        else:
            options.append(option | {
                "available": True, "chance_percent": plan.chance_percent, "blocked_reason": None,
            })
    return options


def cultivation_breakthrough_feedback(outcome: str, target_realm: str, duration_ticks: int, qi_cost: str) -> str:
    spent = f"经过 {duration_ticks} 世界秒，消耗了 {qi_cost} 单位灵气。"
    if outcome == "succeeded":
        return f"突破成功，已进入{_REALM_NAMES.get(target_realm, target_realm)}境。{spent}"
    return f"突破失败，境界没有晋升，已记录此次伤势。{spent}"
