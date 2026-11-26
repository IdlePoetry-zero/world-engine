# 世界数据与接口规范

## 机构自主复审

世界包可在创世状态登记 `organizational_strategies`。每个组织有自己的使命文本、章程授权、复审周期和可比较的有条件目标。排程同时绑定组织 UUID 和组织内策略标识。执行时只检查该组织的现有人员、状态及自有账户。首个满足条件并有足够可用资源的目标获得执行，结果可为延期或不行动。

`core.organization_strategy_review_resolved.v1` 保存组织、复审批次、条件观测、当时有效的经办权限及可能发生的内部划拨。重放从前态复算并校验回执，账本的借贷两侧使用同一资源和数量。策略证据默认仅在作者历史可查。未来的实际结果不写入使命或目标文本。

本规范定义实现前的协议基线。字段类型、错误语义和不变量是模块之间的约束；实现时将其转成 JSON Schema、OpenAPI 与契约测试。示例是协议说明，不是已运行的产品代码。

## 标识与基本类型

所有实体使用稳定 UUID，不以姓名作为主键。所有世界对象带 tenant_id、world_id 和 branch_id；引用必须在同一世界同一分支可见范围内解析。系统内部时间使用有符号 64 位整数 tick，以世界定义的秒为单位；JSON 时间、序号和大整数使用十进制字符串，防止浏览器精度丢失。

世界历法将 tick 转换为年、月、日，不依赖宿主系统日期或时区。宿主 UTC 时间只用于审计、超时和模型计量。历法版本固定在世界规则版本中。金额使用最小货币单位整数，资源数量使用固定精度 Decimal，禁止二进制浮点直接计算账本。

公共版本字段包括 schema_version、ruleset_version、pack_version 和 state_version。major 变化代表不兼容，未知 major 拒绝导入；minor 只允许明确向后兼容字段。向量索引带 embedding_model_id、dimension 和 index_version。

## 世界设定包

| 对象 | 必需字段 | 说明 |
| --- | --- | --- |
| Manifest | pack_id、version、engine_range、title、dependencies、license、content_hash | 发布后不可原位修改 |
| Calendar | epoch、day_seconds、year_definition、display_rules | 允许自定义历法与闰年函数的已批准实现 |
| Ruleset | modules、invariants、action_specs、attribute_specs | 只引用已安装模块及声明式操作 |
| Geography | places、routes、regions | 地点包含层次结构；路径有耗时、通行条件和容量 |
| Society | polity_templates、organization_templates、institutions | 国家、宗门、公司等采用组件组合 |
| Resources | currencies、commodities、recipes、capabilities | 单位、精度、来源和转换规则明确 |
| Cast | character_templates、relationships、knowledge_seeds | 秘密与公开设定分别标记 |
| Scenario | initial_entities、initial_ledger、scheduled_events、player_options | 初态必须通过平衡与引用校验 |
| Presentation | tone、perspective、length、content_preferences | 不可覆盖规则 |

依赖按拓扑排序加载，循环依赖拒绝；同一字段的多个模块不能靠加载顺序隐式覆盖，必须声明替代策略。种族、能力和资源使用命名空间，如 cultivation.qi，避免不同包同名冲突。

## 核心持久对象

| 对象 | 核心字段 | 不变量 |
| --- | --- | --- |
| World | owner、pack_lock、calendar、current_tick、state_version、status | tick 单调递增，分支回退创建新分支 |
| Entity | id、kind、components、created_event、retired_event | 稳定身份，删除采用退役状态 |
| Character | template_id、birth_tick、species、location、status、attributes、needs | 死亡后不执行普通存活日程 |
| Place | parent_id、capacity、environment、access_policy | 包含关系无环 |
| Route | from、to、travel_rule、access_conditions、risk | 移动需合法路径或明确传送机制 |
| Organization | members、offices、charter、assets、goals、status | 职位持有人合法，继承规则有版本 |
| Relation | from、to、dimensions、obligations、evidence_events | 有向关系，变化有来源 |
| InventoryLot | owner、container、resource、quantity、quality、expiry | 单批物资不能同时归两个所有者 |
| LedgerEntry | transaction_id、account、resource、delta、source_event | 交易分录平衡，生产消耗有显式来源去向 |
| Goal | owner、priority、preconditions、success_conditions、deadline、status | 目标不是事实，不能自动写成已完成 |
| Plan | goal_id、steps、reservations、stop_conditions、revision | 计划可失效，执行前重验 |
| ScheduledEvent | due_tick、phase、priority、dependencies、handler_version、status | 同一事件只能结算一次 |
| Event | type、payload、tick、seq、causes、visibility、rules_version | 追加写，序号唯一 |
| Snapshot | last_seq、tick、hash、state_blob、queue_blob、rng_state | 可恢复到精确提交边界 |

国家由组织组件加领土、制度、税收、外交与人口投影构成，不以一段文字描述替代其运行状态。具名人物与人口 cohort 双向关联，具名化从 cohort 扣除，退役到群体也保存历史身份，不重复计算。

## 事实和知识

Fact 保存 subject、predicate、object、valid_from、valid_to、source_event 和 truth_scope。事件本身是真实发生的行为记录，但事件中的角色言论不自动成为客观事实。

Observation 保存 observer、observed_event、perceived_fields、channel、received_tick。Belief 保存 holder、claim、confidence、supporting_observations、contradictions、last_revised_tick。Utterance 保存 speaker、audience、claims、semantic_commitments、heard_by；包括谎言时，只记录说了什么，不修改 claim 对应的事实。

Memory 保存 owner、source_refs、importance、summary、retrieval_tags、visibility 和 branch_prefix。摘要可以更新，原始来源仍可查。召回必须先限定 world、branch、主体权限、模拟时间，再检索；不能先全库向量搜索后仅在 UI 隐藏。

## NPC 模板与实例

模板包含 identity、voice、values、traits、capabilities、initial_resources、relationships、knowledge、secrets、goals、schedule、growth_rules、locks。实例引用模板版本并保存运行差异。年龄从出生 tick 和历法计算，不同时维护两个独立可变的年龄字段。

锁有 immutable_fact、stable_trait 和 author_protection 三类，保护规则需要可执行语义，例如不能死亡时受致命伤转为哪种状态。禁止只有一句永不死亡的提示词，却让战斗系统照常生成死亡事件。

人物 `personality.locked_fields` 记录正在锁定的五维性格、性格特征或价值观字段。普通事件可修改其他字段，锁定状态通过作者修订设置。修改已锁字段须先独立提交解锁；同时解锁和改值会被拒绝。模板跨到新分支应用时也遵循当前实例锁定。未设置锁定的旧模板保留原摘要。锁定字段的值可作为角色自身演绎依据，玩家投影继续过滤内部设定。

NPC 的 `capabilities.combat_death_protection` 默认关闭，由作者修订显式启用。已登记的战斗命中原本会致死时，受保护的 NPC 失去行动能力、剩余 1 点生命并保留致失能伤口，交战结束。失能角色只能等待恢复，已排队的长行动暂停。合资格且同地的角色按普通治疗规则消耗一份药品，处理这处伤口后恢复行动。寿终和非战斗伤害仍按原规则处理。作者开关只出现在作者投影中。

模板更新默认仅影响新实例；现有实例更新通过 AuthorIntervention 生成影响预览和新分支。秘密字段可标记 author_only、known_by 指定角色或需要观察事件才能获知。

`POST /api/v1/worlds/{world_id}/author/npc-rehearsals` 使用请求、分支和角色标识、预期状态版本与已登记的情境。拥有者或编辑者可为活跃 NPC 运行试演。内部决定依据选定版本的当前目标、性格和可知资料。对白输入包括同一假设、公开身份、公开资料、已选决定和语气，并可用锁定的性格值作内部演绎提示。两次调用共用请求追踪。服务端校验选择枚举、输出长度及引用条目，报告携带状态版本、时刻、摘要及 `world_effect=false`。两次调用均遵从作者辅助路由、速度与深度设置，并保存在模型后台。

## GM 输入协议

```json
{
  "schema_version": "1.0",
  "request_id": "uuid",
  "world_id": "uuid",
  "branch_id": "uuid",
  "session_id": "uuid",
  "actor_id": "uuid",
  "expected_state_version": "1042",
  "mode": "auto",
  "text": "如果这条路安全，我就去北城；大概需要多久？",
  "profile": {"speed": "balanced", "depth": "deep"}
}
```

mode 取 auto、ask_gm、roleplay、action、author。分类输出为 segments，每段包含 kind、text_span、requires_clarification、world_effect 和 proposed_action_ref。kind 包括 query、recall、hypothesis、speech、action、author_edit、system_control。服务端重新验证 actor 控制权，不能相信前端传入的身份。

query、recall、hypothesis 默认 world_effect=false，禁止进入变更提交器。自动模式遇到条件行动必须确定条件判断依据；未知的安全性不能擅自替玩家保证。显式模式有助于解释意图，但不赋予额外权限。

查询允许记录 GM 会话、审计与模型费用；这些属于操作数据，不进入世界事实哈希、世界事件序列或角色记忆。所有查询无副作用测试使用这一世界状态边界，避免将记录一次查询误判为世界演化。

## 行动提案与规则规格

ActionSpec 定义 action_type、args_schema、preconditions、duration_rule、cost_rule、resource_claims、resolver、possible_events 和 interruption_policy。每个模块只允许发出已注册事件类型。

```json
{
  "proposal_id": "uuid",
  "command_id": "uuid",
  "basis_version": "1042",
  "actor_id": "uuid",
  "action_type": "core.transfer",
  "args": {"recipient_id": "uuid", "resource_id": "core.silver", "quantity": "2"},
  "intent": "为获得消息付出报酬",
  "constraints": {"max_spend": "2", "allow_partial": false},
  "evidence_refs": ["event:uuid"]
}
```

LLM 可提出参数与备选语义，不能决定余额检查结果或自行创建通用状态补丁。资源预留有时效与来源，取消释放。部分成功用独立的实际消费和实际结果事件表示，不能先全额扣款再靠叙述说退了一半。

临时裁定结构为 scope、analogous_rules、preconditions、allowed_effect_types、bounded_parameters、duration、risk、precedent_signature。必须通过规则检查；超出能力、创造唯一资源、改变永久世界规则的提案转成作者规则提案，不在玩家回合静默生效。

## 事件信封与示例

```json
{
  "schema_version": "1.0",
  "event_id": "uuid",
  "world_id": "uuid",
  "branch_id": "uuid",
  "sequence": "1043",
  "tick": "381240",
  "type": "core.resource_transferred.v1",
  "command_id": "uuid",
  "causation_ids": ["uuid"],
  "correlation_id": "uuid",
  "ruleset_version": "1.0.0",
  "payload": {"from": "uuid", "to": "uuid", "resource": "core.silver", "quantity": "2"},
  "visibility": {"policy": "participants_and_observers", "subject_ids": ["uuid"]},
  "random_draw_refs": [],
  "state_version_after": "1043"
}
```

时间相同的事件以调度阶段、优先级和稳定 ID 排序；seq 是已提交全序。因果引用不能指向未来或兄弟分支。重要历史事件要求 cause 或明确的外生规则来源，例如天气模型，不得只有模型猜测。

常用事件族包括 movement、resource、production、communication、observation、belief、relationship、obligation、combat、health、growth、organization、population、discovery、author_intervention。新增事件需提供 reducer、validator、可见投影、迁移和回放测试。

## API 与进度

下表列出本地网页及其集成使用的主要接口。基础路径为 `/api/v1`。世界相关路径按当前租户和成员权限授权。

| 方法和路径 | 请求或响应核心 | 语义 |
| --- | --- | --- |
| GET /session | principal、permissions | 本地或未来认证统一入口 |
| POST /worlds | pack_id、title、player_name、可选 seed、可选 population_mode、Idempotency-Key 请求头 | 创建并校验世界。seed 为 JavaScript 安全整数，可为负。省略种子时使用背景包默认生成方式。population_mode 默认为 natural，可选 sustained，为地区配置从匿名人口补充具名席位的策略。相同请求键绑定生成参数 |
| GET /worlds/{id}/view | 可选 branch_id 和 mode | 默认返回玩家投影，作者视图另行校验权限 |
| POST /worlds/{id}/messages | GM 输入信封，支持显式行动模式 | 返回答复、已结算结果或作业回执 |
| GET /commands/{id} | 请求 ID，返回 status 和 result | 查询状态，必要时由已提交回执恢复结果 |
| GET /jobs/{id}、GET /worlds/{id}/jobs | 作业 ID，或世界 ID 与 branch_id | 返回授权的作业状态。模拟作业可返回 `committed_state_version`，用十进制字符串表示最后提交的分支版本。尚无提交时可返回入队版本 |
| POST /jobs/{id}/control | pause、resume、cancel | 检查点控制，返回实际状态 |
| POST /worlds/{id}/world-advance | branch_id、expected_state_version、duration_seconds、profile、Idempotency-Key 请求头 | 世界所有者提交快进任务。预期版本为十进制字符串。返回 202 和实际作业状态，全员参与模拟 |
| GET /jobs/{id}/decision | 作业 ID | 向有权限的会话返回等待玩家选择的标题及候选 |
| POST /jobs/{id}/decision | request_id、option_id | 仅接收当前时间及版本绑定的候选。重复提交同一选择返回既有作业 |
| POST /worlds/{id}/branches | branch_id、sequence、label、Idempotency-Key 请求头 | 作者从源分支的初始状态或完整提交创建分支。sequence 为十进制字符串。历史记录的 can_fork 标明可选提交 |
| POST /worlds/{id}/author/characters/preview | branch_id、expected_state_version、人物草稿 | 校验地点、人物组件、私有知识及目标。返回一小时有效的 preview_id 和可回看的事件序列 |
| POST /worlds/{id}/author/characters/commit | preview_id | 当前版本仍和预览一致时原子创建人物及附属记录。重复提交返回既有提交 |
| POST /worlds/{id}/author/places/preview | branch_id、expected_state_version、地点草稿 | 作者或编辑者预览地点、已有目的地的路线及有限采集策略。预览不提交世界事件，资源与账户引用必须通过完整状态校验 |
| POST /worlds/{id}/author/places/commit | preview_id | 在预览绑定的分支版本原子创建地点及零余额环境来源账户；附属账户清单须与最终采集策略完全一致。重复确认返回原回执 |
| POST /worlds/{id}/author/organizations/preview | branch_id、expected_state_version、组织草稿 | 预览国家或组织的位置、治理、现存成员、岗位和可选金库，不能通过草稿创造现存资金 |
| POST /worlds/{id}/author/organizations/commit | preview_id | 仅允许原预览账号确认，版本冲突需重新预览。组织与其零余额金库账户在同一事务创建，重复确认不会重复建档 |
| POST /worlds/{id}/narratives/{nid}/rewrite | style | 只改表达，结果事件不变 |
| POST /packs/import/preview | ZIP 原始字节 | 返回包摘要、兼容性和字段诊断 |
| POST /packs/import | 原 ZIP、X-World-Pack-SHA256 与 Idempotency-Key | 校验预览绑定的摘要并创建可游玩的世界，重复提交沿用原结果 |
| POST /worlds/{id}/interventions/preview | patch_proposal | 作者权限，影响预览 |
| POST /worlds/{id}/interventions | preview_id、expected_version | 创建干预事件或分支 |
| GET /models | provider_id | 模型发现与能力信息 |
| POST /providers/{id}/probe | 供应商 ID | 读取供应商模型目录，生成能力另需实际生成验证 |
| GET /operations/calls?limit=&cursor= | 当前身份的调用记录；limit 默认为 100、范围 1–500，cursor 为不透明键集游标 | 按开始时间和唯一调用 ID 稳定倒序返回一页，并提供 next_cursor/has_more；不含提示正文 |
| GET /worlds/{id}/backup | 可选 format_version | 下载单世界归档，包含已提交历史和归档支持的作者资料，不含凭据 |
| POST /restores/preview | 归档的 ZIP 字节 | 核对版本、完整性和回放，返回预览标识及可用规则迁移 |
| POST /restores | 同一归档、Preview-Id 和可选 Target-Ruleset-Version | 确认后恢复为独立世界，原存档继续可用 |
| GET /extensions | 插件清单 | 返回宿主已加载扩展的声明 |

当前本地网页通过 `GET /api/v1/commands/{id}` 和 `GET /api/v1/jobs/{id}` 查询进度和已提交结果。活跃命令与长行动默认每 1.5 秒查询一次，断线后可使用原 ID 恢复查询。按游标订阅事件属于后续接口设计。

旅途中的角色可以停下来补水、进食、休息或等待。已执行的停留时段把该角色的到达和途中风险时刻顺延等量时间，其他世界事件仍按各自的排程结算。旅程的延误事件与对应时钟推进在同一事务提交，可从旧存档继续并随新归档恢复。恢复旅行时在分支锁内核对角色仍在同一条登记路线途中、结算边界尚未错过，且没有待处理的作业决定。原作业从新的检查点取得写入租约，按更新后的到达时刻继续。恢复请求本身不推进时钟，提交时的原请求参数继续用于幂等重试。

多段旅行使用 `POST /api/v1/worlds/{world_id}/long-actions` 的 `activity=travel` 和有序 `route_ids`。请求只提供路线标识、时间窗口及已提交状态版本。服务端从档案读取路线方向、端点、时长和指纹，据此固定截止时间及每段旅程标识。`route_id` 仍用于单段旅行。路线列表、时间窗口或设置发生变化时，复用原幂等键会冲突。

`POST /api/v1/worlds` 的 `npc_autonomy_mode` 支持 `off` 与 `limited_v1`。默认 `off`，显式传入 `off` 和省略字段使用同一创建幂等语义。选择 `limited_v1` 时，建档过程登记有限名额的后续 NPC 目标复审并记录所用策略，世界作业到达复审边界时调用已配置的决策模型。用同一个幂等键切换模式会产生冲突。该创建选项只对新档生效。

年度结算和寿终等事件可在现有世界时刻提交。前端比较 `committed_state_version` 与当前分支版本，更新游玩视图。旧服务缺少版本字段时使用 `committed_through_tick` 判断推进。时间窗口的百分比仍按已提交时钟计算。

在待出发和在途时分别核对路线与持久旅程。抵达事件、分支时钟和作业游标在同一 SQLite 事务提交。后段出发要引用前段的已提交抵达凭证，先前未到达时不会更新路线进度。提交者可从作业接口读取 `itinerary_progress` 中的总段数和已抵达段数。长计划的截止时刻保持用户原授权的值，补给后若余程无法按时完成，作业应停下等待玩家调整授权。

旅程已因遭遇结算为中断，或原授权时窗已无法覆盖剩余路线时，提交者的暂停回执标记 `itinerary_requires_replan: true`。该作业保留停下的时刻和已抵达段数。尝试重新领取旧方案会返回 409。玩家可以从当前实际位置提交新路线。暂缺自理资源等可恢复暂停继续使用原作业控制。

有序行程可显式提交 `itinerary_auto_care=true`，默认关闭。该授权覆盖途中休息和从本人有效库存中取用粮水。后台以现有行动规则编排自理，事务在租约下复核同一角色、同一在途路段、餐饮资源与确切时钟。实际停留和扣账一并提交，剩余各段的最低旅行时间保留在授权窗口内。遇到安全风险、库存不足或剩余窗口不够时，作业在当时边界暂停。

例行工作使用同一路由的 `activity=work` 和 `work_schedule`，字段为本人任职 `assignment_id`、绝对首班 `first_shift_tick`、重复日数 `repeat_every_days` 及可选的精确金额 `balance_target`。发薪仍使用岗位的已登记条款。每笔已提交工资与作业的 `work_progress` 同一事务写入，后者为提交者提供已发班次数、按币种累计工资及最后一班入账后余额。余额和工资使用十进制字符串。

连续行程的 `core.travel_journey_changed.v1` 出发事件在发生路线风险时可携带 `hazard_timing: journey_seed_v1`。该版本按固定的路段旅程标识计算出发与抵达之间的时点，长度为一秒的路段只使用唯一可行的时点。历史事件省略该字段时沿用原中点算法。归约器重新推导并核对遇险时点，停留事件只顺延已登记时点。玩家投影屏蔽未发生的险情时点和算法字段。

私人赠与使用 `core.private_gift_changed.v1`。直接行动接受 `offer_private_gift`、`resolve_private_gift`，后者必须给出同一提议的版本。默认期限为七个世界日。送礼人提出或撤回，收礼人接受或拒绝。接受要求双方仍在登记地点，并按锁内账目和 FIFO 库存确认资源足额。接受事件携带双方账户、库存版本和批次移交凭证，账本逐笔守恒，请求重发不重复结算。玩家投影和历史仅向双方显示提议及结果。世界所有者扮演送礼时可发起 `request_private_gift_reply`，仅可请未被账号控制的 NPC 自行选择。暂缓不提交事件。

聊天里的明确补给命令遇到同刻待结算事项时，返回后端作业 ID 并持久保存原物资、数量、需求类型及提交时刻。工作器在正式租约内先处理当前时刻的规则或需要玩家答复的决定，再重新检查角色状态和物资条件。补给按原参数一次结算。如果库存不足或有效时窗不足，作业停在边界并提供原因。重新发送同一消息会取得同一作业，事务提交后丢失回执也不会重复扣料。

要求 `Idempotency-Key` 的入口将请求键绑定到请求内容。直接提交长动作与世界快进返回 202 和作业 ID。页面以已提交时刻、分支版本、活动类型和等待决策等实际字段显示进度。

消息接口接受可选的 `draft`。`prepare` 新建本地交易或在场人物邀约草稿，`update`、`confirm`、`cancel` 同时提交草稿 ID 与版本。草稿按账号、角色、世界、分支和会话隔离。只有确认后才把审核过的类型化提案交给现有规则，提交前复核场景版本及角色位置，沿用消息幂等键和事件提交事务。已提交结果可以从世界回执找回。草稿保存在本机服务，不随世界备份恢复。

玩家投影可携带已公开且仍在期限内的 `scene_opportunities`，以及在场角色中显式公开的活动时段和自己的近期获知记录 `scene_npc_readback`。公开时间窗不会揭示实际路线、改期或未来地点。调查中的 `investigation` 小节仅由已有类型化观察、可见证词或规则生成的复核记录产生，证据引用留在原玩家作用域。

明确的原地搜查遇到外部边界时，聊天会登记固定时窗与地点。作业先处理该时刻的事件，再继续剩余授权时间。最终观察由玩家和 NPC 共用搜查结算，结束前不能揭示线索。暂停期间由其他操作推进的时间不计入原搜查。

统一错误结构包含 code、message、retryable、field_errors、correlation_id、committed_through_tick。主要错误为 AUTH_REQUIRED、FORBIDDEN、STALE_VERSION、AMBIGUOUS_INTENT、RULE_REJECTED、INSUFFICIENT_RESOURCE、MODEL_UNAVAILABLE、MODEL_INVALID_OUTPUT、PACK_INCOMPATIBLE、JOB_CANCELLED 和 STORAGE_UNAVAILABLE。RULE_REJECTED 是游戏结果或可纠正输入，不自动当服务 500 错误。

有主线世界包可以在介入点声明 `player_profiles`。预览只返回公开资料；导入时可在 `X-Player-Setup` 提供百分号编码的 UTF-8 JSON，字段限于 `profile_id`、`name`、`description`。不允许自行传入组件、能力或账户余额。配置仅作用于新世界的玩家角色。包内的 `food_requirement=not_required` 只移除进食需求，其余需求保持原开局。幂等键绑定归一化的角色配置与介入点。

`POST /api/v1/worlds/{world_id}/messages` 可用 `scene_observation: true` 请求只读场景回执，模式为 `ask_gm`。也可用 `case_anchor_id` 请求为已选的可见线索建立私人案卷，模式为 `action`。两种选项互斥，沿用角色、分支、会话与请求幂等校验；案卷仍由正式规则验证。正文可编辑的自由输入走普通消息流程。

故事关卡的报告配置可以固定收件人与来源要求。剧情条件 `report_registered` 匹配已提交的正式回执及来源类型。界面可以显示该玩家自己提交的回执。既有归档按其保存的普通考核与里程碑定义重放。

## 多人协调接口

`RoomScope` 将房间绑定到租户、世界和分支，`RoomMember` 将成员标识绑定到已认证账号，`RoomSnapshot` 固定世界版本和世界时刻。主持人可为具备现有角色权限的成员签发有期限的 `ActorControlLease`。角色控制权撤销或到期后，旧控制凭据不再允许提交新提案。接续租约使用递增令牌。

`RoomCoordinator` 在一个时间屏障收集同一快照上的提案，校验角色、权限、版本与幂等键。超时可按房间配置等待、记录弃权或跳过未响应的参与者。关闭时重新核对角色权限及租约，按角色标识给出可复现的 `BarrierBatch`。`CanonicalRoomRules` 接收整个批次，按游戏规则处理竞争。实际接入时由主持端持久化回执，核对分支写入租约和世界版本后统一提交。协调契约本身只生成批次。

## 模型和插件协议

ModelRequest 包含 role_task、messages、schema_ref、capability_requirements、model_policy、timeout_policy、trace_context、data_scope。ModelResult 包含 normalized_output、provider_usage、finish_reason、provider_request_id、timings 和 validation。适配器只能取得该请求必要的 secret_ref 对应秘密。

PluginManifest 包含 id、version、engine_range、input_schemas、output_schemas、subscriptions、permissions、resource_limits 和 migration。媒体请求包含 source_event_ids、authorized_description、asset_refs、style、visibility、idempotency_key。插件回传资产引用与 metadata，世界修改必须另外提交受控命令。

## 导入导出和迁移

世界包 ZIP 使用 manifest、JSON 数据和 assets，限制压缩后与解压后大小，拒绝路径穿越、外部可执行内容和无权限远程引用。导入先校验到临时目录，再以事务登记。作者完整导出包含秘密并有明确标签；玩家报告只含已知事实。

世界包 1.x 使用 `manifest.json` 和 `template.json`，可附带清单登记的 `assets/` 本地图片或音频。清单记录 `schema_version`、`pack_id`、`version`、`engine_range`、`title`、`dependencies`、`license`、`content_hash` 和逐文件 `members`。模板提供 `scenario` 开局状态或 `base_pack_id`，可声明基础版本、种子、人口模式和规则版本。未识别的清单或模板扩展字段、已验证的资源均保存在源文件中，导入时不执行。压缩文件上限 16 MiB，解压大小、单文件大小和条目数也单独校验。

`POST /api/v1/packs/import/preview` 接收 ZIP 字节，返回兼容性报告与原 ZIP 的 `archive_sha256`。`POST /api/v1/packs/import` 接收同一 ZIP 和 `X-World-Pack-Sha256`、`Idempotency-Key` 请求头，重新校验并在新世界创世事务中保存源文件。现有存档不参与导入。`GET /api/v1/worlds/{world_id}/source-world-pack` 仅允许作者权限下载原始 ZIP。

已导入世界可通过 `POST /api/v1/worlds/{world_id}/template-updates/preview?branch_id=...` 预览同一世界包更高版本的 ZIP。预览比较源世界的创世模板、选定分支当前状态和新版模板，列出可应用字段、已有更新、冲突和不支持的变更。当前支持人物性格、作息、属性、能力、地点和生产定义。模板与当前分支改动同一字段且结果不同，预览会拦截；其他已有字段变更会保留。实体标识变动、账本与历史变化及不同规则版本需专门的迁移适配。`POST /api/v1/worlds/{world_id}/template-updates` 接收 `preview_id` 和新分支 `label`，按预览时的状态版本、序号和摘要原子创建分支与提交作者事件。提交重试返回原回执，来源分支可继续独立游玩。

存档包包括 manifest、事件分区、快照、规则锁、资产清单与校验和。世界由 ZIP 导入时，备份同时保存 `world-pack.zip`，恢复时验证作者源文件及创世摘要中的来源指纹。将此类备份交给旧版引擎时需要先升级读取器。恢复先在新目录验证引用与状态哈希，成功后切换入口。数据库、备份格式和世界规则分别版本化。恢复旧格式 1.0 后可用 `GET /api/v1/worlds/{world_id}/backup?format_version=2.0` 从独立副本下载新版分段归档，保留世界规则版本、事件和状态摘要。

世界规则恢复默认沿用原版本。`POST /api/v1/restores/preview` 返回已注册升级的目标版本、影响说明和绑定该目标的预览标识。确认请求可附上 `Target-Ruleset-Version` 和对应预览标识。规则 1.0.0 可明确升级到 1.1.0，升级后若世界设定禁用魔法，新的修炼突破会被规则拒绝。普通修习保持原有规则。升级只写独立副本，既有事件仍按当时登记的规则版本回放，初始分支摘要保留。新提交使用升级后的规则，重复确认复用同一个副本。其他版本对需要专门注册适配。

世界数据库遇到暂时的锁竞争时返回 `503 STORAGE_BUSY`，附可重试标识。重发相同聊天请求时先查已提交回执，已有回执直接返回，未提交的请求可用同一标识重试。普通后台世界推进遇到相同错误时可在原租约内重试，过期租约按原有机制重新领取。

## 核心不变量

1. 查询不能改变 world.current_tick、资源、关系和计划队列。
2. 身份控制权和可见范围在服务器强制执行。
3. 没有合法资源来源就不能增加稀缺资源；生产、销毁、税收与发行有专门事件。
4. 人物位置和容器关系合法；移动期间明确标记在途。
5. 事件只应用一次，交易原子提交。
6. 死亡、退役、继承与所有权转移有明确生命周期。
7. 计划不是事实，摘要不是事实来源，言论不自动成为真相。
8. 分支、快照和模型配置版本可以追溯；回放不重新生成决策。

## NPC 计划的内部执行协议

以下是源码内部契约，尚未作为默认世界开关或公开 HTTP 编辑接口发布。

`npc.goal_review` 是版本为 `1.0.0` 的登记排程，载荷只含人物 `actor_id`。每次发生绑定排程 ID、修订、当前世界版本和确定的请求 ID。死亡、玩家控制、无有效自身目标等情况由明确策略跳过；尚在执行的计划不因周期审查被重新抽取。

规划职责为 `NPC_DECISION`，输入仅含本人可知快照、本人目标和可见义务、登记能力及受限参数。返回 `ready`、`defer` 或 `invalid`；供应商失败与非法输出均不能偷偷转成暂缓。`ready` 提案绑定世界、分支、时钟、状态版本及摘要，计划引用本人目标和登记能力。模型输出不能直接生成事实事件。

规划所见的本地已知路线附带 `access_allowed_now`，由与实际移动共用的通行规则计算。已知的受限路线可继续保留为计划背景。市场提供当前公开挂牌、角色自己的可用货币及各挂牌最低量是否在其预算内；供应方库存和他人余额保持私密。第一步仍按实际数量、当前场地、通行和市场条件重新校验。

`NpcPlan` 持久保存人物、目标、修订、步骤、当前步、下一审查时点、尝试与失败、证据引用及可选的在途执行引用。状态为 `ready/running/blocked/completed/cancelled/abandoned`。结构校验只证明引用形状；正式提交还须重算行动前提和有序事件，不能凭一组 UUID 证明执行成功。

后台只在有效租约下用 `simulation.window` 提交 NPC 生命周期事件。模型答复先以状态版本绑定持久保存，再提交世界；崩溃恢复复用同一答复，不能重复调用来重抽选择。提交时重新核对当前控制权、权限、资源、事件顺序与实际旅程收据。重放和归档导入沿原命令语境验证，继承旅程收据只限实际祖先历史及分叉截点之前。

应用拒绝提案时，租约答复可携带固定枚举 `npc_planning_rejection`，例如输出格式错误、目标不属于该角色、步骤非法、首步不可执行。仅 `invalid` 答复允许此字段。作者可见的审查事件在现有 `reason` 字段保存同一代码，并保留实际模型调用标识。提交验证匹配已保存的答复，回放从事件恢复该代码。原始模型解释、验证例外和受限字段值不写入拒绝回执。旧回执缺少代码时仍按原有通用状态重放。

现有瞬时能力可提交原规则事件；迁移通过真实旅程起止与共享世界时钟结算。其他耗时能力返回 `interval_required` 并明确阻塞，等待区间适配；禁止删掉时间事件后提交收益。计划完成只表示该计划的实际步骤完成，不自动把其引用的自然语言目标判为已满足。

启用 `npc_shared_exploration_v1` 的世界额外支持 `explore`。启动修订保存类型为 `exploration` 的在途执行，绑定地点、开始命令、原分支和 600 秒区间。共享窗口处理需求，到期修订保存 `exploration_result` 并清除执行占用。真实发现写入私有事件，搜索无目标也保留尝试收据；检定落空或地点失效可阻塞重试。授权变更后由正式作业记录终止，历史回放依据该终止事件。

启用 `npc_shared_trade_v1` 的世界支持 300 秒交易区间。开始时锁定报价条件，包括市场、货架、卖家、库存账户、品类、币种和单价，购买量来自当前不可变步骤。开始时只记录报价和买家占用，库存和钱款保持可用。到期时复核报价、双方在场、现金和 FIFO 库存。先结算的买家可能用掉存货，后续买家须在同一时点按新存量重新验算。只有成交时才原子提交账户开立、双边交割和计划收据。报价变化、缺货或不在场都可阻塞，失败不扣款。

启用 `npc_shared_training_v1` 的世界支持 NPC 以共享时钟练习本人已登记技能，单次上限八小时。开始记录地点、技能、起止时间及命令绑定，然后占用该人物。到期核对地点与技能并复用玩家训练的成长计算，同时提交技能、进度和 `training_result` 收据。世界推进期间的需求和时间由同一模拟窗口结算。角色转入账号控制、退役，或地点失效时训练可以终止，未结算的练习不产生技能收益。规则默认关闭。

启用 `npc_shared_production_v1` 的世界支持共享时钟生产。开工核对人物与工坊同场、登记配方、本人技能、原料、批次、产出配额及持续时间，记录人物占用和配方条款摘要。原料和额度在完工前仍可被其他有权限的行动使用。到期按当前状态重算原料转换并核对原配方。多名工人竞争同一库存时按既定提交顺序判定，缺料、地点失效或配方变化产生失败收据。成功时原子提交原料耗用、有限配额出库、产出批次和 `production_result`。计划证据直接引用产出和收据。规划只看到工坊的可见登记配方概要，不获知原始库存或他人账户。规则默认关闭。

启用 `npc_failure_review_v1` 的世界能在计划修订中同时记录 `automatic_review_tick`，原子追加一次本人复审。触发依据为实际受阻、旧复审已执行完毕、仍有效的本人与目标。排程沿用原 ID；已在队列中的复审优先。首次追加沿用重试时点，后续至少间隔一个世界小时和 120 秒两者中的较大值，总运行上限 13 次。回放重新计算时点并更新排程。

`speak` 是零耗时的定向言论能力，每次最多八名同场可见接收者。每对分别形成私密知识记录，发送人保留相应收据，第三方和其他接收人不因此获得名单或内容。记录是人物原话，不自动成为事实、关系变化或接收者的回答；后者必须独立决定。

拥有者可在人物创建预览中附带复审次数和间隔，也可用 `/api/v1/worlds/{world_id}/author/npc-reviews/preview` 与同路径的 `commit` 为已有活跃目标、未被账号控制的 NPC 安排复审。首次到期在当前时刻加一个世界日。既有排程仍在排队时拒绝重复安排，次数有上限。提交绑定作者、分支、命令与预览版本，排程事件仅作者可见。到期后由正常世界推进作业处理。

## 本人已结算休息与移动

GM 问答可读取本人最近六次已提交的直接休息。每条来自受控角色的 `action.rest` 命令及其提交的时间事件，给出开始、结束、持续秒数和可引用事件 ID。读取截至本次查询所见的提交序号，沿分支祖先的实际分叉位置截断。玩家只获知自己控制角色的收据。排队、请求意图和人物口头许诺均不能产生这类收据。休息时段本身仅证明实际耗时，入住、付款和需求恢复分别依据各自事实。

单次结算的移动由 GM 从当前分支及继承历史读取最多四条，仅限本人结算记录，且受查询检查点限制。回顾可引用移动事件、两个本人到访过的地点名称及起讫世界时刻。分段旅行仍从旅程状态读取。两类信息均由只读查询提供。

## 约会后的私人感受

`record_meeting_reflection` 使用邀约 ID 和本人选择的感受，取值为 `closer`、`unchanged` 或 `uneasy`。双方登记出席且时段结束后，在任一方结束邀约前，每位参与者可保存一次。事件 `core.meeting_reflection_recorded.v1` 绑定命令、分支、行动角色和规则版本，由规则计算记录时点。玩家读档和历史仅向记录者显示这条感受。完整作者备份按既有规则保存事件。邀约状态、对方感受和世界时钟保持原值。

`request_meeting_reflection` 只接受邀约 ID。符合条件时，托管 NPC 根据自身能知道的资料选择一种感受或暂缓。暂缓不产生事件。落档的事件在 `requested_by` 记录请求者，在 `actor_id` 记录自行作出选择的 NPC。请求响应与回执都使用固定文字，玩家视角不会返回感受或事件。以后与同一角色交谈时，该 NPC 可依据自己的感受采取不同的交谈方式。

## 伴侣关系

`propose_relationship_partnership` 由一名在场的成年角色向另一名在场的成年角色提出。审批中的记录只向两名参与者开放。接收方可明确同意或拒绝，提出方可撤回，任何一方都可明确结束已成立的关系。接受时须满足在场与成年条件。每对角色至多有一条待回应或已成立的记录，不同对象分别结算。

提议可附 `proposal_terms`，最多六段，每段 400 字以内。自然语言处理只接受能按顺序在玩家输入中核对的原话；网页专用输入将明确交给对方的条件直接带入提议。提议条件随原记录对双方可见，关系告知不转交条件。没有条件的旧记录继续省略该字段，保留原存档摘要。

`resolve_relationship_partnership` 绑定关系 ID、动作与当前修订号。托管角色的 `request_relationship_partnership_response` 只接受 ID 和修订号，按原提议的条件独立选择同意、拒绝或暂缓。由玩家控制的接收方须自行提交决定。提议条件在状态转换后保留，暂缓和对白不会改写提议。关系成立须经明确同意，未写明的排他性或任何具体行为不能从关系状态推定。

`core.relationship_partnership_changed.v1` 将每次有效变化绑定到提交命令和确定性版本。回放重算资格、变化和可见性。暂缓无事件。

### 定向告知关系

参与者可以通过 `disclose_relationship_partnership` 明确把自己已成立的关系告知一名同场可见的第三人。行动绑定关系 ID、当前修订号和接收人。核验在场、角色状态、控制权及关系参与资格后，`core.relationship_disclosure_recorded.v1` 保存一条接收人持有的知识记录。记录的来源是实际告知者，包含地点和发生时点。原关系档案仍只向两名关系参与者开放。同一告知者不能重复向同一人物告知相同修订，伴侣可以独立作出自己的告知。

接收人获知的是某人对其自身关系的陈述摘要。伴侣的态度、同意、约定、其他关系和完整档案均不随告知传递。后续问答和对白区分告知时所闻与当前已证实的状态。接收人投影引用自己的知识记录 ID，原档案 ID 与修订号仅供内部来源核验。未被告知的在场人物沿用自己的视角。成功告知只写入此次知识获取事件，世界时钟及关系档案保持原值。

## 到货粮食发放

`distribute_shipment_relief` 接受货运、当地政治体、领取人和精确数量。行动者须是该政治体现任领导，且与领取人在同一粮仓。规则只从指定货运当前仍有效的收货批次拨粮，同时更新两方账本和库存。新批次保留品质和过期时点，可进入领取人平常的进食流程。发放历史的玩家投影限于行动者和领取人。

## 更正公开指认

`correct_public_accusation` 允许指认者本人当面撤回已由案卷排除的公开指认。动作绑定已关闭案卷 ID、修订号及原结案事件 ID。存储验证原事件属于当前分支历史，核对指认者、被指认人、公开状态和结论。双方均需在场，每份案卷最多登记一次更正。

`core.investigation_public_accusation_corrected.v1` 记录实际更正的时间与地点。原案卷、原指认和此前信任变化保留。案主界面显示更正，证人后续对白可以引用亲耳听到的更正并表达自己的看法。更正行动只追加该事件，耗时为零。

当面更正后，`offer_accusation_repair` 允许原指认者把一段自己的原话同场交给对方。解释进入待回应状态。`resolve_accusation_repair` 由收信人的控制者选择接受、拒绝或暂缓。原指认者可用 `request_accusation_repair_response` 请无人类玩家控制的 NPC 自行决定。暂缓保留待回应状态，回复会写入历史。

对方明确接受时，信任恢复取决于原结案提交中有证据支持的实际降幅。规则最多恢复原损失的三分之一向下取整，并以原信任值为上限，只调整收信人对指认者的信任。案卷、更正及解释分别保留，解释和回应的玩家投影限于双方。

## 玩家角色接续

分支所有者在当前玩家角色已经死亡时，可用 `GET /continuation-options` 查询可接续的活跃角色，通过 `POST /continue-after-death` 选择角色。命令核对版本、前任角色、继承的死亡事件和角色控制权。事件 `core.player_character_continued.v1` 以该死亡事件为因果来源，只在当前分支改变玩家角色指针。重复提交返回原收据。

## 遭遇停战

`core.combat_truce_changed.v1` 记录同场交战两方的提议、明确接受或拒绝。提议绑定当时的交战 ID、交锋次序及最近交锋事件。新交锋、撤退或角色离场会使旧提议失效。明确接受才将双方的该次交战原子标记为已解决。拒绝仍保留交战，双方的地点、伤势和世界时间保持。记录仅对双方可见。

房主可以请求未受玩家控制的 NPC 独立回应。模型只返回接受、拒绝或暂缓，实际提交重新核对提议、当前分支版本和人物控制权。暂缓保留原提议。事件以交锋或提议为精确因果来源。
