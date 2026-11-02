# `app/gui_web/ui`

此目录保存 Web 界面前端。运行时 `app/gui_web/main.py` 托管 `index.html`；该单文件包含 HTML、CSS 与 JavaScript，是当前唯一必需前端文件。

## 开发约定

- 操作请求经 `API_ROUTES` 与 `call(name, ...args)` 映射到 REST `fetch`。
- 任务更新通过 `EventSource` 连接 `GET /api/events`；连接错误由浏览器自动重连，页面可使用游标轮询端点作为补充。
- 前端只负责界面和用户操作，不直接执行磁盘命令。协议、路由、Host 校验、事件缓冲和部署限制见[Web API 文档](../../../Docs/web-api.md)。

## 快速验证

```powershell
python -m app.gui_web.main --mock
python -m app.gui_web.main --dry-run
python -m app.gui_web.main --serve
```

`--mock` 是无后端预览；`--dry-run` 显式要求演练，但 `dryRunByDefault` 也会影响 GUI 会话。不要将普通启动等同于演练。