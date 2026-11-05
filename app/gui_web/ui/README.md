# `app/gui_web/ui`

Web 界面前端。运行时 `app/gui_web/main.py` 托管 `index.html`；`server.py` 另外把
`/static` 挂到同一目录，因此前端不需要打包工具、不需要跨域，也没有 npm 依赖。

## 目录

```
ui/
├─ index.html        # 薄壳：只有布局骨架和 id，没有内联样式和脚本
├─ css/app.css       # 全部样式（结构与配色来自 design/disk-tools-app.html）
└─ js/
   ├─ main.js        # 入口：装配、渲染调度、事件流、轮询
   ├─ api.js         # REST 封装（统一解 {ok,data} / {ok,error}）、EventSource
   ├─ dom.js         # $ / $$ / esc / 事件委托 / 时间戳
   ├─ derive.js      # 纯函数：容量格式化、分区地图切片、大小校验
   ├─ state.js       # 唯一可变状态 + 订阅通知 + 向导数据操作
   ├─ shell.js       # 外壳：菜单、工具栏、日志坞、状态栏、toast、弹窗、主题
   ├─ view-main.js   # 主页面：磁盘概览条、盘头、分区地图、分区明细表
   ├─ view-install.js# 系统安装：4 步向导 + 进度页
   ├─ view-settings.js # 设置页
   └─ dialogs.js     # 修复引导 / 磁盘检查 / 关于 / 快捷键
```

## 开发约定

- **ES 模块，无构建步骤**：`<script type="module">` 直接加载，模块之间用相对路径
  `import './x.js'`。新增文件后要同时更新 `tests/test_ui_assets.py` 的文件清单。
- **渲染策略**：事件 → 改 `state` → `notify()` → `render()`。只有视图读状态，写状态
  的地方改完必须 `notify()`；`main.js` 里对向导和设置页各加了一层防抖，避免进度事件
  把正在输入的输入框刷掉。
- **`derive.js` 不碰 DOM**：所有纯计算放在这里，`node` 才能直接跑行为测试
  （`tools/ui_check.py` 会在 node 里真实调用一遍并断言结果）。
- **`index.html` 保持薄壳**：新增界面写在 `js/` 里，不要往 HTML 里塞内联样式或脚本——
  `tests/test_ui_assets.py` 会拦住。
- **样式只用 `:root` 变量**：颜色从 `css/app.css` 顶部的变量取；深浅色和紧凑密度只靠
  `:root[data-theme="dark"]`、`:root[data-density="compact"]` 覆盖变量，组件里不写第二套
  配色。
- `hidden` 属性对 `display:flex` 的节点原本不生效（`.fatal` 就是），`app.css` 里补了
  `[hidden]{display:none!important}` 兜底——否则一块空的遮罩能把整个界面糊成背景色。
- 页面始终由本机 HTTP 服务加载（桌面窗口与 `--serve` 相同），不存在 pywebview
  `js_api` 桥接分支。
- 任务更新只通过 `EventSource` 连接 `GET /api/events`；断线由浏览器按 `Last-Event-ID`
  自动重连，重连后服务端补发快照。另外两处拉取是刻意保留的兜底，不是周期轮询：
  首屏 `GET /api/jobs`，进度页的日志尾部 `GET /api/jobs/{id}/log?tail=`。
- 设置页的字段表（`FIELDS` / `LABELS`）在前端，因为 `GET /api/settings` 只回
  `{settings, warnings, sections, pathKeys}`。`tests/test_ui_assets.py` 会拿
  `settings.DEFAULTS` 逐个对账，后端加键而前端没加会直接失败。
- 前端只负责界面和用户操作，不直接执行磁盘命令；所有写盘都要过后端
  `POST /api/prepare → submit → confirm` 的五道闸门。协议、路由、Host 校验、事件缓冲
  和部署限制见 [Web API 文档](../../../Docs/web-api.md)。

## 静态检查与冒烟

```powershell
python tools/ui_check.py                                                  # 语法/引用/纯函数行为
node tools/ui_smoke.mjs --url http://127.0.0.1:8765/ --out build/shots     # 浏览器冒烟 + 截图
```

`ui_check.py` 也需要 `node`（只做语法与纯函数行为，不挂浏览器依赖）；没有 `node`
时打印“跳过”并以 0 退出。`ui_smoke.mjs` 用 headless Chrome 的 DevTools 协议走一遍
主页面、四步向导、设置页、菜单和修复引导弹窗，把 `{ok, problems, steps}` 打到 stdout，
截图落到 `--out` 指定的目录。
