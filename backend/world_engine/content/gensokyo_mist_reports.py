"""Statements the player can explicitly register during the first red-mist investigation."""

from uuid import UUID

from world_engine.domain.challenges import ChallengeDefinition, ReportTerms
from world_engine.domain.components import KnowledgeRecord, VisibilityScope

from .gensokyo_reference import _id


OBSERVATION = "我在人里村口对照了檐下日影：红雾越过屋脊后，日影在同一处石阶上消失了。"
RUMOR = "村口的行商说北边的红雾更浓；我没有到北边核实。"
CHECK_ID = _id("world:incident:scarlet-mist:observe-shadow")
RUMOR_ID = _id("world:incident:scarlet-mist:heard-at-village")
SOURCES = {
    "observation": ("提交村口的现场记录", OBSERVATION, "player-report", "灵梦把你在神社提供的目击线索列入红雾调查。"),
    "hearsay": ("转述村口行商的话", RUMOR, "heard-rumor", "灵梦将行商的说法标作待核实的传闻，另行调查。"),
    "self_report": ("登记未经核实的自述", "我觉得雾像是从湖边来的，但没有留下可查记录。", "unverified-account",
                    "灵梦记下你对来雾方向的个人判断，尚未取得现场依据。"),
    "suggestion": ("建议在岔路设置指引", "可以在岔路挂路牌，引导受雾影响的来客。", "recorded-suggestion",
                   "灵梦记下你的路牌建议，照原计划核查雾况。"),
}


def report_id(kind: str) -> UUID:
    return _id("world:incident:scarlet-mist:report" if kind == "observation"
               else f"world:incident:scarlet-mist:report:{kind}")


def mist_reports(player: UUID, contact: UUID, deadline: UUID) -> tuple[list[ChallengeDefinition], KnowledgeRecord]:
    common = {"skill_id": "gensokyo.observation", "skill_label": "现场记录", "closes_after_story_id": deadline}
    village = _id("place:human-village")
    options = [ChallengeDefinition(
        challenge_id=CHECK_ID, venue_id=village, title="在村口查验红雾和日影",
        description="在雾越过屋脊时查看石阶上的日影，写下可带去神社的现场笔记。",
        pass_level="1", note_on_pass=OBSERVATION, action_label="查验并记录日影", **common,
    )]
    for kind, (title, statement, _, _) in SOURCES.items():
        options.append(ChallengeDefinition(
            challenge_id=report_id(kind), venue_id=_id("place:shrine"), title=title,
            description="在博丽神社当面交给灵梦。她将分别记录现场依据、传闻、个人判断和建议。",
            pass_level="0", required_note_from=CHECK_ID if kind == "observation" else None,
            report=ReportTerms(recipient_id=contact, source_kind=kind, statement=statement,
                               rumor_id=RUMOR_ID if kind == "hearsay" else None), **common,
        ))
    return options, KnowledgeRecord(
        id=RUMOR_ID, holder_id=player, subject_id=village, claim=RUMOR, kind="rumor",
        summary="在村口听行商谈雾。", visibility=VisibilityScope(policy="subject_ids", subject_ids=[player]),
    )
