"""Opt-in, deterministic travel opportunities for the built-in backgrounds.

These offers advertise scenes a traveler could choose to explore.  They do not
create people, grant items, perform work, or otherwise resolve an interaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid5

from world_engine.domain.components import WaypointOpportunity


@dataclass(frozen=True, slots=True)
class _Opening:
    place_kind: str
    place_index: int
    key: str
    title: str
    summary: str
    finite_window_days: int | None = None
    minimum_elapsed_days: int = 0


# The catalog's settlement list is in region-major order.  Index 8 is the
# hidden historical site in the shipped catalog; none of these authored scenes use it.
_OPENINGS: dict[str, tuple[_Opening, ...]] = {
    "modern_city": (
        _Opening(
            "region", 0, "harbor-barrier-notices",
            "海堤工程的两份绕行图",
            "可停下核对两版路线图的版号、时段和边界标记，再对照现场可见的路标；它们的差异具体落在哪一段？",
        ),
        _Opening(
            "settlement", 1, "southline-night-transfer",
            "南线站口的夜班换乘",
            "可查看站点夜间时刻表，比较末班到站和接驳发车的时间；表上有没有说明两者是否衔接？",
        ),
        _Opening(
            "settlement", 2, "harbor-market-hearing",
            "港区临时摊位的公开议事",
            "可在公示期内查看临时摊位布置说明，核对通道宽度、装卸时段和未定项目；这份方案是否说明了哪些安排尚未确定？",
            finite_window_days=7,
        ),
        _Opening(
            "settlement", 10, "north-station-night-shifts",
            "北站深夜的通勤记录",
            "旅程累计至少五日后，可放慢脚程查看北站夜间运行对照表，比较不同站台的末班与接驳时刻；表中哪些情况没有记录？",
            minimum_elapsed_days=5,
        ),
    ),
    "low_magic_feudal": (
        _Opening(
            "region", 0, "ashen-march-road-levy",
            "林道旧抄表的通行项目",
            "可查看无日期的旧抄表，核对列出的对象、数字和空栏；地点与抄录日期都没有注明，仅凭这页能定位到哪条路、哪段时间吗？",
        ),
        _Opening(
            "settlement", 1, "march-market-grain-prices",
            "集镇粮食储备登记",
            "可查看粮食储备登记摘要，对照品类和计量单位；这份记录有没有标出价格、持有人或可用数量？",
        ),
        _Opening(
            "settlement", 10, "bridges-toll-appeal",
            "九桥谷桥账核对议程",
            "旅程累计至少五日后，可放慢脚程查看桥账核对议程草稿，区分待核的桥名、日期和计量项目；草稿是否给出了计算结果？",
            minimum_elapsed_days=5,
        ),
        _Opening(
            "settlement", 19, "redcliff-salt-weighing",
            "红崖海岸盐袋复秤样张",
            "可在公示期内查看盐袋复秤样张，复核示例中的毛重、空袋重和净重；这些数字是否表示实际货物？",
            finite_window_days=14,
        ),
    ),
    "long_life_cultivation": (
        _Opening(
            "region", 0, "cloudstep-pass-weather-board",
            "云阶驿路的风候牌",
            "可查看驿路风候簿的观测时段、风向和图上标记，再与沿途所见对照；这页记录能否当作之后路况的保证？",
        ),
        _Opening(
            "settlement", 1, "cloudstep-herb-market",
            "山脚集上的药草验看",
            "可查看药草标签核对样张，对照采地、采收时节、阴干日数和保存日期；样例中哪些字段缺失或写得含糊？",
            finite_window_days=7,
        ),
        _Opening(
            "settlement", 10, "jade-river-water-compact",
            "玉河水契的轮值议论",
            "旅程累计至少五日后，可放慢脚程查看灌渠轮次草表，对照各渠时段和空白栏；这份抄表有没有注明抄录日期和复核情况？",
            minimum_elapsed_days=5,
        ),
        _Opening(
            "settlement", 20, "red-lantern-medicine-manifest",
            "赤灯港药材货单样张",
            "可查看药材货单核对样张，比较采集记号、封存日期和转运栏；样张中哪一处信息不足以判断来路？",
        ),
    ),
}


# Each entry is a public-facing source label followed by the notice's own
# contents.  The documents provide inspectable evidence; their statements are
# not silently promoted into world law, confirmed transactions, or NPC claims.
_NOTICES: dict[str, tuple[str, str]] = {
    "海堤工程的两份绕行图": (
        "港区堤岸施工通行图 · 并列版本抄件",
        "甲版 H-03：堤顶步道西段标为 06:00—10:00 仅供步行通过；维护区画在堤外侧。\n"
        "乙版 H-04：同一时段的限制标在堤顶步道东段；维护区边界仍画在堤外侧。\n"
        "两版的日期栏都写着“本周一”，没有注明交替使用的时刻。",
    ),
    "南线站口的夜班换乘": (
        "南线站点夜间时刻表 · 公示抄件",
        "工作日：末班到站 23:20；夜间接驳发车 23:15。\n"
        "休息日：末班到站 23:12；夜间接驳发车 23:18。\n"
        "表下注明“发车时刻为排班时刻”；未列候车、延误或等候安排。",
    ),
    "港区临时摊位的公开议事": (
        "港区临时摊位布置说明 · 七日公示稿",
        "方案甲：摊位外缘至通行线留出 1.5 米；搬运时段列为 06:00—08:00。\n"
        "方案乙：摊位外缘至通行线留出 2 米；搬运时段列为 16:00—18:00。\n"
        "待补栏：通行线起点、雨天调整方式。页脚注明“本稿用于征集意见，不代表已确定的布置”。",
    ),
    "北站深夜的通勤记录": (
        "北站夜间运行对照表 · 公开记录页",
        "站台甲：末班到站 23:42；接驳时刻栏 23:30。\n"
        "站台乙：末班到站 23:50；接驳时刻栏 00:05。\n"
        "记录范围标作“周一至周四”。页脚注明未记录延误、候车人数或乘客实际换乘结果。",
    ),
    "林道旧抄表的通行项目": (
        "林道通行项目抄表 · 无日期摘页",
        "摘页所列项目：步行者——空栏；单头驮畜——1 枚银记；四轮货车——2 枚银记。\n"
        "地点、抄录日期和计量依据均为空白。页边标注“旧件”，未附其他说明。",
    ),
    "集镇粮食储备登记": (
        "集镇粮食储备登记摘要 · 摘录页",
        "春麦：18 石；燕麦：12 石；留种粮：4 袋。\n"
        "价格、持有人和可用数量栏均未列出。页眉注明“此页只摘录登记分类与数量”。",
    ),
    "九桥谷桥账核对议程": (
        "九桥谷桥账核对议程 · 草稿",
        "待核项目：一、桥名与账期是否对应；二、通行次数和计量单位是否分栏；三、各栏小计相加是否等于页尾总数。\n"
        "结果栏为空。页脚注明“本稿只列核对项目，不作计算结论”。",
    ),
    "红崖海岸盐袋复秤样张": (
        "盐袋复秤登记样张 · 示例页",
        "示例批号 R-04：10 袋；示例毛重 310 斤；示例空袋重合计 10 斤；示例净重 300 斤。\n"
        "复秤值和封包标记栏留空。页脚注明“数字仅供演示填写与换算，不对应实际货物”。张贴期限：十四日。",
    ),
    "云阶驿路的风候牌": (
        "云阶驿路风候簿 · 张贴摘录",
        "辰时记录：山口西北风；云层向谷口移动。午时复记栏为空。\n"
        "图例将避风标记编号为甲、乙，但本页没有标注两处与路口的距离。页脚注明“记录时象，不作后续路况保证”。",
    ),
    "山脚集上的药草验看": (
        "药草标签核对样张 · 公开说明",
        "应核对栏：采地、采收时节、阴干日数、分拣等级、保存日期。\n"
        "示例甲：采地“山地北坡”；采收“霜降前”；阴干七日；保存日期空白。\n"
        "示例乙：采地“山道附近”；采收时节未列。页脚注明“本页为标签样张，不代表现场有对应药材”。",
    ),
    "玉河水契的轮值议论": (
        "玉河灌渠轮次表 · 未核草稿",
        "上渠：初一午前；中渠：初一午后；下渠：空白。\n"
        "抄录日期、记录来源和复核栏均未填写。页脚注明“本稿只摘列时段，不补写空白项目”。",
    ),
    "赤灯港药材货单样张": (
        "药材货单核对格式 · 示例页",
        "核对栏：采集记号、采收时节、封存日期、转运日期、验看记录。\n"
        "示例甲：采集记号“谷地-3”；封存日期 7/12；转运日期 7/10；验看记录空白。\n"
        "页脚注明“示例仅用于核对填写字段，不对应实际货批”。",
    ),
}


_MAX_FINITE_WINDOW_DAYS = max(
    opening.finite_window_days or 0
    for pack_openings in _OPENINGS.values()
    for opening in pack_openings
)


def build_waypoint_openings(
    pack_id: str,
    region_ids: Sequence[UUID],
    settlement_ids: Sequence[UUID],
    *,
    day_seconds: int,
) -> dict[UUID, tuple[WaypointOpportunity, ...]]:
    """Build stable optional opportunities at public places in one background.

    ``opens_tick`` and ``closes_tick`` are absolute world ticks (seconds from
    the world timeline).  Finite notice windows close at the exclusive end of
    their configured seven- or fourteen-day span.  The five-day threshold is
    instead measured from the start of the current itinerary.  Offers are only
    checked at waypoint arrivals, so these windows never interrupt routine
    travel.
    """
    try:
        openings = _OPENINGS[pack_id]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"No built-in waypoint openings exist for pack {pack_id!r}.") from exc

    if not isinstance(day_seconds, int) or isinstance(day_seconds, bool) or day_seconds <= 0:
        raise ValueError("day_seconds must be a positive integer.")
    if day_seconds > (2**63 - 1) // _MAX_FINITE_WINDOW_DAYS:
        raise ValueError("day_seconds is too large for waypoint availability ticks.")
    if not region_ids or any(not isinstance(place_id, UUID) for place_id in region_ids):
        raise ValueError("region_ids must contain UUID places, including region_ids[0].")
    if any(not isinstance(place_id, UUID) for place_id in settlement_ids):
        raise ValueError("settlement_ids must contain UUID places.")

    settlement_indexes = {opening.place_index for opening in openings if opening.place_kind == "settlement"}
    if any(index >= len(settlement_ids) for index in settlement_indexes):
        raise ValueError("settlement_ids does not contain every public waypoint place used by this pack.")
    result: dict[UUID, list[WaypointOpportunity]] = {}
    for opening in openings:
        place_id = region_ids[opening.place_index] if opening.place_kind == "region" else settlement_ids[opening.place_index]
        result.setdefault(place_id, []).append(
            WaypointOpportunity(
                opportunity_id=uuid5(place_id, f"world-engine:waypoint-opening:{pack_id}:{opening.key}"),
                title=opening.title,
                summary=opening.summary,
                opens_tick=0 if opening.finite_window_days is not None else None,
                closes_tick=(opening.finite_window_days * day_seconds
                             if opening.finite_window_days is not None else None),
                minimum_itinerary_elapsed_ticks=opening.minimum_elapsed_days * day_seconds,
            )
        )

    return {place_id: tuple(opportunities) for place_id, opportunities in result.items()}


def waypoint_opening_notice(offer: WaypointOpportunity) -> str:
    """Return the inspectable public notice authored for one built-in offer."""
    try:
        author, contents = _NOTICES[offer.title]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"No built-in public notice exists for offer {getattr(offer, 'title', None)!r}.") from exc
    return f"【{author}】\n{contents}"
