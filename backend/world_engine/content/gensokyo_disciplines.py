"""World-pack skills, training and consensual duels using generic rules."""

from uuid import UUID

from world_engine.domain.challenges import ChallengeDefinition
from world_engine.domain.contests import ContestDefinition
from world_engine.domain.models import WorldEntity

from .gensokyo_reference import _id


_SKILLS = {
    "magic": "魔法实作",
    "youkai": "妖力辨识",
    "technology": "器具检验",
    "danmaku": "弹幕控制",
    "declaration": "符卡声明",
    "observation": "现场记录",
}
_EXERCISES = (
    ("magic", "forest", "魔法森林的术式试验", "练习区分施术意图、材料和术式效果。"),
    ("youkai", "bamboo", "竹林异能辨识", "根据可核实的现象辨认一种个体能力及其触发条件。"),
    ("technology", "kourindou", "香霖堂的外界器具检验", "分别说明道具的名称、用途和实际操作方式。"),
    ("declaration", "shrine", "博丽神社的符卡声明", "说明自拟符卡的名称和约定的胜负边界。"),
)

_ACTOR_SKILLS = {
    "reimu": {"danmaku": 17, "declaration": 18, "youkai": 14},
    "marisa": {"magic": 18, "danmaku": 16, "declaration": 15},
    "yukari": {"danmaku": 18, "youkai": 18, "declaration": 15},
    "patchouli": {"magic": 20, "danmaku": 13},
    "alice": {"magic": 17, "danmaku": 15},
    "nitori": {"technology": 18, "danmaku": 13},
    "eirin": {"technology": 19, "youkai": 16},
    "sanae": {"danmaku": 15, "declaration": 15, "youkai": 12},
}


def skill_levels(key: str) -> dict:
    # Numbers describe this simulation's thresholds, independently of lore citations.
    levels = dict.fromkeys(_SKILLS, 10) if key == "player" else _ACTOR_SKILLS.get(key, {})
    return {"skills": {f"gensokyo.{skill}": {"level": str(level)} for skill, level in levels.items()}}


def local_play(
    entities: dict[UUID, WorldEntity], reimu: UUID, marisa: UUID,
) -> tuple[list[ChallengeDefinition], list[ContestDefinition]]:
    challenges = [
        ChallengeDefinition(
            challenge_id=_id(f"world:challenge:{skill}"), venue_id=_id(f"place:{place}"),
            title=title, description=description + " 及格门槛为本包游玩配置。",
            skill_id=f"gensokyo.{skill}", skill_label=_SKILLS[skill], pass_level="12", requires_pass_level=True,
            note_on_pass=(
                "已经练习过符卡的命名、限额与双方同意，场地规则仍由双方确认。"
                if skill == "declaration" else None
            ),
        )
        for skill, place, title, description in _EXERCISES if _id(f"place:{place}") in entities
    ]
    contests = []
    for place, person, title in (
        ("shrine", reimu, "与灵梦的符卡切磋"),
        ("forest", marisa, "与魔理沙的符卡切磋"),
    ):
        if person not in entities:
            continue
        contests.append(ContestDefinition.model_validate({
            "definition_id": _id(f"world:contest:{place}"), "venue_id": _id(f"place:{place}"), "title": title,
            "terms": "双方先约定命名招式和三轮限额，再确认是否开打。按有效轮数结算，同分和局。切磋不造成伤害。",
            "proposer_challenge_id": _id("world:challenge:declaration"),
            "npc_routine": {person: "boundary-line" if person == reimu else "wandering-stars"},
            "routines": [
                {"key": "boundary-line", "title": "展界线", "skills": [
                    "gensokyo.declaration", "gensokyo.danmaku", "gensokyo.youkai",
                ]},
                {"key": "wandering-stars", "title": "巡星式", "skills": [
                    "gensokyo.magic", "gensokyo.danmaku", "gensokyo.declaration",
                ]},
            ],
        }))
    return challenges, contests
