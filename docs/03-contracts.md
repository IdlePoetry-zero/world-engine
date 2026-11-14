# 世界数据与接口规范

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

模板更新默认仅影响新实例；现有实例更新通过 AuthorIntervention 生成影响预览和新分支。秘密字段可标记 author_only、known_by 指定角色或需要观察事件才能获知。

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

基础路径为 /api/v1。世界相关路径隐含当前租户，服务端每次验证 membership；UUID 难猜不是授权机制。

| 方法和路径 | 请求或响应核心 | 语义 |
| --- | --- | --- |
| GET /session | principal、permissions | 本地或未来认证统一入口 |
| POST /worlds | pack_version、initial_options | 异步创建并校验世界 |
| GET /worlds/{id}/view | branch、actor、version | 仅返回授权投影 |
| POST /worlds/{id}/messages | GM 输入信封 | 回答或返回命令作业 |
| POST /worlds/{id}/commands | ActionRequest、expected_version | 明确的世界行动入口 |
| GET /commands/{id} | status、result、committed_through_tick | 幂等恢复与结果查询 |
| POST /jobs/{id}/control | pause、resume、cancel | 检查点控制，返回实际状态 |
| GET /jobs/{id}/events | after_cursor | SSE 断线续传，不泄露全知事件 |
| POST /worlds/{id}/branches | checkpoint、label | 保存历史前缀，创建新分支 |
| POST /worlds/{id}/narratives/{nid}/rewrite | style | 只改表达，结果事件不变 |
| POST /packs/validate | draft | 返回字段路径、严重度和修复建议 |
| POST /packs/{id}/publish | version、content_hash | 固定版本，处理冲突 |
| POST /worlds/{id}/interventions/preview | patch_proposal | 作者权限，影响预览 |
| POST /worlds/{id}/interventions | preview_id、expected_version | 创建干预事件或分支 |
| GET /models | provider_id | 模型发现与能力信息 |
| POST /providers/{id}/probe | probe_profile | 不返回密钥，区分列举与生成测试 |
| GET /operations/calls | filters、cursor | 用量与错误信息，默认无正文 |
| POST /backups | world_ids | 异步备份，不包含凭据 |
| POST /restores/preview | archive_ref | 验证版本、完整性与目标 |
| POST /restores | preview_id、destination | 恢复副本，成功后切换 |
| GET /extensions | capabilities | 插件状态与权限 |

创建和变更请求使用 Idempotency-Key；相同 key 相同 payload 返回同一命令，相同 key 不同 payload 返回 409。长任务返回 202 和 job_id；即时查询返回 200。流事件包括 accepted、classifying、waiting_input、planning、simulating、committed、narrating、completed、failed；百分比未知时报告已处理 tick 和队列量，不制造虚假进度。

统一错误结构包含 code、message、retryable、field_errors、correlation_id、committed_through_tick。主要错误为 AUTH_REQUIRED、FORBIDDEN、STALE_VERSION、AMBIGUOUS_INTENT、RULE_REJECTED、INSUFFICIENT_RESOURCE、MODEL_UNAVAILABLE、MODEL_INVALID_OUTPUT、PACK_INCOMPATIBLE、JOB_CANCELLED 和 STORAGE_UNAVAILABLE。RULE_REJECTED 是游戏结果或可纠正输入，不自动当服务 500 错误。

## 模型和插件协议

ModelRequest 包含 role_task、messages、schema_ref、capability_requirements、model_policy、timeout_policy、trace_context、data_scope。ModelResult 包含 normalized_output、provider_usage、finish_reason、provider_request_id、timings 和 validation。适配器只能取得该请求必要的 secret_ref 对应秘密。

PluginManifest 包含 id、version、engine_range、input_schemas、output_schemas、subscriptions、permissions、resource_limits 和 migration。媒体请求包含 source_event_ids、authorized_description、asset_refs、style、visibility、idempotency_key。插件回传资产引用与 metadata，世界修改必须另外提交受控命令。

## 导入导出和迁移

世界包 ZIP 使用 manifest、JSON 数据和 assets，限制压缩后与解压后大小，拒绝路径穿越、外部可执行内容和无权限远程引用。导入先校验到临时目录，再以事务登记。作者完整导出包含秘密并有明确标签；玩家报告只含已知事实。

存档包包括 manifest、事件分区、快照、规则锁、资产清单与校验和。恢复先在新目录验证引用与状态哈希，成功后切换入口。数据库升级与内容规则升级分别版本化；不兼容的规则升级生成迁移报告和副本，不修改唯一原件。

## 核心不变量

1. 查询不能改变 world.current_tick、资源、关系和计划队列。
2. 身份控制权和可见范围在服务器强制执行。
3. 没有合法资源来源就不能增加稀缺资源；生产、销毁、税收与发行有专门事件。
4. 人物位置和容器关系合法；移动期间明确标记在途。
5. 事件只应用一次，交易原子提交。
6. 死亡、退役、继承与所有权转移有明确生命周期。
7. 计划不是事实，摘要不是事实来源，言论不自动成为真相。
8. 分支、快照和模型配置版本可以追溯；回放不重新生成决策。
