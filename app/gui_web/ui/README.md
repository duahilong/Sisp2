# app/gui_web/ui

这个目录放**界面前端**。运行时 `app/gui_web/main.py` 会加载这里的 `index.html`。

## 需要的文件

| 文件 | 来源 |
|---|---|
| `index.html` | 由原型 `disk-tools-app.html` 复制而来（单文件即可运行） |
| `bridge.js` | 已提供，负责任意 `window.pywebview.api` 调用的封装 |

最小可运行状态 = `index.html` + `bridge.js`。如果先把原型整份丢进来，
`bridge.js` 可以暂时不引入，原型会走自己的假数据，界面能看但改不了真磁盘。

## 快速验证

```powershell
python -m app.gui_web --mock      # 只开窗口，不连后端
python -m app.gui_web --dry-run   # 连后端，但所有写盘动作都是演练
```

## 把原型接上真后端要改的 5 个地方

见仓库根目录 `app/gui_web/ui/FRONTEND.md`（随补丁包提供）。
