# 变更记录

版本号只有一处来源：`app/version.py`（`packaging/version_info.txt` 与它由
`tests/test_packaging.py` 强校验）。每个发布版在 Git 里对应一个**注释 tag**，
tag 上的提交树就是那一版的完整代码与文档快照。

定版（`v1.1.0`）之后只做优化与修复：功能以 tag 为准，`main` 继续向前走，
需要回到某一版时 `git checkout v1.1.0` 或 `git archive v1.1.0`。

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
