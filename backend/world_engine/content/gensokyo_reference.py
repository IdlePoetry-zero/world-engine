"""Source-backed lore and two starting points for the reference world package."""

from uuid import UUID, uuid5

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.challenges import ChallengeDefinition
from world_engine.domain.contests import ContestDefinition
from world_engine.domain.components import PlaceComponent, PlaceRoute, ScheduledEventRecord
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.story_nodes import STORY_NODE_HANDLER, STORY_NODE_VERSION, StoryNode, StoryReaction
from world_engine.domain.world_books import WorldBookCitation, WorldBookEntry, WorldBookProvenance

from .gensokyo_original_profiles import GEOGRAPHY, PROFILES, source_id, source_register


PACK_ID = "author.gensokyo-reference"
PACK_TITLE = "幻想乡原作资料与异变年代"
PACK_VERSION = "0.5.0"
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
    "anniversary.keywords": "东方 Project 25 年纪念站用语页",
    "kadokawa.suzunaan.1": "KADOKAWA《东方铃奈庵》第一卷简介",
    "kadokawa.suzunaan.7": "KADOKAWA《东方铃奈庵》第七卷馆藏书目",
} | {key: record["work"] for key, record in source_register().items()}

_DIRECT_URLS = {
    "zun.th08.characters": "https://www16.big.or.jp/~zun/html/th08man/html/chara.html",
    "ichijinsha.silent-sinner.characters": "https://www.ichijinsha.co.jp/special/toho/character/index.html",
    "ichijinsha.silent-sinner.world": "https://www.ichijinsha.co.jp/special/toho/world/index.html",
    "ichijinsha.wild-horned-hermit": "https://www.ichijinsha.co.jp/special/toho_ibarakasen/index.html",
    "anniversary.keywords": "https://touhou-x.jp/keywords/",
    "kadokawa.suzunaan.1": "https://www.kadokawa.co.jp/product/321211000203/",
    "kadokawa.suzunaan.7": "https://opac.kadcul.com/en/library/search/000081904",
    **{key: f"https://touhou-project.news/titles/{key.removeprefix('official.')}/"
       for key in _WORKS if key.startswith("official.")},
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
    ("saigyouji-residence", "白玉楼", "白玉楼", "位于冥界的西行寺家宅邸，庭园内有樱树。", "zun.th07.release-notes", "魂魄妖梦、西行寺幽幽子条目"),
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
_RULE_REFERENCES = (
    ("spell-card-duel", "弹幕游戏与符卡", ("符卡", "弹幕决斗"),
     "纪念站用语页将弹幕游戏概述为处理纠纷的一种有规则的决斗，符卡用于以纸记录命名后的弹幕。", "anniversary.keywords", "弹幕游戏、符卡"),
    ("learned-magic", "魔法与魔法使", ("学魔法", "魔法师"),
     "《东方儚月抄》人物介绍区分魔理沙后天习得的魔法与帕秋莉的种族身份。", "ichijinsha.silent-sinner.characters", "魔理沙、帕秋莉条目"),
    ("youkai-powers", "妖力的个体条件", ("妖力", "式神"),
     "《东方妖妖梦》记载橙在带式时力量有变化，接触水会令式脱离。这条限制适用于橙的资料。", "zun.th07.release-notes", "橙条目"),
    ("outside-artifacts", "外界技术与道具知识", ("科技", "外界道具"),
     "《东方儚月抄》人物介绍明确区分香霖堂店主能辨认道具的名称与用途，和是否知道怎么使用道具。", "ichijinsha.silent-sinner.characters", "香霖堂店主条目"),
    ("tech-and-market", "能力卡的市场供给", ("能力卡", "市场神"),
     "《东方虹龙洞》人物背景记载市场神促成的部分交易关系到能力卡补充魔力。", "zun.th18.release-notes", "饭纲丸龙条目"),
)
_PLACE_TYPES = {
    "human-village": "settlement", "bamboo": "wilderness", "forest": "wilderness",
    "netherworld": "region", "old-hell": "region", "sanzu-river": "site",
}
_PLACE_PARENTS = {"eientei": "bamboo", "mansion-library": "mansion", "saigyouji-residence": "netherworld"}
_PLACE_SOURCE_ORDER = {"moriya-shrine": 10, "old-hell": 11, "myouren-temple": 12}
_PROFILE_SOURCE_ORDER = {"kosuzu": 14, "kasen": 14, "sanae": 10, "komachi": 9}
_UNCERTAINTIES = (
    ("scarlet-date", "红雾异变的具体日期", ("红雾日期", "红雾是哪一天"),
     "现已核对的官方故事简介未给出异变发生的世界内年月日。页面给出的发售日期另计。", "historical_event", "official.th06", "故事简介与发售日"),
    ("bamboo-route", "迷途竹林到永远亭的步行路线", ("永远亭路线", "怎么去永远亭"),
     "原文支持永远亭在竹林深处。目前核对的段落没有为游戏提供固定从哪一侧进入、走多久或经过哪些路口。", "place", "ichijinsha.silent-sinner.world", "永远亭段"),
    ("power-units", "跨能力的统一度量", ("妖力等级", "妖力数值", "统一法力"),
     "人物文档说明不同角色的能力和限制，当前引用未给出可通用于所有种族和能力的数值换算。", "rule", "zun.th08.characters", "各人物的能力说明"),
)


def _id(key: str) -> UUID:
    return uuid5(_NAMESPACE, key)


def _entry(key, title, aliases, content, category, source_id, locator, *, status="documented") -> WorldBookEntry:
    return WorldBookEntry(
        entry_id=_id(f"reference:{key}"), source_key=key, title=title,
        keywords=tuple(dict.fromkeys((title, *aliases, *_SEARCH_ALIASES.get(key, ())))),
        content=content, always_on=False, visibility="public",
        provenance=WorldBookProvenance(
            category=category, status=status,
            citations=(WorldBookCitation(source_id=source_id, work=_WORKS[source_id], locator=locator),),
        ),
    )


def build_reference_entries() -> list[WorldBookEntry]:
    """References read by the GM, distinct from the simulation's NPC state."""
    return [
        *(_entry(key, title, (alias,), content, "person", source, alias)
          for key, title, alias, content, source in _PEOPLE),
        *(_entry(key, title, (alias,), content, "person", source_id(number), f"人物章 · {alias}")
          for number, records in PROFILES.items() for key, title, alias, content in records),
        *(_entry(key, title, (alias,), content, "place", source, locator)
          for key, title, alias, content, source, locator in _PLACES),
        *(_entry(key, title, (alias,), content, "organization" if kind == "organization" else "place",
                 source_id(number), locator)
          for number, records in GEOGRAPHY.items() for key, title, alias, kind, _, locator, content in records),
        *(_entry(key, title, (), content, "historical_event", source, "故事简介")
          for key, title, content, source in _HISTORY),
        *(_entry(key, title, aliases, content, "rule", source, locator)
          for key, title, aliases, content, source, locator in _RULE_REFERENCES),
        *(_entry(key, title, aliases, content, category, source, locator, status="unknown")
          for key, title, aliases, content, category, source, locator in _UNCERTAINTIES),
        WorldBookEntry(
            entry_id=_id("reference:ice-spirit-wording"), source_key="ice-spirit-wording", title="琪露诺的称谓差异",
            keywords=("琪露诺的种族", "冰妖怪", "冰精"), always_on=False, visibility="public",
            content="《妖妖梦》人物条目称她为冰的妖怪，《花映塚》的种族栏则登记为妖精。原文用词分别记载。",
            provenance=WorldBookProvenance(category="person", status="conflicting", citations=(
                WorldBookCitation(source_id="zun.th07.release-notes", work=_WORKS["zun.th07.release-notes"], locator="琪露诺条目"),
                WorldBookCitation(source_id="zun.th09.release-notes", work=_WORKS["zun.th09.release-notes"], locator="琪露诺条目的种族栏"),
            )),
        ),
        WorldBookEntry(
            entry_id=_id("reference:mansion-lake-inference"), source_key="mansion-lake-inference", title="红魔馆湖畔与雾之湖",
            keywords=("红魔馆旁是什么湖", "红魔馆和雾之湖"), always_on=False, visibility="public",
            content="两段资料可用来讨论湖畔地形的联系。馆旁湖面与雾之湖的对应在这里保留为地理推断。",
            provenance=WorldBookProvenance(category="place", status="inferred", citations=(
                WorldBookCitation(source_id="zun.th07.release-notes", work=_WORKS["zun.th07.release-notes"], locator="十六夜咲夜条目的湖畔描述"),
                WorldBookCitation(source_id="zun.th14.release-notes", work=_WORKS["zun.th14.release-notes"], locator="琪露诺条目的雾之湖居所"),
            )),
        ),
    ]


def build_geography_index() -> list[dict]:
    return [
        *({"ref": key, "kind": _PLACE_TYPES.get(key, "building"), "parent": _PLACE_PARENTS.get(key)}
          for key, *_ in _PLACES),
        *({"ref": key, "kind": kind, "parent": parent, "source_document": source_id(number)}
          for number, records in GEOGRAPHY.items() for key, _, _, kind, parent, *_ in records),
    ]


def build_source_index() -> dict[str, dict[str, str]]:
    return ({key: {"work": _WORKS[key], "url": url, "host": "创作者、官方站点或出版社"}
             for key, url in _DIRECT_URLS.items()} | source_register())


def _opening_routes(places: dict[UUID, WorldEntity]) -> None:
    # These travel links and durations are for the original roleplay map.
    links = (
        ("human-village", "village-observation-post", 60),
        ("human-village", "bamboo", 360), ("human-village", "shrine", 240),
        ("human-village", "mansion-lakeshore", 300), ("shrine", "forest", 360),
        ("mansion-lakeshore", "mansion", 90), ("bamboo", "eientei", 600),
        ("forest", "youkai-mountain", 180), ("youkai-mountain", "moriya-shrine", 120),
    )
    routes: dict[UUID, list[PlaceRoute]] = {}
    for a, b, duration in links:
        for start, end in ((a, b), (b, a)):
            start_id, end_id = _id(f"place:{start}"), _id(f"place:{end}")
            if start_id in places and end_id in places:
                routes.setdefault(start_id, []).append(PlaceRoute(
                    route_id=_id(f"path:{start}:{end}"), from_place_id=start_id, to_place_id=end_id, travel_ticks=duration,
                ))
    for ident, exits in routes.items():
        entity = places[ident]
        component = PlaceComponent.model_validate(entity.components["place"])
        places[ident] = entity.model_copy(update={"components": entity.components | {
            "place": component.model_copy(update={"routes": exits}).model_dump(mode="json"),
        }})


def _scenario(*, incident: str, description: str, document_limit: int, known_incidents: frozenset[str]) -> dict:
    historical_keys = {key for key, *_ in _HISTORY}
    later_profiles = {key for number, profiles in PROFILES.items() if number > document_limit for key, *_ in profiles}
    later_places = {key for number, records in GEOGRAPHY.items() if number > document_limit for key, *_ in records}
    later_rules = {"tech-and-market"} | ({"youkai-powers"} if document_limit < 7 else set())
    later_notes = {key for key, order in (("ice-spirit-wording", 9), ("mansion-lake-inference", 14)) if order > document_limit}
    entries = [entry for entry in build_reference_entries()
               if _PLACE_SOURCE_ORDER.get(entry.source_key, 0) <= document_limit
               and _PROFILE_SOURCE_ORDER.get(entry.source_key, 0) <= document_limit
               and entry.source_key not in later_profiles and entry.source_key not in later_places
               and entry.source_key not in later_rules and entry.source_key not in later_notes
               and (entry.source_key not in historical_keys or entry.source_key in known_incidents)]
    village = _id("place:human-village")
    station = _id("place:village-observation-post")
    titles = {entry.source_key: entry.title for entry in entries}
    atlas = [row for row in build_geography_index() if row["ref"] in titles]
    places = {}
    for row in atlas:
        key, kind, parent = row["ref"], row["kind"], row["parent"]
        parent_id = _id(f"place:{parent}") if parent in titles else None
        if kind == "organization":
            ident = _id(f"organization:{key}")
            places[ident] = WorldEntity(id=ident, name=titles[key], kind="organization", location_id=parent_id)
        else:
            ident = _id(f"place:{key}")
            places[ident] = WorldEntity(id=ident, name=titles[key], kind="place", components={
                "place": PlaceComponent(place_type=kind, parent_id=parent_id,
                    access_policy="private" if key in {"mansion", "eientei"} else "open").model_dump(mode="json"),
            })
    places[station] = WorldEntity(id=station, name="里外观测棚", kind="place", components={
        "place": {"place_type": "building"}, "public_description": "值簿、试灯砂和登记牌放在棚里。",
    })
    lakeshore = _id("place:mansion-lakeshore")
    places[lakeshore] = WorldEntity(id=lakeshore, name="红魔馆外湖岸", kind="place", components={
        "place": {"place_type": "site"}, "public_description": "这里能望见馆墙。通向馆内的门需要访客许可。",
    })
    _opening_routes(places)
    known_places = [_id(f"place:{key}") for key in ("village-observation-post", "bamboo", "shrine", "forest",
                    "mansion-lakeshore", "youkai-mountain", "moriya-shrine")]
    player = _id("actor:resident")
    assistant = _id("actor:records-keeper")
    observer = _id("actor:card-assistant")
    materials = "practice.lamp_powder"
    learner_stock, station_stock = _id("account:practice-resident"), _id("account:practice-station")
    spell_challenge = _id("challenge:spell-declaration")
    contest_id = _id("contest:lamp-spell-cards")
    practices = (
        ("arcana", "灯符术法演练", "术法", "practice.arcana", "12", "11", None),
        ("aura", "妖力辨识", "妖力辨识", "practice.aura", "11", "10", None),
        ("tools", "外界仪具辨用", "器具学", "practice.tools", "10", "12", None),
        ("spell-declaration", "符卡登记演练", "仪式规则", "practice.declaration", "12", "10", _id("challenge:arcana")),
    )
    places[player] = WorldEntity(id=player, name="乡里的观测员", kind="character", location_id=village, components={
        "capabilities": {"grants": ["world.travel", "practice.clearance"]},
        "known_places": [str(ident) for ident in known_places if ident in places],
        "skills": {"skills": {skill_id: {"level": initial_level} for _, _, _, skill_id, initial_level, _, _ in practices}},
    })
    places[assistant] = WorldEntity(id=assistant, name="守簿人笠秋", kind="character", location_id=station,
        components={"public_role": "观测棚值事", "skills": {"skills": {
            "practice.arcana": {"level": "11"}, "practice.aura": {"level": "13"}, "practice.declaration": {"level": "10"},
        }}})
    places[observer] = WorldEntity(id=observer, name="绘牌学徒秦澄", kind="character", location_id=station,
                                  components={"public_role": "值班观摩者"})
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
    if incident == "mountain-shrine":
        mountain = _id("place:youkai-mountain")
        keeper = _id("actor:mountain-keeper")
        places[keeper] = WorldEntity(id=keeper, name="山径水尺员蒲一径", kind="character", location_id=mountain,
            components={"public_role": "给山路来客登记溪水水位的值事"})
        node_definitions.append((_id("mountain-shrine:pass-notice"), 1020, StoryNode.model_validate({
            "title": "山路水位簿合页", "visibility": "public", "participant_ids": [keeper],
            "variants": [{"when": [{"kind": "world_condition", "owner_id": player,
                "condition": {"kind": "reach_location", "subject_id": player, "target_id": mountain}}],
                "outcome": {"key": "witnessed", "effect": "mitigated", "summary": "蒲一径把你见证的水位随来客记录一同登记。"}}],
            "fallback": {"key": "unwitnessed", "effect": "occurred", "summary": "蒲一径先按值班记录封册，见证栏留空。"},
        })))
    return WorldState(
        player_id=player, entities=places, world_book_entries=entries,
        resources={learner_stock: ResourceAccount(resource_id=materials, owner_id=player, balance=4),
                   station_stock: ResourceAccount(resource_id=materials, owner_id=station, balance=0)},
        challenges=[ChallengeDefinition.model_validate({
            "challenge_id": _id(f"challenge:{key}"), "venue_id": station, "title": title,
            "description": "观测棚的原创演练。门槛与材料是本包的游玩配置。", "skill_id": skill_id,
            "skill_label": skill_label, "pass_level": pass_level, "required_capability": "practice.clearance",
            "prerequisite_id": prerequisite,
            "note_on_pass": "在观测棚练习时，先登记技艺名称和双方的同意，再开始对练。" if key == "arcana" else None,
            "required_note_from": prerequisite if key == "spell-declaration" else None,
            "fee": {"receiving_account_id": station_stock, "resource_id": materials, "quantity": "1"},
        }) for key, title, skill_label, skill_id, _, pass_level, prerequisite in practices],
        contest_definitions=[ContestDefinition.model_validate({
            "definition_id": contest_id, "venue_id": station, "title": "观测棚符卡切磋",
            "terms": "原创道场规程。双方各声明一项命名技艺并确认，进行三轮无伤练习。按赢下的轮数判定结果，同分为平局。",
            "proposer_challenge_id": spell_challenge, "npc_routine": {assistant: "lamp.control", observer: None},
            "routines": [
                {"key": "lamp.step", "title": "灯纹步", "skills": ["practice.arcana", "practice.aura", "practice.declaration"]},
                {"key": "lamp.control", "title": "归焰式", "skills": ["practice.declaration", "practice.aura", "practice.arcana"]},
            ],
        })],
        story_reactions=[StoryReaction.model_validate({
            "reaction_id": _id(f"{incident}:spell-registration"),
            "when": [{"kind": "challenge_passed", "actor_id": player, "challenge_id": spell_challenge}],
            "node": {"title": "符卡演练登记", "visibility": "public", "participant_ids": [player, assistant],
                     "fallback": {"key": "registered", "effect": "occurred", "summary": "值事登记了这次命名演练的通过记录。"}},
        }), StoryReaction.model_validate({
            "reaction_id": _id(f"{incident}:practice-completed"),
            "when": [{"kind": "contest_result", "actor_id": player, "definition_id": contest_id, "result": "settled"}],
            "node": {"title": "观测棚切磋纪事", "visibility": "public", "participant_ids": [player, assistant],
                     "fallback": {"key": "completed", "effect": "occurred", "summary": "观测棚记录了经双方确认完成的命名切磋。"}},
        })],
        scheduled_events=[ScheduledEventRecord(
            id=schedule_id, due_tick=tick, phase="observation", handler_id=STORY_NODE_HANDLER,
            handler_version=STORY_NODE_VERSION, payload=node.model_dump(mode="json"),
        ) for schedule_id, tick, node in node_definitions],
        metadata={"pack_id": PACK_ID, "pack_version": PACK_VERSION, "public_title": PACK_TITLE,
                  "public_description": description + " 观测棚和出入口的通行时长属于本包游玩配置。",
                  "resource_catalog": {materials: "试灯砂"},
                  "reference_incident": incident},
    ).model_dump(mode="json")


def build_archive() -> bytes:
    openings = [
        {"opening_id": "scarlet-mist", "title": "红雾出现后", "position": "红雾已影响日照，世界内日期未定。",
         "known_history": ["幻想乡出现遮挡日照的浓雾。"],
         "scenario": _scenario(incident="scarlet-mist", document_limit=6, description="你在里内负责记录雾的影响。可在合簿前找笠秋。",
                               known_incidents=frozenset({"scarlet-mist"}))},
        {"opening_id": "false-moon", "title": "满月异变之夜", "position": "异常月亮已出现，黎明之前。",
         "known_history": ["今晚的满月与平日不同，人类与妖怪正在查明缘由。"],
         "scenario": _scenario(incident="false-moon", document_limit=8, description="你在里内记录今夜的月相和照明变化。可在合簿前找笠秋。",
                               known_incidents=frozenset({"scarlet-mist", "late-spring", "false-moon"}))},
        {"opening_id": "mountain-shrine", "title": "山上新神社出现之后", "position": "新的山上神社已出现，世界内日期未定。",
         "known_history": ["博丽神社参拜者稀少。", "山上出现一座新神社，两方发生交涉。"],
         "scenario": _scenario(incident="mountain-shrine", document_limit=10,
             description="你从人里出发，经过博丽神社和魔法森林可到妖怪之山。蒲一径在山路封水位簿前需要一名见证人。",
             known_incidents=frozenset({"scarlet-mist", "late-spring", "false-moon", "mountain-shrine"}))},
    ]
    return write_world_pack(manifest=WorldPackManifest(pack_id=PACK_ID, version=PACK_VERSION, title=PACK_TITLE),
                            template={"story_openings": openings})
