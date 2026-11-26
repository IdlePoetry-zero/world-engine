# 审计修复与自主世界验证方案

## 修复顺序

1. 修正会影响玩家判断的分支、目录、窄屏焦点、历法和案卷问题，恢复承诺隐私的既有断言。用正式入口和原浏览器对照复验。
2. 按可复现证据改善存档读延迟、模型记录范围、媒体任务与内容标识。持久化授权与旧存档重放随改动复验。
3. 实测幻想乡当前包的自治，先记录现有表现，按证据补足人物、经济和机构演化。规则推导必须给出当时可用的输入，玩家与普通 NPC 遵循同一世界时钟。

## 审计问题与复验

| 问题 | 调整与验证 |
| --- | --- |
| 切换世界时旧分支仍可选，目录故障与空目录混淆，窄屏焦点落到已关闭区 | Chrome 以实际页面构建复验。新世界的分支请求失败时，旧世界记录立即退出选择器，重试仅发给新世界。目录分别显示正在读取、失败和空目录。键盘能在窄屏完成建档并在退出时回到打开按钮。 [浏览器记录](../../.runtime/autonomy-optimization-browser/observed.json) |
| 自定义日长的预约时刻偏差，容量重试时丢失点名案卷 | GM 与 NPC 使用存档的日长，缩减上下文时先保留有权限的点名案卷和对应证据。 [对话回归](../../tests/api/test_conversation.py) |
| 承诺隐私回归中断，GM 冷读占用事件循环 | 承诺测试已运行到原有隐私断言。GM 的授权读取和上下文构建移到工作线程，继续复用同一获授权的存档快照。 [承诺用例](../../tests/api/test_promise_reliability_dialogue.py) · [对话回归](../../tests/api/test_conversation.py) |
| 调用列表只有近 100 条，媒体状态和隔离恢复操作欠明确 | 历史提供游标分页、保留筛选和 CSV 范围；媒体轮询设终止状态和有限重试；隔离恢复明确端口和工作目录。 [调用用例](../../tests/api/test_operations.py) · [媒体用例](../../frontend/src/components/HistoryMediaControl.test.mjs) · [恢复用例](../../frontend/src/components/restoreLaunchCommand.test.mjs) |
| 现代开局以名称匹配导致同名误判 | 新版内置内容让目的地和案卷使用可查标识，旧档仍走兼容读取并从历史移动回执恢复。 [开局用例](../../tests/api/test_modern_opening_goal.py) |

最新 API 全量回归通过 1,004 项。内容、领域、规则、代理、模拟和应用组通过 1,444 项。储存及作业相关回归通过 172 项。前端 18 项测试、静态检查和生产构建均通过。Starlette 测试客户端产生弃用提示。

## 自然演化的验证方法

本轮验证按用户补充使用正式引擎和隔离存档。需要模型应答的接口使用受控响应，选择依据来自这次运行的可见资料和规则允许的候选。验证记录注明策略来源。

用户随后将长休息验收终点改为六个世界年，并要求验收后更新 GitHub 默认分支。幻想乡每年 360 日，六年即 2,160 世界日。保留原来一次休息已提交的连续账本，推进到六年后结束原试跑。如果事件边界跳过日历年界，就在已验证的年界前分支上补齐最后一段，另外校验精确年界存档。

- 用隔离存档导入发行世界包。在原包有固定起点时，比较不同合法介入点和同起点分支。从同一公开状态分别做间歇观察与等候、世界推进、角色长休息。世界推进和休息是不同授权，分别记录已提交的时间、停止原因以及实际完成的动作。
- 逐段核对事件来源、行为人、参与者、关系与资源的前后变化。记录动机、实际行为、承诺和完成回执的差异。原作时间线节点、例行排程、世界规则和模型决定使用各自证据。
- 观察组织目标、可用人手、现有资源和章程权限。比较不同条件下的重新排序、延后、停止或执行。结果允许相同，也允许失败，需能从资料和正式事件说明原因。
- 复核同一时间轴、账本、权限、分支和重放。只读提问本身保持时钟。玩家消耗时间的低影响行动和长休息给外界同等实际时间。
- 方案不指定哪位 NPC 应该会见谁、哪家机构应该扩张，也不以结果吻合作者构想作为通过条件。

## 基线观测

幻想乡 1.4.0 红雾开局在两个独立导入存档各推进一世界日。间歇观察和等候、直接推进世界两组均得到 7 个 NPC 计划、14 次计划修订、28 条旅程状态事件以及 2 条故事节点回执。两组重放与已提交世界状态一致。旅程状态事件含出发和抵达，同一旅程可有多条。规划替身依据当前角色可知的目标与可用路线选择动作，正式引擎验证并执行。 [等候证据](../../.runtime/autonomy-evidence/controlled-scarlet-mist-quiet-8s2fygjr/evidence.json) · [世界推进证据](../../.runtime/autonomy-evidence/controlled-scarlet-mist-world-0kkbzyh7/evidence.json)

幻想乡 1.4.0 山上神社开局的间歇观察和等候也推进一日。记录有 28 个 NPC 计划、112 条旅程状态事件、2 条故事节点回执。重放一致。这组样本尚未观测到 NPC 私下发言或互相回应。现版社交目标里的对象只出现在叙述文字中，后续接入可由角色视野核验的对象引用。 [山上神社短测](../../.runtime/autonomy-evidence/controlled-mountain-shrine-quiet-hwoacbb3/evidence.json)

对同一介入点的 1.5.0 样本，规划替身按角色当前可见的目标、人物、路线与可回应记录选择候选。玩家只间歇观察和等候。一日内记下 8 条 NPC 间私人交流记忆、8 次熟悉度维度变更和 80 条旅程状态事件。一次可复核的交流在第 6,300 秒发起，在第 7,200 秒得到回应，双方的熟悉度按规则分别变更。外层长行动的发起者与记录中的说话人、收件人各有其字段。重放摘要一致。 [角色交流证据](../../.runtime/autonomy-evidence/controlled-mountain-shrine-quiet-smc8502u/evidence.json)

幻想乡 1.5.0 山上神社的十日休息对照已抵达 864,000 世界秒，玩家角色仍活动，重放状态一致。红雾开局另作一日无介入推进，留下两条 NPC 私人交流记忆、其中一条为回应，另有两条关系变化及二十条旅程状态事件。 [山上神社十日](../../.runtime/autonomy-evidence/controlled-mountain-shrine-rest-5w_t_k3s/evidence.json) · [红雾一日](../../.runtime/autonomy-evidence/controlled-scarlet-mist-world-ih_4hp2h/evidence.json)

山上神社三日的服务不可用对照，在隔离入口注入规划服务不可用错误。初次运行发现，故障可以把原有定期排程反复提早，导致长期机会很快耗尽。修正后，定期复审保持原时点，额外重试采用递增间隔。复验完成三日休息，规则提交 226 次暂缓复审、46 条日程旅程状态和两次机构复审，创世重放一致。该组没有生成新对话。红雾另一组中途退出后以同一作业继续，达到原定终点并通过创世重放。 [不可用对照](../../.runtime/autonomy-evidence/controlled-mountain-shrine-rest-dg4s6bn7/evidence.json) · [接续验证](../../.runtime/autonomy-evidence/controlled-scarlet-mist-rest-4nkgjd2y/evidence.json)

山上神社 1.5.0 另提交过一次十世界年的休息授权，后续验收使用同一作业的前六年。第 103.7 日的一致性数据库副本中，作业与世界时钟都为 8,961,300 世界秒，角色活动，账本从创世重放到第 4,479 条事件与副本一致。此时累计 2,699 条旅程状态事件、16 条私人交流记忆、20 条关系变化和 8 次组织复审。第 91 至 104 日仍观测到新的私人互动和关系变化。 [百日检查点](../../.runtime/autonomy-evidence/rest-checkpoint-audit-nyueum11/evidence.json) · [长休息作业](../../.runtime/autonomy-evidence/controlled-mountain-shrine-rest-phsh74pj/evidence.json)

同一作业的半年检查点在 15,704,100 世界秒，账本已经过 8,285 条事件，角色仍活动，创世重放一致。第 181 日后的首段记录有 9 条 NPC 私人交流和 16 条关系维度变化。逐月记录同时保留了没有私人交流的月份。 [半年检查点](../../.runtime/autonomy-evidence/rest-half-year-checkpoint-p2q5y27m/evidence.json)

首年检查点取自同一作业第 369.82 日。世界和作业的提交时钟同为 31,952,700，创世重放到第 18,661 条事件一致。重放中的 18,389 个提交边界上，玩家角色均为活动状态。累计 45 条 NPC 私下交流，其中 35 条回应，另有 70 次关系维度变化、26 次组织复审。第 4、7、9、10 月都有新交流，第一个年界之后仍有记录。 [首年检查点](../../.runtime/autonomy-evidence/rest-day-360-ijmgo584/evidence.json)

第二个年界后的第 723.58 日，世界和作业同为 62,517,600 世界秒。创世重放与第 42,099 条事件后的状态一致，玩家角色在 41,643 个提交边界上维持活动。累计 79 条 NPC 私人交流、其中 65 条回应，另有 130 次关系维度变化和 50 次组织复审。 [两年检查点](../../.runtime/autonomy-evidence/rest-day-720-u_v69mtx/evidence.json)

已验证的两年检查点也用于接续重放。第 872.48 日的隔离副本从第 42,099 条事件后的已验证状态重放至第 52,808 条，状态哈希与该副本的已提交分支一致。合并两段验证后，52,306 个提交边界上的玩家角色均为活动状态。接续使用存档副本，原作业继续推进。 [分段重放记录](../../.runtime/autonomy-evidence/rest-day-840-vgflm6s_/evidence.json)

第三个年界后的第 1,081.71 日，提交时钟为 93,459,600 世界秒。由已验证的第 52,808 条事件接续重放至第 68,419 条，与存档状态一致。此前累计 67,785 个提交边界上的角色状态均为活动。NPC 私下交流累计 115 条，其中 97 条为回应，另有 194 次关系变化和 72 次组织复审。 [三年检查点](../../.runtime/autonomy-evidence/rest-day-1080-96bfzl5y/evidence.json)

第四个年界后的第 1,441.54 日，提交时钟为 124,549,200 世界秒。由三年检查点接续重放至第 97,751 条事件，与存档状态一致。累计 149 条 NPC 私下交流、254 次关系变化和 96 次组织复审，此前累计的 96,936 个提交边界上，玩家角色均为活动状态。 [四年检查点](../../.runtime/autonomy-evidence/rest-day-1440-ilzyvuka/evidence.json)

第五个年界后的第 1,805.50 日，提交时钟为 155,995,200 世界秒。接续重放至第 131,255 条事件，与存档状态一致。累计 185 条 NPC 私下交流、318 次关系变化和 122 次组织复审，玩家角色在 130,247 个提交边界上维持活动。 [五年检查点](../../.runtime/autonomy-evidence/rest-day-1800-zys30hop/evidence.json)

同一个休息作业在跨过第 2,160 世界日后按修订范围停止，作业和存档的提交时钟同为 186,630,300。由创世检查点及各段已核实的状态依次重放至第 165,501 条事件，哈希与存档一致。重放覆盖 164,375 个提交边界，玩家角色持续活动。账本累计记录 210 条 NPC 私下交流，其中 181 条为回应，还有 362 次关系变化和 144 次组织复审。全程使用隔离存档与受控规划响应。 [停止回执](../../.runtime/autonomy-evidence/controlled-mountain-shrine-rest-phsh74pj/six-year-boundary.json) · [六年跨界重放](../../.runtime/autonomy-evidence/rest-day-2160-76pb8kql/evidence.json)

原作业的最后一个年界前提交在 186,606,000 世界秒。由这条已验证的分支再休息五个世界小时，正式作业成功停在六年整的 186,624,000 世界秒，玩家角色仍活动，重放与分支状态一致。六年整前的账本与跨界检查点的 NPC 交流、关系和机构复审计数相同。 [精确年界验收](../../.runtime/autonomy-evidence/exact-rest-boundary-px_btwp3/evidence.json)

另用 Chrome 从正式页面选择「角色行动 → 休息 → 日历年」，输入 10 年。版面按幻想乡每年 360 日显示 10 年，真实 API 验证只创建一项休息作业，提交的目标为 311,040,000 世界秒。此验证停在入队，实际推进由独立十年作业记录。 [浏览器提交回执](../../.runtime/autonomy-optimization-browser/calendar-ui.json)

为降低单次窗口提交的开销，正式储存层仅在写入成功后保留内部已验证的新分支状态。下一次读取仍核对实际数据库内容、分支、版本和时钟。在 2,500 实体的合成缓存微基准中，五次状态读取合计从 0.357 秒降至 0.169 秒。这是单独的缓存测试。长休息的端到端速度还受到事件数量、校验和数据库写入的影响。 [储存回归](../../tests/storage/test_repository_transactions.py)

## 跨背景机构实跑

- 现代城市 2.13.0 从创世分支推进 65 世界日，期间无玩家行动和模型调用。两个种子内，各有一个国家与另一组织在第 2、32、62 日分别提交复审。前两次分配日常储备，第 62 日因日常储备实际到达目标，两机构才转向发展储备。每次从对应机构账户划出 50，目标账户增加 50。实际终点为 5,616,000 世界秒，重放摘要一致。 [种子 19](../../.runtime/autonomy-evidence/controlled-modern_city-world-ysiq_5nz/evidence.json) · [种子 47](../../.runtime/autonomy-evidence/controlled-modern_city-world-bdy1eb7n/evidence.json)
- 低魔封建与长生修行 2.13.0 也分别从创世分支通过正式 API 推进 65 世界日。两组各有两个机构执行带账本的自主复审，分别使用银马克和灵石，创世重放与实际终点一致。 [低魔封建](../../.runtime/autonomy-evidence/controlled-low_magic_feudal-world-8jume9qd/evidence.json) · [长生修行](../../.runtime/autonomy-evidence/controlled-long_life_cultivation-world-x9y2r36w/evidence.json)
- 幻想乡 1.5.0 从创世分支接受一次三日休息。在第 2 日边界，两个组织分别提交自行复审，期间另有 NPC 私人交流、熟悉度变化和旅程。角色持续可用，作业抵达 259,200 世界秒，重放摘要一致。 [休息与机构回执](../../.runtime/autonomy-evidence/controlled-mountain-shrine-rest-zw8xgafv/evidence.json)
- 同一世界的长休息到第 62 日时，两机构已满足日常储备目标。红魔馆一方有 8 单位可用物资，符合划转和留存规则，开始补发展储备。守矢一方只有 6 单位，记录资金条件不足并保留原余额。第 92 日两家均无足够物资，运行继续，复审记录暂缓。 [同世界异条件账本](../../.runtime/autonomy-evidence/rest-half-year-checkpoint-p2q5y27m/evidence.json)
- 例行旅程的完整历史仍由事件账本保存。启用精简投影后，已结束超过一个世界日且没有后续依赖的日程旅程会移出当前快照。三日复验到达目标，玩家角色仍活动，8 条互相交流的私人记忆中有 4 条回应，均未出现在玩家历史投影。重放与账本一致。 [精简投影复验](../../.runtime/autonomy-evidence/controlled-mountain-shrine-rest-3zekt8tf/evidence.json)
- 早期试跑发现两个组织相同的本地策略标识可能路由到同一机构。复审排程现同时绑定组织和策略标识。两份跨背景报告使用修复后的正式排程。

可从仓库根运行 `./.runtime/venv313/Scripts/python.exe -B scripts/verify-autonomous-world.py mountain-shrine rest 259200 --fork`。运行前用 `scripts/build-gensokyo-reference.py --playable-world` 构建现版包。脚本使用本机隔离数据库和受控响应，写出作业状态、每 30 世界日的事件计数、事件样本、可见性检查和重放结果。可改用 `modern_city world 5616000 --seed 19 --fork` 检查另一背景，或改为 `quiet` 对照间歇观察与等候。长作业达到轮询时限时会保存原作业回执，可用原参数加 `--resume <原运行目录>` 接续。接续沿用原作业和分支，`--from-genesis` 用于核查完整事件链。

本报告的 `.runtime` 链接指向这次本机运行生成的证据。

[全项目审计](world-engine-full-audit.md)
