# Frontend 工程记录

日期：2026-11-15。状态：React/Vite 工作台与首批 API 接口已落地；这不是 R01–R18 完整交付或真实模型质量验收报告。

## 实现范围

- Chinese-first 深色青绿工作台：世界库、设定包创建、GM 对话、作者档案、历史与分支、模型连接、职责路由、速度/深度、调用记录。
- 世界和模型数据只从 `/api/v1` 读取。没有世界、设定包、历史、模型或调用记录时显示空状态，不以演示对象模拟运行数据。
- GM 对话只显示服务端返回的回复。已提交但未完成的工作显示为后台任务；无法确认是否送达时保留原 `request_id` 供同一请求重试。
- 世界档案明确请求 `mode=author`。页面以“作者视角 · 剧透”标示真相投影。组件修订需经过服务端预览、完整组件/引用校验、显式确认和版本检查；人物公开描述、外貌、身份、性格/特征、自定义数值、健康与成长提供表单，复杂组件可用高级 JSON。
- 模型目录探测只显示目录发现结果。界面不会将其描述成生成能力验证；用量、成本未知时明确显示“未知”。调用追踪只展示服务端返回的安全记录，不显示提示正文。
- 桌面采用左侧导航和游戏页三栏；窄窗口将导航改为抽屉、侧栏收起、卡片与表单改为单列。表单和主要操作可用键盘，减弱动态效果遵循 `prefers-reduced-motion`。

## API 与身份

`frontend/src/api/client.ts` 提供 `/api/v1` 类型化请求包装。启动时先访问 `GET /session` 取得本地会话 cookie，之后才读取世界库和设定包。凭据 cookie 为 HttpOnly，客户端请求保持 same-origin。开发服务器将 `/api` 代理到 `WORLD_ENGINE_API_URL`，未设置时使用 `http://127.0.0.1:8000`；端口由 Vite 5173 提供。

安全 GET 收到 401 时，客户端可重新 bootstrap 会话后重试一次。创建世界、分支、发送消息、供应商探测等写请求不会由客户端自动重放；需要幂等的写操作携带稳定的 `Idempotency-Key`。模型供应商表单通过 write-only 密码框调用凭据状态、写入和删除接口；成功保存后立即清空输入，永不回读密钥。输入框只在后端报告 Windows Credential Manager 可用时显示，不会退回浏览器存储或普通文件。

当前客户端覆盖 `/session`、`/worlds`、`/packs`、创建世界、授权世界视图、分支列表、历史、持久对话、消息与命令、模型、供应商和凭据、设置、调用记录、作业控制、扩展和作者组件修订契约。创建世界使用实际设定包目录；目录为空时无法提交。作者组件修订只针对当前投影中已存在的已注册组件；没有对应 API 的元数据编辑、设定包发布、关系编辑和试演仍不会假装保存。

GM 工作区在每个世界分支下读取 `/worlds/{id}/conversation?branch_id=` 返回的稳定 `session_id`、`actor_id` 和消息记录。页面重挂载会恢复历史，不会自行创建临时会话；持久历史读取失败时禁用发送并提供重试入口。发送中、成功和失败请求按服务端状态恢复；失败历史不会自动重放。历史页使用 owner/editor 授权端点，以 oldest-first 游标分页继续读取后续事件。

Windows 适配器使用 `ctypes` 调用 Windows Credential Manager 的精确目标读写与删除函数。它要求 `world-engine/` 目标前缀、对目标长度设上限、将密钥限制在 2560 UTF-8 字节，并且不枚举外部凭据、不写明文回退。Windows 合成凭据读写/覆盖/删除轮回测试使用临时随机值并在 `finally` 中清理；API 集成测试使用独立内存凭据 fixture。

## 依赖与实现依据

使用 Vite React TypeScript 官方模板，组件采用 React function component、props 与本地 state，样式使用 CSS，不额外引入 UI 框架或图标包。图标由项目内的 SVG 组件绘制，字体使用本机系统字体回退，不依赖远程字体服务。

- [React Quick Start](https://react.dev/learn) — 组件、JSX、列表、事件处理与 state。
- [Vite Getting Started](https://vite.dev/guide/) — Vite + React TypeScript 项目结构、开发服务器与构建。
- [Vite Features: TypeScript](https://vite.dev/guide/features.html#typescript) — Vite 负责转译，构建中单独运行 `tsc --noEmit`/TypeScript 检查。
- [TypeScript Handbook](https://www.typescriptlang.org/docs/handbook/intro.html) — 严格类型建模与类型收窄。
- [Microsoft `CREDENTIALW`](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw) — 通用凭据结构与凭据 blob 的 2560 字节上限。
- [Microsoft `CredWriteW`](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew) 与 [Microsoft `CredReadW`](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw) — 精确目标的 Windows 凭据写入和读取。
- [Microsoft `CredDeleteW`](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-creddeletew) 与 [Microsoft `CredFree`](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credfree) — 凭据删除及读取结果释放。
- 本项目协议依据：[API 合同](../03-contracts.md)、[PRD](../01-PRD.md)、[模型与运行后台](../05-model-operations.md)。

没有复制第三方源码或视觉素材。

## 工具链与检查

前端安装使用 npm，锁文件为 `frontend/package-lock.json`。检查环境为 Node.js 24.15.0、npm 11.12.1；已安装的直接依赖锁定为：

- React / React DOM 19.3.1
- Vite 8.3.8、`@vitejs/plugin-react` 6.3.0
- TypeScript 7.0.2、`@types/react` 19.3.0、`@types/react-dom` 19.3.0
- Oxlint 1.93.0

已运行以下检查：

- `npm run build` — 通过；包括 `tsc -b` 类型检查与 Vite production build。
- `npm run lint` — 通过，Oxlint 无警告。
- `npm ls --depth=0` — 通过，直接依赖安装完整。
- `.runtime/venv313/Scripts/python.exe -m pytest tests/credentials/test_windows_credential_store.py -q` — 当前执行环境为 2 passed、1 skipped；真实 Windows Credential Manager 往返测试按平台跳过。根代理另行确认 Windows 合成凭据写入、读取、覆盖和删除成功。

本前端任务没有运行自动化浏览器端到端测试。根代理报告在本地运行时通过浏览器发送真实 GM 查询并成功返回，且世界 tick 与 state version 均保持不变；这不等同于长时、多世界或真实费用验收。

## 尚未覆盖的前端功能

- 关系专用编辑、设定包关联更新、试演报告、批量导入导出和迁移提交，等待对应 API；已开放部分通过组件修订预览与提交。
- SSE 进度续传、历史查询/筛选和完整作业控制面板。
- 供应商生成能力的结构化、工具、流式恢复等连接探测；当前只做目录发现。
- 尚未运行真实模型输出、长时模拟、费用核对或多背景游戏验收。
