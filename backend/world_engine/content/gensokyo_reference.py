"""Source-backed lore and two starting points for the reference world package."""

from uuid import UUID, uuid5

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.components import PlaceComponent, PlaceRoute, ScheduledEventRecord
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.story_nodes import STORY_NODE_HANDLER, STORY_NODE_VERSION, StoryNode
from world_engine.domain.world_books import WorldBookCitation, WorldBookEntry, WorldBookProvenance


PACK_ID = "author.gensokyo-reference"
PACK_TITLE = "幻想乡原作资料与异变年代"
PACK_VERSION = "0.2.0"
_NAMESPACE = UUID("0d0063f6-45d8-4aaa-9873-135f3d9b5a5f")

_WORKS = {
    "zun.th08.characters": "ZUN《东方永夜抄》原版手册",
    "ichijinsha.silent-sinner.characters": "一迅社《东方儚月抄》人物介绍",
    "ichijinsha.silent-sinner.world": "一迅社《东方儚月抄》世界介绍",
    "official.th06": "《东方红魔乡》官方简介",
    "official.th07": "《东方妖妖梦》官方简介",
    "official.th08": "《东方永夜抄》官方简介",
    "official.th10": "《东方风神录》官方简介",
    "official.th11": "《东方地灵殿》官方简介",
    "official.th12": "《东方星莲船》官方简介",
    "official.th13": "《东方神灵庙》官方简介",
    "official.th14": "《东方辉针城》官方简介",
    "official.th16": "《东方天空璋》官方简介",
    "ichijinsha.wild-horned-hermit": "一迅社《东方茨歌仙》人物介绍",
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
    ("kasen", "茨木华扇", "茨木華扇", "住在山中修行的仙人，有时在村里或神社出现。", "ichijinsha.wild-horned-hermit"),
    ("sanae", "东风谷早苗", "東風谷早苗", "山上新神社的巫女。", "ichijinsha.wild-horned-hermit"),
    ("komachi", "小野塚小町", "小野塚小町", "担任三途河渡守的死神。", "ichijinsha.wild-horned-hermit"),
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
    ("mansion-library", "红魔馆地下图书馆", "大図書館", "红魔馆下方的藏书处。《东方儚月抄》人物资料记载帕秋莉居于此处。", "ichijinsha.silent-sinner.characters", "帕秋莉条目"),
    ("saigyouji-residence", "西行寺家宅邸", "西行寺家の屋敷", "位于冥界的西行寺家宅邸，有庭园和樱树。", "ichijinsha.silent-sinner.world", "西行寺宅邸段"),
    ("sanzu-river", "三途河", "三途の河", "小野塚小町在此担任渡守。", "ichijinsha.wild-horned-hermit", "小野塚小町条目"),
    ("moriya-shrine", "守矢神社", "守矢神社", "《东方风神录》中出现在山上的新神社。", "official.th10", "故事简介"),
    ("old-hell", "旧地狱", "旧地獄", "《东方地灵殿》介绍中曾是地狱的一片废墟。", "official.th11", "故事简介"),
    ("myouren-temple", "命莲寺", "命蓮寺", "《东方星莲船》引入的幻想乡佛教寺院。", "official.th12", "故事简介"),
)

_HISTORY = (
    ("scarlet-mist", "红雾异变", "《东方红魔乡》确认笼罩幻想乡的雾和灵梦开始调查。简介中的发售日属于作品发行记录。", "official.th06"),
    ("late-spring", "迟来的春天", "到了惯常花开的季节，幻想乡的降雪仍在继续。作品简介没有给出可用于建档的确切日期。", "official.th07"),
    ("false-moon", "满月异变", "真实满月消失，出现替代的月亮。人类和妖怪尝试延缓时间，争取在黎明前查明原因。", "official.th08"),
    ("mountain-shrine", "山上出现新神社", "博丽神社因缺少参拜者而困扰，新神社出现在山上并提出接收博丽神社的要求。", "official.th10"),
    ("geyser-spirits", "间歇泉与怨灵", "神社附近冒出间歇泉，之后发现随泉水而来的怨灵。", "official.th11"),
    ("treasure-ship", "飞行宝船", "飞船和飞碟形状的物体出现在幻想乡上空，灵梦等人前去调查。", "official.th12"),
    ("divine-spirits", "神灵出现", "幻想乡涌现由欲望构成的神灵，灵梦等人开始调查。", "official.th13"),
    ("animated-tools", "道具自行活动", "幻想乡的道具开始自行活动，一些平日温和的妖怪也躁动起来。", "official.th14"),
    ("four-seasons", "多地季候异常", "幻想乡不同区域同时出现彼此不符的季节现象。", "official.th16"),
)

_SEARCH_ALIASES = {"reimu": ("灵梦",), "marisa": ("魔理沙",), "human-village": ("人里",)}
_PLACE_TYPES = {
    "human-village": "settlement", "bamboo": "wilderness", "forest": "wilderness",
    "netherworld": "region", "old-hell": "region", "sanzu-river": "site",
}
_PLACE_PARENTS = {"eientei": "bamboo", "mansion-library": "mansion", "saigyouji-residence": "netherworld"}
_LATER_REGIONS = frozenset({"moriya-shrine", "old-hell", "myouren-temple"})
_LATER_PERSON_PROFILES = frozenset({"kosuzu", "kasen", "sanae", "komachi"})


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
               if entry.source_key not in _LATER_REGIONS and entry.source_key not in _LATER_PERSON_PROFILES
               and (entry.source_key not in historical_keys or entry.source_key in known_incidents)]
    village = _id("place:human-village")
    station = _id("place:village-observation-post")
    places = {
        _id(f"place:{key}"): WorldEntity(id=_id(f"place:{key}"), name=title, kind="place", components={
            "place": PlaceComponent(
                place_type=_PLACE_TYPES.get(key, "building"),
                parent_id=_id(f"place:{_PLACE_PARENTS[key]}") if key in _PLACE_PARENTS else None,
            ).model_dump(mode="json"),
        }) for key, title, *_ in _PLACES if key not in _LATER_REGIONS
    }
    places[station] = WorldEntity(id=station, name="里外观测棚", kind="place")
    for location, target in ((village, station), (station, village)):
        route = PlaceRoute(route_id=_id(f"path:{location}"), from_place_id=location, to_place_id=target, travel_ticks=60)
        place_type = "settlement" if location == village else "building"
        places[location] = places[location].model_copy(update={"components": {
            "place": PlaceComponent(place_type=place_type, routes=[route]).model_dump(mode="json"),
        }})
    player = _id("actor:resident")
    assistant = _id("actor:records-keeper")
    places[player] = WorldEntity(id=player, name="乡里的观测员", kind="character", location_id=village, components={
        "capabilities": {"grants": ["world.travel"]}, "known_places": [str(station)],
    })
    places[assistant] = WorldEntity(id=assistant, name="守簿人笠秋", kind="character", location_id=station,
                                    components={"public_role": "观测棚值事"})
    observation = _id(f"{incident}:local-observation")
    note = _id(f"{incident}:public-notice")
    node_definitions = [
        (observation, 120, StoryNode.model_validate({
            "title": "里外观测棚合簿", "visibility": "public", "participant_ids": [player, assistant],
            "variants": [{"outcome": {"key": "double-checked", "effect": "mitigated",
                                      "summary": "两名观测员核对了同一时段的读数。"}, "when": [{
                "kind": "world_condition", "owner_id": player,
                "condition": {"kind": "reach_location", "subject_id": player, "target_id": station},
            }]}],
            "fallback": {"key": "single-entry", "effect": "occurred", "summary": "笠秋独立留下一份观测记录。"},
        })),
        (note, 180, StoryNode.model_validate({
            "title": "人里布告", "visibility": "public", "participant_ids": [assistant],
            "variants": [{"outcome": {"key": "confirmed", "effect": "mitigated",
                                      "summary": "布告注明这次观测由两人独立核验。"}, "when": [{
                "kind": "prior_outcome", "schedule_id": observation, "outcome": "double-checked",
            }]}],
            "fallback": {"key": "tentative", "effect": "occurred", "summary": "布告只公布初步情况，注明有待复核。"},
        })),
    ]
    return WorldState(
        player_id=player, entities=places, world_book_entries=entries,
        scheduled_events=[ScheduledEventRecord(
            id=schedule_id, due_tick=tick, phase="observation", handler_id=STORY_NODE_HANDLER,
            handler_version=STORY_NODE_VERSION, payload=node.model_dump(mode="json"),
        ) for schedule_id, tick, node in node_definitions],
        metadata={"pack_id": PACK_ID, "pack_version": PACK_VERSION, "public_title": PACK_TITLE,
                  "public_description": description + " 可在观测棚合簿前去找守簿人笠秋。观测棚和合簿是本包原创玩法。",
                  "reference_incident": incident},
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
