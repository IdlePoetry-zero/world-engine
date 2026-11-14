# 首轮集成契约

当前文档约束并行实现，补充 docs/03 的实际接口。接口缺失时 UI 显示未连接或不可用，不返回伪造成功。

## 运行结构

Python 包位于 backend/world_engine，领域契约由 foundation 子任务实现。前端所有请求使用 /api/v1，开发代理只允许本地 API。生产网页由本地 API 静态提供。模型调用在短数据库事务之外执行。

状态对象以 entities 映射为中心，位置和人物是不同 kind；资源账、关系、知识、目标和日程独立。世界响应必须经过角色可见投影，存储 read_world 的内部完整状态不能直接泄漏到玩家端。

## 第一轮前后端端点

| 接口 | 统一结果 |
| --- | --- |
| GET /session | principal 包含 id 和 display_name；permissions 数组 |
| GET /worlds | items 包含 world_id、title、active_branch_id、current_tick、state_version、status |
| GET /packs | items 包含 pack_id、title、version、description |
| POST /worlds | 输入 pack_id、title、player_name；创建后返回世界或持久作业引用 |
| GET /worlds/{id}/view | world、branch、state、last_seq、state_hash；state 为授权投影 |
| GET /worlds/{id}/branches | items 包含 branch_id、label、current_tick、state_version |
| GET /worlds/{id}/history | branch_id 和 after 游标；返回 items 和 next_cursor |
| POST /worlds/{id}/messages | message_id、kind、content、world_effect，以及可选 command_id、job_id、committed_tick |
| GET /providers | 安全配置 items；不包含秘密 |
| POST /providers | 新建 kind、name、base_url、secret_ref、enabled、default_model |
| PUT /providers/{id} | 更新已存在的安全配置 |
| GET 或 PUT /settings | speed、depth、routes、local_only |

routes 按 role_task 映射 provider_id 和 model。使用 secret_ref 的 API 必须限制为应用注册的凭据引用，不能允许任意读取宿主环境变量。认证和权限不能因本地模式而省略。

## 事件和读写隔离

GM 查询可以保存会话与调用记录，但 world state、tick 和世界事件序列不变。世界命令必须携带幂等键、主体控制权、world/branch scope 和 expected_version。

UI 不得把数据库内部 state_hash 当作可随意计算的前端签名。显示世界版本用于避免基于旧状态误操作；发生版本冲突时重新读取并解释，不自动重放原有不可逆行为。

## 参考与采用

采用 FastAPI lifespan 管理应用资源的初始化与释放，显式管理数据库事务而不让网络请求持有事务锁。相关依据为 [FastAPI 生命周期](https://fastapi.tiangolo.com/advanced/events/) 和 [SQLAlchemy 事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)。

角色与地点采用不同实体类型，后台系统不伪装成不可见 NPC，参考 [Evennia 对象设计](https://www.evennia.com/docs/latest/Components/Objects.html)。世界书用于选择上下文，不承担权威状态，依据 [SillyTavern World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)。以上为架构借鉴，未复制源码。

开发时进一步阅读了 [AI Town abstractGame.ts](https://github.com/a16z-infra/ai-town/blob/main/convex/engine/abstractGame.ts)：其步骤处理中将消费输入、推进时间和保存结果关联，并检查引擎代数及已处理输入。本产品采用相似的一致性原则，使用数据库中的分支版本、命令幂等键和 worker fencing token 实现；不采用其固定实时 tick 作为百年模拟策略，也未复制代码。
