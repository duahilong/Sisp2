# 变更记录

版本号只有一处来源：`app/version.py`（`packaging/version_info.txt` 与它由
`tests/test_packaging.py` 强校验）。每个发布版在 Git 里对应一个**注释 tag**，
tag 上的提交树就是那一版的完整代码与文档快照。

定版（`v1.1.0`）之后只做优化与修复：功能以 tag 为准，`main` 继续向前走，
需要回到某一版时 `git checkout v1.1.0` 或 `git archive v1.1.0`。

## 1.2.2 — 老 Win10 上「窗口开了但连不上」给出明确提示

**症状**：在一台较旧的 Windows 10 上双击 exe，窗口能打开、标题栏和导航都在，
但磁盘列表空着、状态一直停在“连接中”，点任何按钮都没反应，看起来像“后端没起来”。
后端其实是好的——页面本身就是后端发的，问题在**渲染内核**。

### 根因

pywebview 6.x 的 winforms 后端在 import 时选内核：只有「.NET Framework ≥ 4.6.2」
且「WebView2 运行时主版本 ≥ 86」同时成立才用 Chromium，否则**静默**退回
MSHTML（IE11）。而界面只有一个入口 `<script type="module">`，IE11 连解析都不解析，
也不支持 `fetch`——整块脚本不执行，只剩静态骨架。pywebview 的告警只走 stderr，
打包产物是 `console=False`，stderr 进空设备；窗口进程此前根本没配日志，所以
用户和日志目录里都看不到任何线索。

### 改动

- 新增 `app/gui/webengine.py`：建窗口前按 pywebview 同一套判据（同样的注册表位置、
  同样“只比主版本”的规则）探测内核，输出 WebView2 / .NET 版本与问题清单。
- `app/gui_web/main.py`：内核不是 Chromium 时**弹消息框说明原因并退出**（退出码 5），
  正文给出 WebView2 下载地址与 `--serve` 的替代方案；不再给一个点不动的窗口。
- 窗口进程也落盘 `app.log`（`configure_window_logging()`）：内核探测结果、提权后端
  拉起、UAC 被拒、端口占用、服务超时、正常退出都留痕，以前这些都只走 stderr。
- 前端兜底：`ui/js/engine-guard.js`（**ES5 普通脚本，排在模块之前**）在两种情况下
  在页面顶部压一条红色提示——内核明显跑不了模块（`document.documentMode` / 无
  `fetch`），或 10 秒内主模块没有置位 `window.__sispBooted`；另加 `<noscript>` 提示。
  样式同时写在 `app.css` 与内联兜底里，避免“样式表也没加载上”时看不见。
- 版本号 1.2.2，`packaging/version_info.txt` 同步。

### 测试

- 新增 `tests/test_webengine.py`（19 条）：版本解析（含 `120.0.1-beta` 这类后缀）、
  四个 WebView2 GUID、.NET 门槛、主版本判定与 pywebview 一致、坏值/缺键不抛异常、
  提示文案含下载地址与 `--serve`、`_run_desktop` 在内核不满足时返回 5 且弹框、
  正常时继续下发，以及“窗口 logger 不能用 `__name__`（冻结后是 `__main__`，
  会落到 root 导致一条日志都写不进 app.log）”。
- `tests/test_ui_assets.py` 新增 4 条：兜底脚本必须排在模块之前、必须是 ES5、
  主模块必须置位 `__sispBooted`、`index.html` 必须有 `<noscript>` 提示。
- `tests/conftest.py` 新增 `app_logger_isolated`：用例结束后还原 `app` logger 的
  handler，避免日志句柄串到后面的用例、甚至写进真实 `%APPDATA%\Sisp\logs`。

### 验证（VM：Windows 10 19045 + Python 3.15.2）

- `ruff check .` 全绿、`compileall` 通过、`tools/ui_check.py` 11 个模块通过；
  `pytest tests/ -q` → **550 passed**（原基线 527 + 本次新增 23）。
- **桌面窗口模式（真实 exe）**：用 explorer 启动（等价用户双击），app.log 完整落盘
  —— `界面进程启动，日志文件：…` / `渲染内核检查通过：内核=chromium
  WebView2=157.0.4341.42 .NET Release=533325` / `已拉起提权后端，等待 /health 就绪…`
  / `打开窗口：http://127.0.0.1:8765/`；UI Automation 从 WebView2 里读到真实渲染结果
  （`v1.2.2`、`2 块磁盘`、`7 个分区`、两块盘的完整分区树），且**没有**出现兜底红条。
- **缺少 WebView2 的失败路径（真机模拟）**：临时摘掉
  `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-…}` 后启动 exe →
  源码侧探测得到 `mshtml`；exe 弹出我方的说明框（UIA 读到的正文与 `message()`
  完全一致，含下载地址与 `--serve` 方案）、**没有拉起提权后端**、`app.log` 记
  `ERROR 渲染内核不满足，拒绝打开窗口：…`、点掉对话框后**退出码 5**；随后恢复注册表，
  `pv=157.0.4341.42`、探测回到 `chromium`。
- **前端兜底**：把 `main.js` 从静态目录删掉（模块 404），headless Edge + CDP 等 13 秒后
  `#engineWarn.engine-warn` 出现且文案正确；把 `main.js` 放回去后同一探测得到
  `booted=true / banner=false`（不误报）。
- **真实写盘回归（exe 1.2.2）**：2.5 TB 动态 VHDX（声明容量，实际占用随写入增长）上
  `tools/ui_smoke.mjs --start --allow-write` → `ok=true`、`problems=[]`、18 张截图；
  七步与基线一致（`efi/windows/data/layout/image` ok，合成镜像使 `boot` 失败、
  `copy` skipped），EFI 盘符释放、系统盘与数据盘分区结构未变。

## 1.2.1 — 清理非业务代码（无行为变化）

这一版不新增功能，只删除“为测试 / 为兼容 / 为已下线功能”而留在生产路径上的
代码，并把重复的小工具收敛成一份。删除依据是逐符号的引用扫描（AST + 全仓
grep + 测试/文档/前端），保留的每一项都有真实调用方。

**安全向**

- 删掉 `SispService.scan_disks()` / `scan_snapshot()` 在 `disk_info` 无法导入时
  的静默回退（`_SCAN_SCRIPT` / `_scan_with_powershell` / `_normalize_summary`）。
  回退摘要没有分区与盘符，"包含系统盘符"的保护会静默失效；现在缺模块直接报错。
  同时删除构造参数 `powershell=`（只服务于那条回退）。
- `WebApi._gate()` 不再对"没有 `require_disk_writable` 的测试替身"降级为只读扫描，
  写盘闸门只有一条路径。
- 盘符分配只保留 `app.drive_letters` 一份实现，删除 `service` 内的本地兜底池
  （`AVAILABLE_DRIVE_LETTERS` / `LETTERS_PER_DISK` / `DRIVE_LETTER_ROLES`）。

**删除的死代码 / 测试专用入口**

- `workflow._apply_image_defaults()`、`workflow._image_blocker()`
- `coordination.SequencerLease`
- `image.find_default_image()`、`list_directory()`、`KIND_LEGACY`/`KIND_ISO`、
  `LIST_SUFFIXES`（目录列举链整条下线；镜像入口只有设置 A/B 槽位）
- `real_runner.data_fs_token()` / `_FS_TOKENS`、`_partition_verdict` 别名
- `SettingsStore.get_warnings()`、`probe_all()` 与 `POST /api/settings/probe_all`
  （设置页一直用的是逐项 `probe`）
- `version.VERSION_TUPLE`（测试按 `__version__` 现场计算）
- `api.ProbeImages(directory=...)` 兼容参数、`server` 里的 `directory` 转发
- 前端：`state.dataFs()`、extras 的 `fs` 字段、payload 里的 `file_system`
  （数据分区恒为 NTFS，后端 `DATA_FS_CHOICES` 是唯一判定）

**一致性**

- 新增 `app/units.py`：`BYTES_PER_MB` / `BYTES_PER_GB` / `format_size` 的唯一实现；
  `contracts`、`partitions`、`service`、`disk_info` 改为引用它（`contracts` 继续
  重导出，旧 import 不受影响）。
- `service` 的 `_text`/`_as_int` 改用 `runner_utils.to_text`/`to_int`。
- 清掉"业务配置文件 / 任务落盘 / 重放"等已不存在机制的注释。

**版本与验证**

- 版本号升到 1.2.1，`packaging/version_info.txt` 同步。
- 测试同步：删除随功能一起消失的用例，新增/改写权限与探测用例；全部测试与
  真实写盘只在 Windows VM 上执行（见 `Docs/历史测试记录.md`）。
- `tools/ui_smoke.mjs` 改为选择**容量最大**的可选盘，并前置断言该盘 ≥ 240 GB：
  原来固定选第一块盘，小盘上「快捷容量 200」会让第 2 步整组断言失真（旧 `HEAD` 上
  同样失败，属测试工具的环境假设，不是界面回归）。已在 500 GB VHD 上验证 `problems` 为空。

## 1.2.0 — 移除演练模式（破坏性）

启动即真实模式。`--dry-run`、`SispService(dry_run=...)`、`JobSpec/ExecutionPlan.dry_run`、
`POST /api/session|prepare` 的 `dry_run` 字段全部删除，涉及 19 个 Python 文件、6 个前端文件。

### 为什么删

- “看计划”本来就不需要演练模式：`prepare()` 与 `GET /api/partition/preview` 一直是只读的
  （只扫描、算布局、生成与真跑同源的 diskpart 脚本），写盘只发生在 `POST /api/confirm` 之后。
- 两套模式让“以为在演练、其实在写盘”成为可能：旧实现里 `SispService` 默认 `dry_run=True`，
  真实入口再显式覆盖；漏传一处就静默降级成空跑，或者反过来把演练请求当成真跑。
- 删掉之后执行路径只有一条：没有执行器就拒绝（`PermissionError`），不存在“静默跳过”。

### 行为对照

| 旧行为 | 现在 |
|---|---|
| `--dry-run` 启动只演练 | 参数已删除；要预览用第 4 步/`prepare`/`partition/preview` |
| `SispService(dry_run=True)` | 构造参数已删除；未注入 `real_runner` 时执行层直接拒绝 |
| `POST /api/session|prepare` 带 `dry_run` | 字段已删除；`prepare` 只读，`confirm` 才写盘 |
| 任务 `progress` 里多一个 `diskpart_preview` 步骤 | 不再产生；进度仍是 7 个真实阶段 |
| 界面“演练模式”横幅/徽标/“未执行（演练）”文案 | 删除；只显示“真实写盘” |

### 迁移

- 脚本/自动化若传 `dry_run`，请求仍可解析（多余字段被忽略），但**不会**再进入演练：确认后会真写盘。
- 需要“先看后写”的流程：`POST /api/prepare` → `GET /api/partition/preview` → `POST /api/confirm`。
- 版本号升到 1.2.0，exe 的 Windows 版本资源同步更新。

### 验证（2026-11-08，VM）

- 单测：`D:\venv-sisp\Scripts\python.exe -m pytest -q` → **531 passed**（12.31 s）；
  `ruff check app tests` 全绿。
- 源码真实写盘（120 GB 动态 VHD，Disk 3）：`status=ok`，**165.0 s**；
  GPT 三区 `#1 100 MB FAT32 EFI(E:)`、`#2 60 GB NTFS(F:)`、`#3 59.9 GB NTFS(G:)`；
  `F:\Windows\System32\config\SYSTEM`、`ntoskrnl.exe`、`explorer.exe` 均在；
  ESP 内 `EFI\Microsoft\Boot\bootmgfw.efi` 与 `EFI\BOOT\BOOTX64.EFI` 均在；EFI 盘符已释放。
- 打包后 exe 真实写盘（同 VHD，`--serve --port 8898`）：`status=ok`，**167 s**；
  盘符 E/J/K，步骤 `efi/windows/data/layout/image/boot` 全 ok、`copy` skipped；
  产物 `Sisp2.exe` 17.3 MB，`/api/info` 报 `version=1.2.0`。
- 回归中修掉的两个测试自身缺陷：`test_step_events_are_broadcast_for_every_stage`
  误写 `manager.executor`（`JobManager` 读的是 `_executor`，旧代码靠 dry-run 短路才“通过”）；
  `test_service_without_runner_refuses_execution` 未给白名单/身份，断言的是白名单文案而非执行器文案。

## 历史：1.2.0 之后的死代码清理（已并入 1.2.1）

定版后只做优化与修复，不改变既有功能语义。这一批只删“定义了但没人用”的代码，
行为、接口与失败文案均未变（22 个文件，+16 / −252 行）。下面的删除项已包含在
1.2.1 中，保留本节是为了对照当时的提交（`d3d49ee`）。

### 清理

- **从未启用的任务事件落盘**：删除 `JobManager(event_dir=...)`、`_event_dir`、
  `_event_file`（`jobs.jsonl`）与 `_persist_event()`。生产入口与测试都没有传过 `event_dir`，
  这条链既不会写文件、也没有任何读取方；任务状态本来就只在内存里（单进程）。
- **未引用的类型/常量/函数**：`contracts.DiskListResult`、`partitions.role_names()`、
  `models.STEP_STATUSES`、`WorkerResult.failed_steps/skipped_steps`、
  `boot.RepairResult`、`boot.SCAN_TIMEOUT`、`boot.collect_free_letters()`、
  `service._find_disk_identity()`、`service.build_diskpart_segments()`、
  `uefi.BootTarget.efi_root/windows_root`、`orchestrator.describe_identity()`、
  `preflight.summarize_failures()`、`gui.elevation.SE_ERR_FNF/ACCESSDENIED`、`api.WebApi.settings_data()`、
  `workflow.SessionState.selectable_disks`、`workflow._apply_partition_defaults()`、
  `workflow.set_settings_provider()`、`phases.copy._describe()`。
- **未用形参**：`partition._fail(ctx → 去掉)`、`real_runner._build_shell(options → 去掉)`、
  `gui_web.main._wait_until_ready(server → 去掉)`；对应测试的 monkeypatch 同步收敛为单参。
- **前端死导出**：`dom.setText/timeOf`、`shell.pushJobLog/isModalOpen`、
  `state.totalPartitions/activeJobs/targetsValid/diskFreeGb`、`view-main.jobProgressText`、
  `view-settings.probeAllSettings`。（当时保留的 `probe_all` 接口已在 1.2.1 删除。）
- 文档同步：`Docs/安全边界.md` 删掉“可选 JSONL 事件落盘”的说法（该功能已不存在）。

### 测试（全部在 VM 上执行）

- `D:\venv-sisp\Scripts\python.exe -m pytest -q` → **539 passed**（12.90 s）；
  `ruff check app tests` 全绿；本机只做静态检查，未运行任何测试。
- 干跑冒烟（源码 `--serve --dry-run --port 8899`）：会话扫到 4 块盘；
  prepare 盘 0 / 盘 2 → 盘符 E/F/G 与 J/K/P、0 blockers、每盘 3 个分区；
  `preview` 返回 3+3 段脚本；submit/confirm 后两个任务 `ok`、7/7 步、100%。
- **真实写盘（VM，120 GB 动态 VHD，5.06 GB GHO，清理后的代码）**：
  `status=ok`，**169.5 s**；GPT 三区（ESP 100 MB FAT32 / Windows 60 GB NTFS / Data 59.9 GB NTFS）、
  `F:\Windows\System32\config\SYSTEM`、`ntoskrnl.exe`、`explorer.exe` 均存在；
  `bcdboot F:\Windows /s E: /f UEFI` 成功，`boot` 步骤校验通过；收尾释放 EFI 盘符 E；
  日志里 8 条静默心跳（每 20 s 一条），证明写镜像期间仍可观察。

## 历史：执行可见性与定向扫描（已含在 1.2.0）

定版后只做优化与修复，不改变既有功能语义。这一批改了两处。

### 新增

- **子进程流式输出**：`app/application/streaming.py` 用 Popen 按行读取 stdout/stderr，
  `CommandLog.streaming()` 逐行追加并 flush。diskpart / Ghost / bcdboot / robocopy 运行期间，
  任务日志与 `/api/jobs/{id}/log` 就能看到内容，不再等进程结束一次性落盘。
- **Ghost 进度与真取消**：解析命令输出里的百分比，换算成 `image` 步骤的内部比例，
  任务的 `percent` 按“已完成步数 + 当前步比例”插值且只前进不后退；
  取消会终止 Ghost 子进程（terminate → 等 5 s → kill），EFI 盘符仍照常释放。
  diskpart 分区中途不强杀，仍在“清盘后/建分区后”两个检查点停。
- **静默心跳**：命令连续 20 秒没有任何输出时，日志里写一条
  `… 仍在运行（已 N 秒，暂无输出）`。实测 Ghost 在输出重定向时不打印进度，
  心跳是“还在写”与“卡死”的唯一区分（真实装机 166 s 的日志里有 7 条）。
- **一次批量快照**：会话开始/刷新用一条 PowerShell 同时取“磁盘 + 分区 + 卷”和“已用盘符”；
  `prepare()` 复用会话快照里的盘符（跑过任务后自动回到现场重扫）。
- **写盘闸门定向复核**：`SispService._gate_target` 只查目标盘（`VIX_DISK_NUMBERS`），
  不再为每块盘重扫全部磁盘；白名单、身份比对、保护规则与失败文案保持不变。

### 变更

- `POWERSHELL_SCRIPT` 支持 `VIX_DISK_NUMBERS` 过滤，新增 `SNAPSHOT_SCRIPT` 与
  `scan_snapshot()`；`run_powershell()` 支持 `env`。
- `PhaseContext.notify()` 支持可选的 `fraction`；`on_step` 三参回调继续可用（按签名探测）。

### 测试

- 新增 `tests/test_streaming.py`、`tests/test_scan_targeting.py`；VM（Windows 10 19045 +
  Python 3.14.10）全量 **539 passed**，ruff 全绿。
- VM 冒烟：源码 `--serve --dry-run` 会话扫到 3 块盘、prepare 两块盘并生成盘符/分区脚本、
  submit/confirm 后两个任务 100% 完成；交付产物 `dist/Sisp2.exe`（17.4 MB）
  以 `--dry-run --serve` 启动后 `/api/info` 版本 1.1.0、会话扫到 3 块盘。
- 实测定向扫描（3 块盘）：全盘 987 ms、单盘 853 ms、单盘快照 971 ms、全盘快照 1075 ms——
  单次查询主要耗在 PowerShell 解释器启动，收益来自“会话少一次解释器 + 闸门只查目标盘”。
- **真实写盘（VM，120 GB 动态 VHD，5.06 GB GHO）**：
  - 完整装机 `status=ok`，总用时 166.2 s（分区 1 s、Ghost 约 140 s、引导+收尾约 20 s）；
    盘上为 GPT：ESP 100 MB FAT32 / Windows 60 GB NTFS / Data 剩余 NTFS，
    `L:\Windows\System32\config\SYSTEM`、`ntoskrnl.exe`、`explorer.exe` 均存在，
    ESP 有 `EFI\Microsoft\Boot\bootmgfw.efi` 与 `EFI\BOOT\BOOTX64.EFI`，镜像占用 9.5 GB；
    EFI 临时盘符已释放，固件启动项无新增（bcdboot `/s` 只写文件）。
  - 写镜像中途取消：请求后 **0.7 s** 任务转 `cancelled`，`ghost64.exe` 已不存在，
    EFI 盘符已释放，文案“任务已取消（镜像写入被中止，分区已建好）”。
  - 观察到的限制：Ghost 重定向后无进度输出，`percent` 停在 57%（4/7），靠心跳确认存活。

## 1.1.0 — 2026-11-08（功能定版）

功能全部落地、文档与代码对齐后的定版。自 `v1.0.1` 起共 69 个提交。

### 新增

- **收尾步骤 `copy`**：把设置里「拷贝文件目录」的内容拷到**系统分区之后第一个数据分区**根目录下的
  同名文件夹（`robocopy /E /COPY:DAT /DCOPY:DAT /XJ`），保留隐藏 / 系统 / 只读属性与修改时间；
  源为空则跳过，失败即整盘失败。步骤数 6 → 7。
- **多盘并发（分区串行 + 镜像交错）**：分区组按盘号全局串行（`PartitionSequencer`），
  某块盘分区一结束就立刻启动它自己的 Ghost + bcdboot，同时放开下一块盘分区；
  2 盘实测 33 s（串行 64 s），模型上 6 盘 550 s（串行 2100 s）。Ghost 启动闸门 2.5 s +
  8027 瞬时错误重试一次；每盘独立 Ghost 工作目录与 `GHOSTERR.TXT` 归档；`diskpart` 全局互斥。
- **进度页重做**：每盘一张卡片、固定 3 列，卡片内 3 行阶段默认折叠，分区验收与任务日志都在卡片里
  以 `<details>` 收起；底部常驻操作条，取消按钮改为实心红。
- **命令日志**：`logDir` 留空时默认落盘 `%APPDATA%\Sisp\logs`（`command_log.default_log_dir()`）；
  文件名带盘号与任务唯一后缀（`disk-runner-d<N>-<stamp>-<pid>-<uuid6>.log`），任务运行中
  日志也能通过 `/api/jobs/{id}/log` 读到。
- **设置**：新增 `extBcdboot`（可指定外部 bcdboot）、`copySource`（拷贝文件目录）；
  系统镜像改成两个具名槽位 A / B，向导第 3 步按盘二选一。

### 变更

- 写引导与引导校验合并成一个 `boot` 步骤，判据只看 `bootmgfw.efi` + `config\SYSTEM`
  （GPT / 分区数 / MSR 已由 `layout` 验收负责），步骤数 7 → 6。
- 向导第 4 步改成「计划卡片 + 一个勾选框」：每盘一张 `.pcard`，多盘时同一句提示只出现一行
  （行尾标盘号），diskpart 脚本与丢失分区列表都折叠。
- 设置项 29 → **20**：删掉外部程序四个空开关、`extDiskpart`（固定系统自带）、
  引导文件目录与临时目录、`language`、`typeToConfirm`、`imageDefaultIndex`、
  `confirmBeforeStart`、硬盘型号黑名单、默认演练、身份门槛。
- Ghost 分区块序号固定第 1 块（`IMAGE_BLOCK_INDEX`），不再开放设置。
- 真实写盘只需第 4 步勾选授权：`/api/submit` 固定 `require_confirmation=True`，
  任务停在 `WAITING_CONFIRMATION`，由 `/api/confirm` 复查阻断项、补白名单后放行。

### 修复

- 主界面把 GPT 首尾 1~2 MB 保留区显示成「未分配空间」（与磁盘管理口径不一致）。
- 进度页折叠箭头显示成「□B8」（CSS 转义被写坏成控制字符）。
- 「日志」面板与「查看任务日志」始终为空（`logDir` 留空时既不落盘、也不兜底查找）。
- 拷贝只丢了外层文件夹（目标从「分区根」改成「分区根\源文件夹名」）。

### 移除

- dism / WIM 装机路线（装机只走 Ghost 还原 GHO）。
- 业务 JSON 配置文件（`win11.json`、`-j/--config`、`SISP_CONFIG`），分区固定 NTFS。
- 单盘分区工具、`capabilities` 空包。

### 测试

- 全量 **520 passed**（Windows 10 19045 + Python 3.15.2），ruff 全绿，
  `tools/ui_check.py` 10 个模块通过；真实写盘、多盘并发与拷贝均在 VM 上做过端到端。

## 1.0.1 — 2026-11-03

设置接管与单文件 GUI 发布版：`settings.json` 作为唯一配置来源、四步向导、
分区地图与进度事件、单文件 `Sisp2.exe`（asInvoker + UAC 后端子进程）。

## 1.0.0 — 2026-11-03

首个发布版：薄壳 + ES 模块界面、镜像探测、分区 / 修复引导、单文件 exe。
