# Sisp2 Web API 与前后端分离架构

> 对应实现：`app/gui_web/server.py`（路由层）、`app/gui_web/api.py`（业务方法 + 信封）、
> `app/gui_web/main.py`（入口）、`app/gui_web/ui/index.html`（前端）。
> 集成测试：`tests/test_web_api.py`。

Sisp2 的 Web 界面采用**前后端分离**架构：

- **前端**：单文件 `ui/index.html`，负责界面展示、收集用户操作、渲染任务进度；
- **后端**：本机 HTTP 服务（FastAPI + uvicorn），负责硬盘扫描、安全验证（四道闸门）、
  任务调度、设置持久化。浏览器/网页永远无法直接执行磁盘命令。

通信采用 **REST 管操作 + SSE 管事件推送** 的组合：

```
┌──────────────┐   REST（操作/查询）   ┌───────────────────────────────┐
│  前端 HTML    │ ───────────────────▶ │ FastAPI 路由层 server.py       │
│              │ ◀─────────────────── │  （信封 {"ok", "data"/"error"}）│
│  磁盘列表     │   SSE（事件推送）     │            │                  │
│  执行流程     │ ◀─────────────────── │            ▼                  │
│  设置页      │   GET /api/events    │  WebApi → WorkflowController   │
└──────────────┘                      │            → JobManager.subscribe
                                      └───────────────────────────────┘
```

## 运行模式

两种模式**共用同一条通信链路**（HTTP + REST + SSE），前端不感知差异：

| 模式 | 命令 | 说明 |
|---|---|---|
| 桌面模式（默认） | `python -m app.gui_web.main` | 后台线程起服务，pywebview 窗口加载 `http://127.0.0.1:8765/` |
| 浏览器模式 | `python -m app.gui_web.main --serve` | 前台起服务，用 Edge/Chromium 打开，不依赖 pywebview |
| 原型预览 | `python -m app.gui_web.main --mock` | 无后端，界面走自带数据 |

- 服务只监听 `127.0.0.1`（可用 `--host` 改，但见下文「安全基线」）；
- 端口默认 8765，被占用时入口报错退出（退出码 4），可用 `--port` 换；
- 必须**单进程单 worker** 运行：`JobManager` 的任务状态在内存里，多 worker 会撕开状态；
- FastAPI 自动提供接口文档：`http://127.0.0.1:8765/docs`。

## REST 端点

所有业务响应沿用统一信封：

```jsonc
// 成功
{"ok": true, "data": ...}
// 失败
{"ok": false, "error": {"code": "E_PREPARE", "message": "...", "detail": null}}
```

| 端点 | 对应 `WebApi` 方法 | 说明 |
|---|---|---|
| `GET /` | — | 返回 `ui/index.html`（同源托管，无 CORS） |
| `GET /health` | — | 存活探针 `{"ok": true}` |
| `GET /api/info` | `app_info()` | 版本 / 平台 / dry-run 状态 |
| `POST /api/session` | `start_session()` | body: `{config_path, dry_run}`；重建会话并清空事件缓冲 |
| `GET /api/disks` | `refresh_disks()` | 重扫硬盘并应用保护规则 |
| `POST /api/config/check` | `check_config()` | body: `{config_path}` |
| `POST /api/prepare` | `prepare()` | body: `{selection, dry_run}`；selection 支持 `[1,3]` 或 `{1: {"c_size": 400}}` |
| `POST /api/submit` | `submit()` | 提交任务，进入 `waiting_confirmation` |
| `POST /api/confirm` | `confirm()` | body: `{job_ids}`；有阻断项时拒绝 |
| `POST /api/cancel` | `cancel()` | body: `{job_id}`；协作式取消 |
| `GET /api/jobs/{job_id}` | `get_job()` | 单任务状态与结果 |
| `POST /api/events/poll` | `poll_events()` | body: `{cursor, limit}`；轮询兜底 / 断线补发 |
| `GET /api/settings` | `get_settings()` | 读取设置 |
| `PUT /api/settings` | `update_settings()` | body 为局部 patch；任一项非法整体拒绝 |
| `DELETE /api/settings` | `reset_settings()` | 恢复默认 |
| `POST /api/settings/probe` | `probe_path()` | body: `{key}` |
| `POST /api/settings/probe_all` | `probe_all_paths()` | 探测全部路径型设置 |
| `POST /api/settings/open_folder` | `open_settings_folder()` | 资源管理器打开设置目录 |
| `POST /api/dialog/pick` | `pick_path()` | 原生文件对话框；仅桌面模式有窗口时可用 |

实现约定：业务路由全部用**同步 `def`** 声明，FastAPI 自动放入线程池执行——
60 秒的 PowerShell 扫描不会卡住事件循环，SSE 心跳不受影响。

## SSE 事件流

`GET /api/events?cursor=<n>`，替代原先的 1.2 秒轮询。

### 帧格式

```text
data: {"type": "snapshot", "cursor": 0, "events": [...], "jobs": [...]}

id: 12
data: {"cursor": 12, "event": {"type": "job.finished", ...}, "jobs": [...]}

: ping
```

- **首帧 `snapshot`**：从 `cursor` 补发积压事件（复用 `poll_events` 的游标缓冲）+ 当前任务列表；
- **事件帧**：每个 `JobEvent` 一帧，`id` 为事件的 `seq`；`jobs` 是当时的任务快照；
- **心跳**：空闲 15 秒发一行 `: ping`，防止连接被中间设备断开。

### 断线重连

浏览器 `EventSource` 自动重连并携带 `Last-Event-ID`（上一帧的 `seq`），
服务端取 `max(query cursor, Last-Event-ID)` 后重新进入「快照 + 推送」流程，
断线期间的事件从游标缓冲补发，不丢事件。

### 生命周期

每个 SSE 连接在 `JobManager.subscribe()` 上注册一份订阅，
连接断开（或流结束）时在 `finally` 里 `unsubscribe()`，不会泄漏。

## 安全基线

- **只绑 loopback**：默认 `127.0.0.1`，本机之外无法访问；
- **Host 头校验**：绑定 loopback 时启用，拒绝非 `localhost / 127.0.0.1 / [::1]` 的
  Host 头（防 DNS rebinding），非法返回 `403 E_HOST`；
- **同源托管**：HTML 由同一服务返回，不开 CORS；
- 业务层安全不变：默认 dry-run、写盘四道闸门、硬盘身份重扫比对，
  这些都在 `SispService`，HTTP 层不绕过、不削弱。

> ⚠️ `--host 0.0.0.0` 会把**写盘接口**暴露给局域网，且当前**没有鉴权**。
> 入口会打印警告。如需远程访问，先加 token 鉴权与审计，再开放监听地址。

## 前端连接层

`index.html` 里的调用约定保持不变（`call(name, ...args)`），传输层可替换：

- `API_ROUTES` 表把方法名映射到「HTTP 方法 + URL + body 构造」，
  业务调用点零改动；
- `httpMode()` 按 `location.protocol` 判断：HTTP(S) 走 `fetch`，
  否则退回旧的 `window.pywebview.api` 桥（兼容旧桌面模式）；
- `startPolling()` 在 HTTP 模式下转为 `startEventStream()`（EventSource），
  `onerror` 只更新连接徽标，重连交给浏览器；
- `POST /api/events/poll` 仍保留：确认任务后前端会补拉一次，也是 SSE 不可用时的兜底。

## 本次改造记录

1. **新增 `app/gui_web/server.py`**：FastAPI 路由层，把 `WebApi` 的 17 个方法
   平移为 REST 端点；新增 SSE 端点与 Host 校验中间件。
2. **重写 `app/gui_web/main.py`**：删除手写 `BaseHTTPRequestHandler` 与
   pywebview shim 注入；桌面模式改为「后台服务 + 窗口加载 URL」，两种模式统一走 HTTP；
   新增端口占用检测与服务就绪等待。
3. **前端 `ui/index.html`**：`call()` 切换为 fetch + 路由映射表；
   1.2 秒轮询替换为 EventSource；连接状态由 SSE 的 open/error 驱动。
4. **新增 `tests/test_web_api.py`**：10 个集成测试（静态页、Host 校验、会话、
   任务全流程、设置读写、SSE 快照与事件帧）。
5. **依赖**：`requirements-web.txt` 新增 `fastapi`、`uvicorn`（测试另需 `httpx`）。

业务层（`workflow / service / jobs / real_runner / settings`）**零改动**。
