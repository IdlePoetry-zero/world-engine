"""Character activity sites and objectives authored for the playable world.

Each entry gives a usual starting site, an optional alternate activity site,
and an immediate objective. Where the atlas has no documented residence, the
starting site is a place to encounter the character in this scenario.
"""

HABITATS: dict[str, tuple[str, str | None, str]] = {
    # Characters documented in the original reference set.
    "reimu": ("shrine", "human-village", "向村民核对异状最先出现的地点和时辰，决定从哪条路开始调查"),
    "marisa": ("forest", "kourindou", "请香霖堂辨认从林中带回的旧器物，回去试试能否用来导引魔力"),
    "yukari": ("shrine", "mayohiga", "核对境界两侧传来的见闻，找出哪一条归路会把行人送回原处"),
    "sakuya": ("mansion", "mansion-lakeshore", "到馆门外核对今日送货的钟点，把迟到的来客和漏送的物件记入馆务簿"),
    "remilia": ("mansion", "mansion-lakeshore", "看看湖面入夜后还剩多少光，决定今晚在馆内还是湖边待客"),
    "alice": ("forest", "kourindou", "找一卷不会被林雾浸松的细线，给两具新做的人偶试装关节"),
    "youmu": ("saigyouji-residence", "netherworld", "沿白玉楼外的来路清扫积叶，确认访客能平安走到庭门"),
    "yuyuko": ("saigyouji-residence", "netherworld", "察看冥界各处的花开得怎样，挑一处可以请客人赏花的地方"),
    "kaguya": ("eientei", None, "从手边珍藏中挑出一件，给今夜来访的人出个问题"),
    "eirin": ("eientei", "bamboo", "核对药用鲜叶和干叶的差别，找出这批药材应该何时采收"),
    "tewi": ("bamboo", "eientei", "听完兔子们各自的报信，弄清哪条竹径今天最容易走错"),
    "patchouli": ("mansion-library", "mansion", "查阅属性魔法的旧笔记，再找一处通风的地方验证湿气对火属性术式的影响"),
    "kosuzu": ("human-village", None, "核对归还的书有没有夹入异样的纸页，把需要辨读的那册单留出来"),
    "kasen": ("youkai-mountain", "shrine", "沿山径探清兽群出现变动的缘故，再与神社的人对照山下见闻"),
    "sanae": ("moriya-shrine", "youkai-mountain", "问清山路来客的顾虑，再决定守矢神社该怎样招待山下的参拜者"),
    "komachi": ("sanzu-river", None, "对照渡客名单和等候次序，给滞留岸边的亡灵安排下一趟船"),

    # 红魔乡
    "rumia": ("forest", "shrine", "试着让黑影沿林缘小路扩散，找出黑暗遮住自己视线时还能辨路的参照物"),
    "cirno": ("mansion-lakeshore", "misty-lake", "试一试哪处水面能冻出坚实的冰面，用结冰的边沿圈出下一场比试的场地"),
    "meiling": ("mansion-lakeshore", "mansion", "查过馆门外的脚印与访客来向，再用运气练习保持值守时的警觉"),
    "flandre": ("mansion", None, "找一件征得同意可以拆坏的旧物，看看最先瓦解的是里面的哪一处"),

    # 妖妖梦
    "letty": ("forest", "shrine", "找一处阴面留住将散的寒气，亲自看看雪线一日退了多少"),
    "chen": ("mayohiga", "forest", "沿不会碰到水的线路送口信给八云蓝，记下下雨后必须绕开的路段"),
    "lily-white": ("forest", "shrine", "找到能证明春天已近的初花，把这个消息报给路上的人"),
    "ran": ("hakurei-barrier", "shrine", "核算边界巡查中看见的偏移，替八云紫标出最值得复查的两处"),

    # 永夜抄
    "wriggle": ("bamboo", "forest", "召来在夜间活动的虫群，看它们会在哪些路口聚集或四散"),
    "mystia": ("forest", "bamboo", "在林路拐弯处试唱一段，听听夜里的歌声会把路人引向哪里"),
    "keine": ("human-village", None, "把居民口述的村中旧事与记载校对，记清每件事有哪些人在场"),
    "reisen": ("eientei", "bamboo", "巡视竹林来路，辨别异常波动和普通响动，把仍有疑点的方向报给永琳"),
    "mokou": ("bamboo", "human-village", "在竹林里找到走失的人，带他们循仍能辨认的路标回人里"),

    # 花映塚
    "lunasa": ("prismriver-house", "mansion-lakeshore", "找一处听得清弦乐余音的地方，试出合奏开场该留多长静拍"),
    "merlin": ("prismriver-house", "mansion-lakeshore", "练熟一段由她领起的号音，让远处的姐妹听见后跟上合奏"),
    "lyrica": ("prismriver-house", "mansion-lakeshore", "用键盘重排两段旋律的衔接，试着让两位姐姐的声部都能听清"),
    "aya": ("human-village", "youkai-mountain", "追查异变目击者原来站的位置，访到两名独立见证人才写稿"),
    "medicine": ("forest", None, "比较有毒花草在林地留下的气息，找出可以让小动物绕行的警戒范围"),
    "yuuka": ("forest", "human-village", "看看林缘和人里附近的花哪些在同一时辰开放，找出花期变动最早出现的一处"),
    "eiki": ("higan", None, "听完亡灵与证人的陈述，分清该由当事人负责的选择和他从旁听来的事"),

    # 风神录
    "shizuha": ("youkai-mountain", "forest", "查看山间向阳处与背阴处的叶色，找出今年最先泛红的几棵树"),
    "minoriko": ("youkai-mountain", "human-village", "问过收成再挑几样当季供物，看看这季还有哪些作物该等几日才收"),
    "hina": ("youkai-mountain", "forest", "沿行人常走的山麓小路收拢厄运，把它带到少有人经过的地方"),
    "nitori": ("youkai-mountain", "moriya-shrine", "实测山溪水位升降，弄清哪一段能给河童新做的水轮提供稳定水流"),
    "momiji": ("youkai-mountain", "moriya-shrine", "从高处确认进山来客的方向，把有异样的一段巡线报给同伴"),
    "kanako": ("moriya-shrine", "youkai-mountain", "听取山上各方对神社来往的要求，商定能吸引参拜者的办法"),
    "suwako": ("moriya-shrine", "youkai-mountain", "看看新社址下方的土石和水脉，照看可能受修路影响的小社旧址"),

    # 地灵殿
    "suika": ("shrine", "human-village", "替散在各处的熟人定一个碰面的时辰，凑齐一桌能喝酒说话的人"),
    "kisume": ("old-hell", "old-capital", "试着从井桶里放下一点鬼火，看看能照到入洞者脚边哪一级石沿"),
    "yamame": ("old-capital", "old-hell", "寻找洞里有余风的落脚处，免得抱病的旧都居民走进最闷的岔洞"),
    "parsee": ("old-hell", "old-capital", "留意过桥的谈话中哪一句最招人嫉妒，再弄清争执双方究竟在介意什么"),
    "yuugi": ("old-capital", "old-hell", "召集几位愿意守规矩的旧都居民，找一处宽敞地比试怪力"),
    "satori": ("chireiden", "blazing-hell-ruins", "比对宠物传来的焦躁与地底热源的变化，弄清哪条通道令它们不安"),
    "rin": ("blazing-hell-ruins", "chireiden", "查过怨灵出没的岔路，把跟错方向的灵引回熟悉的通道"),
    "utsuho": ("blazing-hell-ruins", "chireiden", "核对旧地狱炉火的强弱，弄清哪股异常热气曾窜出平时的范围"),
    "koishi": ("chireiden", "old-capital", "循一阵熟悉的声音出门，看看自己会在什么地方被人无意中叫住"),

    # 星莲船
    "nazrin": ("myouren-temple", "human-village", "用找失物的本领循迹，把供桌上少掉的物件交还给寅丸星"),
    "kogasa": ("myouren-temple", "human-village", "选一个不挡路的转角躲起来，试试能不能吓到早已认得她的人"),
    "ichirin": ("myouren-temple", "human-village", "和云山核对进门通道的宽窄，挑个两人都能接待香客的位置"),
    "unzan": ("myouren-temple", "human-village", "练习把云身缩到能让出整条过道，等一轮点头后再变回原样"),
    "murasa": ("myouren-temple", "mansion-lakeshore", "估一估雾重时从岸上最早能看见哪里，给以后靠岸留个辨得出的记号"),
    "shou": ("myouren-temple", "human-village", "清点寺院供物，把没有写明施主的财物问清来路再入账"),
    "byakuren": ("myouren-temple", "human-village", "听完人类信众与妖怪来客的顾虑，商量下一次两边都能参加的讲法"),
    "nue": ("myouren-temple", "human-village", "听人们形容同一个难辨真形的东西，看看他们各自会把它认成什么"),

    # 神灵庙
    "kyouko": ("myouren-temple", "temple-cemetery", "用山彦的回声试一遍晨间诵读，查清哪句问候能从寺门传到墓地边"),
    "yoshika": ("yumedono", "temple-cemetery", "记住该拦下的来者和守门位置，免得被路边可吃的东西引离庙门"),
    "seiga": ("yumedono", "temple-cemetery", "查看庙外的墙与门径，另给芳香指一条走门也能到的看守路线"),
    "tojiko": ("yumedono", "temple-cemetery", "看看庙外雷光在何处最显眼，给同伴约定一处远远就能看见的示警点"),
    "futo": ("yumedono", "temple-cemetery", "用风水重看庙门前的朝向，找出摆放器物而不碍过路人的位置"),
    "miko": ("yumedono", "temple-cemetery", "同时听取几位来访者各自所求，从中分清他们是否在争同一件事"),
    "mamizou": ("myouren-temple", "human-village", "换个容貌到人多处听几桩见闻，看看人们能否认出问话的是同一位客人"),

    # 辉针城
    "wakasagihime": ("misty-lake", "mansion-lakeshore", "从水下认出几处湖岸记号，看看起雾以后还能不能循原路回来"),
    "sekibanki": ("human-village", "forest", "找一个不会惊动邻居的地点，练习让离体的头从另一条路回来"),
    "kagerou": ("bamboo", "forest", "提前认好满月之夜能独自走动的林路，标出必须避开人群的岔口"),
    "benben": ("forest", "kourindou", "去询问老琵琶怎样换弦，再与八桥试出既能领奏又不盖过筝音的一段"),
    "yatsuhashi": ("forest", "kourindou", "把筝上弹散的一段重排指法，带去和弁弁的琵琶合一遍"),
    "seija": ("shining-needle-castle", None, "挑一个支持者未曾料到的方向布置退路，借颠倒的方位试出谁还跟得上"),
    "shinmyoumaru": ("shining-needle-castle", None, "清点小槌先前的使用结果，找出哪里仍需要为小个子的来客留一条路"),
    "raiko": ("forest", "kourindou", "找几样能跟鼓点共振的器物，试出一段能让它们一起起动的节奏"),

    # 绀珠传
    "seiran": ("youkai-mountain", "moriya-shrine", "比对地面调查路线与来时约定的方位，找到一处能等到同队消息的会合点"),
    "ringo": ("youkai-mountain", "moriya-shrine", "整理收到的地面情报，约清兰在能坐下吃团子的地方交换各自的发现"),
    "doremy": ("dream-world", None, "辨别重复出现的同一个梦是否由多人带来，在梦中留一处醒得来的出口"),
    "sagume": ("lunar-capital", None, "在开口之前用字条复核每条事态报告，免得自己的言语贸然扭转局势"),
    "clownpiece": ("hell", None, "找一处与旁人隔开的空场练习举起火炬，避免同伴误入火光"),
    "junko": ("dream-world", None, "循梦境交界查找月之民的动静，辨明哪几处踪迹值得继续查探"),
    "hecatia": ("hell", None, "汇总三个去处传来的消息，决定何时亲自在地狱见前来求助的人"),

    # 天空璋
    "larva": ("forest", "mansion-lakeshore", "循仍有暖意的一段林缘撒些鳞粉，查看近处草木是否也显出盛夏的样子"),
    "nemuno": ("youkai-mountain", "yamanba-sanctuary", "重新辨认圣域边缘的界标，把走错山路的来客领到可以通行的一侧"),
    "aunn": ("shrine", "myouren-temple", "循神佛气息巡视门前，把被行人碰歪的供物摆回原处"),
    "narumi": ("forest", None, "查看林中几株在异样雪势里仍活动的草木，试着用魔法照看最弱的一株"),
    "satono": ("forest", "shrine", "和舞核对练习顺序，找一位愿意配合的对象观察背后舞蹈引出的精神变化"),
    "mai": ("forest", "shrine", "和里乃交换练习对象，察看由背后舞步引出的生命力量能持续多久"),
    "okina": ("hakurei-barrier", "forest", "找一处能在背面开门的地物，借门内外的景象比对季候的错位"),

    # 鬼形兽
    "eika": ("sanzu-river", None, "在岸边挑形状合适的石头，完成一座涨潮前也不会轻易倒下的石堆"),
    "urumi": ("sanzu-river", None, "把碍事的重石移到水边，试试改变重量后能否用它固定岸上的物件"),
    "kutaka": ("hell-checkpoint", "hell", "对照地狱关口的来访登记，弄清哪位过客仍缺通行所需的交代"),
    "yachie": ("animal-realm", "primate-garden", "分别听取鬼杰组眼线的报告，查清对手是否调动了灵长园外的力量"),
    "mayumi": ("primate-garden", "animal-realm", "检查执勤埴轮的位置和答号，确保换岗后仍有人认得来往灵长园的队伍"),
    "keiki": ("primate-garden", "animal-realm", "察看几尊偶像在搬动后的损耗，决定下一批先为哪个岗位制备替补"),
    "saki": ("animal-realm", "primate-garden", "亲自跑完劲牙组探明的新路线，比较从外围赶到灵长园需要的时间"),

    # 虹龙洞
    "mike": ("youkai-mountain", "human-village", "试着把招财猫的招徕用在约好的摊位上，记录求客流的卖家今天接待了多少人"),
    "takane": ("youkai-mountain", "forest", "借山林的气息辨认几段隐蔽路径，挑一条方便山童搬运交易货品的路"),
    "sannyo": ("youkai-mountain", "rainbow-dragon-cave", "观察自己吹出的烟在高地怎样散开，把烟雾会飘向过路人的位置让出来"),
    "misumaru": ("rainbow-dragon-cave", "youkai-mountain", "查看新出土的龙珠适不适合做勾玉，选一颗留样和旧作对照"),
    "megumu": ("youkai-mountain", "rainbow-dragon-cave", "对照星象和采掘人员的报告，定下下一批龙珠出洞交接的时辰"),
    "tsukasa": ("youkai-mountain", "rainbow-dragon-cave", "听完采掘方的两种说辞，找出哪一方正为盟约松动而着急"),
    "chimata": ("youkai-mountain", "human-village", "观察一次货品易手的经过，分清哪件物品已借市场交易消去原有归属"),
    "momoyo": ("rainbow-dragon-cave", "youkai-mountain", "沿新挖出的洞壁挑出还能开采的石层，把不能继续掘进的段落指给同伴"),

    # 兽王园
    "biten": ("yamanba-sanctuary", "youkai-mountain", "问常在山麓出入的猴群哪里来了新客，看看圣地周围有无陌生动物活动"),
    "enoko": ("youkai-mountain", "forest", "查看哪几处陷阱已有误触，重新给山路熟客留出安全经过的路线"),
    "chiyari": ("old-hell", "blazing-hell-ruins", "到旧地狱热源附近辨别火光的变化，选一处能独自试验自己力量的地方"),
    "hisami": ("hell-checkpoint", "hell", "记清每位愿随她走的来客要去哪里，决定怎样把他们领进地狱"),
    "zanmu": ("hell", "hell-checkpoint", "听取先后到达的各方使者陈述，弄清他们对无主之地的争执从哪一步开始"),

    # 锦上京
    "ubame": ("yamanba-sanctuary", "youkai-mountain", "沿山脚认出圣域防护中最早松动的一段，看看破口会不会继续扩大"),
    "chimi": ("youkai-mountain", "yamanba-sanctuary", "察看山与水相接处的气息，追查哪一道气流把山麓异状带至别处"),
    "nareko": ("triangular-mountain", "youkai-mountain", "在山路岔口核对可通行的时段，拦下试图闯过未开放道路的来客"),
    "yuiman": ("kinjou-capital", "yamanba-sanctuary", "认清能辨回锦上之京的路标，查清哪些山麓来者知道这处地方"),
    "toyohime": ("triangular-mountain", None, "从三角山查看山与海的联系，记下下次要在何处确认两端是否相通"),
    "ariya": ("kinjou-capital", "yamanba-sanctuary", "比较两处石纹在一段时日里的变化，选一处停住变化供往后来者查验"),
    "nina": ("triangular-mountain", "youkai-mountain", "找一段可以从两个方向望见的远景，试试从何处能看破自己现出的蜃景"),
}
