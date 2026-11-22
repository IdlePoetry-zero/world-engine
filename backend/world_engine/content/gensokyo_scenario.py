"""Original Gensokyo districts and an opening for the portable world-pack importer."""

from uuid import UUID, uuid5

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.components import GoalRecord, KnowledgeRecord, RelationRecord
from world_engine.domain.models import CalendarConfig, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.ruleset_versions import MAGIC_GATE_RULESET_VERSION
from world_engine.domain.world_books import WorldBookEntry


PACK_ID = "author.gensokyo-returning-lanterns"
PACK_VERSION = "1.1.0"
PACK_TITLE = "幻想乡·回灯之约"
_NAMESPACE = UUID("fa6c06d0-0abd-4eb7-a8ac-c742fe06c856")
_HOUR = 3_600
_YEAR = 360 * 24 * _HOUR
_OPENING_TICK = 18 * _HOUR
_INCIDENT_TICK = 16 * _HOUR
_MOON_READING_TICK = 18 * _HOUR + 25 * 60
_BOOKSHOP_RETURN_TICK = 19 * _HOUR + 15 * 60
_CASE_TITLE = "借影亭的闭灯告示"
_CASE_BRIEF = (
    "下午的闭灯告示称借影亭提前停值，外路旅客因此改道。核对两份独立记录与证人的说法，查明谁递交了失实告示，"
    "以及受怀疑的桥灯工当时在哪里。灯棚有一联接递底票，纸铃社存着一份传号簿，芦绳桥还保留了修灯工单。"
)
_COIN = "lanterns.copper_cash"
_FOOD = "lanterns.rice_cake"
_WATER = "lanterns.sweet_water"
_MEDICINE = "lanterns.medicine"

_PLACES = {
    "lantern-yard": ("回灯棚", "building", "人间之里西街的公用灯棚。人们将写着归路的纸条折进灯底，木牌写着回灯节只问归路，不问来历。门外可去芦绳桥、纸铃社或茶汤集。"),
    "reed-bridge": ("芦绳桥", "site", "芦绳拴着两排朝水面照的灯。过桥的人轮流报一声脚边水深，低处传来的回答偶尔迟半拍。东头通回灯棚，北岸是渡铃坞。"),
    "paper-bell": ("纸铃社", "building", "人类誊写人和听风的妖怪共用这座小屋。墙上公布互不伤人的灯影试手章程，门口的传号簿借给任何走过夜路的人核对。"),
    "tea-crossing": ("茶汤集", "settlement", "米饼贴锅，井水吊在门前。熟客可以赊茶，回灯节第一夜只按秤重收现钱。出了西口就是魔法森林的量雨径。"),
    "borrowed-shadow": ("借影亭", "site", "亭里给晚归的人留了一盏借影灯。值守人换灯时敲一长两短。纸铃社的窗正好看见亭顶，林路与回声隘从这里分开。"),
    "rain-path": ("量雨径", "wilderness", "魔法森林边缘的一条窄路。人们把落在空菌伞上的雨柱记进册子，挂纸绳处可歇脚。循灯可回茶汤集，沿树根可到借影亭。"),
    "echo-notch": ("回声隘", "wilderness", "快到幻想乡外缘时，口信会比脚步先一步折回来。岩间留了给迷途者的空白灯签。再往前便是界缘风口。"),
    "border-wind": ("界缘风口", "site", "界缘的风只吹响没有写名字的纸签。返程刻在石座朝内的一面，沿回声隘返回借影亭。"),
    "ferry-bell": ("渡铃坞", "site", "旧船托在竹架上，船头悬一枚扎了布的铃。水边妖怪替渡客记泊船时辰，人间之里来换米的人把篮子放在木台上。"),
    "paper-ladder": ("纸梯书铺", "building", "人间之里南巷的二手书铺。借阅牌把异变纪事、祭日见闻和街谈分栏。誊书人傍晚去观月石台校图；石台遇暴雨，就改到束页廊。"),
    "binding-porch": ("束页廊", "building", "有顶的旧书晾架和一盏暖灯。值事牌写着雨天把当晚的公开读图移到这里。廊下也常有人拿旧书换灯纸。"),
    "moon-deck": ("观月石台", "site", "石台照着水上的月影，低矮栏杆挂着两张观测表。比方位也比时刻，免得把水面变形的倒影当成天上的异变。"),
    "ringing-shrine": ("鸣石小社", "site", "村外一座小社，供桌有山路行人留下的石片。值事人记供物，也替走错路的人问问隔壁山社的归路。"),
    "mist-sluice": ("雾坡分渠", "wilderness", "上游来水下到这里便分两道，一道进染坊，另一道留给过夜的船。石脊指向山脚水尺台，贴着旧渠标的岔道通到地底风井。"),
    "water-gauge": ("山脚水尺台", "site", "靠近妖怪之山山麓的河渠观测点。河童水工用木尺记下两边水位，路人可以饮水、买干粮、核对渡口的尺表。"),
    "wind-vent": ("地底风井", "site", "通向旧地狱一带的地面换气井。井沿挂着一份双方轮换验风的字据。凡是签过名的访客都从同一段护栏外读刻度。"),
    "red-clay-gallery": ("赤泥交班廊", "building", "地底第一层的交班廊，灯油和空气从不同孔洞进来。地上有人来问风向，值事人会把当日井口读数交给对方核对。"),
}

_PATHS = (
    ("lantern-yard", "reed-bridge", 600),
    ("lantern-yard", "paper-bell", 480),
    ("lantern-yard", "tea-crossing", 360),
    ("reed-bridge", "ferry-bell", 720),
    ("paper-bell", "borrowed-shadow", 540),
    ("tea-crossing", "rain-path", 840),
    ("rain-path", "borrowed-shadow", 660),
    ("borrowed-shadow", "echo-notch", 900),
    ("echo-notch", "border-wind", 720),
    ("ferry-bell", "echo-notch", 1_020),
    ("lantern-yard", "paper-ladder", 420),
    ("paper-ladder", "binding-porch", 300),
    ("paper-ladder", "moon-deck", 300),
    ("binding-porch", "ringing-shrine", 540),
    ("moon-deck", "ringing-shrine", 720),
    ("ringing-shrine", "border-wind", 780),
    ("ringing-shrine", "water-gauge", 840),
    ("ferry-bell", "mist-sluice", 780),
    ("mist-sluice", "water-gauge", 600),
    ("mist-sluice", "wind-vent", 900),
    ("wind-vent", "red-clay-gallery", 600),
)

_CAST = {
    "player": ("朝砂", "lantern-yard", "core.human", 22, "替灯棚跑腿的巡灯学徒", "记得街坊们各自回家的走法，第一次负责核对节夜的传号。", "随身带着纸笔、口粮和一点铜钱。", ["观察细致"], ["让每个出门人都认得归路"]),
    "courier": ("盐见织帆", "lantern-yard", "core.human", 31, "接递告示的抄手", "总把湿纸夹在左袖。见人行礼很快，答涉及时间的问题会慢下来。", "盐灰色袖口缝着一条补线。", ["谨慎", "怕失去营生"], ["守住师傅交来的递送席位", "事情可以在柜前当面更正"]),
    "keeper": ("箬苏", "lantern-yard", "lanterns.paper_yokai", 76, "回灯棚的纸灯妖", "熟知每一户借灯人的习惯，愿给没署名的旅客也留一盏。", "斗篷边缘薄得透出灯火。", ["记性好", "话直"], ["守好公用灯火", "借灯不问出身"]),
    "repairer": ("柴汀", "reed-bridge", "core.human", 43, "芦绳桥换灯的桥灯工", "做完活就把时辰刻在咬过的竹筹上，很不喜欢被人替他记工。", "腰间挂一串长短不同的竹筹。", ["不爱辩解", "重手艺"], ["让过桥人能看见落脚的地方"]),
    "register": ("铃浦缝", "paper-bell", "lanterns.echo_yokai", 112, "为纸铃社抄写传号的回声妖", "会将亲眼所见与记下的传号分栏誊写。若听见传号错误，先问来路再更正。", "右耳戴着一枚不发声的纸铃。", ["爱较真", "肯改错"], ["每一行都给更正留位置"]),
    "vendor": ("渡松南枝", "tea-crossing", "core.human", 49, "茶汤集卖米饼与甜井水的小贩", "节夜常替两边的客人传递口信，向来先问旅程还有多长。", "竹围裙的一角藏着两枚量杯。", ["健谈", "算账清楚"], ["客人先喝上水", "分清人情债和卖货款"]),
    "watcher": ("白榧", "borrowed-shadow", "lanterns.branch_yokai", 145, "借影亭守灯人", "看到熟悉的路标也会先对上一遍方向。懂得给误路的人保留体面。", "帽边垂着榧树纤维编的短绳。", ["不苟言笑", "替旅客着想"], ["值守交接必须双人核对"]),
    "sorter": ("伊皿澪", "rain-path", "core.human", 27, "量雨径的菌伞记录员", "不急着为异象命名，经常先量雨再翻旧册。", "背着两个防雨笔筒，左边放借来的笔。", ["专注", "不信巧合"], ["让林径能平安通行"]),
    "ferrier": ("滩悬", "ferry-bell", "lanterns.river_yokai", 90, "渡铃坞点泊船灯的水妖", "待夜渡的人如同待送米的亲邻，讨厌别人急着替潮水下结论。", "披着青黑色齐膝蓑衣，雨沿边缘绕开。", ["有耐心", "讲邻里情面"], ["等同船的人都靠岸再熄灯"]),
    "border-reader": ("薄盏", "border-wind", "lanterns.wind_yokai", 85, "巡看界缘纸签的风妖", "能辨认几条路风的味道，不会接写着陌生真名的纸签。", "一件窄袖衣裳从来不随风飘。", ["惜字", "好奇"], ["替折返的人留一条明路"]),
    "bookbinder": ("折羽祐岚", "paper-ladder", "lanterns.ink_yokai", 83, "书铺誊图和讲读人", "傍晚带图去石台校月影。遇暴雨就在束页廊用上周的读数开读图会，晚些时候回书铺整理借阅牌。", "提一只压得很实的布包，掌侧有装订染料留下的蓝印。", ["熟悉借阅人的眼光", "讲究证据"], ["把书上看见的和亲眼看见的记在不同栏"]),
    "shrine-steward": ("雨坂弦", "ringing-shrine", "core.human", 37, "鸣石小社的供灯值事", "以石片记不同山路的还愿，替迷路人向山上与村里各问一次。", "木匣里装着大小不等的写字石。", ["温和", "会记小事"], ["孩子第一次夜渡时要有人接应"]),
    "river-surveyor": ("砂渠照水", "water-gauge", "lanterns.kappa", 95, "山脚测流和分渠的河童水工", "看人来问水路就先找成对的尺表。值守时也卖一份当天补给给怕赶夜路的客人。", "水尺挂在背袋外面，尺端还缠着一段旧灯绳。", ["重测量", "容易急切"], ["分水时每边的基本用量都得留下"]),
    "well-warden": ("铁砂绰", "red-clay-gallery", "lanterns.underground_oni", 139, "赤泥交班廊的换气值事", "说话声音很轻。遇到地上来人问风井，愿先拿当日班牌和访客登记交叉核验。", "袖子上别着悄无声息的厚铁环。", ["稳当", "厌恶臆测"], ["让两边的值事都能复核读数"]),
}

_WAYPOINTS = {
    "echo-notch": ("empty-lantern-tags", "核对界缘的折返灯签", "岩间有空白灯签和一张刻度抄页，可将刻线与石上返程标记互相对照。"),
    "water-gauge": ("river-gauges", "分辨山麓两道水尺", "水工在值守台放了两张水尺表。走夜路的人可核对哪张记录渡口那一侧，也可向砂渠照水补些吃喝。"),
    "wind-vent": ("vent-shift", "核对地底风井的交班记号", "井口班牌留着两种印记。可停下来找当天的复写条，再去赤泥交班廊请值事人说明。"),
}
_MARKETS = {
    "tea-crossing": ("vendor", ((_FOOD, 3), (_WATER, 2))),
    "water-gauge": ("river-surveyor", ((_FOOD, 4), (_WATER, 1))),
}


def _id(kind: str, name: str) -> UUID:
    return uuid5(_NAMESPACE, f"{kind}:{name}")


def _place(name: str) -> UUID:
    return _id("place", name)


def _person(name: str) -> UUID:
    return _id("person", name)


def _places() -> list[WorldEntity]:
    routes = {key: [] for key in _PLACES}
    for left, right, duration in _PATHS:
        for origin, destination in ((left, right), (right, left)):
            routes[origin].append({
                "route_id": _id("route", f"{origin}:{destination}"),
                "from_place_id": _place(origin),
                "to_place_id": _place(destination),
                "travel_ticks": duration,
                "travel_modes": ["travel.walk"],
            })
    entities = []
    for key, (name, kind, description) in _PLACES.items():
        place = {"place_type": kind, "routes": routes[key]}
        if key == "moon-deck":
            place["environment"] = {"weather": "storm"}
        if key in _WAYPOINTS:
            identifier, title, summary = _WAYPOINTS[key]
            place["waypoint_opportunities"] = [{
                "opportunity_id": _id("waypoint", identifier), "title": title, "summary": summary,
            }]
        components = {"public_description": description, "place": place}
        if key in _MARKETS:
            seller, offers = _MARKETS[key]
            components["market"] = {
                "currency_resource_id": _COIN,
                "offers": [
                    {"listing_id": _id("listing", resource if seller == "vendor" else f"{seller}:{resource}"),
                     "seller_id": _person(seller), "resource_id": resource,
                     "stock_account_id": _holder(seller, resource),
                     "unit_price_minor": price}
                    for resource, price in offers
                ],
            }
        entities.append(WorldEntity(id=_place(key), kind="place", name=name, components=components))
    return entities


def _holder(person: str, resource: str) -> UUID:
    return _id("account", f"holder:{person}:{resource}")


def _stock(person: str) -> dict[str, int]:
    if person == "player":
        return {_COIN: 18, _FOOD: 4, _WATER: 4, _MEDICINE: 1}
    if person == "vendor":
        return {_COIN: 30, _FOOD: 24, _WATER: 28}
    if person == "river-surveyor":
        return {_COIN: 20, _FOOD: 18, _WATER: 36}
    return {_FOOD: 4, _WATER: 4}


def _bookbinder_routine() -> dict:
    reading = _id("routine", "bookbinder:moon-reading")
    return {
        "daily_routine": {"windows": [
            {"window_id": reading, "start_second": _MOON_READING_TICK, "duration_seconds": 45 * 60,
             "activity": "study", "place_id": _place("moon-deck")},
            {"window_id": _id("routine", "bookbinder:inventory"), "start_second": _BOOKSHOP_RETURN_TICK,
             "duration_seconds": 45 * 60, "activity": "work", "place_id": _place("paper-ladder")},
        ]},
        "routine_mobility": {"alternatives": {str(reading): [_place("binding-porch")]}, "avoid_conditions": ["storm"]},
    }


def _people() -> list[WorldEntity]:
    entities = []
    for key, (name, location, species, age, role, description, appearance, traits, values) in _CAST.items():
        lifespan = (92 if species == "core.human" else 240) * _YEAR
        grants = ["world.travel"]
        if key == "player":
            grants.extend(("world.explore", "world.trade"))
        entities.append(WorldEntity(
            id=_person(key), kind="character", name=name, location_id=_place(location),
            components={
                "public_role": role,
                "public_description": description,
                "appearance": appearance,
                "personality": {"traits": traits, "values": values},
                "life": {"species_id": species, "birth_tick": _OPENING_TICK - age * _YEAR,
                         "lifespan_ticks": lifespan, "maturity_ticks": 18 * _YEAR, "life_stage": "adult"},
                "health": {"current": 24, "maximum": 24},
                "needs": {"food": {"value": 85}, "water": {"value": 85}, "rest": {"value": 83}},
                "skills": {"skills": {
                    "skill.investigation": {"level": "90" if key == "player" else "35"},
                    "skill.local_knowledge": {"level": "82" if key == "player" else "47"},
                }},
                "capabilities": {"grants": grants},
                "known_places": [_place(place) for place in _PLACES] if key == "player" else [_place(location)],
                "inventory": {"lots": [
                    {"lot_id": _id("lot", f"{key}:{resource}"), "account_id": _holder(key, resource),
                     "resource_id": resource, "quantity": quantity}
                    for resource, quantity in _stock(key).items() if resource != _COIN
                ]},
            } | (_bookbinder_routine() if key == "bookbinder" else {}),
        ))
    return entities


def _accounts() -> dict[UUID, ResourceAccount]:
    accounts = {}
    totals = {_COIN: 0, _FOOD: 0, _WATER: 0, _MEDICINE: 0}
    for key in _CAST:
        for resource, amount in _stock(key).items():
            accounts[_holder(key, resource)] = ResourceAccount(
                resource_id=resource, balance=amount, owner_id=_person(key),
            )
            totals[resource] += amount
    for resource, total in totals.items():
        accounts[_id("account", f"issue:{resource}")] = ResourceAccount(
            resource_id=resource, balance=-total, account_type="issuance_source",
        )
    for resource in (_FOOD, _WATER, _MEDICINE):
        accounts[_id("account", f"consume:{resource}")] = ResourceAccount(
            resource_id=resource, balance=0, account_type="consumption_sink",
        )
    return accounts


def _group(key: str, name: str, place: str, members: list[str], charter: str) -> WorldEntity:
    leader = _person(members[0])
    return WorldEntity(
        id=_id("organization", key), kind="organization", name=name, location_id=_place(place),
        components={
            "public_description": charter,
            "governance": {"charter": charter, "members": [_person(member) for member in members],
                           "leader_id": leader, "succession_policy": "council",
                           "offices": [{"office_id": "office.leader", "title": "召集人", "holder_id": leader}]},
        },
    )


def _item(key: str, name: str, place: str, description: str, **components) -> WorldEntity:
    return WorldEntity(
        id=_id("item", key), kind="item", name=name, location_id=_place(place),
        components={"public_description": description} | components,
    )


def _source(key: str, *, case: str = "1.0.0:lantern-notice") -> dict[str, str]:
    return {"kind": "pack", "reference_id": f"{PACK_ID}:{case}:{key}"}


def _case() -> tuple[list[WorldEntity], list[KnowledgeRecord]]:
    station = _place("borrowed-shadow")
    status_open = {"predicate": "entity_status", "value": "active", "effective_tick": _INCIDENT_TICK}
    status_closed = status_open | {"value": "retired"}
    repairer = _person("repairer")
    alibi = {"predicate": "entity_location", "value": _place("reed-bridge"), "effective_tick": _INCIDENT_TICK}
    routes = []
    for route, suspect, result, subject, fact, finding, observation, claim in (
        ("delivery-copy", _person("courier"), "supports", station, status_open,
         "conflict", "delivery-slip", "courier-claim"),
        ("bell-register", _person("courier"), "supports", station, status_open,
         "conflict", "register-copy", "register-claim"),
        ("bridge-alibi", repairer, "rules_out", repairer, alibi,
         "corroborated", "bridge-order", "repairer-claim"),
    ):
        routes.append({
            "route_id": route, "suspect_id": suspect, "finding": result,
            "requirements": [{"subject_id": subject, "predicate": fact["predicate"],
                              "effective_tick": _INCIDENT_TICK, "finding": finding,
                              "observation_provenance": [_source(observation)], "claim_provenance": [_source(claim)]}],
        })

    items = [_item(
        "case-anchor", "回灯棚的查告公夹", "lantern-yard",
        "夹子标着“借影亭闭灯告示待核”。上层纸还带着被雨洇过的红边，空页留着给查验人登记线索。",
        investigation_case={"case_id": _id("case", "borrowed-shadow-notice"), "title": _CASE_TITLE,
                            "brief": _CASE_BRIEF, "routes": routes,
                            "wrong_suspect_effect": {"dimension": "trust", "delta": -12}},
    )]
    for key, title, place, description, subject, fact, text in (
        ("delivery-slip", "灯棚接递底票", "lantern-yard", "一联湿纸夹在玻璃下，印记横跨接柜时间两栏。", station, status_open,
         "底票记有十六时借影亭向灯棚借取两只灯面的经手印。接柜人在同一行登记织帆递来停值告示，亭灯当时仍在照路。"),
        ("register-copy", "纸铃社传号簿", "paper-bell", "传号簿一行分为见闻栏和来文栏，两栏各有不同的印角。", station, status_open,
         "见闻栏记有十六时借影亭发过一长两短的当值灯号，窗前两人联署。来文栏另记盐见织帆在同一时刻递来的闭灯告示。"),
        ("bridge-order", "芦绳桥修灯工单", "reed-bridge", "两道竹痕将桥头换灯的验工时间压在同一张工单上。", repairer, alibi,
         "十六时，柴汀在芦绳桥与验工人当面核对三盏灯的竹筹，工单存有两人所记桥位与时间。"),
    ):
        items.append(_item(key, title, place, description, historical_evidence={
            "evidence_id": _id("evidence", key), "subject_id": subject, "claim": text,
            "fact": fact, "provenance": [_source(key)],
        }))
    claims = []
    for key, holder, subject, fact, text in (
        ("courier-claim", "courier", station, status_closed,
         "十六时的告示是我交到灯棚的。那会儿借影亭早已停值。我当时照着木签抄的。"),
        ("register-claim", "register", station, status_closed,
         "我抄过织帆交来的闭灯告示，写的是十六时借影亭停值。我还没把告示与同页的见闻栏合校。"),
        ("repairer-claim", "repairer", repairer, alibi,
         "十六时我在芦绳桥给最后一盏换灯，当面给验工的人递了竹筹，没去借影亭贴告示。"),
    ):
        claims.append(KnowledgeRecord(
            id=_id("knowledge", key), holder_id=_person(holder), subject_id=subject,
            kind="claim", claim=text, fact=fact, confidence="0.72", received_tick=_OPENING_TICK,
            disclosure_policy="if_asked", visibility={"policy": "private"}, provenance=[_source(key)],
        ))
    return items, claims


def _vent_case() -> tuple[list[WorldEntity], list[KnowledgeRecord]]:
    source = "1.1.0:vent-record"
    vent = _place("wind-vent")
    steward = _person("shrine-steward")
    warden = _person("well-warden")
    vent_open = {"predicate": "entity_status", "value": "active", "effective_tick": _INCIDENT_TICK}
    vent_closed = vent_open | {"value": "retired"}
    underground = {"predicate": "entity_location", "value": _place("red-clay-gallery"),
                   "effective_tick": _INCIDENT_TICK}
    routes = []
    for route, suspect, result, subject, fact, comparison, observation, claim in (
        ("vent-copy", steward, "supports", vent, vent_open, "conflict", "vent-carbon", "shrine-copy"),
        ("warden-shift", warden, "rules_out", warden, underground, "corroborated", "warden-roll", "warden-copy"),
    ):
        routes.append({
            "route_id": route, "suspect_id": suspect, "finding": result,
            "requirements": [{"subject_id": subject, "predicate": fact["predicate"],
                              "effective_tick": _INCIDENT_TICK, "finding": comparison,
                              "observation_provenance": [_source(observation, case=source)],
                              "claim_provenance": [_source(claim, case=source)]}],
        })

    items = [_item(
        "vent-case-anchor", "鸣石小社的井口核抄夹", "ringing-shrine",
        "两份抄条都用墨圈标了十六时，报上的井口状态却不同。夹内附纸请核抄人和地底值事分别说明。",
        investigation_case={
            "case_id": _id("case", "vent-copy-dispute"), "title": "井口抄条的倒印",
            "brief": "鸣石小社把换气井的‘开’抄成‘停’，地底值事又被怀疑来过地面改条。"
                     "问雨坂弦用了哪张抄条，沿山脚与雾坡分渠到井口找旧底联，再到赤泥交班廊核对值事人的去处。",
            "routes": routes,
            "wrong_suspect_effect": {"dimension": "trust", "delta": -8},
        },
    )]
    for key, title, place, description, subject, fact, text, hidden in (
        ("vent-carbon", "井沿开闭底联", "wind-vent", "旧底联卷在护栏木箱的夹缝里。它只记录当时的井口状态。", vent, vent_open,
         "两边的值事在十六时对签，地面读数与井下读数都落在开放区间，版角左侧印着‘开’。", True),
        ("warden-roll", "赤泥交班簿", "red-clay-gallery", "签名经过不同班次保管人核抄，原页的地底工位仍可辨认。", warden, underground,
         "赤泥交班廊的当班记录记有铁砂绰十六时在地底与接班人同查回风计，双方后来在廊里一同交簿。", False),
    ):
        items.append(_item(key, title, place, description, hidden=hidden, historical_evidence={
            "evidence_id": _id("evidence", key), "subject_id": subject, "claim": text,
            "fact": fact, "provenance": [_source(key, case=source)],
        }))
    claims = [KnowledgeRecord(
        id=_id("knowledge", key), holder_id=holder, subject_id=subject,
        kind="claim", claim=text, fact=fact, confidence="0.69", received_tick=_OPENING_TICK,
        disclosure_policy="if_asked", visibility={"policy": "private"}, provenance=[_source(key, case=source)],
    ) for key, holder, subject, fact, text in (
        ("shrine-copy", steward, vent, vent_closed,
         "十六时井口停验，我就是这么抄到社里的。抄来的一联背后没有井沿那块台账的印章。"),
        ("warden-copy", warden, warden, underground,
         "十六时我在赤泥廊和接班人查回风计，地面那张抄条我从未经手。"),
    )]
    return items, claims


def _lore() -> list[WorldBookEntry]:
    """Public setting and local traditions for the GM's keyword lookup."""
    return [WorldBookEntry(
        entry_id=_id("lore", key), source_key=key, title=title, content=text,
        keywords=keywords, always_on=False, visibility="public",
    ) for key, title, keywords, text in (
        ("village-books", "村西借阅与南巷书铺", ("人间之里", "纸梯书铺", "书铺", "束页廊"),
         "人间之里给人类提供聚居的街巷，各家对妖怪来访的看法不同。纸梯书铺把异变记述、路上亲见和街谈分栏，"
         "同一件事可以同时有三种记法。凡要给返乡旅客引路的段落，都要和灯棚留下的抄签核对。"),
        ("shrines", "山社与村外小社", ("神社", "鸣石小社", "妖怪之山", "供灯"),
         "山上的神社、人间之里以外的神社和村头小社各有往来的人。鸣石小社的值事按实际走过的路径收石片，"
         "靠水道还愿的走山脚水尺台，沿界缘归来的人从回声隘来。回灯节每户可借一盏灯。"),
        ("moon-tales", "月影抄图里的证词", ("观月石台", "月影", "月都", "满月", "永夜"),
         "关于月亮的旧闻，纸梯书铺记录了谈月都的故事，也记录曾有满月异变的记载。观月石台专做当夜的观测，"
         "把岸上方位与水中倒影分开填写。暴雨时到束页廊比对往日的图，观月人把传闻、记载和今夜观测分开。"),
        ("mountain-water", "山麓的两份分水尺表", ("山脚水尺台", "雾坡分渠", "河童", "水尺"),
         "上游的山地信仰与下游的吃水规矩归不同的人照管。河童在雾坡分渠量分水，人间之里的染坊记用水，"
         "渡铃坞另登记夜泊是否够深。两份尺表必须同时注明岸别与时段。"),
        ("below-ground", "地面与旧地狱之间的气井", ("旧地狱", "地底", "地底风井", "赤泥交班廊"),
         "地面传说常把归不来的风都指向旧地狱。地底风井由地上、地底两班共同验读，赤泥廊只留当班记录与来访签条。"
         "关于井下其他街道的说法，须看讲述者到底来自哪一段路。"),
        ("outer-boundary", "魔法森林与界缘灯签", ("魔法森林", "界缘", "结界", "回声隘", "外界"),
         "幻想乡和外界有结界相隔。回声隘的折返灯签只给界内往来的人记路。魔法森林边缘的量雨径以树根和纸绳为标，"
         "到了界缘，风吹落哪张灯签可以记进抄页，真正的返程方向以眼前的石刻为准。"),
        ("light-patterns", "回灯节的灯影试手", ("回灯节", "灯影", "符卡", "弹幕"),
         "街坊熟悉人和妖怪以受约束的弾幕较量来处理争执，从中借来轮流出题和事前约定的做法。"
         "纸铃社的节夜游戏用两盏灯照纸线，只比各人辨认图样的本领。双方照完一轮便互换灯盏。"),
    )]


def build_scenario() -> WorldState:
    """Materialize the independently validated genesis snapshot."""
    items, claims = _case()
    vent_items, vent_claims = _vent_case()
    entities = [*_places(), *_people(), *items, *vent_items,
        _item("festival-rules", "回灯节的灯影牌", "paper-bell", "两人各照亮一条地上纸线，在尽头互换灯盏。若灯火熄灭，等双方拾好纸线再重开。"),
        _item("reverse-tags", "岩隙的空白灯签", "echo-notch", "空签挂在短竹上，竹节间的刻线标着返程方向。页脚留着一个给回来者记路的格子。"),
        _item("rain-log", "雨径量雨册", "rain-path", "今日最后一页记录三次同向的落雨和一次反向的滴水。采菌者可以在下一页记下沿路观测。"),
        _item("library-ledger", "纸梯书铺借阅牌", "paper-ladder", "正面记可外借的山路摘记，背面记三次返书时间。最晚一行旁写着十八时二十五分读月影图。"),
        _item("observatory-charts", "两夜月影读图", "binding-porch", "一页记岸上方位，一页记水面倒影；折羽祐岚用细绳在十八时的读数旁做了两种记号。"),
        _item("river-gauges", "分渠的两张水尺表", "water-gauge", "二表分别夹了蓝绳和褐绳，对着渠口上的蓝褐余记可以分辨染坊和渡口所用的那张。"),
        _item("warding-stones", "鸣石小社的还愿石片", "ringing-shrine", "过渡口的人刻一点，从界缘回村的人刻半圈。石片上消磨掉一笔的旧名字能向值事人问明。"),
        _group("lantern-bench", "回灯棚值事会", "lantern-yard", ["keeper", "courier", "player"], "轮值照路，原则是危急时先留一盏给陌路人。公开更正由经手人署名。"),
        _group("paper-bell", "纸铃社", "paper-bell", ["register", "watcher", "sorter"], "传号按见闻和来话分栏。屋里可将两份独立记录一同复核。"),
        _group("ferry-bench", "渡铃坞泊灯社", "ferry-bell", ["ferrier", "repairer"], "夜渡由守铃人与验灯工合签，留灯时段给往来同行的人查阅。"),
        _group("paper-ladder-circle", "纸梯共读会", "paper-ladder", ["bookbinder", "sorter"], "读书时把亲见、转述和抄件分栏；遇雨搬到束页廊，来得迟可以在借阅牌上找当晚读过的页。"),
        _group("water-and-wind", "水尺风井互核所", "water-gauge", ["river-surveyor", "well-warden", "shrine-steward"], "换班双方共同验读。本地的水尺和风井记录都须写明取数地点。"),
    ]
    player = _person("player")
    return WorldState(
        current_tick=_OPENING_TICK,
        calendar=CalendarConfig(year_days=360, calendar_id="lanterns.festival-calendar"),
        entities={entity.id: entity for entity in entities}, resources=_accounts(), player_id=player,
        relations=[RelationRecord(id=_id("relation", f"{a}:{b}"), from_id=_person(a), to_id=_person(b),
                                  dimensions={"trust": trust}, label=label, updated_tick=_OPENING_TICK)
                   for a, b, trust, label in (
                       ("player", "keeper", 25, "巡灯师友"), ("player", "courier", 6, "共事的抄手"),
                       ("repairer", "player", 10, "值事会的巡灯人"), ("courier", "player", 6, "共事的学徒"),
                       ("register", "watcher", 28, "交班时互相校记"), ("ferrier", "repairer", 24, "共管桥灯"),
                   )],
        knowledge=[*claims, *vent_claims], world_book_entries=_lore(),
        goals=[GoalRecord(
            id=_id("goal", "check-the-notice"), owner_id=player, title="查清借影亭的闭灯告示",
            description="在回灯棚的查告公夹登记。先核对盐见织帆的话和灯棚接递底票，也可以到纸铃社查第二条线索，去芦绳桥听柴汀说明当值去处。",
            priority=85, created_tick=_OPENING_TICK, updated_tick=_OPENING_TICK,
        )],
        metadata={
            "pack_id": PACK_ID, "pack_version": PACK_VERSION, "public_title": PACK_TITLE,
            "public_description": "人间之里迎来回灯节。今天却有人宣称借影亭已停值，归路上的人因此改道。你是灯棚的巡灯学徒，须核清告示来路。"
                                  "从村巷书铺可以走到鸣石小社和观月石台，沿水上行能到山麓分渠；水尺另一侧的岔道通往地底交班廊。",
            "content_rules": {"magic": {"enabled": True}},
            "resource_catalog": {_COIN: "铜钱", _FOOD: "炙米饼", _WATER: "甜井水", _MEDICINE: "药膏"},
            "resource_roles": {
                "food_resource_id": _FOOD, "water_resource_id": _WATER,
                "consumption_sink_account_ids": {
                    resource: str(_id("account", f"consume:{resource}"))
                    for resource in (_FOOD, _WATER, _MEDICINE)
                },
            },
        },
    )


def build_archive() -> bytes:
    """Serialize this opening via the same package policy used by import."""
    return write_world_pack(
        manifest=WorldPackManifest(
            pack_id=PACK_ID, version=PACK_VERSION, title=PACK_TITLE, engine_range=">=0.1.0,<0.2.0",
        ),
        template={"scenario": build_scenario().model_dump(mode="json"), "ruleset_version": MAGIC_GATE_RULESET_VERSION},
    )
