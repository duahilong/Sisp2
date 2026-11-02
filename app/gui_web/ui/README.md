# app/gui_web/ui

这个目录放**界面前端**。运行时 `app/gui_web/main.py` 会加载这里的 `index.html`。

## 需要的文件

| 文件 | 说明 |
|---|---|
| `index.html` | 单文件界面，包含全部 HTML/CSS/JS |

`index.html` 是唯一必需文件。界面通过 **REST（fetch）+ SSE（EventSource）**
与本机 HTTP 后端通信，不再依赖 `window.pywebview.api`：

- 操作调用走 `API_ROUTES` 路由映射表（`call(name, ...args)` 约定不变）；
- 任务事件走 `GET /api/events` 实时推送，断线自动重连补发；
- 完整协议见仓库根目录 `Docs/web-api.md`。

## 快速验证

```powershell
python -m app.gui_web.main --mock       # 只开窗口，不连后端
python -m app.gui_web.main --dry-run    # 连后端，但所有写盘动作都是演练
python -m app.gui_web.main --serve      # 浏览器模式：起本机 HTTP 服务
```

界面启动后可访问接口文档：`http://127.0.0.1:8765/docs`。
