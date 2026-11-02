# `app/gui_web/ui`

此目录保存 Web 界面前端。运行时 `app/gui_web/main.py` 托管 `index.html`；该单文件包含 HTML、CSS 与 JavaScript，是当前唯一必需前端文件。

## 开发约定

- 页面始终由本机 HTTP 服务加载（桌面窗口与 `--serve` 浏览器模式相同），不存在 pywebview `js_api` 桥接分支。
- 操作请求经 `API_ROUTES` 与 `call(name, ...args)` 映射到 REST `fetch`。
- 任务更新只通过 `EventSource` 连接 `GET /api/events`；断线由浏览器按 `Last-Event-ID` 自动重连。`POST /api/events/poll` 是同一游标缓冲的 REST 补发端点，界面只在确认任务后补拉一次任务快照，不做周期轮询。
- 设置页的多数字段目前只做持久化与路径探测；哪些字段真正影响业务流程以 `Docs/configuration.md` 为准。
- 前端只负责界面和用户操作，不直接执行磁盘命令。协议、路由、Host 校验、事件缓冲和部署限制见[Web API 文档](../../../Docs/web-api.md)。

## 快速验证

```powershell
python -m app.gui_web.main --dry-run
python -m app.gui_web.main --serve
```

`--dry-run` 显式要求演练，但 `dryRunByDefault` 也会影响 GUI 会话。不要将普通启动等同于演练。
