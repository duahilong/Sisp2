# Web API 与部署说明

实现：`app/gui_web/main.py`、`server.py`、`api.py`；前端为 `ui/index.html`（薄壳）+ `ui/css/app.css` + `ui/js/*.js`（ES 模块，由 `/static` 托管）。桌面窗口和浏览器模式共用 HTTP、REST、SSE。成功/业务失败使用 `{"ok":true,"data":...}` / `{"ok":false,"error":...}` 信封（Host 拒绝使用 HTTP 403）。

## 启动与部署

```powershell
python -m app.gui_web.main                 # 桌面窗口
python -m app.gui_web.main --serve          # 前台服务
python -m app.gui_web.main --serve --port 8765
```

参数：`--dry-run`、`--elevate`、`--debug`、`--serve`、`--port`（默认 8765）、`--host`（默认 `127.0.0.1`）、`--settings`、`--width`、`--height`。（`-j/--config` 已移除。）桌面模式需要 pywebview；`--serve` 不依赖它。两种模式都加载同一个本机 HTTP 服务。

桌面模式的进程模型：**窗口进程不提权**（exe 清单 `asInvoker`），它会用 UAC 拉起
`Sisp2.exe --backend --serve --host 127.0.0.1 --port <N> --parent-pid <界面 pid>`
作为提权后端，窗口加载后端提供的 `http://127.0.0.1:<N>/`。所以：

- 界面进程不持有管理员令牌，也不建 `WorkflowController`（所有业务状态都在后端）；
- `--backend` / `--parent-pid` 是内部参数（argparse 里 `SUPPRESS`），不要手工依赖；
- 后端只监听回环地址，且父进程消失时自己退出；
- 提权被取消时退回进程内服务（普通权限），此时真实写盘会被预检拦住。

有效 GUI dry-run 是 `--dry-run OR settings.dryRunByDefault`；两者默认均为 false。该值在启动时锁定为硬上限，`POST /api/session` 传 `dry_run:false` 或 `POST /api/prepare` 传 `dry_run:false` 都不能升级为真实执行。`POST /api/session` 与 `POST /api/prepare` 的 `dry_run` 可省略，省略表示**沿用进程模式**（前端刷新会话时不再把强制演练悄悄降级成真实模式）。服务必须单进程单 worker，因为任务和缓冲状态保存在内存。端口被占用入口返回 4；FastAPI 自动文档地址默认为 `http://127.0.0.1:8765/docs`（地址随 host/port 改变）。

## 路由

| 方法与路径 | 行为 / 请求要点 |
|---|---|
| `GET /` | 托管 `ui/index.html` |
| `GET /static/*` | 托管 `ui/css`、`ui/js`（无构建步骤，直接送 ES 模块） |
| `GET /health` | 健康探针 |
| `GET /api/info` | 版本、平台、会话 dry-run |
| `POST /api/session` | `{dry_run?}`；重启会话并清空 WebApi 事件缓冲 |
| `GET /api/disks` | 重扫硬盘 |
| `POST /api/prepare` | `{selection, dry_run}`；selection 为盘号数组或盘号到覆盖对象的映射（每盘覆盖支持 `c_size`、`data_size`、`data_partitions`、`win_gho`、`image_index`）；省略 selection 表示「本次扫描里所有可选定的盘」，显式传空数组仍然报错（`E_PREPARE`） |
| `POST /api/submit` | 登记等待确认任务 |
| `POST /api/confirm` | `{job_ids, confirm_text?}`；`typeToConfirm` 开启时 `confirm_text` 必须是盘号或磁盘型号 |
| `POST /api/cancel` | `{job_id}`，协作式取消 |
| `GET /api/jobs` | 任务列表（首屏兜底，SSE 未连上时也能画出进度） |
| `GET /api/jobs/{job_id}` | 单任务查询（含 `progress`） |
| `GET /api/jobs/{job_id}/log?tail=200` | 任务日志尾部；只允许 `logDir` 下的 `.log` 文件，越界返回 `E_PATH` |
| `POST /api/partition/preview` | `{disk_number?}`；返回**真实会执行的三段 diskpart 脚本**（只读，需先 `/api/prepare`；与执行器同源生成） |
| `POST /api/images` | `{directory?, path?}`：给 `directory` 列镜像目录（`kind: gho/iso/legacy`、`installable`、`note`），给 `path` 校验单个 GHO（读文件头 `FE EF`，返回 `magic_ok`/`block_index`）。全部是纯文件读取，不 spawn 任何进程 |
| `GET /api/boot/report` | 扫描各盘的 Windows 目录与 ESP，返回可修复性报告 |
| `POST /api/boot/repair` | `{disk_number}`；临时盘符挂 ESP → `bcdboot … /f UEFI` → 重新扫描，结束必定释放盘符；真实模式受写盘闸门约束 |
| `POST /api/events/poll` | `{cursor, limit}`，游标轮询补发 |
| `GET /api/events?cursor=N` | SSE 事件流；可读 `Last-Event-ID` |
| `GET /api/settings`、`PUT /api/settings`、`DELETE /api/settings` | 读取、patch 更新、重置设置；patch 任一项非法整体拒绝 |
| `POST /api/settings/export`、`POST /api/settings/import` | 导出/导入设置（封套 `{"version":1,"settings":{…}}`；导入任一字段非法则整体拒绝） |
| `POST /api/settings/probe` | `{key}` 路径探测 |
| `POST /api/settings/probe_all` | 探测路径设置 |
| `POST /api/settings/open_folder` | 打开设置文件夹（Windows） |
| `POST /api/dialog/pick` | `{key, kind}` 原生文件/目录选择；需有 pywebview 窗口 |

业务路由以同步 `def` 实现，由 FastAPI 在线程池运行。

### `POST /api/prepare` 的返回（每个盘一项）

`plan` 里除了 `executable` / `blockers` / `checks` / `drive_letters`，还有分区布局的实际结果：

```jsonc
{
  "drive_letters": { "efi": "E", "windows": "F", "data": "G", "data2": "H" },
  "partition_plan": [
    {"role":"efi","label":"EFI","file_system":"FAT32","size_mb":300,
     "size_display":"300.00 MB","letter":"E"},
    {"role":"windows","label":"Windows","file_system":"NTFS","size_mb":122880,
     "size_display":"120.00 GB","letter":"F"},
    {"role":"data","label":"Data","file_system":"NTFS","size_mb":307200,
     "size_display":"300.00 GB","letter":"G"},
    {"role":"data2","label":"Data2","file_system":"exFAT","size_mb":204800,
     "size_display":"200.00 GB","letter":"H"}
  ],
  "unallocated_bytes": 0
}
```

- `role` 与 `drive_letters` 的键一一对应；`data2` 只有加了额外数据分区（E）才出现。
- `partition_plan` 是界面第 4 步的直接数据源（真实大小、文件系统、盘符），不要自己再算。
- `unallocated_bytes` 是按计划建完分区后剩下的未分配空间：最后一个数据分区永远 `auto`（不带 `size=`），所以正常情况下是 0，只可能剩下 GPT 首尾那点对齐余量。
- 同一份布局也决定了容量预检（`LAYOUT_SLACK_MB = 3`：GPT 盘首/盘尾各 1 MB + 1 MB 冗余）与 `verify` 的期望分区数（**等于** `len(partition_plan)`：MSR 已删，多一个或少一个都算失败）。


## Host 校验与安全

默认 loopback 绑定启用 Host 校验。按 Host 主机名（去端口后）允许 `127.0.0.1`、`localhost`、`::1`、`[::1]`，非空且不匹配返回 403 `E_HOST`。这只是 Host 头限制，不是用户身份认证、授权或 CSRF 防护。

`--host` 为非 loopback 时，入口关闭 Host 检查并输出警告。当前无鉴权，网络可达时写盘相关 API 也可访问，属高风险，勿暴露于不可信网络。业务写盘门禁的入口差异、GUI 隐式白名单等见[安全说明](安全边界.md)。

## SSE 游标与缓冲

SSE 首帧为 `snapshot`，包含 `poll_events(cursor, 1000)` 返回的可用补发事件及任务快照；后续帧为 `{cursor, event, jobs}`，事件帧 `id` 为整数 `seq`。客户端重连时服务端取查询参数 `cursor` 与合法数字 `Last-Event-ID` 的较大值。空闲约 15 秒发 `: ping`。每个连接订阅 JobManager，连接结束时取消订阅。

进度事件是 `job.step`：

```json
{"key":"windows","status":"running",
 "message":"正在安装系统分区",
 "progress":{"total":6,"completed":["efi"],"current":"windows","percent":33,"message":"正在安装系统分区"}}
```

`key` 取 `STEP_TITLES` 的键（`efi/windows/data/image/boot/verify`），`status` 是
`running` 或步骤结果（`ok`/`failed`/`skipped`）。同一份 `progress` 也会写进
`Job.progress`，随 `GET /api/jobs` 一起返回，所以前端断开重连后不用重放事件也能
把进度条摆对。

补发并非持久、无损队列：WebApi 只在内存保留最多 5000 个事件，超量淘汰最旧事件；SSE 每连接队列最多 2000 条，满时丢弃新事件。缓冲淘汰或队列丢弃后不能保证完整补发。服务重启也清空内存缓冲。`POST /api/events/poll` 使用同一游标缓冲，并非独立持久化恢复机制。

## 前端开发（`app/gui_web/ui`）

Web 界面前端。运行时 `app/gui_web/main.py` 托管 `index.html`；`server.py` 另外把
`/static` 挂到同一目录，因此前端不需要打包工具、不需要跨域，也没有 npm 依赖。

### 目录

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

### 开发约定

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
  和部署限制见本文后面的「路由」「Host 校验」章节。

### 静态检查与冒烟

```powershell
python tools/ui_check.py                                                  # 语法/引用/纯函数行为
node tools/ui_smoke.mjs --url http://127.0.0.1:8765/ --out build/shots     # 浏览器冒烟 + 截图
```

`ui_check.py` 也需要 `node`（只做语法与纯函数行为，不挂浏览器依赖）；没有 `node`
时打印“跳过”并以 0 退出。`ui_smoke.mjs` 用 headless Chrome 的 DevTools 协议走一遍
主页面、四步向导、设置页、菜单和修复引导弹窗，把 `{ok, problems, steps}` 打到 stdout，
截图落到 `--out` 指定的目录。
