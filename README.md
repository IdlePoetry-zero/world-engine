# 万象世界引擎

设计基线为 1.0。当前版本可在本机启动并试玩。完整验收进度见 [需求与验收证据](docs/development/acceptance-matrix.md)。

时间推进、跨地点剧情与世界包复用的复现步骤见 [改造验收记录](docs/development/engine-improvement-acceptance.md)。

产品面向个人本地使用，以自然语言与 GM 沟通，支持多背景设定、自定义关键 NPC、完整 RPG 玩法和长期社会模拟。账号、多人协调及媒体扩展保留明确接口。所有玩法均在本次交付范围内，实施顺序不等于功能删减。

## 本地启动

第一次在 Windows 上运行时，请在项目根目录按步骤安装 Python 和前端依赖，再启动服务。[首次安装](docs/development/runtime.md#首次安装)

环境已就绪时，在项目根目录打开 PowerShell，运行下列命令并保持服务窗口打开。只有网页尚未构建或前端源码更新后才需重跑第一条。

```powershell
npm --prefix frontend run build
.\.runtime\venv313\Scripts\python.exe -m world_engine.runtime
```

打开 [本地世界引擎](http://127.0.0.1:8000)。模型连接、密钥和各任务使用的模型可在网页的「模型与运行」设置。默认世界库位于 `.data`，运行独立恢复副本时可通过 `--data-directory` 和 `--port` 指定实例。

建档、模型设置、GM 模式、人物编辑、长行动、分支和备份恢复的操作步骤见 [本地用户操作指南](docs/development/user-guide.md)。

当前本机试玩验证覆盖已有存档加载、三背景建档和真实云端及本地模型接入。复杂指令授权和跨玩法后果的代表性实玩、NPC 一致性评分进度、常规读取和超长存档耗时均见[验收矩阵](docs/development/acceptance-matrix.md#交付判断中的关键证据)。

## 阅读顺序

| 文件 | 用途 |
| --- | --- |
| [产品需求](docs/01-PRD.md) | 产品范围、玩法、用户流程、页面和质量要求 |
| [架构设计与架构图](docs/02-architecture.md) | 模块边界、运行链路、部署、账号与插件架构 |
| [数据与接口规范](docs/03-contracts.md) | 数据对象、命令、事件、API、权限与版本规则 |
| [GM 与长期模拟](docs/04-simulation.md) | 输入意图、有限裁定、人物决策、600 年模拟设计 |
| [模型与运行后台](docs/05-model-operations.md) | 多模型、速度与深度、费用、观测、恢复及本机检查 |
| [开发与验收计划](docs/06-delivery-validation.md) | 工作包、依赖、追踪矩阵、测试方法与完成门槛 |
| [开源参考与设计决策](docs/07-research-decisions.md) | 12 个项目的借鉴点、来源及关键架构取舍 |

架构图可直接查看 [PNG 总览](docs/diagrams/architecture-overview.png) 或 [可编辑 SVG](docs/diagrams/architecture-overview.svg)。架构和模拟文档另含 5 张 Mermaid 图，独立源文件位于 docs/diagrams。

文档检查结果见 [设计资料检查记录](docs/08-design-review.md)。

## 已确定的产品边界

- 首版单人本地网页，持久化账号身份、世界成员与角色控制权接口。
- 云端 API 和 Ollama 均支持；云端基准模型为 deepseek-flash。密钥不进入此文档包。
- 人物关系、日常生活、探索调查、经营生存、势力政治、战斗成长均要求完整闭环。
- 自由输入先判断是在问 GM、讨论假设、扮演对话、执行行动还是编辑设定。
- 允许受限临时裁定，不允许模型绕过世界硬规则和资源账本。
- 设计容量为 24 个国家、24 个其他大型组织、32 名关键 NPC、240 名同时存续具名人物，长测方案为 600 年。本轮采用已有长期存档的实际连续审计区间。历史人物总数可随模拟增长。 [容量验收记录](docs/development/acceptance-matrix.md)
- 速度与深度分别设置；测试不设置用户费用或总时间预算上限，但具有取消、检查点和异常循环保护。

## 设计承诺和验证边界

状态一致性、权限和可恢复性是硬要求。自然语言体验、社会涌现和模型对秘密的处理需要实测评估，不以架构名称保证质量。600 年按分层事件模拟执行，不承诺逐秒模拟每个人的一生，也不把一段生成的历史摘要算作模拟通过。

账号登录界面、实时多人房间、语音和图片生成的完整产品实现不属于本次功能交付；其数据、协议及可执行契约测试属于本次交付。个人用户的本地身份、权限和数据隔离必须实现。

## 当前外部条件

DeepSeek Flash 和本机 Ollama 已完成真实调用验证，具体用例和覆盖范围见 [开发状态](docs/development/status.md)。Ollama 从既有项目复用模型文件，在独立运行目录保存配置与日志，步骤见 [运行时记录](docs/development/runtime.md)。

NPC 人物一致性已用原有 90 条真实试演抽查。发现的事实与动机问题已修复并在对应案例复核。硬件、模型和长历史性能按实际测量记录。[交付验收矩阵](docs/development/acceptance-matrix.md)
