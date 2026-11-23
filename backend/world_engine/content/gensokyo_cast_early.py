"""Character evidence from the early Windows releases of Touhou Project."""

from typing import NamedTuple


class EarlyCastFact(NamedTuple):
    document: int
    locator: str
    traits: tuple[str, ...]
    anchor: str | None = None
    source: str | None = None


class EarlyCastRelation(NamedTuple):
    subject: str
    target: str
    kind: str
    description: str
    document: int
    locator: str
    source: str | None = None


EARLY_CAST_SOURCES = {
    "zun.th06.manual": {
        "work": "ZUN《东方红魔乡》正式版网页说明书",
        "url": "https://thwiki.cc/附带文档:东方红魔乡/Manual",
        "host": "第三方留存的日文原文",
    },
    "zun.th07.sakuya-dialogue": {
        "work": "ZUN《东方妖妖梦》十六夜咲夜路线游戏文本",
        "url": "https://thwiki.cc/游戏对话:东方妖妖梦/十六夜咲夜/中日对照",
        "host": "第三方留存的日文原文",
    },
}

# The document number is the evidence cutoff. An anchor is a playable initial
# position based on the person's documented activities, workplace or home.
EARLY_CAST_FACTS: dict[str, EarlyCastFact] = {
    "reimu": EarlyCastFact(6, "自机介绍与故事背景 · 博麗霊夢", (
        "性情悠闲的博丽神社巫女", "红雾出现后，凭直觉向湖的方向调查",
    ), "shrine", "zun.th06.manual"),
    "marisa": EarlyCastFact(6, "自机介绍与故事背景 · 霧雨魔理沙", (
        "头脑灵活，乐于做自己喜欢的事", "住在森林一带，经常到神社找灵梦玩",
    ), "forest", "zun.th06.manual"),
    "rumia": EarlyCastFact(6, "人物章 · ルーミア", (
        "身边笼罩着黑暗", "头上缠着符札，她自己无法触碰它",
    )),
    "cirno": EarlyCastFact(6, "人物章 · チルノ", (
        "喜欢恶作剧", "会冻住青蛙，再把它们放进水里解冻",
    )),
    "meiling": EarlyCastFact(6, "人物章 · 紅美鈴", (
        "担任红魔馆门卫", "会拦截从湖外接近红魔馆的闯入者",
    ), "mansion-lakeshore"),
    "patchouli": EarlyCastFact(6, "人物章 · パチュリー·ノーレッジ", (
        "习惯待在藏书室里", "精于多种属性魔法，哮喘会妨碍完整咏唱",
    ), "mansion-library"),
    "sakuya": EarlyCastFact(6, "人物章 · 十六夜咲夜", (
        "统筹红魔馆馆务，也亲自做清扫工作", "会运用停止时间的能力做家务",
    ), "mansion"),
    "remilia": EarlyCastFact(6, "人物章 · レミリア·スカーレット", (
        "性格任性，保有稚气", "是红魔馆的主人，人物资料记载她能操纵命运",
    ), "mansion"),
    "flandre": EarlyCastFact(6, "人物章 · フランドール·スカーレット", (
        "平常留在红魔馆内", "仰慕姐姐，平常很少发怒",
    ), "mansion"),
    "letty": EarlyCastFact(7, "人物章 · レティ·ホワイトロック", (
        "享受冬日", "到了春天会消失，去向不明",
    )),
    "chen": EarlyCastFact(7, "人物章 · 橙（チェン）", (
        "冬天会蜷在被炉里", "式神附体解除后仍能说人话和飞行",
    ), "mayohiga"),
    "alice": EarlyCastFact(7, "人物章 · アリス·マーガトロイド", (
        "爱收集书籍、魔法道具及来历不寻常的人偶", "交手时惯于观察对手并保留实力",
    ), "forest"),
    "lily-white": EarlyCastFact(7, "人物章 · リリーホワイト", (
        "有向人传达春天到来的能力", "春迟时曾飞上云端查看，并试着把春讯告诉遇到的人",
    )),
    "lunasa": EarlyCastFact(7, "人物章 · 騒霊三姉妹 · 長女", (
        "重视公正的较量，性情直率而容易相信别人", "擅长小提琴等弦乐器，平时偏好独奏",
    ), "prismriver-house"),
    "merlin": EarlyCastFact(7, "人物章 · 騒霊三姉妹 · 次女", (
        "性情开朗，举止从容", "擅长管乐，尤其是小号",
    ), "prismriver-house"),
    "lyrica": EarlyCastFact(7, "人物章 · 騒霊三姉妹 · 三女", (
        "行事精明，冲突时倾向让姐姐们先出手", "能演奏各类乐器，平时多负责键盘或打击乐",
    ), "prismriver-house"),
    "youmu": EarlyCastFact(7, "人物章 · 魂魄妖夢", (
        "负责白玉楼的庭院，也护卫幽幽子", "继任庭师后仍每天修习剑术",
    ), "saigyouji-residence"),
    "yuyuko": EarlyCastFact(7, "人物章 · 西行寺幽々子", (
        "居于冥界白玉楼，平时喜欢阅读", "翻到旧记录后，对西行妖的封印产生兴趣",
    ), "saigyouji-residence"),
    "ran": EarlyCastFact(7, "人物章 · 八雲藍", (
        "趁紫睡觉时代她活动", "樱花季常到冥界赏花",
    )),
    "yukari": EarlyCastFact(7, "人物章 · 八雲紫", (
        "隐居于幻想乡边境，能操纵境界", "平时多让式神八云蓝代为办事",
    ), "hakurei-barrier"),
    "wriggle": EarlyCastFact(8, "人物章 · リグル·ナイトバグ", (
        "身边经常聚集许多虫子", "怕冷，也怕杀虫剂",
    )),
    "mystia": EarlyCastFact(8, "人物章 · ミスティア·ローレライ", (
        "只在夜间出没", "能用歌声扰乱判断，也能使人难以在暗处视物",
    )),
    "keine": EarlyCastFact(8, "人物章 · 上白沢慧音", (
        "亲近人类，使用能力保护他们", "通常以人形活动，遇满月会变为白泽",
    ), "human-village"),
    "tewi": EarlyCastFact(8, "人物章 · 因幡てゐ", (
        "是永远亭兔群的首领，性情激烈", "能带给人类好运，竹林迷路者因而把她视作引路人",
    ), "eientei"),
    "reisen": EarlyCastFact(8, "人物章 · 鈴仙·優曇華院·イナバ、蓬莱山輝夜", (
        "从月球逃到地上后受到辉夜收留", "能通过月兔的波动接收同类传来的消息",
    ), "eientei"),
    "eirin": EarlyCastFact(8, "人物章 · 八意永琳、蓬莱山輝夜", (
        "通晓多种药物的制作", "很早便认识辉夜，并选择陪她留在地上",
    ), "eientei"),
    "kaguya": EarlyCastFact(8, "人物章 · 蓬莱山輝夜", (
        "原是月都的公主，能力与永远和须臾有关", "感念曾照顾自己的地上人，曾希望留在地上生活",
    ), "eientei"),
    "mokou": EarlyCastFact(8, "人物章 · 藤原妹紅", (
        "曾因不会衰老而多次迁居，后来隐居山中", "服下蓬莱之药后，仍能感到饥饿或疼痛",
    )),
}

# When a place association is documented in a different section or work than
# the traits, this locator supplies the more specific citation for the anchor.
EARLY_CAST_ANCHOR_EVIDENCE = {
    "chen": ("zun.th07.sakuya-dialogue", "Stage 2 · マヨヒガの黒猫"),
    "alice": ("zun.th08.release-notes", "人物章 · アリス·マーガトロイド"),
}

EARLY_CAST_RELATIONS = (
    EarlyCastRelation("marisa", "reimu", "regular_visitor", "魔理沙经常到神社找灵梦玩。", 6,
                      "自机介绍 · 霧雨魔理沙", "zun.th06.manual"),
    EarlyCastRelation("flandre", "remilia", "younger_sister", "芙兰朵露是蕾米莉亚的妹妹，并且仰慕姐姐。", 6,
                      "人物章 · フランドール·スカーレット"),
    EarlyCastRelation("patchouli", "remilia", "friend", "帕秋莉是蕾米莉亚的朋友。", 6,
                      "人物章 · パチュリー·ノーレッジ、十六夜咲夜"),
    EarlyCastRelation("sakuya", "remilia", "head_maid", "咲夜担任蕾米莉亚馆里的女仆长，并负责馆内事务。", 6,
                      "人物章 · 十六夜咲夜、レミリア·スカーレット"),
    EarlyCastRelation("ran", "yukari", "shikigami", "八云蓝是八云紫的式神。", 7, "人物章 · 八雲藍"),
    EarlyCastRelation("chen", "ran", "shikigami", "橙是八云蓝的式神。", 7, "人物章 · 橙（チェン）"),
    EarlyCastRelation("yukari", "yuyuko", "longtime_acquaintance", "紫与幽幽子是旧识。", 7, "人物章 · 八雲紫"),
    EarlyCastRelation("youmu", "yuyuko", "retainer", "妖梦是西行寺家的庭师，也是幽幽子的护卫。", 7,
                      "人物章 · 魂魄妖夢"),
    EarlyCastRelation("alice", "marisa", "collector_rival", "爱丽丝和魔理沙都收集魔法物品，时常遇上，也时常合不来。", 7,
                      "人物章 · アリス·マーガトロイド"),
    EarlyCastRelation("lunasa", "merlin", "ensemble_sister", "露娜萨与梅露兰是骚灵三姐妹中的长女和次女，共同演奏。", 7,
                      "人物章 · 騒霊三姉妹"),
    EarlyCastRelation("lunasa", "lyrica", "ensemble_sister", "露娜萨与莉莉卡是骚灵三姐妹中的长女和三女，共同演奏。", 7,
                      "人物章 · 騒霊三姉妹"),
    EarlyCastRelation("merlin", "lyrica", "ensemble_sister", "梅露兰与莉莉卡是骚灵三姐妹中的次女和三女，共同演奏。", 7,
                      "人物章 · 騒霊三姉妹"),
    EarlyCastRelation("eirin", "kaguya", "longtime_ally", "永琳早在月都便认识辉夜，后来选择同她在地上生活。", 8,
                      "人物章 · 八意永琳、蓬莱山輝夜"),
    EarlyCastRelation("kaguya", "reisen", "protector", "铃仙从月球逃到地上后，辉夜把她留在家里。", 8,
                      "人物章 · 蓬莱山輝夜"),
    EarlyCastRelation("eirin", "reisen", "nickname_given", "优昙华院是永琳给铃仙取的称呼。", 8,
                      "人物章 · 鈴仙·優曇華院·イナバ"),
    EarlyCastRelation("mokou", "kaguya", "adversary", "妹红自幼因父亲的遭遇敌视辉夜。", 8, "人物章 · 藤原妹紅"),
)
