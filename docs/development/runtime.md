# 隔离运行时与版本验证

2026-11-15 已在项目内安装 Python 3.13.18，实际查询 SQLite 为 3.53.1。路径 `.runtime/python/cpython-3.13.18-windows-x86_64-none/python.exe`，工作虚拟环境 `.runtime/venv313`。未注册 Windows Python 解释器、未修改 PATH、未替换旧项目或系统 Python。

原因：旧 Python 3.12.10 携带 SQLite 3.49.1；[SQLite 官方 3.51.3 发布说明](https://sqlite.org/releaselog/3_51_3.html)记录 WAL-reset 数据损坏问题修复。长周期模拟与并发提交的发布运行时必须包含该修复；不能仅凭旧运行时测试偶然通过认定风险不存在。

安装工具 uv 0.13.8 仅位于原 `.runtime/venv`。依据 [uv 官方 Python 安装文档](https://docs.astral.sh/uv/guides/install-python/)，uv 下载的是 Astral python-build-standalone 构建，而非 python.org Windows 安装器。部署记录需保持这个来源区别。命令使用 `--no-bin --no-registry`，缓存和安装目标均在项目内。

## 首次安装

在 Windows x64 上准备 PowerShell、Node.js 24（含 npm）和可从命令行调用的 `uv`。在项目根目录运行以下命令，前一条完成后再执行下一条。已有项目内的 `uv` 时会优先使用它。Python、虚拟环境和前端依赖均安装在当前项目目录。

```powershell
$worldUv = if (Test-Path .\.runtime\venv\Scripts\uv.exe) { '.\.runtime\venv\Scripts\uv.exe' } else { 'uv' }
& $worldUv python install 3.13.18 --install-dir .runtime\python --no-bin --no-registry --cache-dir .runtime\uv-cache
& $worldUv venv .runtime\venv313 --python .runtime\python\cpython-3.13.18-windows-x86_64-none\python.exe --cache-dir .runtime\uv-cache
& $worldUv pip install --python .runtime\venv313\Scripts\python.exe -r requirements.lock.txt -e . --cache-dir .runtime\uv-cache
npm --prefix frontend ci
npm --prefix frontend run build
.\.runtime\venv313\Scripts\python.exe -m world_engine.runtime
```

保持服务窗口打开，在浏览器访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。首次启动会在 `.data` 中创建本地数据库。已经配置好环境后可直接执行最后一条，前端源码有更新时先重新构建。

2026-11-22 从空运行目录下载 Python、安装锁定依赖并构建网页，验证了上述命令的完整顺序。隔离实例用同一个启动模块在独立回环端口返回正常健康状态，首页与刚生成的构建文件一致，并在隔离目录创建了默认数据库。

2026-11-15 首次切换运行时的回归为 51 项通过，有一条 Starlette TestClient 针对 httpx 的弃用提示。当前回归、真实供应商验证与干净安装记录见[验收证据](acceptance-matrix.md)。替身测试中的供应商故障由 MockTransport 注入。

## 现有 Ollama 复用

`scripts/start-local-model.py` 读取指定旧项目的可执行文件和模型清单，对所有引用的 blob 检查大小与 SHA-256，在新项目 `.runtime/ollama-models` 建立内容寻址 blob 的硬链接并复制清单。模型内容不修改、不重新下载；元数据、用户目录、临时文件和日志使用新项目目录。没有调用旧项目的启动脚本，也不修改其 PID 或日志。

实际 `/api/tags` 已识别 `qwen3:8b-q4_k_m` 和 `qwen3:14b-q4_k_m`。初次通过网关探测失败，实测定位为 HTTP 客户端自动继承代理：相同回环 URL 在默认 HTTPX 下返回 502，`trust_env=False` 直接请求返回 200。已要求模型适配器对回环端点禁用环境代理，避免本地推理请求被误送到代理；这项检查不能用替身 HTTP 测试代替。

修复后运行 `scripts/probe-local-model.py` 成功。真实 Qwen3 8B 返回符合 schema 的对象，`kind=hypothetical`、`advances_world=false`；供应商报告输入 47、输出 19、总计 66 token，总耗时约 1.987 秒（含加载约 1.756 秒）。详细结构化记录位于 `.runtime/evidence/ollama-probe.json`。探测提示直接指定了目标结构和值，因此这只是协议与 schema 验证，不是独立语义判断能力、NPC 一致性或游戏验收证据。14B 当前仅确认目录发现，尚未执行生成验证。

后续 `scripts/probe-local-stream.py` 对真实 14B 流式调用成功，收到 22 个规范化事件，最终结果通过 schema 校验。总耗时约 3.090 秒，首 token 约 2.832 秒。该探测提供分类规则但不直接指定答案，模型将“如果我闭关十年，会怎样？”判为假设且不推进世界。记录为 `.runtime/evidence/ollama-stream-probe.json`。这是单个有界语义例与流式协议的证据，不能推及完整意图识别质量，更不能代表尚未接入的游戏流程。

## 长期容量审计

将已有长期存档目录传给 `scripts/audit-world-capacity.py --data-directory <目录>`，可以追加 `--checkpoint-year 20` 一类已记录的检查点。脚本从创世按事务回放至选定区间，报告全段具名人口最低值及首次低于 240 的位置。用 `--output <报告路径>` 保存独立 JSON 报告。本轮用已有审计区间验收，完整 600 年另作可选压力测试。

报告同时核对 24 个活跃国家、24 个其他活跃组织和 120 个活跃地点的全段最低数量。另列有具名活跃成员及有在任活跃成员领导的机构数，避免把空壳机构计作有成员运转。32 名原设关键 NPC 按创世名单核验，实际死亡在各段人口统计中保留。人口通过标记仍专指连续人口容量。

2.4 持续人口探针以 `--pack-version 2.4.0 --population-mode sustained` 新建独立目录。旧作业续跑继续使用原目录、原设定版本与已提交状态。Windows 工作进程退出后，先核查进程与作业状态，再以相同参数接管到期租约。每次续跑可指定新的报告路径，保存之前的中断和进度证据。

已成功完成的带种子探针可用 `--continue-completed` 在原分支继续到更晚的绝对年日目标。保留原设定、种子、人口模式和模型模式，只增加 `--years` 或 `--days`。续跑前核对前一作业终点和存档，将原清单按字节归档，以原终点创建后继作业。检查点年份仍以创世时刻计算。同一续跑请求可重试。

接续校验优先从终点以前最近的固定十年检查点回放到前一作业终点，同时核对该检查点的序列、时刻与摘要。没有较早十年检查点时，从创世回放。普通恢复也核对已保存的前序清单和作业。选择完整 600 年压力测试时，容量报告从创世完整回放。

可以将从创世完成的检查点前缀与后续逐提交区段报告接起来。`scripts/merge-capacity-intervals.py` 校验相邻的序列、时刻、版本、摘要与报告范围。需要同时核对正式作业终点时，使用 `--genesis-prefix` 并传入 `--verify-terminal-data-directory`。工具以只读方式核对 SQLite 的成功状态、已提交终点及原清单。探针仍在执行后续回放和归档时，使用已经持久化且与精确十年检查点一致的目标回执。终点验明后，全程仍须达到普通活跃具名居民最低数、国家与组织及地点结构门槛和创世关键人物要求。只合并报告时仍分别记录历史范围，终点作业凭证待核对。

新版探针会在十年整点的各个已提交版本写入去重快照，越过整点或到达最终目标才登记正式检查点。若同刻还有寿终、继任等事件，检查点取该时刻最后一次提交。这样可以减少越过整点后从上一快照重建的工作，连续人口验收仍由独立创世回放检查。

## 紧凑账本试用

本地运行时可在新数据目录中选择紧凑账本。在项目根目录使用 PowerShell 启动独立实例：

```powershell
$env:PYTHONPATH = 'backend'
.\.runtime\venv313\Scripts\python.exe -m world_engine.runtime --data-directory .runtime\compact-world-data --port 8001 --compact-ledger
```

布局保存于数据库，重开时仍按原布局读写。启用该布局的实例所创建的新恢复副本也会沿用它。安装程序拒绝转换已有世界的原布局。容量探针可独立使用同名 `--compact-ledger` 参数，长跑续租继续使用同一个存档和物理布局。

紧凑布局使用按事件与分录序号排序的复合主键、16 字节标识和资源字典。读写视图保持分录原有字段，写入时校验提交时间和事件关联。归档仍保存逻辑事件，可恢复到两种布局。实现依据 [SQLite 的只插入视图触发器](https://sqlite.org/lang_createtrigger.html) 和 [WITHOUT ROWID 表](https://sqlite.org/withoutrowid.html)。

## Windows 存档压缩实测

在 NTFS 上对自然模式 600 年冷存档的隔离副本应用文件级可写压缩。用 `GetCompressedFileSizeW` 核对实际占用，从 1,036,513,280 字节降到 486,027,264 字节，减少 53.1%。压缩前后副本的数据库 SHA-256 一致，原库字节一致，通过 SQLite 完整性检查，保留 24,851 个事件和 1,646,306 笔分录。证据 `.runtime/evidence/ntfs-cold-save-compression-20261119.json`。 [Windows compact 文档](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/compact) [Windows 实际分配字节接口](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getcompressedfilesizew)

另从现代 2.6 的同一 60 天紧凑账本基线生成两个可写副本。使用同一正式作业标识各推进两天，仅一个副本的主数据库使用 NTFS 压缩。两个数据库均通过完整性检查和创世回放，最终摘要、时间、事件末端与 7,312 笔总账分录一致。单次实测写入和模拟为 2.80 秒与 3.10 秒。该短样本尚未验证多 GB 长期写入、磁盘故障回滚或恢复性能。正式运行中的数据库保留现有布局。证据 `.runtime/evidence/ntfs-writable-world-compression-20261119.json`。

对持续运行的现代 2.4，用 SQLite 在线备份在只读事务中固定来源快照，在独立文件上压缩。主库实际占用从 3,814,522,880 字节降至 1,948,270,592 字节（减少 48.9%）。隔离副本从第 200 年的固定检查点建分支，租约推进七天，新增事件以同一检查点回放，摘要一致。全库完整性检查通过，核对到 15,601 个事件和 9,268,564 笔分录。检查结束时原作业提交序号已由 15599 推进至 15652。该样本覆盖多 GB 格式与真实写入，尚未验证长周期写入性能或生产库原位切换。证据 `.runtime/evidence/ntfs-large-world-compression-20261119.json`。 [SQLite 在线备份文档](https://sqlite.org/backup.html)

容量探针的停机压缩入口为 `scripts/compress-capacity-save.py`。接受明确的 `--data-directory`、保存的 `--job-id` 与可选 `--output`。工具取得同一个探针 OS 锁，核对 SQLite 中的作业确已暂停或成功结束，验证作业和分支的已提交时钟一致，再对已完成检查点的主库执行文件压缩。记录压缩前后实际分配空间，并核对主库整体 SHA-256、SQLite 完整性及作业游标。维护结束后，原探针按自身的暂停原因和正常入口续租。工具占用锁时，另一个探针不能进入同一数据目录。

在现代 2.6 的 45 天隔离作业，正常探针先于第 30 天暂停。锁冲突测试确认工具拒绝并保留数据库摘要。随后压缩从 13,385,728 字节降至 6,160,384 字节，用相同作业 ID 续跑。租约代次从 1 到 2，事件序号从 4 到 7，第 30 天回放摘要与暂停时一致，创世至第 45 天的完整回放同样一致。证据 `.runtime/evidence/maintenance-compact-resume-20261119-audit.json`。

另在完整修仙隔离世界故意停在 DeepSeek Flash 的答复已写入作业、世界尚未提交任何事件的边界。正式作业暂停后用同一维护入口压缩，恢复时复用相同作业 ID，旧租约被拒，持久答复摘要一致。原答复的首步未通过可行性验证，恢复后只追加一次作者可见的无效复审，拒绝代码为 `npc_plan_first_step_unavailable`。没有重复生成或补造行动，世界以正常规则到第 3600 秒，创世回放一致。证据 `.runtime/evidence/ntfs-model-receipt-resume-20261119.json` 和 `.runtime/evidence/ntfs-model-receipt-resume-20261119-review-audit.json`。

使用新版容量探针时，维护命令加 `--request-pause` 可为正在运行的明确作业写入请求。探针在已提交边界确认请求；如下一次模型调用尚未开始，先暂停，如当前调用已开始，先保留原答复及其结算再进入暂停。维护工具之后取得原锁，做完整性核对，成功后把本次请求暂停的同一作业重新排队。重启原探针或由监督器重新领取。工具仍接受已经完成或因其他原因暂停的存档，后者保留原暂停原因。带新入口的隔离作业实际从活跃进程收到请求，第 30 天暂停，压缩后由正式工作器续至第 180 天；见 `.runtime/evidence/cooperative-storage-maintenance-20261119-audit.json`。

已有提交的续跑作业在维护后重新排队时，预检按作业记载的提交时刻和状态版本对照当前分支，核对其未越过原授权范围。尚未提交的作业仍须停在前任作业的精确终点。现代 2.7 原作业在第 135 年附近更换校验器，并从交接快照回放核对新工作器的首批提交和账本分录。见 `.runtime/evidence/modern-270-json-validator-cutover-20261120/verified-after.json`。

确认维护暂停后，探针现在只写入可核对的交接游标并释放目录锁。全库统计、完整性检查、创世回放和完整归档留给对应的维护或终点验收路径。旧探针即使已经持久暂停仍先生成常规报告，在多年存档里这会延迟维护进程接手。

同一交接也适用于持久化的 `model_decision_failed` 暂停。探针保留停止原因与调用计数，监督器读取原作业及调用记录后按现有策略决定是否续租。终点仍执行完整回放和归档检查。该交接不改变供应商失败重试和计费判定策略。

对容量探针的普通模型决策，探针自身的计时器确实到期时，暂停结果会保存请求标识、世界时刻和分支版本。监督器需要该回执与当前提交游标及同一请求的取消调用同时匹配，且没有已返回、待提交或已提交的决定，才识别为可重试的本机期限耗尽。费用未知仍要求明确授权，并执行相同的退避。仅有旧调用的耗时或取消记录时，仍按普通取消处理。新版代码在相应探针与监督器进程重新加载后生效。 [Python 超时上下文文档](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.timeout)

监督器运行时，手动启动的探针应同时使用脚本与 `--data-directory` 的绝对路径。Windows 进程清单只有启动参数，无法把相对目录可靠地归到某个存档。监督器会把无法确定归属的探针视为潜在写入者。修仙对照档第二次用相同原作业与冻结规则续租时改用绝对路径，监督器不再把它列入另一套修仙长跑的活跃进程。

独立容量审计在读取窗口内仍可与写入并行，但长时间读取会推迟 WAL 回收。已结束审计的正式现代 2.4 主库仍保留 1,723,803,912 字节 WAL，封建 2.1 保留 55,240,992 字节。使用 SQLite 的 `wal_checkpoint(TRUNCATE)` 后，两次均返回 `(0, 0, 0)`，日志长度降为零。现代 1.8 第一次检查点返回忙，保留其日志等待后续空闲窗口。今后优先按相接的确切检查点拆分长区间，让连续提交审计可以合并，还能在每段之间释放读事务。 [SQLite WAL 回收说明](https://sqlite.org/wal.html#avoiding_excessively_large_wal_files)

`audit-capacity-interval.py --split-decades` 可将多十年区间按已经保存的十年检查点分段读取，保留一个全区间报告。每段回放前核对持久化检查点，结束时验证摘要和序列。交界状态只统计一次，相接的短缺窗口与组织缺员窗口连续计数。报告记录实际回放事务数。遇到缺失或不匹配的中间检查点时停止，避免跳过提交。

另对两份无写入进程的隔离试玩副本，在 Windows 独占文件句柄内做文件级压缩和压缩前后 SHA-256 核对。两份 WAL 均为空，合计回收 1,480,192,000 字节，压缩后 SQLite 完整性检查均通过。用小型有写事务的 SQLite 文件验证独占开启会被系统拒绝。证据 `.runtime/evidence/idle-copy-ntfs-compression-20261119.json`。 [微软文件压缩接口](https://learn.microsoft.com/windows/win32/api/winioctl/ni-winioctl-fsctl_set_compression) [Windows 文件独占说明](https://learn.microsoft.com/en-us/windows/win32/fileio/creating-and-opening-files)

修仙 2.4 的 40 年对照档在约第 22.25 年点响应维护请求。通过 SQLite 核对已持久暂停在序号 1400、版本 822，终止旧版进程仍在运行的维护尾部报告流程后接手原目录。主库物理占用从 383,901,696 字节降到 190,464,000 字节，逻辑字节、分支游标和 SQLite 完整性均匹配。由同一冻结后台和相同配置重新领取原作业，租约代次 2 已越过序号 1400 并继续提交。第 10–20 年逐提交只读核对另行启动。这是长跑维护实证，对照档的 40 年容量结论仍待正式核对。证据 `.runtime/evidence/capacity-cultivation-240-dense-40-ntfs-maintenance-20261119.json`。

同一入口用于实际运行到第 450 年的修仙 2.0 正式长跑。探针确认维护请求并在序号 25210、租约代次 46 释放目录锁。主库分配字节从 7,408,467,968 降至 3,646,468,096。完整 WAL 检查点后的文件摘要和已提交分支游标在压缩前后一致，SQLite 完整性通过。原作业由维护工具重新排队，监督器领取到代次 47 后继续提交，已核对超过序号 25210。同期现代 2.4 完成第 230–240 年审计后，另通过 SQLite 检查点回收 1,679,307,912 字节 WAL。证据 `.runtime/evidence/capacity-cultivation-200-sustained-ntfs-maintenance-20261119.json`。

磁盘余量降至约 10 GiB 后尝试为现代 1.8 请求维护。该进程早于协作暂停入口部署，检查仍有时间和提交推进，因此归档未响应的请求，保留活跃工作器。改对现代 2.5 发出请求并验证原作业在约第 154.52 年、序号 11477、版本 6415、租约代次 9 停在提交边界。主库分配字节从 2,872,991,744 降至 1,466,396,672，压缩前后 SHA-256、SQLite 完整性和提交游标通过核对。监督器为重新排队的原作业取得代次 10，同一作业已提交到世界秒 4878144000。证据 `.runtime/evidence/capacity-modern-250-seed1-sustained-ntfs-maintenance-20261119.json`。

## PostgreSQL 存储契约验证

隔离验证使用项目测试目录中的 PostgreSQL 16.16 Windows 二进制和 Psycopg 3.3.9，数据库只监听回环地址。应用自身通过 Alembic 完成迁移。真实契约覆盖写入、授权、审计、回放及租约，SQLite 验证另有对应报告。 [PostgreSQL Windows 安装说明](https://www.postgresql.org/download/windows/) [EDB 二进制下载](https://www.enterprisedb.com/download-postgresql-binaries) [Psycopg 安装说明](https://www.psycopg.org/install/)

隔离基线的 11 项现有契约通过 9 项，双写进入版本冲突并保持幂等。另两项发现非 UTC 连接下作业及分支租约被提前八小时写入，修补后需保持非 UTC 连接复测。证据见 `.runtime/evidence/postgresql-storage-contracts-1616-20261121.json` 和 `.runtime/evidence/postgresql-lease-zone-baseline-20261121.json`。
