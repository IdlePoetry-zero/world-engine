# 开源参考与设计决策

研究范围包括角色扮演前端、生成式人物、社会模拟和传统文字游戏引擎。以下 12 个独立项目用于设计参考，不把分支、插件或同一项目的两个版本重复计数。材料为官方仓库 README、官方文档与部分关键代码；未进行逐项部署或性能对比，因此不使用星数或宣传规模作为质量结论。

## 项目参考

| 项目与主要来源 | 已确认的设计方向 | 本产品采用的思路 | 不直接继承的部分 |
| --- | --- | --- | --- |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern) | LLM 前端，角色和世界书生态 | 角色编辑、设定检索、上下文配置 | 不将提示词世界书当成权威数据库 |
| [RisuAI](https://github.com/kwaroran/Risuai/blob/main/README.md) | 多 API、群聊、Lorebook、插件、记忆管理 | 人物表现、可创作性、模型与内容解耦 | 不将单个聊天会话当成完整世界 |
| [Agnaistic](https://github.com/agnaistic/agnai/blob/dev/README.md) | 多用户多角色聊天、人物格式、记忆书 | 身份、角色与会话独立，兼容导入 | 多人聊天不能代替世界并发裁定 |
| [KoboldAI Client](https://github.com/KoboldAI/KoboldAI-Client/blob/main/README.md) | 小说与文字冒险模式、记忆、作者注释、存取档 | 自然文字操作、表达与创作模式 | 旧客户端官方标记不维护，不作为新底座 |
| [Generative Agents](https://github.com/joonspk-research/generative_agents/blob/main/README.md) | 人物模拟、历史初始化、运行与回放 | 记忆反思规划和人物连续生活 | 研究演示不等于完整游戏或成本保证 |
| [AI Town](https://github.com/a16z-infra/ai-town/blob/main/README.md) | 可定制小镇、共享状态、事务与模拟 | 模型与状态提交分离，统一共享世界 | 不强制采用其 Convex 与图形交互栈 |
| [Concordia](https://github.com/google-deepmind/concordia/blob/main/README.md) | 生成式实体组件、GM 角色、环境裁定 | 自然语言意图和世界结果分离 | 关键数值由本产品规则系统约束 |
| [AgentSociety](https://github.com/tsinghua-fib-lab/agentsociety/blob/main/README.md) | 社会模拟、模块化环境、实验回放；当前区分 v2 与 legacy | 宏观社会模块、实验干预、轨迹审计 | 不照搬研究平台部署与规模承诺 |
| [OASIS](https://github.com/camel-ai/oasis/blob/main/README.md) | 社交媒体代理、信息传播、行动空间 | 消息传播渠道与群体认知 | 社交媒体代理数量不等于完整 RPG 人物规模 |
| [Evennia](https://github.com/evennia/evennia/blob/main/README.md) | 题材中立的持久文字游戏框架 | 地点对象、动作、周期系统与扩展 | 本产品需额外实现 LLM 认知和长周期模拟 |
| [LIGHT](https://github.com/facebookresearch/LIGHT/blob/main/README.md) | 世界图、图事件、代理接口、文本情境对话 | 对象与行动有具体可执行世界基础 | 仓库已归档，参考架构而非默认维护底座 |
| [ink](https://github.com/inkle/ink/blob/master/README.md) | 互动叙事脚本与运行时 | 作者事件、条件剧情、表达与逻辑分离 | 不用分支脚本替代自主世界模拟 |

## 关键依据

SillyTavern 将 World Info 定义为动态插入提示的背景信息机制，并说明输出仍依赖模型利用上下文的能力。因此采用独立世界账本，世界书只作为描述与检索层。[World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)

Generative Agents 提出观察、计划与反思协作，检索实现结合近期性、重要性和相关性。本产品在此之前增加角色知情范围过滤，并对承诺、债务、身份和伤势采用结构化记录。[论文](https://arxiv.org/abs/2304.03442)、[检索实现](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/cognitive_modules/retrieve.py)

Concordia 环境将观察、调度、裁定与终止组织为引擎职责，并区分顺序与同时行动。本产品采用统一调度和提案收集，再通过资源冲突与事务规则提交。[环境引擎](https://github.com/google-deepmind/concordia/blob/main/concordia/environment/README.md)

LIGHT 图结构描述地点连接、节点包含、人物死亡和事件操作；Evennia 将可交互对象与周期系统区分。本产品将实体状态、系统状态和事件处理明确拆开。[LIGHT 图](https://github.com/facebookresearch/LIGHT/blob/main/light/graph/README.md)、[Evennia 对象](https://www.evennia.com/docs/latest/Components/Objects.html)、[Evennia Scripts](https://www.evennia.com/docs/latest/Components/Scripts.html)

## 本产品独立设计的内容

600 年连续模拟标准、两轴质量设置、受限临时裁定、分支隔离、历史账本、身份权限、模型计量和发布验收属于本产品的设计决策，不能据上述参考就宣称已得到实证保证。

| 决策 | 选择理由 | 代价或替代方案 |
| --- | --- | --- |
| 独立内核 | 避免前端生态与研究原型的领域假设限制 | 开发量更大，必须完善规则和测试 |
| 类型化事件加当前状态 | 兼顾可追溯与查询效率 | 维护 reducer、迁移与投影一致性 |
| 多尺度模拟 | 支持秒到百年的跨度 | 需验证聚合误差和精度切换 |
| 人物信念独立 | 支持秘密、误会、欺骗和调查 | 检索权限与语义评价更复杂 |
| 本地模块化单体 | 易部署，减少服务运维负担 | 后续多人扩展需实际数据库与协调验证 |
| 模型能力注册 | 不把 API 兼容等同能力相同 | 增加适配器探测与契约测试 |
| 插件命令边界 | 扩展可替换且不破坏权威状态 | 插件不能任意改库，需要明确协议 |

## 代码复用与版本

长行动批处理复核了 AI Town 的 `runStep` 与 `applyEngineUpdate`。它先执行有界步骤，再以预期版本提交时钟、状态及已处理输入。本项目保留这个事务边界，在 SQLite 同一写事务中提交事件、投影和作业进度。年度人口事件和年龄组规则独立实现。SQLite 的 WAL 检查点负责把已提交页转入主数据库，与游戏历史检查点分别维护。[AI Town 实现](https://github.com/a16z-infra/ai-town/blob/main/convex/engine/abstractGame.ts)、[SQLite WAL 文档](https://sqlite.org/wal.html)

探索可见性参考 Evennia 对 `view`、`search` 和 `traverse` 权限的独立检查。万象用当前角色持有且生效的观察记录开放潜藏目标与对应路线，再检查出行能力和地点权限。旧版已记录的全局揭露仍按原事件语义回放。实现自行编写。[Evennia Locks](https://www.evennia.com/docs/latest/Components/Locks.html)

本轮没有复制这些项目的源码或素材，也不新增其运行依赖。若后续决定复用，必须固定具体 commit、文件和对应许可证，记录代码、模型权重、数据和图像素材各自的授权；仓库总体许可证不能替代逐项核对。AGPL、GPL 等要求应在实际复用方式确定后审查，不在当前设计中假定可随意嵌入。

研究结论基于 2026 年 11 月 15 日读取到的资料与前序调研。实现前对拟复用或作为依赖的项目重新固定版本；设计参考项目不需要全部安装。

主动继任的表决另参考 Freeciv 服务端的资格校验与提前决出逻辑。世界引擎的方案固定提案时的角色选民名单，弃权仍计入门槛分母，阈值采用精确十进制，治理版本变更使提案失效。项目采用独立实现，无 Freeciv 代码复用。Freeciv 按仓库标示采用 GPL-2.0。 [Freeciv 表决实现](https://github.com/freeciv/freeciv/blob/main/server/voting.c) · [Freeciv 许可证](https://github.com/freeciv/freeciv/blob/main/COPYING)

外交条款参考 Freeciv 中两方同意后才实施、实施前复核资格的设计。通行协定的世界规则以发起方的提案记录第一方同意，另一方明确批准才生效。该约定只授予一条已登记路线的专用通行许可，可撤销，且随世界时间失效。项目独立实现。 [Freeciv 条约审批实现](https://github.com/freeciv/freeciv/blob/main/server/diplhand.c)

长行动的暂停、完成和恢复参考 Cataclysm: DDA 的活动生命周期。上游分别保存可恢复活动与完成后的状态，并为长期活动单独配置自理行为。本项目用版本化作业和已提交边界实现这一生命周期，显示原截止、执行进度和暂停原因，恢复时保留原授权。未复用上游代码或内容，上游许可证为 CC BY-SA 3.0。 [活动合同](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/doc/PLAYER_ACTIVITY.md) · [活动存档实现](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/src/savegame_json.cpp) · [许可证](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/LICENSE.txt)

## 角色卡互操作

角色卡 JSON 导入识别 V1 平面字段和 V2 的版本信封。项目提交链只接受可审阅的姓名、描述和简短性格，预览列出未映射的非空字段。未复用上游实现代码。 [Character Card V1 规范](https://github.com/malfoyslastname/character-card-spec-v2/blob/main/spec_v1.md) · [Character Card V2 规范](https://github.com/malfoyslastname/character-card-spec-v2/blob/main/spec_v2.md)

## Windows 本机语音

本机媒体插件采用 Windows PowerShell 和 .NET Framework `System.Speech`，调用系统已安装的语音合成引擎。插件接收获准的纯文本，暂存 WAV，返回收据和受大小约束的字节。语音引擎和声音随 Windows 或用户安装，项目不分发这些组件，也没有从上游复制示例源码。[WAV 输出接口](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.setoutputtowavefile?view=netframework-4.8.1) · [已安装声音接口](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.getinstalledvoices?view=netframework-4.8.1)

## 在途库存

ERPNext 以发货与收货两笔关联记录表示跨仓运输。万象采用类似的在途账本边界，发运时占用真实库存批次并移入运输保管账户，按模拟路线到达后才增加目的地的可用库存。损耗、失效和中断需要有可复查的库存及来源记录。这一设计仅参考工作流程，程序和事件契约由本项目独立实现。[ERPNext 在途流程](https://docs.frappe.io/erpnext/how-to-manage-material-transfers-and-goods-in-transit) · [库存条目实现](https://github.com/frappe/erpnext/blob/develop/erpnext/stock/doctype/stock_entry/stock_entry.py) · [ERPNext 项目许可证](https://github.com/frappe/erpnext/blob/develop/license.txt)

## 承诺状态

参考 django-fsm-2 集中定义来源状态、目标状态和前置条件的做法。万象把立约、归还、解除和逾期核验实现为纯领域状态转换。归还还须比对同一实际转移的出入账记录、双方账户和时点。项目仅借鉴状态管理结构，未引用实现或增加依赖。[django-fsm-2 前置条件](https://github.com/django-commons/django-fsm-2#preconditions-conditions) · [MIT 许可证](https://raw.githubusercontent.com/django-commons/django-fsm-2/main/LICENSE)

## 库存与市场报价

ERPNext 保持基础价格记录与按条件生效的报价规则独立。世界引擎借用这一分离方式，注册基准价保留为稳定输入，实际报价由已生效的库存批次、目标库存与限幅计算，价格变化不产生货物或货币。规则自行实现，未复制 ERPNext 源码，也未引入其运行依赖。[ERPNext 定价规则](https://docs.frappe.io/erpnext/pricing-rule#does-a-pricing-rule-change-the-item-price-record) · [ERPNext 项目许可证声明](https://github.com/frappe/erpnext/blob/develop/package.json)
### 家庭与职业状态

- ClanGen 的亲缘实现分别存储血缘和收养关系，再从父母关系推导旁系。新家庭模块采用带生效时间和来源的直接亲缘边。兄弟姐妹等关系按已知的直接边推导，角色姓名不构成亲缘证据。参考代码标有 MPL 2.0，仓库另列素材许可。本项目采用独立实现。 [ClanGen 亲缘实现](https://github.com/ClanGenOfficial/clangen/blob/development/scripts/cat_relations/inheritance.py) · [许可](https://github.com/ClanGenOfficial/clangen/blob/development/LICENSE.md)
- FreeCol 把人员的工作安排与长期形成的熟练度分开。世界引擎的职业合同会记录任职单位、职位及适用技能，职业变化经事件确认，熟练度继续用既有技能模型。参考仓库载有 GPL v2 文本。本项目采用独立实现。 [FreeCol 手册](https://www.freecol.org/docs/FreeCol.html) · [许可](https://github.com/FreeCol/freecol/blob/master/LICENSE)
- Cataclysm-DDA 根据当前安全和活动决定营地居民是否投入工作。世界引擎拟在已有生活需求与工作资格检查后结算职业活动，急迫需求和转场可打断工作。主体许可为 CC BY-SA 3.0。本项目采用独立实现。 [Cataclysm-DDA NPC 行为](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/src/npcmove.cpp) · [许可](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/LICENSE.txt)

- NPC 职业活动的恢复设计参考 Evennia 将持久系统记录、周期调度与实体对象分离的方式。世界引擎将班次身份和任职版本绑定到世界时钟，供恢复和回放核对。即使任务被打断，已有班次的出勤和工资仍需通过规则证明。采用独立实现。Evennia 使用 BSD 3-Clause 许可。 [Evennia Scripts](https://www.evennia.com/docs/latest/Components/Scripts.html) · [Evennia 许可](https://github.com/evennia/evennia/blob/main/LICENSE.txt)

- 私人邀约参考 Evennia 把角色控制权和对象属性读取权分别授权的设计。万象在提交时核对角色授权，并在投影时按参与者限制邀约状态和历史。邀约决定用独立可回放事件表达。项目采用独立实现，Evennia 使用 BSD 3-Clause 许可。 [Evennia Locks](https://www.evennia.com/docs/latest/Components/Locks.html) · [Evennia 许可](https://github.com/evennia/evennia/blob/main/LICENSE.txt)

- 定向告知关系沿用角色控制权与记录读取权的区分。角色主动告诉某人一项信息时，接收人获得一条自己的所闻记录，原对象的读取范围独立校验。模型只收当前角色有权读取的陈述。设计参考 Evennia 对每项访问类型单独校验的做法，代码自行实现。[Evennia Locks](https://www.evennia.com/docs/latest/Components/Locks.html)

- 现代地区职业资料参考 Cataclysm: Dark Days Ahead 将职业与初始技能作为内容数据声明的方式。万象自行实现地区权重与可核验技能，供开局和有配给支持的补员共用。组织招募继续按自己的章程审查候选人。借鉴数据建模思路，未复制代码或内容。[职业配置文档](https://docs.cataclysmdda.org/JSON/JSON_INFO.html#professions) · [原项目许可](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/LICENSE.txt)

- 遭遇谈判参考 OpenMW 对已选对话回应重新匹配角色与场景条件，再执行结果脚本的流程。万象以当前遭遇版本和对方明确回应决定是否写入停战事件。ink 的运行接口将选项及其标签交给宿主，万象同样把选择作为结构化数据传递。采用独立实现，参考仓库分别提供 GPL v3 与 MIT 许可。 [OpenMW 对话实现](https://github.com/OpenMW/openmw/blob/master/apps/openmw/mwdialogue/dialoguemanagerimp.cpp) · [OpenMW 许可](https://github.com/OpenMW/openmw/blob/master/LICENSE) · [ink 运行接口](https://github.com/inkle/ink/blob/master/Documentation/RunningYourInk.md) · [ink 许可](https://github.com/inkle/ink/blob/master/LICENSE.txt)

- 职业学徒设计查阅 FreeCol 的教学实现。它校验师徒双方的关联，按轮次累计时间，在另一个步骤复核完成条件。万象准备把已有具名居民的培训进度与入会分开，只有发生过训练和考核才记录资格，仍由原章程审核候选。采用独立实现，参考文件为 GPL v2 或更新版本。 [FreeCol 教学实现与许可](https://github.com/FreeCol/freecol/blob/master/src/net/sf/freecol/server/model/ServerBuilding.java) · [培训轮次接口](https://www.freecol.org/javadoc/net/sf/freecol/common/model/Specification.html#getNeededTurnsOfTraining(net.sf.freecol.common.model.UnitType,net.sf.freecol.common.model.UnitType))

- 长行动快进的时点顺序参考 SimPy 的同刻事件排序和 OMNeT++ 的未来事件集。引擎仍按自己的世界秒、业务阶段、优先级和稳定标识比较最近事件。合并无须停留的时间区间时，需同样结算休息、需要、工资、货物与事件，且同刻的抵达、机会截止和补给必须保留原先顺序。以同源存档核对逐次事件、账本和回放，并单独测量实际运行耗时。此处参考设计和官方文档，使用本项目实现。[SimPy 时间和调度](https://simpy.readthedocs.io/en/latest/topical_guides/time_and_scheduling.html) · [OMNeT++ 事件循环](https://doc.omnetpp.org/manual/sp/index)
- 路上休整的计时参考 SimPy 的抢占示例。流程中断时只扣去已执行工作，维修时间进入环境时钟，恢复后继续消耗剩余工作量。万象让停留推进世界时钟，以可回放的旅程延误事件保存实际余程，并同时顺延到达和途中风险的结算时刻。实现使用项目自有事件日志和租约。[SimPy 抢占示例](https://simpy.readthedocs.io/en/latest/examples/machine_shop.html) · [MIT 许可](https://pypi.org/project/simpy/)

- 例行工作以档内历法安排重复班次，参考 Evennia 根据游戏时钟设置重复事件的接口。暂停与自动补给的设计参考 Cataclysm: Dark Days Ahead 将重复、恢复和自动自理分别声明的活动契约。项目沿用自己的作业、规则与事件账本，未复用上游代码或内容。[Evennia 时间排程](https://www.evennia.com/docs/4.x/Howtos/Howto-Game-Time.html) · [BSD-3-Clause 许可](https://github.com/evennia/evennia) · [Cataclysm 活动文档](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/doc/PLAYER_ACTIVITY.md) · [CC BY-SA 3.0 许可](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/LICENSE.txt)

- 角色卡 PNG 兼容参考 SillyTavern 的发布版协议，读取 `tEXt` 中以 Base64 保存的 UTF-8 角色卡，存在 `ccv3` 时采用该条记录。PNG 签名、区块边界和 CRC 按 W3C 规范校验，预览通过现有角色卡字段映射和作者确认流程。SillyTavern 按 AGPL v3 发布，本项目采用独立实现。 [SillyTavern 角色卡读写](https://raw.githubusercontent.com/SillyTavern/SillyTavern/release/src/character-card-parser.js) · [SillyTavern 许可](https://github.com/SillyTavern/SillyTavern/blob/release/LICENSE) · [W3C PNG 规范](https://www.w3.org/TR/png-3/)

- 独立世界书兼容参考 SillyTavern 的条目格式和关键词检索。作者逐条审阅后可选择公开条目，仅公开内容可在字面关键词命中或常驻时进入 GM 问答背景。世界状态和行动结果仍由事件账本裁定。保留原始导入数据供作者备份。实现独立编写。 [World Info 文档](https://docs.sillytavern.app/usage/core-concepts/worldinfo/) · [发布版条目字段](https://raw.githubusercontent.com/SillyTavern/SillyTavern/release/public/scripts/world-info.js)

- 连续行程的路况日期参考 Evennia `TaskHandler` 对可恢复延迟任务的处理。万象继续用自己的世界时钟和已登记的解析事件，以稳定的旅程编号计算路线内时点。存档重放验证出发事件记录的算法版本及既定日期，工作器换租约也读取同一次出发记录。采用独立实现。[Evennia 持久任务源代码](https://www.evennia.com/docs/latest/_modules/evennia/utils/utils.html#delay) · [BSD-3-Clause 许可](https://github.com/evennia/evennia/blob/main/LICENSE.txt)

- 自动自理提交的状态校验对照 Python eventsourcing 的聚合版本和存储写入冲突检查。万象沿用自己的行锁、期望版本与事件日志，仅在锁内前置状态、具体动作和事件序列完全匹配时复用进程内的规则结果。该结果不进入持久化契约。上游代码未引入。[eventsourcing 并发检查](https://eventsourcing.readthedocs.io/en/v8.2.0/topics/infrastructure.html#optimistic-concurrency-control) · [BSD-3-Clause 许可](https://github.com/pyeventsourcing/eventsourcing/blob/main/LICENSE)

- 私人赠与设计查阅 FreeCol 的报价记录及正式接受流程。其服务端确认实际移交前复核参与者、来源所有权和可用库存。万象计划用独立的私密提议与同意记录，在实际接受时复核双方同地、余额和可转移的库存批次，并在同一事务提交双边账本。参考 GPL v2 或后续版本发布的上游资料，拟采用项目自有实现。[FreeCol 提议契约](https://www.freecol.org/javadoc/net/sf/freecol/common/model/DiplomaticTrade.html) · [服务端移交检查](https://github.com/FreeCol/freecol/blob/master/src/net/sf/freecol/server/model/ServerGame.java)

### 带负载作业的隔离对照

长途性能对照以 SQLite 在线备份产生一致副本，从同一副本派生两个独立作业环境。原进程继续持有原分支租约。对照只改变新写入的事件存放方式，逐窗口比较事件内容、序列、账本和状态摘要，并从同一检查点回放。SQLite 将交付的库与文档奉献给公有领域，项目继续通过现有 Python 接口使用 SQLite。 [SQLite 在线备份](https://www.sqlite.org/backup.html) · [SQLite 版权声明](https://sqlite.org/copyright.html)

另查阅 Marten 对内联投影和写入缓存的区分。它只在保留版本核对和追加并发检查的前提下复用缓存，对照时应该保持相同的提交顺序、快照验证和租约检查。此处只参考该约束。Marten 使用 MIT 许可，万象保留本项目的存储实现。 [Marten 并发与投影性能](https://martendb.io/events/optimizing) · [Marten 许可](https://github.com/JasperFx/marten/blob/master/LICENSE)

### 备用模型与调用记录

参考 LiteLLM 将供应商内部重试与显式备用顺序分开，以及每次切换前检查目标权限的设计。世界引擎沿用现有模型适配器和职责配置，按任务顺序尝试适用目标，每次调用独立计量。仅本地策略在发起请求前过滤所有配置目标。世界提交继续使用原任务的版本、规则与租约校验。上游核心路由采用 MIT 许可，本项目独立实现。 [LiteLLM 可靠性文档](https://docs.litellm.ai/docs/router/learn/reliability) · [路由源码](https://github.com/BerriAI/litellm/blob/main/litellm/router.py) · [许可](https://github.com/BerriAI/litellm/blob/main/LICENSE)

长行动继续使用分段提交和可恢复授权。Cataclysm: DDA 的活动合同将可打断、可恢复、活动结束与中止分别定义，提供了一种可比较的生命周期设计。万象只参考这个合同，使用自己的持久作业、精确计划边界与事件账本。封建 320 日实档的窗口承担实际世界结算，合并数据库事务会延后可恢复的落盘位置。SQLite WAL 的 `synchronous=FULL` 在提交时同步写入，`NORMAL` 会降低掉电或硬重启时的事务持久性。本轮优先消除同一事务内的重复分录提取，并以同存档实测区分性能和等价性。Cataclysm: DDA 许可证为 CC BY-SA 3.0，未复用上游代码或内容。 [Cataclysm: DDA 活动合同](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/doc/PLAYER_ACTIVITY.md) · [上游许可证](https://github.com/CleverRaven/Cataclysm-DDA/blob/master/LICENSE.txt) · [SQLite WAL 持久性与性能](https://www.sqlite.org/wal.html#performance_considerations)

### 人物计划的语义复核

LangGraph 以独立节点读取草稿并返回结构化评价，Anthropic 将并行评估区分为不同关注点的独立检查和同一目标的多次判断。人物计划采用独立判断，标准与深入档核对步骤是否推动已选目标，充分档另核对理由是否与计划矛盾。复核只接收同一快照的相关角色可知资料，结果不参与规则裁定，超出单次决策时限的提议交还给原复审流程。一次未通过就截断本次提议，避免反馈循环在长模拟中无限调用。项目仅参考工作流，未复制源码或新增依赖。 [LangGraph 评估节点](https://docs.langchain.com/oss/python/langgraph/workflows-agents#evaluator-optimizer) · [LangGraph MIT 许可证](https://github.com/langchain-ai/langgraph/blob/main/LICENSE) · [Anthropic 工作流设计](https://www.anthropic.com/research/building-effective-agents)

### 反思与正式世界记录

Generative Agents 根据近期经历的重要程度触发反思，并让新见解及其证据进入人物记忆，供后续规划取用。万象采用已落盘的角色目标达成、模型计划周期和类型化证据冲突作为各深度档的触发依据。反思只产生该人物的私有记忆，世界事实仍由原始事件决定。一次反思绑定到一个已提交来源；工作器重领租约后使用同一请求与已保存结果。研究仓库采用 Apache 2.0，本项目独立实现，也未加载其世界素材。 [论文](https://arxiv.org/abs/2304.03442) · [上游反思实现](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/cognitive_modules/reflect.py) · [上游许可证](https://github.com/joonspk-research/generative_agents/blob/main/LICENSE)

### 条件购买

ink 依据运行状态求值后才开放条件选项。万象采用同样的职责分工，将玩家授权的总价上限和币种写入交易命令，由市场规则按当前挂牌价及实际购买数量核对，失败时保留原时点、物资和账本。意图分类只负责交给正确的规划路径。交易合同和结算独立实现。 [ink 条件选项](https://github.com/inkle/ink/blob/master/Documentation/WritingWithInk.md#conditional-choices) · [ink MIT 许可](https://github.com/inkle/ink/blob/master/LICENSE.txt)

### 机构信贷与持续经营风险

OpenTTD 在周期结算中区分经营预警、持续困难和处置阶段，并会在财务恢复时清除困难计数。万象借鉴分阶段核对，保存借款、实际划拨、到期以及已还清等各自的世界记录，以便区分单笔逾期和持续经营风险。时期、处置选项和账本规则由世界设定给出，独立实现，不复用 GPL 源码。 [OpenTTD 经济规则](https://github.com/OpenTTD/OpenTTD/blob/master/src/economy.cpp) · [上游许可证](https://github.com/OpenTTD/OpenTTD/blob/master/COPYING.md)
