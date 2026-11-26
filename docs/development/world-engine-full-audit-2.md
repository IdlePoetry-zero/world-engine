# 万象世界引擎全项目审计（二）｜玩家操作、后台运行与存档可靠性

2026-11-24 · 基线为 `66474dc` 加冻结时的工作树。本文是独立工程审计；此前的 60 项玩法报告保持原样。

## 结论

产品已经拥有可追溯的正式行动、可分叉的存档、有限自主 NPC 和按权限读取的历史。值得优先修的两条路径发生在玩家等待反馈时：发送一句话可在异步服务的主事件循环同步读库，切换世界时分支选择器又能继续展示旧世界的列表。另一个直接影响运维判断的缺口是调用后台只能导出最近 100 次调用。三者分别会使慢档看似卡住、让玩家挑错分支、让运营者漏算范围。本文给出八个发现及逐项验收条件。

**审计单位是整项目文件，验证单位是具体路径。** 冻结清单 693 项全部登记；688 项核对了实际字节与 SHA-256，另五份评审材料按独立性要求仅登记。人工顺着核心链路阅读，也对其余文件做了内容结构检查。覆盖所有第一方实现文件不代表每条分支或每一行都由人审读，更不代表全部玩法经过真人试玩。[逐文件证据表](world-engine-full-audit-2-coverage.tsv)

## 范围、深度、方法

以仓库根目录 `git -c core.quotepath=false ls-files -c -o --exclude-standard` 独立枚举，再和冻结的 `.runtime/full-audit-inventory-20261124-231848/paths.txt`、`sha256.tsv` 对账。两份集合一致。688 项实读文件的摘要全部吻合。索引与未跟踪但未忽略的源码、当日验收、原始设计 ZIP、历史文档与图均在清单内。没有进入用户存档、凭据库、旧评审内容或第三方依赖的源码。使用了项目自带运行时和已安装的语法解析器。

| 深度 | 数量 | 实际核查 |
|---|---:|---|
| H | 42 | 完整内容的结构检查，再人工追读入口、关键分支或跨文件协议；29 个后台文件、11 个前端文件、CI 与检查脚本各 1 个。 |
| C | 299 | 读入全文；Python 遍历 AST 的定义、调用、条件、拒绝、断言；UI 和脚本检查定义、条件渲染、IO、错误处理。TSV 给出每文件实际行号和取样表达式。 |
| T | 287 | 284 个 tests 路径与 3 个前端组件测试文件。Python 检查测试体、断言、场景名与辅助函数，JS 查看用例与断言。仅下述 19 项是本人执行的结果。 |
| D | 52 | 文档要求、图源、锁文件、开发与部署配置全文扫描；JSON/JSONC 核对结构，重点人工对照 R01–R18、运行与备份说明。 |
| B | 8 | 两个 PNG 查块、尺寸及解码，五个 SVG 查 XML 元素，设计 ZIP 的 17 个成员全量 CRC 校验；未逐个把 ZIP 成员当作当前源码。 |
| M | 2 | 两份历史独立审计留在账册，按本轮独立审计约束只采用冻结清单元数据。 |
| X | 3 | 三份本轮先前玩法方案纳入基线计数，未作为项目证据阅读。 |

**范围对账。** 后台 278（迁移 7、适配器 17、agents 3、API 12、application 66、content 28、domain 109、rules 10、simulation 13、storage 11，另有包标志与 runtime 各 1）；前端 65（src 53、公有资源 2、构建/项目文件 10）；测试目录 284；脚本 15；docs 43；其余根文件及 CI 8，合计 693。文本共 226,711 行。另用已安装的 Rolldown 解析器解析全部 44 份 TS、TSX、MJS，遍历 134,752 个有类型的 AST 节点，语法诊断为零。语法成功不代表交互正确。TSV 中的 `static` 表示没有直接执行该文件自己的用例，间接从测试被调用不另计次数。

## 按优先级排定的八个发现

**F1　高优先级 · 存档慢读可以阻塞同一服务中的其他请求。高置信度。** 消息 HTTP 路由直接在异步上下文 await 服务，服务一开始同步调 `repository.read_world`；随后准备 GM 回答也同步查询多类历史。后台 NPC 规划的初始读档也有同类调用。隔离探针使用真正的 `ConversationApplication.send` 和注入的只读仓库替身，令第一次读阻塞 150 ms；原定 10 ms 的独立事件循环回调在 150.4 ms 才被调度。这个数字说明阻塞机制，不是生产存档延迟。建议把完整的、需要同一快照的授权读取收拢到受控工作线程；保留规则提交的版本/范围校验。验收用冷读或人为慢读并发发出状态轮询，确认轮询可在慢读完成前返回；比较改动前后的 GM 可见数据、分支摘要和回执。[HTTP 入口](../../backend/world_engine/api/conversation.py) L19–24 · [对话](../../backend/world_engine/application/conversation.py) L618–619、L935–963 · [NPC 决策](../../backend/world_engine/application/model_decisions.py) L54–60

**F2　高优先级 · 切换世界后分支列表能串到上个世界。高置信度，静态。** 世界视图通过 world/branch 组合校验，分支列表却是全局数组；切换世界没有清空或绑定此数组，新列表失败时也没有错误分支。游玩页只根据数组是否为空启用选择器。A 有多个分支时切到 B，慢请求期间就能选 A 的分支；B 的列表请求失败时，旧选项还会保留。服务端校验仍应拒绝实际跨世界请求，表现为错选和读取失败，没有证据表明它跨越权限。修法是用 world ID 绑定列表、错误和加载，并检查待选 branch 属于当前 world。验收模拟 A/B 不同分支、乱序成功及 B 的单独 503，核对从切换瞬间到重试成功的可选项和请求。[应用选择状态](../../frontend/src/App.tsx) L32、L82–102、L116–124、L173 · [选择器](../../frontend/src/pages/PlayPage.tsx) L498–500

**F3　中优先级 · 超过 100 次的调用无从在网页导出或翻页。高置信度，存储层实测。** 存储接口默认 100 条、最大 500 条；HTTP 端点不接受游标，CSV 只从当前数组生成。隔离临时 SQLite 写入 101 份合成记录，正式默认读取返回 100，最老记录仍存在但未返回。用户拿 CSV 统计某次长作业成本时容易低估范围。增加带稳定次序的键集分页（时间与唯一 ID），明确导出当前页或整个筛选范围；未知费用继续保持未知。验收 101 份、同一时刻多份以及翻页时有新调用的情况，确保范围、行数与数据库一致。[存取](../../backend/world_engine/application/operations.py) L277–287 · [API](../../backend/world_engine/api/operations.py) L67–69 · [导出](../../frontend/src/pages/OperationsPage.tsx) L194–201

**F4　中优先级 · 初次建档可把目录请求失败伪装成后台没有发布模板。高置信度，静态。** 世界列表成功时，设定包请求失败会被忽略且清除总错误；弹窗按照空数组显示「当前没有可用的设定包」。弹窗若在初次加载结束前打开，选中 ID 还可能保留首次渲染的空串；提交有无声短路。给目录建立独立加载/错误/重试状态，数据到达后校准初选且保留用户已有的合法选择。用“已有存档可正常进入，设定包首次 503，随后只返回一个包”验收，弹窗无需关闭重开即可建档，真正空目录的提示仍然准确。[世界库加载](../../frontend/src/App.tsx) L57–71 · [弹窗](../../frontend/src/components/CreateWorldDialog.tsx) L45–46、L61–66、L92 · [开放按钮](../../frontend/src/pages/LibraryPage.tsx) L11–14

**F5　中优先级 · 实际已归档的开局案卷只改标题，就会失去归档指引。高置信度，投影函数实测。** 查找按汉字标题匹配，而内容作者已经使用稳定的 `opening-investigation-rumor` 生成 case ID。用真实投影函数给一条抵达回执与已归档案卷，只改同一案卷的显示标题，结果中的「已归档」从有变无，底层案卷并未消失。该探针模拟内容改版/本地化，未证明 UI 允许任意修改所有案卷。新增基于稳定内容引用的匹配，展示投影中可知的新标题，为老存档保留受限回退。验收改名、重名、异地隐藏与旧版本回放；读到已归档不可泄露未知锚点。[投影](../../backend/world_engine/application/modern_opening_goal.py) L35–51 · [原始内容](../../backend/world_engine/content/investigation_openings.py) L209–212 · [现有回归](../../tests/api/test_modern_opening_goal.py)

**F6　中优先级 · 媒体任务因永久错误变得不可读后，界面仍无限轮询。高置信度，静态。** 图像/语音面板读取任务的 catch 分支对所有错误同样在 1.8 秒后再试。撤销权限、任务已经不可访问等终态与暂时断线走同一路径，只要组件还挂载就会重复请求。服务端在生成后重新检查权限是正确的；应在客户端区分终态和可重试失败，瞬时错误采用有上限的退避并提供手动重试。挂载两个已被撤权的任务面板，用虚拟时钟确认只报告一次终态，暂时 503 仍可恢复。[前端错误处理](../../frontend/src/components/HistoryMediaControl.tsx) L63–82 · [媒体重新授权](../../backend/world_engine/application/media_jobs.py) L225–237 · [权限测试](../../tests/api/test_media_jobs.py)

**F7　中优先级 · 通用模型请求的截止时间从取得槽位后才起算。高置信度，静态行为，优先级取决于产品时限定义。** 调度器排队 await future，此处无截止；运行服务取得槽位才进入 gateway，gateway 为当前目标启动自身时限。多目标回退重新创建一次目标调用。因此把“请求截止”理解为用户总等待上限并不成立。已有特定后台决策有独立 100 秒外层保护，不能把这项发现扩展为所有 NPC 作业都无限等待。为前台 GM 请求明确排队预算/总期限，在排队与回退时传递剩余预算，区分未发出的取消与可能产生费用的网络超时。用不联网替身占满单槽，短时限请求应按承诺结束、记录排队时间与零供应商尝试；回退在总期限内决策。[调度](../../backend/world_engine/application/model_scheduler.py) L114–145 · [目标循环](../../backend/world_engine/application/operations.py) L357–374、L458–467 · [供应商截止](../../backend/world_engine/adapters/models/gateway.py) L108–127 · [后台例外](../../backend/world_engine/application/model_decisions.py) L28、L71–79

**F8　低优先级 · 恢复成功界面的复制命令在一种受支持的部署里会立即退出。高置信度，静态。** 源服务以 `WORLD_ENGINE_DATABASE_URL` 启动后，独立恢复本身有隔离保护；从同一环境运行网页生成的 `--data-directory` 命令会触发 CLI 的显式拒绝。这会阻止误写源库，但完成恢复的用户只看到“复制并运行”。备份指南已经提示清除变量，修法是让同样说明出现在成功界面，或生成子进程内隔离该变量的启动方式，保留父终端原配置。验收在一份无生产数据的临时库设置源 URL，照界面指示启动副本并核对源/副本的 hash 与父窗口环境值；端口占用应给出可操作提示。[复制命令](../../frontend/src/pages/HistoryPage.tsx) L29–31、L109–113 · [CLI 保护](../../backend/world_engine/runtime.py) L327–329 · [已有文档](backups.md) L7

## 从入口追到结果：业务与回放

| 路径 | 状态和业务回执 | 失败和重放如何约束结论 |
|---|---|---|
| 问 GM、聊天、行动 | `api/conversation.py:20` → `ConversationApplication.send` → `RulesActionDispatcher` 或只读回答。真正写世界的分支调用 `WorldRepository.commit_command:2057`；回执携带 command/state version，领域 `apply_event:253` 处理已验证事实。 | 提问不能冒充观察；消息路径核对世界与请求键。存储测试含中途失败回滚、旧内联回执与幂等冲突；本轮作了 F1 的调度反例。 |
| NPC、日常、关系 | `runtime.py:105` 把调度决定路由给规划、反思或赴约。角色的可见信息经 `agents/knowledge.py:42` 和规划上下文过滤。行动要落为真实的出发、投票或还物事件。 | `simulation_jobs.py:106` 及仓库租约校验阻止旧工作器继续结算；对应测试比较错分支回执、旅途中断、死亡与承诺未兑现。未在此轮复跑多世代自治。 |
| 调查与误指 | 开局案卷定义 → 现场取得来源 → `InvestigationCaseFile` 的私有证据与结案状态 → 公开指认与更正的独立记录。玩家目标投影读取本人、当前分支的抵达和案卷。 | 角色私下写错结论和公开误指有不同关系结果；合成同 ID 改名复现 F5。存在案卷本体和前端提示两个独立判断层。 |
| 工作、贸易与政治 | 职业入口 → `plan_paid_work_shift` 校验雇主库存、班次及账目；市场报价与实际交易分开。选举 `cast_leadership_ballot_vote` 校验固定选民与版本，机构信贷展期另有同意流程。 | 工资用例验证借贷分录与恢复；房间并发抢最后一件库存用正式市场规则，两个先后顺序有稳定批结果；该房间套件本轮已执行。 |
| 战斗、成长与旅行 | 交战/撤离与停战进入正式规则；突破按注册境界、真气、用时和失败伤害结算。公开路线的长行动由 `inspect_travel_itinerary_job` 找到下一段，抵达必须有对应路段的实际回执。 | 隐藏路线、途遇、在途行为与失败受规则约束；受伤或未到达不能写成成功。在示例包内，剧情节点通过库存、知识和既有状态决定结果。 |
| 作者、世界包与恢复 | `EditorPage` → `AuthoringApplication.preview:44/commit:139` 核身份、组件与版本；`WorldPackApplication.import_world:251` 固定 ZIP 摘要、开场和玩家设定。恢复经 `api/app.py:717` 流式校验 → `WorldBackupImporter.import_verified_backup:309` → 仅独立目标；可注册升级通过 `ruleset_migrations.py`。 | 归档流量有上限，预览绑定 SHA；重试导入核对原包与同键开场。非空恢复目标拒写、检查点跨分支回放和摘要校验在本轮定向测试中执行。 |
| 后台费用、权限、扩展与部署 | `ModelOperations._generate_target` → slot → gateway → trace → operations.db，费用以报告用量和已知价目估算。事件媒体经 `MediaJobEnqueuer`、`process_claim` 查旧资产、生成后重新鉴权。CI 分 Rules、SQLite、前端；本机 runtime 绑定数据目录、进程锁、迁移与 worker。 | 费用未知与估算分开；媒体写资产后任务未收尾可按原请求恢复，撤权不再发布。API 与模型部署、Windows 真实语音、生产并发负载未经本轮执行。 |

## R01–R18 及项目包覆盖

| 需求 | 代表性源码与测试/脚本 | 审计判断和边界 |
|---|---|---|
| R01 背景创作 | `content/catalog.py`、`application/world_pack_import.py`、`tests/content/test_world_packs.py` | 模板版本、介入点、人物设定和预览绑定有实现；新包可操作性另见 F4。 |
| R02 自然语言 GM | `api/conversation.py`、`application/conversation.py`、`application/actions.py`、`tests/api/test_conversation.py` | 查询、草稿、授权行动分流；F1 影响整条客户路径，未开启真实模型。 |
| R03 人物创作 | `application/authoring.py`、`application/npc_rehearsal.py`、`tests/api/test_authoring.py` | 作者预览、版本与演练分开，玩家控制权不会自动授予作者编辑权。 |
| R04 自主生活 | `agents/policy.py`、`application/npc_planning.py`、`simulation/npc_plan_execution.py`、`tests/simulation/test_npc_plan_execution.py` | 采购、旅程、投票等计划以真实事件验收；默认配置与实际模型可用性影响最终体验。 |
| R05 认知记忆 | `agents/knowledge.py`、`application/visibility.py`、`tests/api/test_visibility.py` | 角色、地点、受众与来源影响可知范围；未把对白当成第三方证据。 |
| R06 关系日常 | `domain/npc_commitments.py`、`application/relationship_decisions.py`、`tests/rules/test_relationship_promises.py` | 私有约定与实际履行分开，期限、所有者和物品数量另校验。 |
| R07 调查 | `domain/investigation_cases.py`、`content/investigation_openings.py`、`tests/rules/test_investigation_cases.py` | 已有按来源归档与公开更正；F5 指出跨版本表述脆弱点。 |
| R08 经营生存 | `domain/work_shift.py`、`domain/market_pricing.py`、`simulation/work_purchase.py`、`tests/storage/test_work_shift_lifecycle.py` | 真实工资、报价和购买可各自落账；未估算长期经济平衡。 |
| R09 势力政治 | `domain/leadership_ballots.py`、`domain/institutional_credit_extension.py`、`tests/storage/test_leadership_ballot_lifecycle.py` | 投票有资格与修订检查；展期有其批准程序。未把一个制度案例外推到所有国家。 |
| R10 战斗成长 | `domain/combat.py`、`domain/combat_truce.py`、`domain/cultivation_breakthrough.py`、`tests/rules/test_combat_encounters.py` | 撤退会复用真实路线，聚气、筑基成本和失败有规则依据；战斗平衡未实玩。 |
| R11 长期演化 | `simulation/engine.py`、`domain/population_seats.py`、`scripts/audit-capacity-interval.py`、`scripts/merge-capacity-intervals.py` | 逐提交计数、断档窗口和合并证据有实现。原 PRD 的 600 年与最新验收的不同已审计区间分列，后者才是当前用户确认口径；本轮未触碰实档。 |
| R12 存档与干预 | `storage/repository.py`、`domain/reducers.py`、`tests/storage/test_checkpoint_replay_range.py` | 检查点、祖分支及错误摘要的测试通过。F2 为前端选择范围问题，未见底层分支串写证据。 |
| R13 多模型后台 | `adapters/models/gateway.py`、`adapters/models/costs.py`、`application/operations.py`、`tests/providers/test_model_gateway.py` | 网关有重试和失败痕迹；未知价不补零。本轮只跑该文件的费用样本，运行后台应修 F3、F7。 |
| R14 速度与深度 | `application/model_scheduler.py`、`adapters/models/contracts.py`、`tests/api/test_model_scheduler.py` | 队列公平与推演设置是两个维度；没有用配置名推断真实吞吐。 |
| R15 多人与账号 | `api/security.py`、`application/room_coordination.py`、`tests/application/test_room_coordination.py` | 本轮实跑过期和撤权、控制权围栏、超时和稳定批处理。当前房间协调器是对宿主的契约；持久外部主持和公网多人房间未验。 |
| R16 模块与媒体 | `application/media_contracts.py`、`application/media_jobs.py`、`application/media_authorization.py`、`tests/api/test_media_jobs.py` | 本轮执行媒体 worker 的幂等、恢复、生成后撤权及续租案例；F6 是播放器轮询端缺陷。 |
| R17 兼容迁移 | `backend/migrations/versions/`、`application/ruleset_migrations.py`、`storage/backup_import.py`、`tests/storage/test_world_pack_archives.py` | SQL 迁移、规则升级与内容包发行有独立版本边界；未来未注册版本不能推定兼容。 |
| R18 操作可靠性 | `runtime.py`、`frontend/src/pages/HistoryPage.tsx`、`scripts/check-engine.ps1`、`.github/workflows/check-engine.yml` | 有明确本地安装、SQLite 版本与测试入口；F1、F2、F4、F6、F8 应进入用户可见的故障回归。 |

**六类玩法。** 按交付设计 W06 的分类，上表 R06 对应生活关系、R07 对应调查、R08 对应经营生存、R09 对应政治、R10 对应战斗成长。第六类为交叉事件：现代出勤与赴约竞争时间；封建途中核对或续约改变路线；修行受耗时与伤势制约；红雾报告通过知识来源改变物资；回灯棚通过证词与接递凭证推进案卷；远岬站把玩家的验收与固定时刻的外部脉冲分别结算。相应一方源码分别见 `content/routine_openings.py`、`content/waypoint_openings.py`、`domain/cultivation_breakthrough.py`、`content/gensokyo_mist_reports.py`、`content/gensokyo_scenario.py`、`content/distant_station_story.py`。核查的是这些样例，而非声称所有六种分类和每一背景的笛卡尔积均已本轮实测。

## 本次运行与反证

- 本机使用项目指定的 Python 3.13 环境，`-B -m pytest -q -p no:cacheprovider`。运行目标为 `tests/application/test_room_coordination.py`、`tests/api/test_media_jobs.py`、`tests/providers/test_model_gateway.py::test_usage_and_cost_keep_unknown_separate_from_estimated`、`tests/api/test_backup_restore.py::test_restore_refuses_nonempty_destination_without_modifying_it`、`tests/storage/test_checkpoint_replay_range.py`。**19 passed in 3.67s**。这些是契约、临时数据库和替身测试；没有付费调用。
- 三个额外探针只使用合成输入，无用户数据读写：150 ms 慢读观察事件循环；101 条合成运行记录；稳定 case ID 的标题更名。数值和边界分别列于 F1、F3、F5。
- 检查时曾考虑“模拟 worker 一定在模型返回前丢掉 120 秒租约”。代码对普通模型决策和规划另设 100 秒外层保护，该证据不足以作此推断，因此没有列为缺陷。也曾怀疑从界面打开独立副本可能覆盖源库存档，CLI 会预先拒绝这种环境；实际问题仅是 F8 的启动指引。
- 未启动新的产品服务或浏览器，未读取数据库中的玩家存档/会话，未跑独立的数百年长测、全量 PostgreSQL、真实模型费用对账、视觉对比或外部多人主持。全量回归由主审另行记录。未更改产品代码、既有计划、部署或版本库历史。
