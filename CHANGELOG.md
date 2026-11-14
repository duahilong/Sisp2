# 变更记录

版本号只有一处来源：`app/version.py`（`packaging/version_info.txt` 与它由
`tests/test_packaging.py` 强校验）。每个发布版在 Git 里对应一个**注释 tag**，
tag 上的提交树就是那一版的完整代码与文档快照。

定版（`v1.1.0`）之后只做优化与修复：功能以 tag 为准，`main` 继续向前走，
需要回到某一版时 `git checkout v1.1.0` 或 `git archive v1.1.0`。

## 未发布（main）— 分区串行、身份复核与验收恢复加固

按分区审查建议依次完成六项加固，代码与回归测试均在本地编辑，全部测试只在专用
`windows-mcp-vm`（Windows 10 19045 / Python 3.12.16）执行：

1. `PartitionSequencer` 不再让未登记磁盘绕过批次互斥；无顺序表的独立调用也共享互斥。
   运行中或旧批次未结束时拒绝 `set_order()` 重置，重复进入同一运行盘也会失败。
2. 清盘、MSR 删除、建分区续跑、逐分区格式化与盘符校正的**每一轮破坏性操作**前都复核
   磁盘身份；重扫后的重试不再沿用旧身份。MSR 复查失败时不再按旧分区号盲删；生产路径
   释放 EFI 盘符前也会重新复核身份与状态。
3. 最终分区验收缺少 GPT 类型、offset、容量、卷标或文件系统时按数据不完整失败，不再将
   `None` 当作正确；`details.issues` 提供稳定问题码、分区号和 `repairable` 标记。
4. `allow_small_partitions` 经 `PhaseContext` 传到最终验收，前置检查与小盘验收策略一致。
5. 验收补救只处理明确的文件系统/盘符问题；缺失分区、几何或卷标错误不会触发无效格式化，
   每项补救前重新查询真实快照。
6. 不中途强杀 diskpart，但在格式化重试、分区续跑和下一项写操作前响应取消。

VM 验证：Ruff、`compileall` 通过，完整测试 **760 passed**（80.59 秒）；仍只有一条
Starlette 第三方弃用警告。全程启用 `SISP_BLOCK_REAL_DISK=1`，未做真实写盘。
PyInstaller 6.22.3 已在同一 VM 构建 `1.5.0-beta.10`，产物 17,108,566 字节，SHA256
`2657bd1a0f7469e8f3e903e99ff0bf29e4f3f4cf59bd95bae9ca93adb50bdffb`；exe 的 `/health`
与 `/api/info` 冒烟通过，FileVersion / ProductVersion 与完整版本一致。

## 未发布（main）— Windows VM 复验与 Beta 9

2026-11-14 在专用 `windows-mcp-vm`（Windows 10 19045）上使用 Python 3.12.16 完成非破坏性复验，
本机未执行任何测试或构建：

- `ruff check app tests tools packaging` 全部通过，`compileall` 通过，完整测试 **744 passed**；
  仅有 FastAPI TestClient 引入的一条 Starlette 弃用警告。
- 测试全程设置 `SISP_BLOCK_REAL_DISK=1`，未执行真实清盘、Ghost 写盘或现场硬件验收。
- PyInstaller 6.22.3 构建 `Sisp2-beta.exe`，完整版本 `1.5.0-beta.9` 已核对 FileVersion、
  ProductVersion 与 `/api/info`；产物 17,105,152 字节，SHA256
  `9d271ef574e8b86158ce2b252a60bdd1e9423a28f45ff99b1e9d22672d8e83b9`。
- exe 的 `--serve` 冒烟通过：`/health` 返回 `ok=true`，关于接口报告 Python 3.12.16 /
  Windows 10 19045。构建后源码版本模板已恢复，Git 工作区无构建残留。

## 未发布（main）— 自动构建与滚动 Beta

- `.github/workflows/windows-tests.yml` 扩展为检查、测试、构建、发布一体化流水线：main 推送后，
  Python 3.10 / 3.12 的 ruff、`compileall`、pytest 全部通过才构建单文件 exe。
- 构建生成 SHA256 校验文件，Actions Artifact 保留 7 天；滚动预发布改用固定 `beta` 通道，
  自动更新到对应提交并替换 `Sisp2-beta.exe`。完整版本 `X.Y.Z-beta.N` 由基础版本与
  GitHub run number 生成，构建期同步注入关于页面和 exe 版本资源，并用于 Release 标题；
  构建后恢复仓库基础版本模板。连续推送只发布最新结果，失败时不替换现有 Release。
- 首次完整云端运行 `43444839983` 已通过：两个 Python 版本的 741 项测试、ruff、compileall、
  PyInstaller 构建及 Release 创建均成功；首包 17,934,591 字节，SHA256
  `1a197067fcaccfa03d2fecdf2b99b2fc5dd7577ed0461eedf49652f175b160af`。

## 未发布（main）— 第二批外围功能修复

以下修复不涉及写盘路径；新增回归用例，验证只在 GitHub Actions 的隔离 Windows runner 上进行。

1. **任意目录导出的日志包可定位**：原生保存框选定的 ZIP 在导出成功后加入当前进程的 reveal 白名单；不再因文件不在家目录/日志目录而无法打开所在文件夹，同时不允许前端借此定位任意系统文件。
2. **磁盘 0 正确显示**：`disk_label()` 不再用数字真值判断盘号，合法的 Windows 磁盘 0 不再显示为“未知磁盘”。
3. **第二实例等待已有窗口出现**：首个实例已经持有互斥体但尚未创建窗口时，第二个实例会轮询最多 10 秒再提示，而不是只查一次就放弃切换。
4. **日志尾部补齐单独 CR 的空行语义**：增量读取与全文 `splitlines()` 在文件只有 `\r`、连续 `\r\r`、换行后末尾 `\r` 以及跨块 CRLF 时保持一致，不再漏掉空行。

## 未发布（main）— 第一批安全与正确性修复

以下修复只改分区安全边界和验收逻辑。代码与 Mock 回归用例已同步；云端 Windows 单测通过不等于真实写盘验收，真实清盘、多盘串行与盘符校正仍需在隔离 VM 上复验。

1. **分区串行不再被租约超时破坏**：`PartitionSequencer.enter()` 不再把等待超过 30 分钟的运行中磁盘当成已完成并放行下一盘。只有 `done()` / `reset()` 能释放分区闸门；排队任务仍可取消。
2. **清盘前复核改为 fail-closed**：缺少确认时身份、连续查询失败或查询结果不可用时，分区阶段直接失败且不执行任何 diskpart 脚本；不再因 PowerShell 暂时故障继续清盘。
3. **错误盘符可恢复**：格式化前发现 Windows 自动分配了非计划盘符时，先移除旧字母、分配计划字母并重新查询；格式化后与验收补做也走同一校正路径。仅盘符错误时不重复格式化。
4. **显式分区计划允许未分配空间**：只有存在 `auto` 分区时，未分配的 slack / `LargestFreeExtent` 才判为验收失败；所有分区都显式指定大小时，保留空闲空间是合法结果。
5. **云端非破坏性验证**：新增 GitHub Actions Windows workflow，在 Python 3.10 / 3.12 上运行 ruff、`compileall` 与带真实写盘护栏的 pytest；不执行真实磁盘写操作。
6. **Ghost 中文错误跨系统区域解码**：优先尝试 UTF-8 / GB18030，再退回 mbcs；避免英文 Windows 把 GBK 字节按 cp1252 解码成乱码。对应测试固定生成 GBK 字节，不再依赖 runner 的中文系统区域。

## 未发布（main）— 优化第二批（一）：契约修正与日志增量尾随

只修“代码与已写明的契约不一致”和两个内部实现热点，不改写盘路径、安全语义与提示文案主体
（`ruff` 全绿、`pytest` 731 passed；源码与**本机打包的 exe** 各在 300 GB VHDX 上跑一遍真实写盘，均全步骤 ok）。

### 1. 引导修复 / 引导检查的字段错位（界面显示错误）

- `dialogs.js` 读 `result.exit` 判成败，而 `boot/service.py` 只返回 `{success, message, steps, log_path}`：
  `undefined === 0` 恒为 false → **修复成功也渲染成“失败（退出码 undefined）”，日志还标红**。
  改为 `result.success === true || result.exit === 0`；后端成功返回补 `"exit": 0`（失败路径本来就抛 `BootError`）。
- 同一个对话框里 findings 的说明读 `item.detail`，而后端 `_finding()` 只给 `message` → **每条说明永远是空白**。
  改成 `item.detail || item.message`。

### 2. 任务日志尾部改成增量读取（`app/gui_web/log_tail.py`）

`GET /api/jobs/{id}/log` 在进度页日志展开时每 1.5 秒被调一次，旧实现每次 `read_text()` 读整个文件再取尾部
（Ghost 两小时能有几十 MB，而每次只要最后 120 行）。新实现维护 `(已消费字节, 尾部环形缓冲, 总行数, 未完成半行)`，
只读新增字节，并与“全文 `splitlines()[-tail:]`”**逐条一致**（CRLF、末尾半行、UTF-8 跨块都有测试钉住）。

VM 实测（14.8 MB / 200,000 行）：旧实现 42.0 ms/次，新实现首次 29.5 ms、常态 **0.085 ms/次**（约 490 倍）；
真实写盘期间 104 次轮询：`total` 单调 35→235，接口 `total` 与全文 `splitlines()` 长度一致，最后 5000 行逐行相同。

### 3. 设置落盘顺序

`SettingsStore.update()` 原来先改内存再 `save()`：磁盘满/目录被占时接口报“保存失败”，但进程内行为已按新值走。
改成 `save(候选值)` 落盘成功后才换内存（`reset()` 同理）。

### 4. 日志句柄关闭后不再刷屏

`streaming()` 的写入在句柄关闭后每行一条带堆栈 warning（Ghost 取消后成百上千条），改成一个闭包标志位只告警一次；
内存尾部仍然完整。

### 5. ctypes 显式声明签名

`CreateMutexW` / `CloseHandle` / `FindWindowW` / `ShowWindow` / `SetForegroundWindow`（`single_instance.py`）、
`ShellExecuteW` / `OpenProcess` / `WaitForSingleObject` / `MessageBoxW`（`main.py`、`gui/elevation.py`）都补了
`argtypes` / `restype`。默认 `restype` 是 `c_int`，64 位下会把 `HANDLE` / `HWND` / `HINSTANCE` 截断成 32 位
——现在句柄值都很小所以没出事，属于“运气正确”。

### 6. 工具：`ui_check.py` 增加契约检查

新增 `check_contracts()`（不需要 node）：`.pline` 必须带 `data-role`（`ui_smoke` 靠它数数据分区）、
`dialogs.js` 出现 `result.exit` 时必须同时有 `result.success`、出现 `item.detail` 时必须同时有 `item.message`。
三条都真的出过错，用来防回归。

## 未发布（main）— 优化第一批：热点路径与死代码

只做性能/健壮性优化，不改对外行为、写盘路径与安全语义（`ruff check` 全绿、`pytest` 707 passed；
源码与 exe 各在 300 GB VHDX 上跑一遍真实写盘，均全步骤 ok）。

### 1. 命令日志

- `CommandLog.block()` / `banner()` 改成整块一次写入：diskpart 脚本预览、命令的完整输出
  动辄几十上百行，原来每行一次 open/close（Windows 上这几个系统调用比写内容本身贵得多）；
- 内存摘要从无界 list 改成 `deque(maxlen=1000)` + `islice` 取最后 40 行：写镜像两个小时
  也不再无界增长；
- `streaming()` 的写入补捕获 `ValueError`（句柄已关闭时读取线程仍在写，原来会打崩读取线程）。

### 2. 事件与任务

- `WebApi` 事件缓冲：`list` 切片淘汰改成 `deque(maxlen=5000)` + `_base` 递增，消除每次
  溢出的 O(n) 整体复制；游标语义（`cursor = _base + 下标`）完全不变；
- `JobManager` 的 `on_step` 签名探测按执行器缓存，不再每个任务调一次 `inspect.signature()`。

### 3. 工具探测与脚本编码

- `find_program` 加 `functools.cache`：`WindowsDiskRunner` 按任务造 `DiskpartShell`，
  8 块盘就是几十次 `shutil.which` + System32 目录探测（`tests/conftest.py` 在用例间清缓存）；
- diskpart `/s` 脚本编码从 `ascii` 改成 `mbcs`（`/s` 只认系统代码页）：非 ASCII 卷标/说明
  原来会在真正调起 diskpart 之前抛 `UnicodeEncodeError`；映射不了时回退 `errors="replace"`。

### 4. SSE 心跳（3.10 上真的会断流）

旧实现是 `asyncio.wait_for()` + `except TimeoutError`。`asyncio.TimeoutError` 在 3.10 是
独立类（MRO `TimeoutError → Exception`），到 3.11 才是内置 `TimeoutError` 的别名
（MRO `TimeoutError → OSError`），所以 3.10 上这个 except 捕不到 → SSE 每 15 秒断一次
（3.10.11 实测：`asyncio.exceptions.TimeoutError` 直接漏出去）。

现在改用 `asyncio.wait({task}, timeout=)`：超时**不抛异常**，只返回空的 done 集合，
3.10–3.13 行为完全一致；`finally` 里 cancel + await 掉 `queue.get()`，客户端断开时也不会
再出现 `coroutine 'Queue.get' was never awaited`（pytest 输出里出现过这条 RuntimeWarning）。
逻辑抽到 `app/gui_web/sse.py`（不 import FastAPI，方便单测与低版本实测），
`tests/test_sse_heartbeat.py` 钉住三条行为。

### 5. 清理与文案

- 删除无引用代码：`api._session_ready`、`real_runner._which`、`workflow._service_injected`、
  `main.js` 的 `goStep` 导入、`state.js` 的 `freeBytes` 导入、`shell.js` 里 CSS 不存在的
  `'dry'` class 操作；
- 设置页 `autoRefresh` 的说明改成“只保存、不参与行为”，与 1.5.0 起的实际代码一致。

### 6. 测试工具

- `tools/ui_smoke.mjs` 新增 `--disk <盘号>`；`--start --allow-write` 缺少 `--disk` 时
  直接以退出码 2 拒绝运行——它默认选“容量最大的可选盘”，在多盘测试机上会选中真实数据盘。
- **第 4 步（确认页）的断言以前永远被跳过**：危险提示本身就是 `.warnbox.warnline`，
  旧代码把 `.warnbox` 一律当 error。现在只有“没有任何计划卡片”才算生成计划失败；
  数据分区数改成数 `.pline[data-role^="data"]`（`partRowHtml` 补了 `data-role`），
  盘符改成数 `.lbadge:not(.ghost)`，并要求最后一个分区显示“剩余空间（约 …）”。
- **第 2 步“删掉 E 再加回去”其实一直失败**：把 C|D 拖到 0.999 后剩余空间不够，
  `addExtraPartition` 返回 false、界面只弹 toast。现在先点快捷容量 150 复位再加，
  并新增 `step2extraBack` 断言；最后稳定在 120（四分区路径）。

## 1.5.0 — 单实例（一台电脑只许开一个）+ 启动提速（窗口早 2 秒、首屏早 3 秒）

### 1. 单实例：不允许开第二个

以前可以同时开两份：两份界面各自扫盘、各自算计划、各自持有内存里的任务队列与
写盘白名单。用户完全可能在一份里点了「开始安装」，又在另一份里对同一块盘点了
一次——没有任何机制把这两个进程串起来，日志也会分成互不相干的两套。

现在用 **Windows 命名互斥体**（`CreateMutexW`）做门闩：

| 行为 | 说明 |
| --- | --- |
| 第二个实例 | 先把已打开的窗口叫到前台（`ShowWindow` + `SetForegroundWindow`），再弹框说明，**以退出码 6 结束**；`--serve` 模式只打一行 stderr（控制台程序不弹模态框） |
| 崩溃/被强杀后 | 互斥体是内核对象，进程一结束系统就释放，**不会留下需要手工清理的锁文件** |
| 提权后端（`--backend`） | 不抢锁（它是本实例的一部分，抢了会把自己锁在外面） |
| 提权重启交接 | 父子进程有极短的交接窗口，`--elevate` 路径上最多等 3 秒 |
| 排障 / 自动化测试 | `--allow-multiple` 显式放行（正常使用不要加：两个实例会同时扫盘并抢同一块盘） |
| 非 Windows（开发机） | 退回独占锁文件（`O_CREAT\|O_EXCL` + pid 存活检查，pid 已死的残留文件会被接管） |

实现：新增 `app/gui_web/single_instance.py`；入口 `main()` 在任何初始化之前过闸
（`acquire_single_instance()`，可注入替身，便于单测）。

### 2. 启动提速

现场感受是“双击后要等，窗口出来还要再等后端”。用 `--serve` 计时（进程创建 →
`GET /health` 返回 200）定位到三处浪费：

1. **`build_api()` 同步跑会话**：预检 + 扫盘（PowerShell 冷启动约 1 秒）跑完才
   `bind` 端口，窗口只能干等；
2. **`POST /api/session` 把会话重算一遍**：后台刚算过，界面再要一次又扫一遍盘；
3. **首屏多刷一次 `GET /api/disks`**：会话本来就带回磁盘列表，前端又扫了一次。

改法：

| 改动 | 效果 |
| --- | --- |
| 会话改成**后台热身**（`_warm_session()`），`/health` 立刻可用 | 窗口早约 2 秒出现 |
| `WebApi.start_session(force=False)`：已有会话直接返回缓存，`{"force": true}` 才重扫 | 首屏省一次 PowerShell 扫描（约 1 秒） |
| 前端 `boot()` 先 `render()` 画骨架 → 再并行取 `info`/`settings` → 不再刷 `/api/disks` | 白屏消失，首屏再省约 1 秒 |
| 设置项 `autoRefresh` 语义收敛为“首屏要不要**再**刷一次” | 关掉它只少一次重复扫描（不会再出现“关掉后界面没磁盘”） |

实测（Windows 10 19045，`--serve`，从进程创建算起）：

| 指标 | 1.4.1 | 1.5.0 |
| --- | --- | --- |
| `/health` 就绪（窗口可以开的时刻） | 2.58 s | **0.54 s** |
| 首屏拿到磁盘列表（health + session） | ≈ 4.7 s（还有一次多余的全盘扫描） | **≈ 1.7 s** |

### VM 验证

- 单测 **707 passed**（1.4.1 为 682；新增 `tests/test_single_instance.py` 21 条 +
  会话缓存 4 条），`ruff` / `compileall` / `ui_check.py`（11 个模块）全绿；
- **单实例实测**：源码版与打包 exe 各起一个 `--serve`，第二个都 `exit=6`、
  打印「硬盘工具箱已经在运行了（只允许一个实例）」，且**没有再 bind 端口**；
  `--allow-multiple` 能起来；`--backend`（提权后端）不受影响；
- **真实盘（两块 120 GB 一次性 VHD）**：源码版与 exe 各跑一轮，注入
  「不带 `override` 的 format 必失败」，两块盘的 3 个分区全部按计划落位、
  `分区验收：通过`；`POST /api/logs/export` 在 exe 上产出 13,061 B 排查包；
- 收尾：VHD 已 detach 并删除、无 `diskpart`/`ghost64`/`Sisp2-150` 残留、
  8892/8893/8895–8899 无监听，**Disk 1 分区（100 / 307200 / 1600428 MB）逐项未变**。

## 1.4.1 — 格式化：用上 diskpart 自己的 `override`，修好 ESP 兜底的死结

现场（2026-11-11 13:53，三块盘：GeIL G7T 1TB ×2、GeIL P3L 512GB）三份日志
**同一个位置、同一个错误码**：

```
13:53:38  create partition efi size=100           成功
13:53:41  卷就绪：盘上已有 4 个分区（计划 4）
13:53:43  format quick fs=fat32 label=EFI         卡在 0% 8.5 秒
13:53:51  虚拟磁盘服务错误: 设备使用中。
          所选的卷或分区正在使用。
          若要继续操作，请使用 OVERRIDE 参数。     ← Windows 自己给了答案
13:53:56  assign letter=E                         成功（drive_letter=null, mounted_letter=E）
13:53:58  盘符 E: 与分区 #1（EFI）对不上，放弃 format.com 兜底   ← 我们把自己的退路堵死了
```

### 三层原因

1. **Windows 侧**：刚建好的分区被判「设备使用中」（VDS `0x80042413`），
   diskpart 明确提示「若要继续操作，请使用 OVERRIDE 参数」；
2. **我们的漏洞一**：`format.com /X` 兜底要先校验盘符归属，而 ESP 是隐藏卷，
   `drive_letter` 永远是 `null`、真盘符只在 `mounted_letter` —— 只看 `drive_letter`
   必然「对不上」，于是**最需要兜底的 ESP 恰恰永远拿不到兜底**；
3. **我们的漏洞二**：安全闸「拒绝」返回 `-1`，被 `is_transient_diskpart_exit()`
   判成永久失败 → `break`，后面真正能用的途径（含从未尝试过的 `override`）全被掐掉。

### 改法

| # | 改动 | 落点 |
| --- | --- | --- |
| 1 | **`format` 一律带 `override`**（第一次就用）：对刚在清过盘的磁盘上建出来的 RAW 分区没有任何数据可丢，而 `override` 就是 VDS `FormatPartitionEx` 的 `bForce`；它按「磁盘号 + 分区号」选对象，**不需要盘符**，ESP 也能用 | `partitions.FORMAT_OVERRIDE` / `format_lines(..., force=True)` |
| 2 | **ESP 盘符识别**：`drive_letter` **或** `mounted_letter`，但只有隐藏类型（`System`/`OEM`/`Recovery`/GPT GUID `c12a7328…`）才允许走 `mounted_letter` 这条道；数据分区仍然必须精确命中 `drive_letter`，并且都要过容量核对（≤ 2 MB） | `_letter_of()` / `_is_hidden_partition()` / `_letter_belongs_to()` |
| 3 | **拒绝 ≠ 失败**：安全闸拒绝 `FALLBACK_REFUSED(-2)`、工具没起来 `TOOL_NOT_STARTED(-1)` 各自记账并**继续下一种途径**，不再 `break` | `_format_one()` / `_format_with_tool()` |
| 4 | **失败文案如实汇总**：`4 次尝试均未成功（第1次 diskpart（含 override）：0x80042413…；第2次 format.com /X：…）`，不再出现假的 `0xFFFFFFFF 途径 format.com`；`details` 增加 `format_history` / `format_force` | `_partition_failure()` |
| 5 | 去掉重复等待：`_wait_volumes()` 末尾不再多等一次 `partitionSettleMs`（逐分区那次已经等过，省 1 秒/分区） | `_wait_volumes()` |

`format_force=False` 保留为可回退开关（回到 1.4.0 的“先普通 format、失败再换工具”）。

### VM 验证（两块 120 GB 一次性 VHD，Disk 2/3；Disk 0/1 事后逐项核对未变）

自建 `diskpart.exe` 包装器，新增两条注入：`require_override=1`（不带 `override`
的 `format` 一律返回 `0x80042413`）、`format_fail_label=EFI`（EFI 的 `format` 永久失败）。

| 场景 | 源码版 | 打包 exe |
| --- | --- | --- |
| S1 不带 override 就失败 | 第 1 次 `diskpart（含 override）` 直接过，3 个分区全部格式化，验收通过；`nooverride` 计数 **0** | 同上（`1.4.1.0`、`python=3.14.6`） |
| S2 EFI 的 diskpart 永久失败 | 第 2 次走 `format.com G: /FS:FAT32 /Q /Y /X /V:EFI` → 成功 → 验收通过（**1.4.0 在这里报“对不上”直接放弃**） | 同上 |
| S3 全部途径都失败 | `步骤 ✗ 创建 EFI 分区`，原因列出两条途径（`0x80042413` + format.com 的退出码），无 `0xFFFFFFFF`，其余步骤 skipped | — |

- 单测 **682 passed**（1.4.0 为 674），`ruff` / `compileall` / `ui_check` 全绿；
- `POST /api/logs/export` 在 1.4.1 exe 上仍可用（9,641 B，含 `summary.txt/json`、
  `logs/`、`settings.json`）；
- 收尾：VHD 已 detach 并删除、无 `diskpart`/`ghost64`/`Sisp2-141` 残留、8892/8893 无监听；
  Disk 1 分区（100 / 307200 / 1600428 MB）逐项未变。

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
