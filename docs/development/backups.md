# 世界备份归档（开发状态）

世界备份归档经过访问控制、格式检查和确定性回放验证。所有者和编辑者可以从历史界面下载备份。恢复由预览和确认两步组成，成功后页面提供在独立端口启动副本的命令。

归档记录各分支已提交的世界状态、事件与必要的回放信息。仍在排队、暂停或等待决定的本机作业需要在副本中重新安排；本机对话、交易与邀约草稿、模型设置和凭据按各自的本机存储管理。导出前如要保留某段尚未结算的进度，请先完成作业并核对最新提交。

服务使用 `WORLD_ENGINE_DATABASE_URL` 时，恢复仍在生成的独立数据库上迁移和导入。若从设置过该变量的 PowerShell 打开副本，请先在该窗口移除变量，再运行页面生成的独立数据目录启动命令。

## 当前接口

- `GET /api/v1/worlds/{world_id}/backup` 将归档写入暂存文件，以分块响应下载。发送完成或客户端断开后关闭文件。
- `POST /api/v1/restores/preview` 在写入前校验并回放归档，返回摘要和预览 ID。
- `POST /api/v1/restores` 重新校验上传的归档，并核对预览 ID 后创建独立副本。

`world_engine.application.backups.BackupApplication` 提供：

- `export_world(scope, world_id) -> bytes`：只允许世界 owner/editor。归档是确定性 `.zip` 数据流，不在服务器临时目录解压。
- `validate_archive(archive_bytes) -> VerifiedWorldBackup`：对不可信归档执行成员安全检查、schema 验证、分支和提交边界校验、事件回放及精确状态哈希校验；不写数据库。
- `VerifiedWorldBackup.summary()`：返回预览所需的标题、源 ID、分支和事件数量、归档摘要及分支状态哈希，不包含世界状态正文。
- `WorldBackupImporter(destination_repository).import_verified_backup(scope, verified_backup)`：只接受空隔离世界库；先再次回放和校验，再在一个数据库事务中写入世界、分支、批次快照、事件和派生账本记录。重复 ID、已有世界、约束失败或行校验失败都会回滚。返回 `BackupImportResult.to_dict()`。

验证结果就是后续导入器的最小类型化数据契约：

```text
VerifiedWorldBackup
  archive_sha256, format_version
  world: world_id, title, active_branch_id, ruleset_version, pack_version, created_at
  branches[]:
    branch_id, name, parent_branch_id, fork_seq, head_seq
    initial_state_version, state_version, current_tick
    initial_state_hash, state_hash, created_at
    initial_state: WorldState
    events[]: EventRecord
```

导入器在独立、临时的存储命名空间中保留 world/branch/event ID，逐批重放记录并重新检查分支投影和哈希；将原数据库保持不变。归档的 active branch 会随 world header 在同一事务写入。运行时负责独立数据目录的创建，完成验证后返回副本位置和启动参数。导入目标必须是空数据库。

事件表引用的 `command_id` 需要本地 `CommandRow` 承载。由于归档刻意不带原始命令正文，导入器为每个已验证提交批次写一条明确标记 `source_request_payload_available=false` 的合成快照，保留原 command ID、actor、序号、事件引用、结果哈希和提交时间。读取回执时从已恢复的事件行重建完整事件，不冒充原始用户请求。每个分支会写 fork 初态快照和 head 快照。不会恢复旧 outbox、jobs、租约、provider 配置或调用记录，因此历史事件不会被重新派发或续跑。

## 归档布局

归档标识为 `world-engine.world-backup`。新建小型归档默认使用兼容布局 `1.0`；当某分支的完整事件数组超过单成员限制时，自动切换到分段布局 `2.0`。调用方也可以通过 `export_world(..., format_version="1.0" | "2.0")` 显式选择布局。读取器同时支持两个版本，并在回放前将 2.0 事件段按序展平为同一个 `VerifiedBranch.events` 契约。归档只接受明确列出的文件：

- `manifest.json`：归档格式、world ID，以及每个数据成员的 UTF-8 字节数和 SHA-256。
- `world.json`：不含 tenant/owner 身份的世界头信息。
- `branches.json`：分支树、fork/head 序号、版本、tick、哈希和成员路径。1.0 使用每分支一个 `events_path`；2.0 使用有序的 `event_segments[]`，记录每段路径、首尾序号和事件数。
- `checkpoints/{branch_id}.json`：每个分支 fork 边界的完整 `WorldState`。根分支从序号 0 开始。
- 1.0 `events/{branch_id}.json`：仅该分支在 fork 后的本地事件。父分支历史不会重复复制到子分支。
- 2.0 `events/{branch_id}/{first_seq}-{last_seq}.json`：连续的本地事件段。导出只在完整命令提交边界拆分；每个段的字节长度和 SHA-256 由通用 manifest 覆盖。

导出在开始读取历史时固定各分支的已提交序号。后台推进随后追加的提交留在原存档，可在下一份备份中查看。并发回归在固定分支序号后追加 SQLite 提交，并验证归档的历史与哈希仍对应原序号。

每批记录保留 `command_id`、因果引用、actor、事件、顺序、提交时间、tick、state version 和 `commit_boundary`。源 event IDs 与分支 IDs 保持原值。验证器要求序列连续、批次不得被截断、每批版本只递增一次、fork 落在已提交边界；它在 fork checkpoint 上校验父历史，再使用当前注册 reducer 回放每个本地事件并核对 tick、state version、终态 hash。未知 major/格式版本或未知事件类型拒绝验证，不会尝试猜测迁移。

## 不包含的数据

归档不读取 provider/模型设置或 OS credential store；不写入凭据、secret reference、session、GM 会话、调用/价格追踪、jobs、command 请求或 idempotency 数据。专用的 provider credential/config key scanner 会在导出和导入时拒绝意外进入 world state 的字段；未知 JSON 字段也由严格 schema 拒绝。

作者完整存档含世界作者可见信息和角色秘密，必须按敏感世界数据保管。导出授权限制为 owner/editor。面向玩家的部分可见报告不是此格式，今后应另外定义投影和泄密校验。

## 不可信归档边界与限制

当前默认上限是压缩后 128 MiB、展开后 512 MiB、最多 50,000 个 ZIP 成员、单成员 32 MiB、压缩比 200:1、事件总数 1,000,000。该容量值尚未通过 600 年长跑存档的规模测试；超过上限时需报告失败并保留原世界。

HTTP 恢复上传逐块计数，超过压缩体积限制时返回 413。超过 1 MiB 的上传暂存到临时文件，校验时分块计算归档摘要并按成员读取，完成或失败后关闭临时文件。验证结果仍保留类型化事件供回放和导入。

验证器不提取路径到文件系统。它拒绝路径遍历、绝对/盘符/反斜杠路径、重复项、未知成员、目录、软链接和特殊文件、加密项、未知压缩算法、ZIP comments/extra fields、超限成员、过高压缩比、重复 JSON key、非有限数和成员摘要不匹配。任何校验或回放错误都会在写入之前停止。

## 验证范围

`tests/api/test_backups.py` 使用临时 SQLite 世界验证 owner/editor 范围、分支本地事件、批次边界、父 fork checkpoint、准确哈希、命令正文不导出，以及损坏成员、恶意路径、重复名称/键、未知配置成员和篡改后回放不一致的拒绝行为。`tests/api/test_backup_restore.py` 验证隔离目标恢复后 ID/哈希不变、事务写入完整、命令正文缺失标记、源库未变、无 outbox/jobs/leases，以及非空目标和被篡改验证对象在写入前被拒绝。

## 上游参考

- Python 3.13 `zipfile` 文档：成员元数据、`ZipInfo` 属性和不可信归档提取风险说明 <https://docs.python.org/3.13/library/zipfile.html>
- Python 3.13 `json` 文档：`object_pairs_hook` 和 `parse_constant`，用于拒绝重复 key 和 NaN/Infinity <https://docs.python.org/3.13/library/json.html>
## 分段格式的运行边界

实际现代世界快照在 tick 298080000 包含 492 个事件。两段压缩归档总计 11102048 字节，完成独立校验、因果与状态回放、隔离导入和重开，源与恢复哈希一致。测试时导出、校验、恢复分别约 401、420、468 秒，性能样本来自当时的进程版本和系统负载。后续优化尚未在同一归档上重新测量。结果位于 `.runtime/evidence/modern-decade-history-v2-restore.json`。

2.0 已实现单成员上限和分段序号/命令边界检查；损坏或被重新哈希的非法范围仍会在回放前拒绝。导出逐事件编码一次，用这些字节的长度选择 1.0 或 2.0 布局，2.0 使用累计字节数确定分段。导出当前仍会把固定分支范围的事件及归档成员留在内存中，因此 2.0 HTTP 下载的输出流尚未覆盖端到端的导出阶段，也没有长跑归档内存峰值或恢复耗时验收。后续优化应保留固定分支末端、已验证的完整命令边界和当前 typed replay/import 契约，并测量原始大小、压缩大小及恢复峰值。

SQLite Online Backup API 可用于本机整库快照和验收检查点。整库复制会包含其他世界和本地账户数据，因此面向单世界的可移植归档仍采用明确的记录投影和权限检查。两种路径均可使用现有数据库接口，本项目未复制示例代码。[SQLite Online Backup API](https://www.sqlite.org/backup.html) · [SQLite VACUUM INTO](https://www.sqlite.org/lang_vacuum.html)
