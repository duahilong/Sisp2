# 变更记录

版本号只有一处来源：`app/version.py`（`packaging/version_info.txt` 与它由
`tests/test_packaging.py` 强校验）。每个发布版在 Git 里对应一个**注释 tag**，
tag 上的提交树就是那一版的完整代码与文档快照。

定版（`v1.1.0`）之后只做优化与修复：功能以 tag 为准，`main` 继续向前走，
需要回到某一版时 `git checkout v1.1.0` 或 `git archive v1.1.0`。

## 未发布（main）— 执行可见性与定向扫描

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
