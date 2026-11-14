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

本轮没有复制这些项目的源码或素材，也不新增其运行依赖。若后续决定复用，必须固定具体 commit、文件和对应许可证，记录代码、模型权重、数据和图像素材各自的授权；仓库总体许可证不能替代逐项核对。AGPL、GPL 等要求应在实际复用方式确定后审查，不在当前设计中假定可随意嵌入。

研究结论基于 2026 年 11 月 15 日读取到的资料与前序调研。实现前对拟复用或作为依赖的项目重新固定版本；设计参考项目不需要全部安装。
