"""Source-backed lore and two starting points for the reference world package."""

from uuid import UUID, uuid5

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.world_books import WorldBookCitation, WorldBookEntry, WorldBookProvenance


PACK_ID = "author.gensokyo-reference"
PACK_TITLE = "幻想乡原作资料与异变年代"
PACK_VERSION = "0.1.0"
_NAMESPACE = UUID("0d0063f6-45d8-4aaa-9873-135f3d9b5a5f")

_WORKS = {
    "zun.th08.characters": "ZUN《东方永夜抄》原版手册",
    "ichijinsha.silent-sinner.characters": "一迅社《东方儚月抄》人物介绍",
    "ichijinsha.silent-sinner.world": "一迅社《东方儚月抄》世界介绍",
    "official.th06": "《东方红魔乡》官方简介",
    "official.th07": "《东方妖妖梦》官方简介",
    "official.th08": "《东方永夜抄》官方简介",
    "kadokawa.suzunaan.1": "KADOKAWA《东方铃奈庵》第一卷简介",
    "kadokawa.suzunaan.7": "KADOKAWA《东方铃奈庵》第七卷馆藏书目",
}

# Entries summarize supported facts. Identical names in other contexts need separate source checks.
_PEOPLE = (
    ("reimu", "博丽灵梦", "博麗霊夢", "博丽神社的巫女，居于幻想乡与外界交界处；手册记载她能够飞行。", "zun.th08.characters"),
    ("marisa", "雾雨魔理沙", "霧雨魔理沙", "住在魔法森林的人类魔法使。", "zun.th08.characters"),
    ("yukari", "八云紫", "八雲紫", "住在幻想乡边界一带，能够操纵境界。原版手册未给出精确年龄。", "zun.th08.characters"),
    ("sakuya", "十六夜咲夜", "十六夜咲夜", "红魔馆的女仆长。手册记载她有操纵时间的能力。", "zun.th08.characters"),
    ("remilia", "蕾米莉亚·斯卡蕾特", "レミリア・スカーレット", "居住在红魔馆的吸血鬼。", "zun.th08.characters"),
    ("alice", "爱丽丝·玛格特洛依德", "アリス・マーガトロイド", "住在魔法森林，擅长人偶相关魔法。", "zun.th08.characters"),
    ("youmu", "魂魄妖梦", "魂魄妖夢", "半人半灵。", "zun.th08.characters"),
    ("yuyuko", "西行寺幽幽子", "西行寺幽々子", "西行寺家的亡灵，住在冥界。", "ichijinsha.silent-sinner.characters"),
    ("kaguya", "蓬莱山辉夜", "蓬莱山輝夜", "原来是月之都一方的公主。《东方儚月抄》介绍记载她居于永远亭。", "ichijinsha.silent-sinner.characters"),
    ("eirin", "八意永琳", "八意永琳", "与辉夜一同居于永远亭，擅长制药。", "ichijinsha.silent-sinner.characters"),
    ("tewi", "因幡帝", "因幡てゐ", "迷途竹林里的兔妖，在野兔中有领导地位。", "ichijinsha.silent-sinner.characters"),
    ("patchouli", "帕秋莉·诺蕾姬", "パチュリー・ノーレッジ", "在红魔馆地下图书馆居住的魔法使，擅长属性魔法。", "ichijinsha.silent-sinner.characters"),
    ("kosuzu", "本居小铃", "本居小鈴", "与租书店铃奈庵有关的少女，《东方铃奈庵》第一卷的主要角色。", "kadokawa.suzunaan.1"),
)

_PLACES = (
    ("human-village", "人间之里", "人間の里", "原作中出现的人类聚居地。", "kadokawa.suzunaan.7", "第七卷介绍"),
    ("bamboo", "迷途竹林", "迷いの竹林", "永远亭位于这片竹林深处，难以抵达。", "ichijinsha.silent-sinner.world", "永远亭段"),
    ("mansion", "红魔馆", "紅魔館", "蕾米莉亚居住的洋馆。馆内有地下图书馆。", "ichijinsha.silent-sinner.world", "红魔馆段"),
    ("shrine", "博丽神社", "博麗神社", "位于幻想乡的外界交界处。", "ichijinsha.silent-sinner.world", "博丽神社段"),
    ("eientei", "永远亭", "永遠亭", "位于迷途竹林内的宅邸。", "ichijinsha.silent-sinner.world", "永远亭段"),
    ("forest", "魔法森林", "魔法の森", "魔理沙和爱丽丝的居所所在区域。", "zun.th08.characters", "魔理沙、爱丽丝条目"),
    ("netherworld", "冥界", "冥界", "西行寺家宅邸所在的异界。", "ichijinsha.silent-sinner.world", "西行寺宅邸段"),
    ("kourindou", "香霖堂", "香霖堂", "森林入口附近的道具店，经营多类道具。", "ichijinsha.silent-sinner.world", "香霖堂段"),
)

_HISTORY = (
    ("scarlet-mist", "红雾异变", "《东方红魔乡》确认笼罩幻想乡的雾和灵梦开始调查。简介中的发售日属于作品发行记录。", "official.th06"),
    ("late-spring", "迟来的春天", "到了惯常花开的季节，幻想乡的降雪仍在继续。作品简介没有给出可用于建档的确切日期。", "official.th07"),
    ("false-moon", "满月异变", "真实满月消失，出现替代的月亮。人类和妖怪尝试延缓时间，争取在黎明前查明原因。", "official.th08"),
)

_SEARCH_ALIASES = {"reimu": ("灵梦",), "marisa": ("魔理沙",), "human-village": ("人里",)}


def _id(key: str) -> UUID:
    return uuid5(_NAMESPACE, key)


def _entry(key, title, aliases, content, category, source_id, locator) -> WorldBookEntry:
    return WorldBookEntry(
        entry_id=_id(f"reference:{key}"), source_key=key, title=title,
        keywords=tuple(dict.fromkeys((title, *aliases, *_SEARCH_ALIASES.get(key, ())))),
        content=content, always_on=False, visibility="public",
        provenance=WorldBookProvenance(
            category=category, status="documented",
            citations=(WorldBookCitation(source_id=source_id, work=_WORKS[source_id], locator=locator),),
        ),
    )


def build_reference_entries() -> list[WorldBookEntry]:
    """References read by the GM, distinct from the simulation's NPC state."""
    return [
        *(_entry(key, title, (alias,), content, "person", source, alias)
          for key, title, alias, content, source in _PEOPLE),
        *(_entry(key, title, (alias,), content, "place", source, locator)
          for key, title, alias, content, source, locator in _PLACES),
        *(_entry(key, title, (), content, "historical_event", source, "故事简介")
          for key, title, content, source in _HISTORY),
    ]


def _scenario(*, incident: str, description: str, known_incidents: frozenset[str]) -> dict:
    historical_keys = {key for key, *_ in _HISTORY}
    entries = [entry for entry in build_reference_entries()
               if entry.source_key not in historical_keys or entry.source_key in known_incidents]
    village = _id("place:human-village")
    places = {
        _id(f"place:{key}"): WorldEntity(id=_id(f"place:{key}"), name=title, kind="place")
        for key, title, *_ in _PLACES
    }
    player = _id("actor:resident")
    places[player] = WorldEntity(id=player, name="乡里的观测员", kind="character", location_id=village)
    return WorldState(
        player_id=player, entities=places, world_book_entries=entries,
        metadata={"pack_id": PACK_ID, "pack_version": PACK_VERSION, "public_title": PACK_TITLE,
                  "public_description": description, "reference_incident": incident},
    ).model_dump(mode="json")


def build_archive() -> bytes:
    openings = [
        {"opening_id": "scarlet-mist", "title": "红雾出现后", "position": "红雾已影响日照，世界内日期未定。",
         "known_history": ["幻想乡出现遮挡日照的浓雾。"],
         "scenario": _scenario(incident="scarlet-mist", description="你在里内负责记录雾的影响。",
                               known_incidents=frozenset({"scarlet-mist"}))},
        {"opening_id": "false-moon", "title": "满月异变之夜", "position": "异常月亮已出现，黎明之前。",
         "known_history": ["今晚的满月与平日不同，人类与妖怪正在查明缘由。"],
         "scenario": _scenario(incident="false-moon", description="你在里内记录今夜的月相和照明变化。",
                               known_incidents=frozenset({"scarlet-mist", "late-spring", "false-moon"}))},
    ]
    return write_world_pack(manifest=WorldPackManifest(pack_id=PACK_ID, version=PACK_VERSION, title=PACK_TITLE),
                            template={"story_openings": openings})
