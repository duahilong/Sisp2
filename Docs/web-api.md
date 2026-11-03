# Web API 与部署说明

实现：`app/gui_web/main.py`、`server.py`、`api.py`；前端为 `ui/index.html`。桌面窗口和浏览器模式共用 HTTP、REST、SSE。成功/业务失败使用 `{"ok":true,"data":...}` / `{"ok":false,"error":...}` 信封（Host 拒绝使用 HTTP 403）。

## 启动与部署

```powershell
python -m app.gui_web.main                 # 桌面窗口
python -m app.gui_web.main --serve          # 前台服务
python -m app.gui_web.main --serve --port 8765
```

参数：`-j/--config`、`--dry-run`、`--elevate`、`--debug`、`--serve`、`--port`（默认 8765）、`--host`（默认 `127.0.0.1`）、`--settings`、`--width`、`--height`。桌面模式需要 pywebview；`--serve` 不依赖它。两种模式都加载同一个本机 HTTP 服务。

有效 GUI dry-run 是 `--dry-run OR settings.dryRunByDefault`；两者默认均为 false。该值在启动时锁定为硬上限，`POST /api/session` 传 `dry_run:false` 或 `POST /api/prepare` 传 `dry_run:false` 都不能升级为真实执行。服务必须单进程单 worker，因为任务和缓冲状态保存在内存。端口被占用入口返回 4；FastAPI 自动文档地址默认为 `http://127.0.0.1:8765/docs`（地址随 host/port 改变）。

## 路由

| 方法与路径 | 行为 / 请求要点 |
|---|---|
| `GET /` | 托管 `ui/index.html` |
| `GET /health` | 健康探针 |
| `GET /api/info` | 版本、平台、会话 dry-run |
| `POST /api/session` | `{config_path, dry_run}`；重启会话并清空 WebApi 事件缓冲 |
| `GET /api/disks` | 重扫硬盘 |
| `POST /api/config/check` | `{config_path}` |
| `POST /api/prepare` | `{selection, dry_run}`；selection 为盘号数组或盘号到覆盖对象的映射 |
| `POST /api/submit` | 登记等待确认任务 |
| `POST /api/confirm` | `{job_ids, confirm_text?}`；`typeToConfirm` 开启时 `confirm_text` 必须是盘号或磁盘型号 |
| `POST /api/cancel` | `{job_id}`，协作式取消 |
| `GET /api/jobs/{job_id}` | 单任务查询 |
| `POST /api/events/poll` | `{cursor, limit}`，游标轮询补发 |
| `GET /api/events?cursor=N` | SSE 事件流；可读 `Last-Event-ID` |
| `GET /api/settings`、`PUT /api/settings`、`DELETE /api/settings` | 读取、patch 更新、重置设置；patch 任一项非法整体拒绝 |
| `POST /api/settings/probe` | `{key}` 路径探测 |
| `POST /api/settings/probe_all` | 探测路径设置 |
| `POST /api/settings/open_folder` | 打开设置文件夹（Windows） |
| `POST /api/dialog/pick` | `{key, kind}` 原生文件/目录选择；需有 pywebview 窗口 |

业务路由以同步 `def` 实现，由 FastAPI 在线程池运行。

## Host 校验与安全

默认 loopback 绑定启用 Host 校验。按 Host 主机名（去端口后）允许 `127.0.0.1`、`localhost`、`::1`、`[::1]`，非空且不匹配返回 403 `E_HOST`。这只是 Host 头限制，不是用户身份认证、授权或 CSRF 防护。

`--host` 为非 loopback 时，入口关闭 Host 检查并输出警告。当前无鉴权，网络可达时写盘相关 API 也可访问，属高风险，勿暴露于不可信网络。业务写盘门禁的入口差异、GUI 隐式白名单等见[安全说明](security.md)。

## SSE 游标与缓冲

SSE 首帧为 `snapshot`，包含 `poll_events(cursor, 1000)` 返回的可用补发事件及任务快照；后续帧为 `{cursor, event, jobs}`，事件帧 `id` 为整数 `seq`。客户端重连时服务端取查询参数 `cursor` 与合法数字 `Last-Event-ID` 的较大值。空闲约 15 秒发 `: ping`。每个连接订阅 JobManager，连接结束时取消订阅。

补发并非持久、无损队列：WebApi 只在内存保留最多 5000 个事件，超量淘汰最旧事件；SSE 每连接队列最多 2000 条，满时丢弃新事件。缓冲淘汰或队列丢弃后不能保证完整补发。服务重启也清空内存缓冲。`POST /api/events/poll` 使用同一游标缓冲，并非独立持久化恢复机制。

## 前端开发

前端通过 REST `fetch` 与 `EventSource` 通信，详见[前端 README](../app/gui_web/ui/README.md)。