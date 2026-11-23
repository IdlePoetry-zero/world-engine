# 万象游玩与工程优化验收

基线为 `9be63a1`。本轮用独立存档、真实浏览器或正式 API 验证。自动测试包含受控模型；真实调用单列。

全量后端回归为 2614 项通过、1 项跳过。收尾变更另跑相关定点回归，包括最后时刻的搜查结算、草稿提交后的断电式回执恢复和显式源库配置下的备份恢复。前端组件测试 14 项通过，lint 和生产构建通过。

## 改动与试演

| 路径 | 证据 |
| --- | --- |
| 红雾开场 | Chrome 在桌面和 390 像素窄屏显示线索、对象、期限与路线。准备 GM 草稿保持开场时钟。正式集成用例比较只对白、实际登记、错过调查的结算。 |
| 守矢交涉 | 使用实际导入的包和三个同起点分支。交涉报告在第 12 小时 15 分带来 4 份口粮，山道核查改变玩家实际通行权限，实走近道用 75 分钟。旁观分支保持原通行和库存。三支创世重放一致。使用受控 NPC 模型。 |
| 现代游玩 | Chrome 在现代档的分支从场景路线准备结构化委托，实际推进 3,600 秒，主分支仍在原处。重开分支仍显示目的地。正式 API 对照实际赴约与错过第二班，错过班次零薪，原计划继续后达到 30 银币的购买门槛，另行购买才扣账。真实 DeepSeek Flash 将咨询判为假设，保持时钟与作业队列。另下明确行动后，正式作业推进 30 世界日，实际结算 30 班及 375 银币，重放一致。 |
| 古代长途 | Chrome 从封建背景开局提交两段旅程，第 115200 秒实际到达途中地点，重载后仍能选择继续或停留。停留期间的 GM 查询保持时钟，随后恢复原作业并保留两段抵达记录。正式 API 另验证 360 日时间窗在第 120 日实际抵达时交回选择。通行协定到期后，两支在第 82 日续约直达和第 94 日绕路，均保留首段回执。 |
| 多轮行动及调查 | 交易补充整笔限价、邀约补充时段与场地，只有明确确认会提交。重发同一请求复用结果。Chrome 重载保留待确认草稿，确认前版本 0，确认后版本 1、时钟 300 秒。调查日志按有权看到的记录显示观察、证词和复核来源，公开指认由玩家自己判断。 |
| 隐私 | 隔离 SQLite 环境和 Chrome 双标签页交错写入隐私、速度、深度和备用。观察到 3 次版本冲突后按此次修改重试，设置保留。键盘能操作隐私开关与云端确认；仅一次明确目录查询发出请求。传输由 MockTransport 接管。 |
| 旧包兼容 | 幻想乡 1.1.0 和《回灯之约》1.2.0 通过新引擎的导入及创世重放。《回灯之约》重建归档与原版本逐字节相同。 |
| 备份副本 | Chrome 导出后预览 2 条分支和 17 个事件。独立启动副本，核对第 115200 秒的归档摘要与分支一致。浏览器试演发现显式配置源数据库 URL 时，迁移会误用源地址。修复后，Chrome 和 HTTP 分别在这两种配置下验证恢复，源分支摘要保持原值。 |

## 复现入口

- 构建幻想乡 `1.2.0`。在仓库根目录运行 `./.runtime/venv313/Scripts/python.exe -B scripts/build-gensokyo-reference.py --playable-world`。本机产物 `.runtime/world-packs/gensokyo-world-1.2.0.zip`，SHA-256 为 `923d94c7b2c92242ccced94cec83e21d169e2cca61a163fc6097aa40dcbd7d9f`。山道近路、物资与公开作息是世界包模拟内容。
- 使用上述 Python 运行 `-B -m pytest -q -p no:cacheprovider`。重点用例位于 `test_gensokyo_live_play.py`、`test_market_price_ceiling_dispatch.py`、`test_npc_relationship_reply.py`、`test_visibility.py`、`test_travel_chat.py`、`test_waypoint_opportunity_jobs.py`、`test_long_itinerary_treaty_branch.py` 和 `test_operations.py`。
- 在 `frontend` 运行 `npm run lint`、`node --test src/components/*.test.mjs`、`npm run build`。持续集成执行相同组件命令。

本机脱敏记录可在 `.runtime/evidence/settings-privacy-two-tab-20261124.json`、`.runtime/evidence/optimization-real-month-work-20261124.json`、`.runtime/scene-play-20261124-X6EsYZ/evidence.json`、`.runtime/optimization-draft-GXSK6Q/evidence.json` 及 `.runtime/scene-feudal-backup-20261124-1795451425656/` 查看。日志、会话、浏览器状态与凭据留在各自隔离目录。
