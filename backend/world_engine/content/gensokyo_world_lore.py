"""Source-backed setting details for the Gensokyo reference world book."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote


@dataclass(frozen=True)
class LoreEntry:
    key: str
    title: str
    aliases: tuple[str, ...]
    content: str
    category: Literal["place", "organization", "rule", "other"]
    source_id: str
    locator: str
    available_from: int = 6
    status: Literal["documented", "unknown"] = "documented"
    viewpoint: str | None = None


AKYUU = "稗田阿求《幻想乡缘起》的记述"
_MEMENTO = "zun.perfect-memento"
_KEYWORDS = "anniversary.keywords"

LORE_SOURCES = {
    f"{_MEMENTO}.{key}": {
        "work": "ZUN《东方求闻史纪》",
        "url": "https://thwiki.cc/" + "/".join(map(quote, ("东方求闻史纪", section, "中日对照"))),
        "official_work_page": "https://data.ichijinsha.co.jp/detail/75801063",
        "host": "第三方留存的日文原文",
    }
    for key, section in (
        ("foreword", "序"),
        ("fairies", "妖精"),
        ("phantoms", "幽灵"),
        ("youkai", "妖怪"),
        ("magicians", "魔法使"),
        ("keine", "上白泽慧音"),
        ("tengu", "天狗"),
        ("aya", "射命丸文"),
        ("yama", "阎魔"),
        ("outsiders", "外来人"),
        ("eirin", "八意永琳"),
        ("village", "人类村落"),
        ("mountain", "妖怪之山"),
        ("sanzu", "三途河"),
        ("duel-draft", "命名决斗法案"),
    )
} | {
    "kadokawa.lotus-eaters.1": {
        "work": "KADOKAWA《东方醉蝶华》第一卷简介",
        "url": "https://www.kadokawa.co.jp/product/322005000454/",
        "host": "出版社",
    },
    "official.th135": {
        "work": "《东方心绮楼》官方简介",
        "url": "https://touhou-project.news/titles/th135/",
        "host": "官方站点",
    },
}


# Availability is the earliest relevant package opening, rather than a calendar date.
# Some later reference publications describe practices already established in earlier stories.
LORE_ENTRIES = (
    LoreEntry(
        "village-trade", "人间之里的日用买卖", ("村里买东西", "人里商铺", "日用品", "人间之里做生意"),
        "阿求称里内能买到人类所需的生活用品，也有店铺接待妖怪。有的店夜里另做面向妖怪的生意，酒铺里也常有人类与妖怪同席饮酒。",
        "other", f"{_MEMENTO}.village", "第136页 · 人间之里", viewpoint=AKYUU,
    ),
    LoreEntry(
        "village-protection", "人间之里的安全与保护", ("妖怪袭击人里", "人里安全吗", "妖怪贤者", "谁保护人里"),
        "阿求将里内相对少见袭击归因于妖怪贤者的保护，并提到里内也有以退治妖怪为职业的居民。她把这里列作低危险区域。",
        "rule", f"{_MEMENTO}.village", "第136页 · 危险度与妖怪贤者", viewpoint=AKYUU,
    ),
    LoreEntry(
        "village-demographics", "人间之里的人口与治理细则", ("人里人口", "人里有多少人", "人里的户数", "人里谁管", "人里村长"),
        "人间之里是幻想乡的人类主要聚居点。引用的村落资料只描述居住、商铺和保护关系，人口总数、户数及治理机构的正式编制仍未确定。",
        "other", f"{_MEMENTO}.village", "第136页 · 人间之里", status="unknown", viewpoint=AKYUU,
    ),
    LoreEntry(
        "keine-school", "人间之里的寺子屋", ("人里学堂", "人里学校", "慧音教课", "寺子屋"),
        "上白泽慧音居住在里内，开办寺子屋并编纂历史。阿求记载，稗田家积累的资料用于慧音的教学。",
        "organization", f"{_MEMENTO}.keine", "第73—74页 · 上白泽慧音、附注3", 9, viewpoint=AKYUU,
    ),
    LoreEntry(
        "hieda-chronicle", "稗田家与《幻想乡缘起》", ("幻想乡缘起", "稗田家", "阿求记载", "妖怪图鉴"),
        "稗田家为帮助人类辨认妖怪与危险而持续整理资料，阿求自述为第九代编纂者。她在序中把种族通性、个体目击和未解决资料列作不同内容。",
        "organization", f"{_MEMENTO}.foreword", "第4—5页 · 稗田阿求的序", 9, viewpoint=AKYUU,
    ),
    LoreEntry(
        "eientei-medicine", "永远亭的看诊与药箱", ("看病", "永远亭买药", "永琳的药", "药箱", "置药"),
        "永琳在永远亭为人类和妖怪看诊、制药。阿求记下两种供药方式：家中药箱按用量收钱，部分特殊药则在永远亭购买。人类与妖怪用药有别。",
        "other", f"{_MEMENTO}.eirin", "第128—129页 · 八意永琳的供药与诊疗", 9, viewpoint=AKYUU,
    ),
    LoreEntry(
        "bunbunmaru-press", "《文文。新闻》", ("文文新闻", "天狗报纸", "报纸", "记者", "新闻"),
        "射命丸文以采访问讯和摄影编写这份报纸。阿求记载它不定期发行，天狗之外的妖怪及人类也可以订阅。",
        "other", f"{_MEMENTO}.aya", "第89—91页 · 文文。新闻", 9, viewpoint=AKYUU,
    ),
    LoreEntry(
        "suzunaan-lending", "铃奈庵的租书铺", ("铃奈庵", "鈴奈庵", "租书", "借书", "出租书店"),
        "铃奈庵是《东方铃奈庵》的租书铺。本居小铃是这家店的少女，她在第一卷引发了与妖怪苏醒有关的骚动。",
        "place", "kadokawa.suzunaan.1", "第一卷 · 出版社故事简介", 14,
    ),
    LoreEntry(
        "geidontei-tavern", "鲵吞亭", ("鯢呑亭", "鲵呑亭", "人里居酒屋", "奥野田美宵"),
        "《东方醉蝶华》第一卷在里内的居酒屋鲵吞亭展开，美宵是店里的招牌店员。酒客被妖怪掳走的说法以传闻出现，灵梦为此展开调查。",
        "place", "kadokawa.lotus-eaters.1", "第一卷 · 出版社故事简介", 18,
    ),
    LoreEntry(
        "species-usage", "妖怪称谓与种族分类", ("种族", "幻想乡有什么种族", "妖怪分类", "妖精算妖怪吗"),
        "官方纪念站给出两种用法。广义的妖怪泛指人类以外的种族，细分时则分别谈妖怪、妖精、魔法使、幽灵、鬼、天狗和神等。",
        "other", _KEYWORDS, "种族条目",
    ),
    LoreEntry(
        "youkai-traits", "妖怪的常见体质", ("妖怪寿命", "妖怪恢复", "妖怪怕什么", "妖怪体质"),
        "阿求列举的常见倾向包括比人类长寿、身体强韧和容易受信念影响。她也明确记下例外，具体个体需要结合自己的资料判断。",
        "other", f"{_MEMENTO}.youkai", "第31—32页 · 妖怪的特征", viewpoint=AKYUU,
    ),
    LoreEntry(
        "fairy-nature", "妖精与自然现象", ("妖精", "妖精会死吗", "妖精复生", "妖精怕什么"),
        "阿求把妖精和风雨、花开、冷热等自然现象联系起来。她记述妖精在自然中广泛分布，常在热闹处出现，失去现有形体后可以很快以原有外貌重生。",
        "other", f"{_MEMENTO}.fairies", "第8—9页 · 妖精的特征", viewpoint=AKYUU,
    ),
    LoreEntry(
        "phantoms-and-ghosts", "幽灵与亡灵", ("幽灵", "幽霊", "亡灵", "幽灵和亡灵的区别"),
        "阿求把幽灵描述为气质具现，可以没有死者作为来历，形态也可改变。亡灵则是死者本人的具现。",
        "other", f"{_MEMENTO}.phantoms", "第20—21页 · 幽灵的特征", viewpoint=AKYUU,
    ),
    LoreEntry(
        "magician-transformation", "成为魔法使的记载", ("人变成魔法使", "魔法使种族", "舍食魔法", "舍虫魔法"),
        "阿求区分天生的魔法使与经修习转变的人类。她把舍食之术列作人类转变的条件，并把舍虫之术与魔法使停止老化联系起来。",
        "rule", f"{_MEMENTO}.magicians", "第52—53页 · 魔法使的特征", viewpoint=AKYUU,
    ),
    LoreEntry(
        "mountain-society", "妖怪之山的技术与出入", ("河童技术", "天狗技术", "妖怪之山外来者", "妖怪之山巡逻"),
        "阿求称山里的妖怪形成了自己的社会，通常会逐走侵入者。她列举了天狗的摄影、印刷和出版，以及河童的钢铁、建筑和制作道具技术。",
        "other", f"{_MEMENTO}.mountain", "第141页 · 妖怪之山、附注14", viewpoint=AKYUU,
    ),
    LoreEntry(
        "mountain-interior-rumor", "妖怪之山内部的传闻", ("妖怪之山内部", "山中自动工厂", "山里有外界通道", "山中空洞"),
        "阿求把山内空洞、自动化工厂及连通外界的缺口列作传闻。卷中未确认这些地点存在，也未提供可靠的进入路线。",
        "place", f"{_MEMENTO}.mountain", "第141页 · 山中传闻", status="unknown", viewpoint=AKYUU,
    ),
    LoreEntry(
        "tengu-society", "天狗的群体行动", ("天狗", "天狗如何生活", "天狗领地", "天狗护卫"),
        "阿求记载天狗在山中有各自的社会，飞行速度较快，对同伴或领地受侵犯的情况常采取集体行动。记录也提到天狗从事采编和出版。",
        "other", f"{_MEMENTO}.tengu", "第87—88页 · 天狗的特征与对策", viewpoint=AKYUU,
    ),
    LoreEntry(
        "barrier-common-sense", "博丽大结界的常识边界", ("博丽大结界", "常识的境界", "结界能穿过吗", "外界怎么进来"),
        "大结界隔绝幻想乡和外界，也改变结界内外事物被视作常识的条件。通常难以穿越，个别情况下人或物可以越过结界。",
        "rule", _KEYWORDS, "博丽大结界条目",
    ),
    LoreEntry(
        "outsider-resettlement", "外来人的去向", ("外来人", "外界人怎么回去", "外界人定居", "神隐"),
        "阿求记载了从外界误入的活人。他们在野外可能遇险，有人抵达人间之里后由博丽神社协助返回，也有人留在幻想乡。",
        "rule", f"{_MEMENTO}.outsiders", "第110页 · 外来人", viewpoint=AKYUU,
    ),
    LoreEntry(
        "spell-card-declaration", "符卡的宣言与使用", ("怎么用符卡", "偷走符卡", "符卡宣言", "复制弹幕"),
        "使用符卡时先宣布卡上的名称，弹幕由使用者自身能力形成。拿走别人写下名称的纸也无法藉此获得对方的能力。",
        "rule", _KEYWORDS, "符卡条目",
    ),
    LoreEntry(
        "named-duel-draft", "命名决斗法案原案", ("符卡规则", "命名决斗", "决斗回数", "符卡认输"),
        "原案要求在开战前给出命名决斗的回数，给招式赋予名称和意义，败者即使有余力也须承认结果，胜者不得杀害人类。原案把实际做法留待与巫女商议。",
        "rule", f"{_MEMENTO}.duel-draft", "第160页 · 法案原案", viewpoint="《幻想乡缘起》收录的未解决文书",
    ),
    LoreEntry(
        "named-duel-authorship", "命名决斗原案的执笔者", ("谁写了符卡规则", "符卡规则的创立者", "谁提出符卡规则"),
        "阿求在附记中把巫女定下规则和原案由谁执笔分别提出。她因所用纸张推想也许有妖怪提议，但保留了原案作者的问题。",
        "rule", f"{_MEMENTO}.duel-draft", "第160页 · 稗田阿求的附记", status="unknown", viewpoint=AKYUU,
    ),
    LoreEntry(
        "hakurei-shrine-customs", "博丽神社的参拜与宴会", ("博丽神社参拜", "博丽神社赛钱", "博丽神社赏樱", "博丽神社宴会"),
        "博丽神社虽常有妖怪来访，香火钱却难以积攒。纪念站也提到这里以樱花闻名，作品中有在神社办宴会的场景。",
        "other", _KEYWORDS, "博丽神社条目",
    ),
    LoreEntry(
        "hakurei-faith", "博丽神社的信仰与参拜者", ("博丽神社信仰", "博丽神社香火", "为什么缺参拜者"),
        "《风神录》开始时，博丽神社一直缺少人类参拜者。作品简介将信仰不足视作足以影响神社存续的问题。",
        "rule", "official.th10", "故事简介 · 博丽神社的处境", 10,
    ),
    LoreEntry(
        "moriya-deities", "守矢神社供奉的神明", ("守矢神社供奉谁", "守矢神社的神", "神奈子与诹访子", "山上的巫女"),
        "《东方茨歌仙》人物介绍指出，山上新建神社的巫女是东风谷早苗，神社供奉八坂神奈子与洩矢诹访子。",
        "organization", "ichijinsha.wild-horned-hermit", "东风谷早苗的人物介绍", 10,
    ),
    LoreEntry(
        "mountain-faith-settlement", "守矢神社与山中妖怪的交涉", ("守矢与天狗", "守矢与河童", "神奈子在山上的信仰"),
        "《风神录》额外剧情记载，神奈子与河童和天狗和解后，得以在山上设立神社、向山中妖怪聚拢信仰。",
        "organization", "zun.th10.release-notes", "额外篇故事 · 神奈子与山中妖怪和解后", 11,
    ),
    LoreEntry(
        "myouren-worship", "命莲寺的供奉与缘日", ("命莲寺供奉谁", "毗沙门天", "命莲寺缘日", "白莲的寺院"),
        "圣白莲建立了命莲寺。寺院原以救助妖怪为目的，也接纳人类，供奉毗沙门天。有时会举办人类和妖怪都来参加的缘日活动。",
        "organization", _KEYWORDS, "命莲寺条目", 13,
    ),
    LoreEntry(
        "taoist-institutions", "道教在幻想乡的登场", ("道教", "道士", "丰聪耳神子", "博丽神社的宗教对手"),
        "《东方神灵庙》的官方简介将出现的道教视作博丽神社在宗教上的对手。异变所见的神灵由欲望构成。",
        "organization", "official.th13", "故事简介与道教登场说明", 13,
    ),
    LoreEntry(
        "religious-public-contest", "《心绮楼》中的信仰争夺", ("宗教竞争", "宗教家争信仰", "心绮楼", "博丽神社命莲寺神子"),
        "《东方心绮楼》中，博丽灵梦、圣白莲、丰聪耳神子一方的宗教家以救助人类为由竞争，争取居民的人气与信仰。",
        "other", "official.th135", "故事简介 · 宗教家对人气的争夺", 14,
    ),
    LoreEntry(
        "sanzu-ferry", "三途河的摆渡", ("三途河怎么过", "三途河渡船", "彼岸渡口", "三途河能游吗"),
        "阿求把三途河描述为此岸与彼岸的分界。她记载渡河须搭乘死神的船，普通船只会沉没，活人面对的渡费也高得几乎无法承担。",
        "rule", f"{_MEMENTO}.sanzu", "第142页 · 三途河及其附注", 9, viewpoint=AKYUU,
    ),
    LoreEntry(
        "afterlife-judiciary", "是非曲直厅与阎魔的审判", ("是非曲直厅", "是非曲直庁", "阎魔审判", "死后谁审判"),
        "阿求记载，阎魔负责裁断死者，地狱的公立机构是非曲直厅由阎魔、死神和地狱的鬼共同组成，十王居于其上层。",
        "organization", f"{_MEMENTO}.yama", "第106—107页 · 阎魔及是非曲直厅", 9, viewpoint=AKYUU,
    ),
)
