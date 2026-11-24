"""Intervention points with separately authored, causal consequences."""

from dataclasses import dataclass
from uuid import UUID

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.story_nodes import (
    STORY_NODE_HANDLER,
    STORY_NODE_VERSION,
    StoryFollowup,
    StoryNode,
)
from world_engine.domain.world_books import WorldBookEntry

from .gensokyo_reference import _id
from .gensokyo_village_life import local_aid

MORIYA_SURVEY_PERMIT = "gensokyo.moriya_survey_passage"
MORIYA_SURVEY_ID = _id("world:incident:mountain-shrine:survey")


@dataclass(frozen=True)
class Chapter:
    key: str
    work: int
    position: str
    contact: str
    meeting: str
    hours: int
    joined: str
    independent: str
    consequence: str
    start: str = "human-village"
    access: tuple[str, ...] = ()


# The work number indexes evidence. The order is a package presentation choice
# where the source supplies no exact calendar date. Hours are from the opening.
CHAPTERS = (
    Chapter("scarlet-mist", 6, "红雾已遮住日照。", "reimu", "shrine", 4,
            "灵梦把你在神社提供的目击线索列入红雾调查。",
            "灵梦自行调查红雾；她没有收到你的现场报告。",
            "神社把雾况和调查方向告知人里，居民可以据此调整外出计划。"),
    Chapter("late-spring", 7, "本该开花的季节仍在下雪。", "marisa", "forest", 8,
            "魔理沙参考你在魔法森林交来的失春记录，扩大搜查范围。",
            "魔理沙自行追查冬季迟迟不退的原因。",
            "花期记录经整理后成为下次寻找春度的参考。",
            access=("gensokyo.netherworld_passage",)),
    Chapter("false-moon", 8, "异常的月亮已出现，黎明尚未到来。", "marisa", "forest", 3,
            "魔理沙把你在林中交来的月相观察纳入追查。",
            "魔理沙自行追查异常的月亮，不等人里的观测。",
            "参与调查的人开始比对不同地点留下的月相记录。"),
    Chapter("mountain-shrine", 10, "山上出现了新的神社，两方开始交涉。", "sanae", "moriya-shrine", 8,
            "早苗把你在守矢神社提出的通行顾虑带入交涉。",
            "早苗依守矢神社自己的考虑推进交涉。",
            "交涉结束后，相关参拜和过路事宜进入下一轮讨论。"),
    Chapter("geyser-spirits", 11, "神社附近的间歇泉带出了怨灵。", "reimu", "shrine", 6,
            "灵梦收到你带到神社的怨灵目击报告，优先核查报告中的地段。",
            "灵梦按自己的观察调查间歇泉和怨灵。",
            "神社收束第一轮搜查，周边居民据此选择当天的出行路线。"),
    Chapter("treasure-ship", 12, "幻想乡上空出现了一艘飞船。", "reimu", "shrine", 6,
            "灵梦把你从寺院一带带来的飞船目击报告加入搜查记录。",
            "灵梦等人自行追查飞船。",
            "第一轮目击报告留下了飞船活动的线索。",
            access=("gensokyo.makai_passage",)),
    Chapter("lunar-envoys", 15, "月之使者已出现，永远亭正研究对策。", "eirin", "eientei", 6,
            "永琳将你带来的现场记录纳入地面使者的调查。",
            "永远亭按自己取得的信息继续研究地面使者。",
            "永远亭保留此次接触的资料，作为判断月方意向的参考。",
            start="eientei", access=("place.enter_private", "gensokyo.dream_passage", "gensokyo.lunar_passage")),
    Chapter("animal-spirits", 17, "动物灵来袭，地狱已有通向更深处的线索。", "kutaka", "hell-checkpoint", 8,
            "久侘歌把你带来的通行记录整合进地狱关口的核验工作。",
            "久侘歌按关口已有记录处理今次通行。",
            "关口整理来往情况，后续访客仍须依凭各自许可通行。",
            start="hell-checkpoint", access=("gensokyo.sanzu_passage", "gensokyo.hell_passage",
                                             "gensokyo.animal_realm_passage")),
    Chapter("ability-card-trade", 18, "能力卡已流通，来源和交易动机仍待调查。", "megumu", "youkai-mountain", 8,
            "饭纲丸龙听到了你在山上转达的交易疑问，交易方重新核查参与供给的人。",
            "山上交易方继续按自己的安排分发能力卡。",
            "第一轮交易之后，可以根据实际流通追查卡片出处。"),
    Chapter("unclaimed-land", 19, "市场开放后，古老土地的原有归属已经消失。", "reimu", "shrine", 6,
            "灵梦把你报告的居民出入情况列入土地争夺调查。",
            "新的土地争夺继续发生，灵梦沿已知线索调查。",
            "第一批地块记录整理完毕，仍未明确归属的地段继续观察。",
            access=("gensokyo.sanzu_passage", "gensokyo.hell_passage")),
    Chapter("sanctuary-disturbance", 20, "少数人发现山脚圣域有异样。", "marisa", "forest", 8,
            "魔理沙在出发前经你提供的观察核对了圣域周边的异状。",
            "魔理沙带着已有线索继续调查异变与山脚圣域。",
            "初次调查之后，神社和森林分别保留自己的观察，可继续比对。",
            access=("gensokyo.barrier_passage", "gensokyo.sky_passage", "gensokyo.heaven_passage")),
)


HISTORY_WORKS = {
    "scarlet-mist": 6, "late-spring": 7, "false-moon": 8, "widespread-bloom": 9,
    "mountain-shrine": 10, "geyser-spirits": 11, "treasure-ship": 12, "divine-spirits": 13,
    "animated-tools": 14, "lunar-envoys": 15, "four-seasons": 16, "animal-spirits": 17,
    "ability-card-trade": 18, "unclaimed-land": 19, "sanctuary-disturbance": 20,
}

# Scenario timing for later story anchors in a continuous save. The interval
# controls when regional encounters become available. It is not a canon date.
CONTINUATION_DAYS = {
    "scarlet-mist": 120,
    "late-spring": 120,
    "false-moon": 180,
    "mountain-shrine": 120,
    "geyser-spirits": 120,
    "treasure-ship": 180,
    "lunar-envoys": 180,
    "animal-spirits": 180,
    "ability-card-trade": 180,
    "unclaimed-land": 180,
}


def incident_nodes(
    chapter: Chapter,
    evidence: WorldBookEntry,
    player_id: UUID,
    contact_id: UUID,
) -> tuple[ScheduledEventRecord, tuple[StoryFollowup, ...]]:
    """Openings share rule types, while the encounter and its outcome vary by incident."""
    incident_id = _id(f"world:incident:{chapter.key}")
    references = evidence.provenance.citations
    node = StoryNode.model_validate({
        "title": f"{evidence.title}·首轮调查",
        "visibility": "public",
        "references": references,
        "participant_ids": [contact_id],
        "opportunity": {
            "title": f"{evidence.title}的首轮调查",
            "detail": "到现场了解情况。想提交线索时，可联系调查人并登记；你也可以先继续自己的安排。",
            "place_id": _id(f"place:{chapter.meeting}"),
            "contact_id": contact_id,
            "lead_ticks": chapter.hours * 3600,
            **({
                "known_cost": "从人间之里到博丽神社，按当前开放路线步行约需 45 分钟。",
                "possible_impact": "及时登记目击可影响灵梦的调查和神社对受红雾影响者的安置。口粮属于公共救济。",
            } if chapter.key == "scarlet-mist" else {}),
            **({
                "detail": "可去守矢神社联系早苗并登记来客顾虑，也可在妖怪之山完成山道安全核查。",
                "known_cost": "两种处理方式都要赶在首轮交涉结束前到场。核查另需具备器具检验能力。",
                "possible_impact": "交涉提案可促成口粮援助。若只完成核查，可获得山道试行资格。",
            } if chapter.key == "mountain-shrine" else {}),
        },
        "variants": [{
            "when": [
                {"kind": "challenge_passed", "actor_id": player_id,
                 "challenge_id": _id(f"world:incident:{chapter.key}:report")},
                {"kind": "world_condition", "owner_id": player_id, "condition": {
                    "kind": "relationship_dimension_minimum", "subject_id": player_id,
                    "target_id": contact_id, "dimension": "familiarity", "threshold": "1",
                }},
            ],
            "outcome": {"key": "player-report", "effect": "mitigated", "summary": chapter.joined},
        }, *([{
            "when": [{"kind": "challenge_passed", "actor_id": player_id, "challenge_id": MORIYA_SURVEY_ID}],
            "outcome": {
                "key": "survey-passage", "effect": "mitigated",
                "summary": "山道核查获认可。守矢神社准许你试行已核查的近道，口粮仍依原来安排。",
                "awards": [{"actor_id": player_id, "capability": MORIYA_SURVEY_PERMIT}],
            },
        }] if chapter.key == "mountain-shrine" else [])],
        "fallback": {"key": "independent", "effect": "occurred", "summary": chapter.independent},
    })
    schedule = ScheduledEventRecord(
        id=incident_id, due_tick=chapter.hours * 3600, phase="observation",
        handler_id=STORY_NODE_HANDLER, handler_version=STORY_NODE_VERSION, payload=node.model_dump(mode="json"),
    )

    def after(outcome: str, prefix: str) -> StoryFollowup:
        response = local_aid(chapter.key) if outcome == "player-report" else None
        variants = []
        if response is not None:
            variants.append({
                "when": [
                    {"kind": "prior_outcome", "schedule_id": schedule.id, "outcome": "player-report"},
                    *({"kind": "world_condition", "owner_id": place, "condition": {
                        "kind": "entity_status", "subject_id": place, "status": "active",
                    }} for place in (response.origin_id, response.destination_id)),
                ],
                "outcome": {
                    "key": "aid-delivered", "effect": "mitigated",
                    "summary": prefix + response.summary, "transfer": response.transfer.model_dump(mode="json"),
                },
            })
        return StoryFollowup(
            followup_id=_id(f"world:incident:{chapter.key}:{outcome}:followup"),
            after_id=schedule.id, after_outcome=outcome, delay_ticks=response.delay_ticks if response else 3600,
            node=StoryNode.model_validate({
                "title": f"{evidence.title}·后续记录", "visibility": "public",
                "participant_ids": [contact_id],
                "variants": variants,
                "fallback": {"key": "recorded", "effect": "occurred", "summary": prefix + chapter.consequence},
            }),
        )

    afters = (
        after("player-report", "本轮记录保留了玩家提供的线索。"),
        after("independent", "本轮记录按原有调查渠道汇总。"),
    )
    if chapter.key == "mountain-shrine":
        afters += (after("survey-passage", "山道试行资格已登记，外来访客仍适用原有通行安排。"),)
    return schedule, afters
