# 隔离运行时与版本验证

2026-11-15 已在项目内安装 Python 3.13.18，实际查询 SQLite 为 3.53.1。路径 `.runtime/python/cpython-3.13.18-windows-x86_64-none/python.exe`，工作虚拟环境 `.runtime/venv313`。未注册 Windows Python 解释器、未修改 PATH、未替换旧项目或系统 Python。

原因：旧 Python 3.12.10 携带 SQLite 3.49.1；[SQLite 官方 3.51.3 发布说明](https://sqlite.org/releaselog/3_51_3.html)记录 WAL-reset 数据损坏问题修复。长周期模拟与并发提交的发布运行时必须包含该修复；不能仅凭旧运行时测试偶然通过认定风险不存在。

安装工具 uv 0.13.8 仅位于原 `.runtime/venv`。依据 [uv 官方 Python 安装文档](https://docs.astral.sh/uv/guides/install-python/)，uv 下载的是 Astral python-build-standalone 构建，而非 python.org Windows 安装器。部署记录需保持这个来源区别。命令使用 `--no-bin --no-registry`，缓存和安装目标均在项目内。

复现当前环境：

```powershell
.\.runtime\venv\Scripts\uv.exe python install 3.13.18 --install-dir .runtime\python --no-bin --no-registry --cache-dir .runtime\uv-cache
.\.runtime\venv\Scripts\uv.exe venv .runtime\venv313 --python .runtime\python\cpython-3.13.18-windows-x86_64-none\python.exe --cache-dir .runtime\uv-cache
.\.runtime\venv\Scripts\uv.exe pip install --python .runtime\venv313\Scripts\python.exe -r requirements.lock.txt -e . --cache-dir .runtime\uv-cache
.\.runtime\venv313\Scripts\python.exe -m pytest -q
```

当前新运行时全套测试 51 项通过；有一条 Starlette TestClient 针对 httpx 的弃用提示。供应商协议故障测试使用 MockTransport，不能作为云端或本地模型实测证据。后续新模块应继续在该运行时验证。

## 现有 Ollama 复用

`scripts/start-local-model.py` 读取指定旧项目的可执行文件和模型清单，对所有引用的 blob 检查大小与 SHA-256，在新项目 `.runtime/ollama-models` 建立内容寻址 blob 的硬链接并复制清单。模型内容不修改、不重新下载；元数据、用户目录、临时文件和日志使用新项目目录。没有调用旧项目的启动脚本，也不修改其 PID 或日志。

实际 `/api/tags` 已识别 `qwen3:8b-q4_k_m` 和 `qwen3:14b-q4_k_m`。初次通过网关探测失败，实测定位为 HTTP 客户端自动继承代理：相同回环 URL 在默认 HTTPX 下返回 502，`trust_env=False` 直接请求返回 200。已要求模型适配器对回环端点禁用环境代理，避免本地推理请求被误送到代理；这项检查不能用替身 HTTP 测试代替。

修复后运行 `scripts/probe-local-model.py` 成功。真实 Qwen3 8B 返回符合 schema 的对象，`kind=hypothetical`、`advances_world=false`；供应商报告输入 47、输出 19、总计 66 token，总耗时约 1.987 秒（含加载约 1.756 秒）。详细结构化记录位于 `.runtime/evidence/ollama-probe.json`。探测提示直接指定了目标结构和值，因此这只是协议与 schema 验证，不是独立语义判断能力、NPC 一致性或游戏验收证据。14B 当前仅确认目录发现，尚未执行生成验证。
