# 全项目独立审计一 · 玩家知道什么，世界实际做了什么

**建议先修两个会误导玩家的读取问题，再决定是否扩内容。** 非标准历法的会面时间在 GM 资料里算错；本地模型容量不足时，追问第四份案卷会在重试资料中找不到它。两处都在玩家已经做出了有效选择之后破坏反馈。NPC 投票、结案、回放的针对性回归通过。对玩法最有价值的下一项验证仍是玩家能否看见别人的实际行动并主动回应，尤其是默认建档与显式开启 NPC 计划复审的区别。

## 审计边界与可复核性

输入是 2026-11-24 的 HEAD `66474dc` 加当时工作区，含开局修复。对账基准为 `.runtime/full-audit-inventory-20261124-231848/paths.txt` 和同目录 `sha256.tsv`。693 个不同路径来自 `git ls-files -c -o --exclude-standard`，本报告、清单和临时探针均不属于冻结输入。逐路径证据、字节数、行数、散列和检查层级见[覆盖表](world-engine-full-audit-1-coverage.tsv)。前次三份玩法报告均计入分母。

| 核查深度 | 文件数 | 实际含义 |
| --- | ---: | --- |
| 全文结构检查 | 337 | 读完整文本。Python 遍历全 AST 的定义、调用、异常和断言；前端与脚本提取全文调用和定义，样式、模板、配置识别结构。这一级不能证明每一个函数的业务语义正确。 |
| 测试定义及断言结构检查 | 277 | 解读测试文件结构和用例名，统计断言、调用；未执行这些整文件。 |
| 局部调用或入口追读 | 17 | 在结构检查上再人工读特定逻辑，按 TSV 指出的入口核对调用、条件或界面。 |
| 入口追读及本机函数探针 | 3 | GM 精简、预约时间和选举校验使用真实函数、合成输入，不调用模型。 |
| 本轮运行整文件测试 | 7 | 两次本地 pytest 合计 39 项通过，具体命令见末节。 |
| 文档结构与主题检查／配置结构检查 | 36／4 | 文档核对章节、规范、验收与主题；配置核对键与值的结构，关键规则另行追读。 |
| 资产检查 | 8 | 两张 PNG 的头和尺寸、五个 SVG 的 XML 与节点、设计 ZIP 的 17 个成员和 CRC；未做视觉质量验收。 |
| 仅清单 | 4 | 三评汇总、其他两位的前次报告、此前独立项目审计均隔离正文，沿用冻结散列。自己的前次报告已计入文档。 |

范围包括后端 278 路径、前端 65、测试 284、脚本 15、文档目录 43，余下 8 项为 CI、仓库规则、依赖、README、更新日志和设计归档等。后端覆盖 domain 109、application 66、content 28、adapters 17、simulation 13、api 12、storage 11、rules 10、agents 3、迁移 7、顶层和运行时 2。适配层同时包含凭据、媒体、模型。未读取私人存档、环境凭据，未发付费请求。没有做逐行人工证明、完整浏览器试玩或 600 年复跑。没有复用其他评审结论。

## 可验证的发现

### 1 · P2 · GM 在自定义日长下给预约附上错误的本地日期与时刻

`backend/world_engine/domain/models.py:150` 允许正整数日长。`backend/world_engine/application/visibility.py:52` 的 meeting_local_time 以 86,400 秒换日。`backend/world_engine/application/conversation.py:918` 对当前玩家每个含 meeting 的邀请无条件填入这两个字段。同一游戏的预约草稿在该文件第 84 行按 day_seconds 换算，而 NPC 对话组装在 `application/actions.py:1774` 已显式保护非标准日长。

**本机复现。** 日长 3,600 秒，当前 tick 3,599，开会 tick 3,600。GM 辅助函数返回当天 01:00:00，草稿返回“世界第 2 日 00:00”。截止条件仍比对真实 tick。风险是玩家按错误的自然语言说明赴约；本轮证实错误数据进入调用路径，未向模型询问它最终采用哪个表述。

**修法与验收。** 统一传入存档历法，由同一换算生成绝对或相对时间。分别对日长 3,600、86,400 和日界前一秒发起邀约，核对草稿、GM 输入资料和双方当场对白的时间；正式到场回执的 tick 应完全一致。

### 2 · P2 · 小上下文精简按顺序丢弃玩家点名的案卷

`backend/world_engine/application/visibility.py:565` 投影角色自己的案卷；`backend/world_engine/application/conversation.py:128` 在调查问句精简时只留前三份，随后按保留内容筛引用 ID。第 1186 行从模型容量错误进入该路径。玩家可以有权知道答案，重试却不再有材料回答。

**本机复现。** 给真实精简函数四份案卷，输入“请核对案卷『卷宗4』的证据”。得到 case1、case2、case3，case4 的引用也消失。此探针验证输入转换，不证明真实模型必然编造答案。

**修法与验收。** 在已授权投影内先选明确点名的案卷及其直接证据，再按最近相关性填预算。两个同名卷需让玩家区分。测第四份、旧档、未点名、私有卷四种路径；题目点名的合法卷可以被引用，其他角色的私有卷仍不可见。

### 3 · P2（规模条件） · 机构投票校验的代价随选民与选票数量相乘

`backend/world_engine/domain/leadership_ballots.py:65` 接受至多十万选民。第 79 行对每份已投票在完整元组中线性查找，第 239 行附近每次投票后又走完整模型验证。旧票的数量会影响新票耗时，回放或读入大机构时也可能遇到校验开销。

**本机测量。** 合成单个合法模型，用相同选民排序、等量弃权票、五次中位数。250、500、1,000 人的完整 model_validate 分别为 1.63、6.86、28.96 毫秒。此处只测模型校验；未测试十万人、整体吞吐或实际存档，也没有证据说明常见小机构已卡顿。

**修法与验收。** 利用同一次验证已建立的选民集合，按 UUID 集合核对资格，保留有序元组与序列化表示。重跑上面三档及选举领域测试，复核重复票、非选民票、终态伪造仍被拒绝，检查差异随规模趋缓。优先级低于前两项，适合与下一次政治模块改动合做。

### 4 · P2 产品验证 · 默认建档与展示 NPC 自主性的实验不是同一条件

`frontend/src/components/CreateWorldDialog.tsx:50` 的自主模式初值为 off，界面第 147 行描述旅行与采购预置的三个节点。`backend/world_engine/content/npc_autonomy_opening.py:133` 启用该预置；`backend/world_engine/domain/npc_plan_review.py:118` 说明排程默认一次，持续复审须声明间隔和次数。普通生理、作息和世界规则仍然运行。把“后台有计划执行器”直接推成“新玩家能看懂 NPC 的自主打算”会使试玩选错样本。

**试做与验收。** 默认世界和显式选择旅行与采购的世界各取同种子，同走到复审时点。记录实际提交的行为、玩家当时能看到什么、是否据此改变计划、等候模型的时间。只有已发生且可见的动作才可加一句场景变化。保留关闭开关和配置成本知情，避免为了试验让所有存档自动发模型请求。

### 5 · P3 发布风险 · CI 未直接覆盖浏览器里的状态切换

`.github/workflows/check-engine.yml:48` 运行组件测试后构建；`frontend/src/components/PlaySceneCard.test.mjs:50` 用静态渲染核对文字。CI 的这些命令不能直接验证窄屏输入焦点、点击、刷新后恢复同一请求等浏览器行为。本地既有验收提供过浏览器证据，历史证据的日期与本次审计分开。

**修法与验收。** 在现有隔离测试后端上加一条无外部模型的浏览器冒烟：窄屏观察→输入原话→请求进行时刷新→核对仅一张回执→键盘返回输入。CI 失败时保留首次出错步骤。无需把每一条规则回归都改成 UI 测试。

## R01–R18 的入口、收益和证据缺口

下表用 `B/` 指 `backend/world_engine/`，`F/` 指 `frontend/src/`。行号均按冻结的工作区。规则能力和玩法是否耐玩分别判断。

| 需求 | 锚点 | 当前判断及下次验收 |
| --- | --- | --- |
| R01 多背景 | B/content/catalog.py:134；B/application/world_pack_import.py:76 | 建档列出背景可用版本、人口版本和开局；固定身份包会拒绝改配身份。需跨背景验证相同自由意图获得符合各自背景的结果。 |
| R02 沟通 | F/pages/PlayPage.tsx:524；B/application/conversation.py:1186 | 确定性观察与自由提问有不同成本。先修点名案卷、历法会面，再做窄模型对照。 |
| R03 人物创作 | F/pages/EditorPage.tsx:119；B/domain/npc_plan_review.py:106 | 编辑视图按世界及分支读取，复审需合格的活动人物及自有目标。测试作者设定的目标怎样被玩家发现。 |
| R04 人物自主 | B/agents/policy.py:70；B/simulation/npc_plan_execution.py:120 | 背景决策与真实规则转换都有入口，现有用例实跑包含投票、同行时刻的不同旅程。需要默认档观察频率。 |
| R05 认知 | B/agents/knowledge.py:42；B/application/visibility.py:565 | 可见实体及私人案卷有不同投影；精简重试又会改变实际可用事实。用持有者与旁观者对照。 |
| R06 关系与日常 | B/domain/relationship_invitations.py:134；B/domain/occupation_settlement.py:15 | 邀约和职业决定都有正式规则。用拒绝、错过及后续重约验证日历一致。 |
| R07 调查 | B/domain/investigation_cases.py:279、351 | 来源匹配与关案后果可核查，本轮规则组覆盖替代路线与误指认；自由输入仍有发现 2。 |
| R08 经营与生存 | B/rules/market_pricing.py:15；B/application/long_action_readback.py:144 | 定期调价和工作购买可负担回执属不同链条；建议同一生活档验证一次涨价如何改变采购与出勤。 |
| R09 政治 | B/domain/leadership_ballots.py:72、218 | 固定选民和治理修订保护程序；本轮运行 7 项选举领域测试。玩家是否记得新当选者还需另测。 |
| R10 战斗与成长 | B/domain/combat_truce.py:49；B/domain/cultivation_breakthrough.py:156 | 停战提议有受众，突破有资源和结果计划。可用「接受停战以后还愿不愿再见」与「失败后的选择」测体验。 |
| R11 长期演化 | B/rules/engine.py:372、581；scripts/audit-capacity-interval.py:108 | 有排程、年度人口与区间检查工具。既有区间口径见 docs/development/acceptance-matrix.md:3，本次没有重跑长期作业；应比较每段低谷及回升。 |
| R12 历史与作者干预 | F/pages/HistoryPage.tsx:43、132；B/storage/npc_plan_replay.py:40 | 恢复先预览，NPC 重放校验来源批次；本轮跑了祖先检查点与边界回放。玩家能否找到一个决定发生前的分叉点仍待测。 |
| R13 模型后台 | B/adapters/models/gateway.py:97；F/pages/OperationsPage.tsx:205 | 真实生成与管理页有独立入口。此次只检查逻辑，未比较供应商延迟、实际路由或费用。 |
| R14 速度深度 | B/adapters/models/routing.py:20；B/application/conversation.py:1184 | 模型路由可按职责与速度／深度选择并过滤策略；容量错误另外走压缩路径。验证配置相同而回答依据不同的情况。 |
| R15 账号预留 | B/api/security.py:46；B/application/media_authorization.py:12 | 身份适配器与本地会话入口分开，媒体又核对角色、分支及来源。本轮跑现有安全接口用例；没有验证公网多人部署。 |
| R16 媒体与扩展 | B/application/media_contracts.py:133；B/application/media_jobs.py:154；F/components/HistoryMediaControl.tsx:22 | 媒体协议、任务工作器、历史页有连接；本轮运行协议用例，没有实际生成语音或装第三方插件。 |
| R17 内容兼容 | B/api/world_packs.py:22、38；B/application/world_pack_import.py:33 | 导入先控制 ZIP 大小、进行预览，复审排程在新世界绑定。下次用原版与派生包验证同一旧人物的身份和读档。 |
| R18 可操作性与发布 | .github/workflows/check-engine.yml:48；scripts/check-engine.ps1:24；backend/migrations/versions/e0647dcbe392_direct_movement_index.py:12 | 后端矩阵拆分，前端测试和构建独立；历史直达回执迁移本次实跑。浏览器状态切换及全新机器安装均非本轮实测。 |

## 六种内容入口与六类游玩

三个内置背景走 catalog 的建档入口，另外三个是可携带世界包，不要求每个包都具有内置背景的完整人口规模。

| 内容 | 核对入口 | 最值得做的跨系统对照 |
| --- | --- | --- |
| 现代城市 | B/application/modern_opening_goal.py:6；B/content/routine_openings.py:29 | 同一人赶班或赴约，次日回到站台区分确定事实和谁愿作证。 |
| 低魔封建 | B/content/waypoint_openings.py:185；B/content/catalog.py:150 | 林道继续与停下核对后的抵达时间及通行后果，优先观察错过后的生活。 |
| 长寿修仙 | B/domain/cultivation_breakthrough.py:110；B/content/catalog.py:155 | 在同样有材料的存档可学习、访人或突破。测玩家是否能说明放弃突破的理由。 |
| 幻想乡异变年代 | B/content/gensokyo_world.py:31；B/content/gensokyo_mist_reports.py:30 | 现场证据、转述、回避异变三路对照，拒绝参与者也有次日可做的事情。 |
| 回灯之约 | B/content/gensokyo_scenario.py:16；scripts/build-gensokyo-pack.py:1 | 独立包身份和介入点应独立验收；对照旧档与同系列新包，确认私有关系不串档。 |
| 远岬站 | B/content/distant_station_story.py:140、157 | 避护区有无应急电源会改变脉冲后的照明；用缺席对照已验收电池，现场与消息需分开。 |

人物关系、日常生活、探索调查、经营生存、势力政治、战斗成长见 PRD 第 72 行，分别由 R06、R06/R08、R07、R08、R09、R10 涵盖。六类都应保留「我另有安排」这条路。最少做三个横向存档：站务员同时面对日常与调查，机构成员面对人情与投票，受伤修行者面对恢复与突破。衡量能否事前理解取舍、次日认出自己留下的结果。六背景乘六玩法的 36 种组合本次没有逐一跑通。

## 新实测与限制

在仓库根用 `.runtime/venv313/Scripts/python.exe -B -m pytest -p no:cacheprovider -q` 分两次运行。第一组为 `tests/simulation/test_npc_plan_execution.py`、`tests/storage/test_checkpoint_replay_range.py`、`tests/rules/test_investigation_cases.py`，使用 `--basetemp=.runtime/audit-one/test-state`，19 passed，59.55 秒。第二组为 `tests/domain/test_leadership_ballots.py`、`tests/api/test_media_contracts.py`、`tests/api/test_security.py`、`tests/storage/test_migrations.py`，使用 `--basetemp=.runtime/audit-one/test-policy`，20 passed，1.23 秒，有一条 Starlette 测试客户端弃用警告。

三个独立函数探针位于本机 `.runtime/audit-one/{compact.py,probe_meeting.py,ballot.py}`。试验只使用合成资料。覆盖表记录源文件的最终读深，一个路径只算一次；测试文件通过并不意味着同名产品模块所有分支都已执行。前端只做源文本结构与选定入口核查，本轮未完成独立 TypeScript AST 验证，未启动外部模型、浏览器、远程发布或实际多人会话。全项目测试与其他评审的发现未并入本报告。
