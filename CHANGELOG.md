# 变更记录

版本号只有一处来源：`app/version.py`（`packaging/version_info.txt` 与它由
`tests/test_packaging.py` 强校验）。每个发布版在 Git 里对应一个**注释 tag**，
tag 上的提交树就是那一版的完整代码与文档快照。

定版（`v1.1.0`）之后只做优化与修复：功能以 tag 为准，`main` 继续向前走，
需要回到某一版时 `git checkout v1.1.0` 或 `git archive v1.1.0`。

## 1.4.0 — 分区彻底重做：建分区与格式化拆开 + `format.com /X` 兜底

现场（2026-11-10，4 块 NVMe 只成功 1 块）导出的三份日志把问题钉死了：

```
12:47:12  create partition primary size=307200   成功（0.2 s）
12:47:12  format quick fs=ntfs label=Windows     卡在 0% 整整 17 s
12:47:29  虚拟磁盘服务错误: 设备使用中  (0x80042413)
12:47:39  续跑：同一条 format → 11.7 s 后同样失败
12:47:59  续跑第 2 次 → 仍然失败
```

两块不同型号的盘（KINGSTON / GeIL）失败位置**完全一致**：EFI 100 MB 能格式化，
300 GB 的 Windows 分区不行；而第一块盘在系统空闲时 1 秒就格式化完了。
结论：**坏在「建完分区立刻格式化」**——新卷还在被系统异步挂载/枚举，
`format` 拿不到独占锁；失败后卷停在"被占用"状态，再发同一条命令一百次都一样
（第二次起 6~12 秒就失败，就是证据）。

### 改法（四件事）

1. **建分区与格式化拆成两段**
   - 第 3 段 `create_geometry_lines`：只 `create`，不 `format`、不 `assign`；
   - 第 4 段 `format_lines`：**每个分区单独一次 diskpart**，中间有 `partitionSettleMs`
     的等待。失败只影响这一个分区，也不会连带把后面几个 `create` 一起作废。
2. **格式化前等卷就绪**：轮询到「分区数 == 计划数」（最多 12 s）再开始格式化，
   把"新卷还没挂载完"这个窗口直接消掉。
3. **卷被占用就换工具**：`0x80042413`（以及 PC 忙/文件被锁一类）时按
   **1 s → 5 s → 10 s** 退避 + `rescan`，并交替使用
   `format.com <盘符>: /FS:NTFS /Q /V:<卷标> /Y /X`——`/X` 是官方的
   "必要时强制卸载卷"，正是这类失败的解法。VM 实测：diskpart 卡 17 s 失败的地方，
   `format.com` **0.2 s 格式化完 60 GB**。
   - **安全闸**：走 `format.com` 之前必须确认「这个盘符真的挂在目标分区上、
     容量也对得上」，否则宁可失败——绝不格式化别人的卷（现场机器上 L/M/N、
     S/T/U/V 都挂着别的盘的卷）。
4. **失败归因修正**：以前不管第几个分区挂，界面都标「创建 EFI 分区」✗；
   现在按真实状态标到失败的那个分区（`分区 #2（Windows）格式化失败…
   （前面 1 个分区已完成）`），并把 `failed_partition` / `format_attempts`
   放进步骤详情，导出包里直接能看到。

### 顺带

- 工具没能启动（`format.com` 缺失/被拒）时报的是原因，不再丢一个 `0xFFFFFFFF`；
- 分区脚本预览从 3 段变 4 段（多了「格式化 + 分配盘符」），内容与真跑同源；
- 非瞬时错误（权限/参数）仍然一次就报，不做无意义重试。

### VM 验证

- 单测 **674 passed**（1.3.0 为 660），`ruff` 全绿、`compileall` 通过；
- 两块 120 GB VHD + 注入「Windows 分区 format 报 0x80042413」：
  日志依次出现 `卷就绪：盘上已有 3 个分区` → `格式化分区 #2` →
  `第 2 次尝试（format.com）前等待 1s 并重新扫描` →
  `改用 format.com H: … /X（强制卸载）` → `格式化已完成` → `分区验收：通过`，
  落盘 `#1 100 MB FAT32 + H 60 GB NTFS + K 59.9 GB NTFS`；
- 注入「一直失败」：界面/日志准确标到「创建 Windows 分区」，
  数据分区与后续步骤记 skipped，失败原因里写明「前面 1 个分区已完成」；
- 打包 exe（1.4.0，18,895,466 B，SHA256 `B40BA24D9DFEC0114A004E775159E39DD81AE1D74D88135253B2B7A103DC4237`）
  同场景复测通过：`建分区未完成 → 补齐检查 → 卷就绪 → format.com /X → 验收通过`。

### 文档同步

- `Docs/架构与工作流.md`：分区从「三段 diskpart + 两次复核」改为**四段 + 三次复核**
  （清盘 / 删 MSR / 建几何 / 逐分区格式化），补上"为什么拆开"和 `format.com` 安全闸；
- `Docs/安全边界.md`：新增"格式化的两条边界"（正常 `diskpart format`，
  兜底 `format.com /X` 必须先验证盘符归属），并把导出包列为**完整现场快照**；
- `Docs/配置说明.md`：`partitionSettleMs` 说明补上"每个分区格式化前也会等"；
- `Docs/测试指南.md`：基线 531 → **674**，补 1.3.0/1.4.0 新增的六组测试；
- `Docs/打包与分发.md`：体积 22 MB → 19 MB，新增「发版记录」表（字节数 + SHA256）；
- `README.md`：版本 v1.2.1 → v1.4.0，新增「出问题怎么排查（日志与导出）」一节；
- `Docs/历史测试记录.md`：修掉重复的「八」，重排为一～十三并重建目录。

## 1.3.0 — 日志一键导出 + 日志按「型号（磁盘 N）」说话 + 分区三处新护栏

三件事来自同一句现场反馈："又出现错误了，只知道是 disk1，日志太长看不懂，
还得手动找文件。"所以这一版不改功能，只改**出问题时能不能一眼看清、能不能
把证据带走**。

### 1. 安装页「⤓ 导出日志」

进度页底部（以及每块盘日志弹窗里）多了一个按钮：把这一轮任务打成一个 zip。

```
Sisp2-日志-20261109-1720.zip
├── README.txt      这个包是什么、先看哪个、怎么回传
├── summary.txt     人读：每块盘的型号/容量/每步结果/失败步骤原文/诊断（先看这个）
├── summary.json    机器读：同上的结构版本（贴 issue 也行）
├── settings.json   当时的设置快照（只有本机路径，不含任何凭据）
├── logs/           原始执行日志 disk-runner-d2-*.log（diskpart 命令、退出码、耗时、重试/续跑）
└── artifacts/      ghosterr-*.txt 之类（有才带）
```

- 弹原生保存框（默认落在桌面），取消不写任何文件；
- 任务还在跑时也能导：没有 ``log_path`` 的按「盘号 + 启动时间」兜底找最新日志；
- 导完可直接「打开所在文件夹」（``POST /api/reveal``，只允许家目录/日志目录内的路径）。

### 2. 日志与统计改成「型号（磁盘 N）」

盘号会变（热插拔/换口），型号不会；四块同款盘只写 ``磁盘 2`` 谁也认不出。现在：

```
===== Msft Virtual Disk（磁盘 2）=====
  磁盘号    2
  型号      Msft Virtual Disk
  容量      120.00 GB
  序列号    ...
  盘符      efi=G windows=H data=K
...
[17:16:35] [诊断] 开始分区（diskpart 间隔 1000 ms；其它盘活动：GeIL P3A 2TB（磁盘 4）=写镜像（Ghost））
[17:16:39] 步骤 ✓ 硬盘分区：已创建、格式化并通过验证（60 GB，盘符 H）
[17:16:41] 步骤 ✗ 写引导：bcdboot 失败（退出码 31）…
===== 结论：失败 =====
  原因      写引导失败
  失败步骤  写引导：bcdboot 失败（退出码 31）
```

- 日志文件名也带型号片段：``disk-runner-d2-msft-virtual-disk-2026…log``
  （``disk-runner-d2-`` 前缀保持不变，界面按盘号找日志的老逻辑照旧）；
- 界面卡片标题、分区验收/阻塞项/写盘闸门的报错、``activity`` 诊断行统一用同一个标签；
- 新增 ``app/application/disk_label.py``：型号为空/占位值（``None``/``unknown``/``未知型号``）
  一律退化成 ``磁盘 N``，三种形态（句子里 / 卡片 / 纯型号）各有单测。

### 3. 分区（diskpart）三处新护栏

| 护栏 | 做法 | 防的是什么 |
| --- | --- | --- |
| **动盘前复核** | 清盘前 + 建分区前各查一次 ``Get-Disk``（型号/序列号/容量/脱机/只读/健康） | 批量装机排队几分钟后盘号漂移、托盘掉线、换盘——``clean`` 不可逆 |
| **删 MSR 可重试** | 瞬时错误（CRC/设备未就绪/卷占用）→ 重扫 + 退避重试，**重试前先复查 MSR 还在不在** | 慢盘/USB 桥接偶发读错就整盘作废；又不会重复删（撞"分区不存在"） |
| **验收后补做** | 验收不通过 → 按真实分区表补 ``format/assign`` 一次 → 再验一次 | 某个分区没格式化（RAW）或盘符没挂上这类"差一口气"的失败 |

复核的失败策略是刻意不对称的：**查得到且不一致 → 立刻中止**（安全）；
**查询本身失败 → 只记日志、继续执行**（不拿抖动当结论，任务开始前的写盘闸门
已经复核过一次）。补做只做 ``format``/``assign``，大小/类型不符这类"补不动"的
问题不硬来。

### VM 验证

- 单测 **660 passed**（1.2.5 为 603），`ruff` 全绿、`compileall` 通过、
  `tools/ui_check.py` 11 个模块通过；
- 两块 120 GB VHD + 注入 `0x80042413` 的实跑：`[复核]` 两条、续跑日志、
  `步骤 ✓/✗`、结论块齐全；日志从约 350 行降到 **189 行**（只读查询的 PowerShell
  脚本折成一行）；`POST /api/logs/export` 产出 12.7 KB 的排查包；
- 打包 exe（1.3.0，18,884,497 B，SHA256 `CD207435…62E5DE8B`）复测同样通过，
  `/static/js/view-install.js` 里确认带上了导出按钮。

### 现场判读（新增）

导出的 ``summary.txt`` 末尾自带排查顺序：关掉该盘资源管理器窗口与杀软实时扫描、
换数据线/直连口、看 SMART ``C7``（UltraDMA CRC）是否在涨——涨说明是线/口/桥接板。

## 1.2.5 — `0x80042413`（VDS：卷被占用）不再整盘放弃，按真实分区表续跑

承接 1.2.4。用户拿 1.2.4 实测 2 块盘，其中 1 块在「硬盘分区」阶段失败：

```
[13:40:23] 虚拟磁盘服务错误:
[13:40:23] 设备使用中。
[13:40:23] 所选卷或分区正在使用。
[13:40:23] 若要继续操作，请使用 OVERRIDE 参数。
[13:40:23] 退出码 2147755027，用时 18.9s
```

`2147755027 = 0x80042413` 是 **VDS（虚拟磁盘服务）** 自己的错误码，不是 Win32 的
`0x8007xxxx`，所以 1.2.3 的翻译表完全没覆盖它——用户又只看到一个裸数字。
日志里成串的「百分比已完成 0」只可能来自 `format`，说明：**分区其实已经建出来了**，
只是某个 `format` 赶上卷被别的进程抓住（杀软实时扫描、资源管理器、索引服务等），
而建分区脚本是「一条脚本跑到底」，一条命令失败就整盘放弃，白白浪费 52.8 s。

### 改了什么

1. **补 VDS 错误码翻译**（`app/application/shell.py`）：`0x80042413`（卷/分区正在使用）、
   `0x80042408`（分区非空）、`0x8004240C/0E/0F`（盘不在/脱机/不可用）、
   `0x8004240A`（操作被拒绝）、`0x80042406`（大小不被接受）、`0x80042405`（找不到对象）、
   `0x80042404`（另一个磁盘操作在进行中）——都带「下一步怎么办」，不再是裸码。
2. **建分区失败后按真实分区表续跑**（`partitions.create_repair_lines` +
   `PartitionPhase._repair_create`）：重扫磁盘 → 查询现场分区 → 只补没做完的
   （缺的 `create`、没格式化的 `format`、没分配盘符的 `assign`），最多 2 次。
   重跑原脚本会在 `create partition` 上撞「分区已存在」，所以续跑**必须**按现场状态生成；
   续跑里绝不重复 `create` 已存在的分区（有单测钉住）。
3. **`clean` 也能受益**：`0x80042413 / 0x80042404 / 0x80042405` 加入瞬时类错误，
   幂等的清盘段会重扫 + 重试（1.2.3 的通道直接复用）。

### 为什么这样修，而不是简单“重试整条建分区脚本”

`create partition` **不幂等**：已经建出来的分区再 `create` 会失败，重试只会把
「1 个 format 被占用」放大成「分区已存在 + 整盘失败」。续跑是按**磁盘真实状态**
补齐，既解决了占用问题，又保证不会重复建分区。

### VM 验证（注入真实 `0x80042413`）

用 `diskpart.exe` 包装器（PATH 优先）在两块 120 GB VHD 的第一块上注入失败：
建分区脚本「只真实执行到第一条 `format`，随后返回 `0x80042413`」。验证结果：

- 生效行：`建分区未完成（2147755027 = 0x80042413 所选卷或分区正在使用（虚拟磁盘服务：设备使用中）），按真实分区表续跑`
  → 等 1000 ms（`partitionSettleMs`）→ `补 8 条命令` → `select partition 1 / assign letter=G`
  只补 EFI 盘符、C/D 照建 → `分区验收：通过`；
- 两块盘分区组都是 4/4 ok，落盘 `#1 100 MB FAT32 + 60 GB NTFS + 59.9 GB NTFS`，
  EFI 临时盘符按设置释放；任务失败原因只有合成 GHO 的 `bcdboot` 退出码 31（预期）；
- 单测（VM）`pytest -q` **603 passed**（1.2.4 为 593，新增 10 条：续跑生成器 4、
  失败恢复 4、VDS 码翻译 2），`ruff` 全绿、`compileall` 通过。

### 现场判读

如果续跑 2 次仍失败，报错会写清「已按真实分区表续跑 2 次仍未完成」并附建议。
那种情况基本不是程序问题，按建议排查：关掉该盘的资源管理器窗口/杀软实时扫描、
换数据线或 USB 口（配合 SMART `C7` UltraDMA CRC 判断链路）。

## 1.2.4 — 修掉「分区没有真正串行」+ diskpart 可调等待与诊断

承接 1.2.3 的现场问题（4 盘批量装机，2 块在分区阶段报 `0x80070017` CRC）。
1.2.3 解决了「失败后能不能救回来」，这一版查到了**为什么会失败**。

### 根因（重要）：`PartitionSequencer` 在生产路径上从来没接进执行器

`WorkflowController._build_service()` 调 `build_real_runner(...)` 时**没有传**
`sequencer` / `ghost_gate`，所以：

- 「分区全局串行」只在**单测里成立**（单测都是自己 new 一个 runner 再注入 sequencer）；
- 生产环境里四块盘的分区脚本是**交错执行**的：只有一个 diskpart 进程在跑（模块级锁），
  但「清盘 → 删 MSR → 建分区」三段会在盘与盘之间穿插——A 盘刚 `clean` 完，B 盘立刻
  `clean`，A 盘再 `convert gpt`……磁盘链路被这样来回抽打，链路差的盘就报 CRC；
- 顺带 `GhostStartGate` 也没生效（两个 Ghost 可能同瞬间启动撞 8027 的错误路径一直在）。

结构性原因：`self._sequencer = PartitionSequencer()` 原本写在 `_build_service()` **之后**，
装配 service 时这个对象还不存在，所以当时只能省掉这两个参数。

VM 实测（4 块 120 GB VHD，exe 1.2.4 前后对比，日志按时间排序）：

```
修前：11:49:43 d2 → 11:49:44 d3/d4/d5 → 11:49:45 d2 → 11:49:46 d3/d4/d5 …（交错）
修后：11:55:31 d2 ×4 → 11:55:39 d3 ×3 → 11:55:46 d4 ×3 → 11:55:54 d5 ×3（串行）
```

修后的诊断行能看清流水线仍在工作：`磁盘 3` 开始分区时
`其它盘活动：磁盘 2=写镜像（Ghost）`——**分区串行、镜像交错**才是设计意图。
代价：4 盘的分区段从 15 s 变 33 s（每盘多两次 1 s 等待 + 串行），
而单盘 Ghost 就要 165 s，整体吞吐几乎不受影响。

### 为什么会怀疑「命令发太快」

- 分区本身是全局串行的（`PartitionSequencer` + `_DISKPART_LOCK`），不存在多线程同时发
  diskpart；真正存在的重叠是**A 盘在写镜像（约 74 MB/s 持续 I/O）时 B 盘在分区**；
- `clean` 只改写盘首分区表扇区、命令返回即认为写完；紧接着 `convert gpt` **立刻回读**
  同一片扇区。桥接板/慢盘还没落盘时回读就可能拿到坏数据 → `0x80070017`。
- 约束：**diskpart 脚本里没有 sleep 命令**，所以"命令之间等待"只能在两次 diskpart
  **调用之间**做（不能写进同一条脚本）。本版按这个方式实现。

### 改动

- **`partitionSettleMs`（设置 · 安全与提醒，默认 1000ms，范围 0–5000）**：
  两次 diskpart 之间的等待。默认每盘多等 ~2 秒（通常 2 次切换），4 盘约 +8 秒，
  相对单盘 Ghost 的 165 秒可忽略；`0` = 不等待。下发路径：设置 →
  `runner_options["partition_settle_ms"]` → 分区阶段（越界自动夹紧）。
- **诊断（A5）**：新增 `app/application/activity.py`（盘号 → 当前阶段），
  分区组 / 写镜像 / 收尾都会登记。分区阶段在日志里写
  `[诊断] 开始分区（diskpart 间隔 1000 ms；其它盘活动：磁盘 3=写镜像（Ghost））`，
  等待时也带上这句；**失败详情**里额外写 `settle_ms` 与 `parallel_activity`，
  下次复现能直接看出「是不是别的盘正在写镜像时炸的」。
  登记表任何异常都不影响执行（最坏退化成"没有信息"）。
- 版本号 1.2.4，`packaging/version_info.txt` 同步。

### 测试

- 新增 `tests/test_partition_settle.py`（20 条）：设置项默认值/范围/夹紧、
  `option_int` 防御性、等待真的落在两次 diskpart 之间（MSR 有/无两态）、
  `0` 可关闭、值来自 `runner_options`、荒唐值被夹到 5s、活动登记表描述与容错、
  日志里出现「其它盘活动」、失败详情带 `parallel_activity`、
  编排层登记/清理（含分区失败路径）。
- `tests/test_concurrency.py` 新增 2 条**钉住生产装配**：`_build_service()` 必须把
  `sequencer` / `ghost_gate` 交给 `build_real_runner`（同一个对象），注入执行器时
  不得再偷偷造一个。这两条正是这次事故的直接回归防线。
- `tests/test_settings_wiring.py` 的 `_runner_options()` 断言补上新键。
- VM（Windows 10 19045 + Python 3.15.2）：`ruff` 全绿、`compileall` 通过、
  `tools/ui_check.py` 通过、`pytest tests/ -q` → **593 passed**。
- VM 4 盘真实写盘（exe 1.2.4，4×120 GB 动态 VHD）：四块盘 `efi/windows/data/layout`
  全部 ok、Ghost 全部 ok（合成镜像使 `boot` 按设计失败、`copy` skipped），
  日志时间线证明分区已串行、镜像仍交错；每盘的等待与诊断行均已落盘。
- **VM 6 盘真实写盘**（exe 1.2.4，6×120 GB 动态 VHD，平台上限形态）：
  六块盘 `efi/windows/data/layout` 全部 ok、`image` 全部 ok、`copy` 按设计 skipped，
  18 个盘符（每盘 EFI/Windows/Data 各一）一次分配到底（`G,H,K / L,M,N / O,P,Q /
  R,S,T / U,V,W / X,Y,Z`，正好用尽），任务结束后 6 个 EFI 临时盘符
  （G/L/O/R/U/X）全部释放，六块盘均为 GPT、3 分区、`largestFree=0`。
  时间线（严格串行分区 + 镜像交错）：

  ```
  12:06:00 d2 分区 → 12:06:08 d2 Ghost / d3 分区
  12:06:15 d3 Ghost / d4 分区   12:06:23 d4 Ghost / d5 分区
  12:06:31 d5 Ghost / d6 分区   12:06:39 d6 Ghost / d7 分区
  12:06:46 d7 Ghost             12:06:48 最后一个任务结束
  ```

  6 盘总墙钟 **49 s**（首盘 10 s、每盘约 8 s 分区），分区段严格不重叠；
  诊断行示例：`磁盘 5` 开始分区时 `其它盘活动：磁盘 4=写镜像（Ghost）`。

## 1.2.3 — 清盘失败不再只丢一个退出码：翻译 + 幂等重试

**现场**：4 块盘批量装机，2 块在「硬盘分区」就失败，另外 2 块正常。日志里只有

```
DiskPart 成功地清除了磁盘。
DiskPart 遇到错误: 数据错误(循环冗余检查)。
--- 退出码 2147942423，用时 2.2s ---
```

### 定位

`2147942423 = 0x80070017 = ERROR_CRC`，是**存储层**读到坏数据，不是分区逻辑算错：

- 失败点在 `clean` 之后的那条命令，也就是脚本里的 `convert gpt`
  （`select disk N / clean / convert gpt / exit`）；
- 两次现场里失败的始终是同样两块盘 → 指向那两块盘本身或它们的数据线/接口/
  转接板，而不是多盘并发的设计（分区本来就是全局串行的：`PartitionSequencer`
  串行分区组 + `_DISKPART_LOCK` 串行所有 diskpart 调用）；
- VM 上用 4 块 VHD 跑同一条并发路径（分区全部通过）验证了这一点，
  失败是那台机器的存储链路问题，不是并发把盘搞坏了。

### 改动

- **退出码翻译**（`app/application/shell.py`）：`DISKPART_EXITS` 覆盖 CRC / 设备未就绪 /
  一般故障 / I/O 错误 / 卷占用 / 访问被拒 / 写保护 / 参数错误等常见 HRESULT，
  失败文案从「退出码 2147942423」变成
  「退出码 2147942423 = 0x80070017 数据错误（循环冗余检查）」并附下一步建议
  （换线/换接口/查 SMART 的 C7、05）。认不出来的码原样给数值，不编原因。
- **清盘步骤可重试**（`phases/partition.py`）：「清盘 + 转 GPT」是幂等的，遇到
  **瞬时类**错误（CRC、设备未就绪、一般故障、I/O 错误、卷占用、资源不足）自动重试，
  默认 3 次、退避 2s→4s，重试前先 `diskpart rescan` 让枚举跟上；
  权限/参数类错误不重试（重试也不会好，直接把真实原因报出来）。
  重试次数与退避可注入（`wipe_attempts` / `retry_delay` / `sleep`），单测不真睡。
- 建分区、删 MSR 这两段**不重试**（不是幂等的，重跑会和已建分区撞车），但失败
  文案同样带退出码翻译。
- 版本号 1.2.3，`packaging/version_info.txt` 同步。

### 测试

- 新增 `tests/test_diskpart_recovery.py`（18 条）：退出码翻译（含负数的 32 位有符号
  形式、未知码不编原因）、瞬时/永久分类、CRC 后重试一次即成功且重扫一次、
  永久错误一次就报、重试耗尽后文案含「已自动重试 2 次仍失败」且 `diskpart_attempts`
  写进步骤详情、重试次数可配、退避递增且可注入、`clean` 自身失败也重试。
- VM（Windows 10 19045 + Python 3.15.2）全量 **567 passed**，`ruff` 全绿、
  `tools/ui_check.py` 通过。
- 4 盘并发回归（exe 1.2.3，4 块 120 GB 动态 VHD）：四块盘 `efi/windows/data/layout`
  全部 ok、Ghost 全部 ok（合成镜像使 `boot` 按设计失败、`copy` skipped），
  与改动前一致——证明并发路径本身没问题。
- **端到端故障注入**（exe 1.2.3，PATH 前置一个包装过的 `diskpart.exe`，只对
  「清盘 + 转 GPT」这一段按配置返回错误码，其余原样转发系统 diskpart）：
  - 注入 `0x80070017` 一次：日志出现
    `清盘第 1/3 次失败（退出码 2147942423 = 0x80070017 数据错误（循环冗余检查）），2s 后重新扫描并重试`
    → `diskpart 重新扫描磁盘` → `已重新扫描磁盘总线` → 第二次通过，
    该盘 `efi/windows/data/layout` 全部 ok（改动前这一单会整盘失败）；
  - 注入 `0x80070005` 恒定：只跑 1 次、不 rescan，任务结束文案为
    `diskpart 失败（退出码 2147942405 = 0x80070005 拒绝访问）。权限不够或盘被保护：…`。

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
