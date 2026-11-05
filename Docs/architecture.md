# 架构与工作流

## 分层

- 入口：`app/gui_web/main.py`（唯一入口，桌面窗口与 `--serve` 浏览器模式共用）。它装配 WebApi、工作流和 FastAPI；命令行入口 `app/cli.py` 已移除，全部能力都在界面里。
- HTTP/UI：`app/gui_web/server.py` 将 REST/SSE 转换为 WebApi 调用；`api.py` 包装响应信封、转发工作流并缓冲事件；`ui/index.html` 是只含布局骨架的薄壳，界面由 `ui/css/app.css` 与 `ui/js/*.js`（ES 模块，`/static` 托管）组成。
- 专项能力：`modules/partition`（diskpart 分区/格式化脚本、保护分区校验）、`modules/boot`（ESP 扫描 + `bcdboot` 修复）、`modules/image`（WIM/ESD/SWM/ISO 索引探测，ISO 必定卸载）都是只被 REST 层调用的独立模块，写盘仍复用服务层的闸门。
- 应用：`application/workflow.py` 管理会话、预检结果、扫描快照、计划及确认；`service.py` 提供扫描、配置检查、盘符分配与安全执行门；`partitions.py` 是分区布局（EFI / Windows / 数据分区）的唯一真相；`jobs.py` 管理状态、调度、取消和事件。
- 执行与能力：`real_runner.py` 调用 Windows 系统工具；`preflight.py` 读配置并做启动检查；磁盘扫描、设置、盘符分配等位于 `modules/` 与 `drive_letters.py`。

## 一次工作流

1. `WorkflowController.start()` 运行预检、加载业务配置并扫描硬盘，应用系统盘/启动盘/离线/只读/系统盘符及型号排除规则。
2. `refresh_disks()` 可重新扫描；`check_config()` 单独重验配置。
3. `prepare()` 根据选盘与配置构造分区计划、盘符、身份快照及检查/阻断信息。优先级从弱到强是：业务配置 → 界面设置 → 向导里这块盘的逐盘覆盖（之前顺序反了，设置会把用户在第 2/3 步填的大小与镜像改回去，已修）。盘符分配会避开磁盘摘要、系统盘符以及 OS 层已占用/遗留的“幽灵盘符”（例如 EFI 隐藏卷留下的 DriveInfo 条目）。**镜像在这一步就要“可应用性”校验**：ISO 直接拒（dism 不能 `/Apply-Image` 到分区，提示先取 `sources\install.wim`）、非 WIM/ESD/SWM 拒、dism 读不到索引或索引不存在也拒——都会变成 `plan.blockers` 并让“开始”置灰，避免“清完盘才发现镜像用不了”。

**分区布局是显式模型**：`app/application/partitions.py` 的 `plan_partitions()` 把 `partition_info` 解析成 `[EFI, Windows, Data(, Data2)]`，真实执行器拼 diskpart 脚本、容量预检、dry-run 预览、界面第 4 步摘要都消费同一份结果（以前这三处各写一遍，加一个分区要改三处）。向导第 2 步可以再加一个数据分区（E）：此时 payload 用 `data_partitions` 列表逐项给大小与文件系统（NTFS/exFAT）。
**最后一个数据分区默认让 diskpart 吃掉剩余空间**（`size_mb=None` → `create partition primary` 不带 `size=`）：
比界面自己算「总容量 − C − EFI − 对齐余量」更准，也不会出现「界面上刚好、diskpart 报空间不足」。
计划表里它显示为 `剩余空间（约 X GB）`，但实际大小仍由 diskpart 决定（`estimate_mb` 只用于展示）。盘符按角色分配：`efi` / `windows` / `data` / `data2`，每块盘 3 个或 4 个。

第 2 步的容量条有两条可拖分隔条：**C|D** 改系统分区（变化由 D 吸收，E 不动），**D|E** 只调整两个数据分区的比例（D+E 总量不变）。两者都做了下限钳制（C ≥ 60 GB、D ≥ 20 GB、E ≥ 1 GB）并向下取整，所以拖动**永远不会超过硬盘真实容量**；加 E 时默认与 D 平分剩余容量。E 的文件系统不在界面上单独选择——它与 D 一致（`dataFs()`：跟随设置 `defaultFs`，非 NTFS/exFAT 一律回落 NTFS）。
4. `submit()` 默认创建等待确认的任务；`confirm()` 对无阻断的任务开始调度。确认通过（勾选 + 盘号/型号）后会按任务盘号重新补齐隐式白名单——刷新页面会重开会话并撤销隐式授权，不补就会“确认时报白名单为空”。
5. JobManager 调用服务执行任务。真实服务在执行前检查 dry-run 策略、白名单、身份复核及执行器；执行器按分区、镜像、引导、校验步骤调用系统命令，并在任务结束后释放 EFI 盘符（避免隐藏卷盘符在下次清盘后残留成幽灵盘符）。**`verify` 判定“装完能不能开机”，不只数分区**：分区表是 GPT、分区数**等于**计划分区数（三分区布局是 3，加 E 是 4）、盘上没有 Reserved/MSR 分区、盘上没有大块未分配（最后那个分区吃满）、EFI 分区里真的有 `EFI\Microsoft\Boot\bootmgfw.efi`（或 `EFI\BOOT\BOOTX64.EFI`）、系统分区里真的有 `Windows\System32\config\SYSTEM`（或 `ntoskrnl.exe`）；缺一项就判失败，并把证据写进步骤 `details`。
6. 运行结果由任务状态和事件返回；取消为协作式，已运行的系统命令不会因此立即中断。

## 分区时的 MSR 处理

`convert gpt` 会**自动**在盘首建一个约 16 MB 的 MSR（Microsoft Reserved）分区——这不是我们脚本里的命令，是 Windows 的行为（Windows 10 19045 / diskpart 10.0.19041 实测：`p1 Reserved 15.98 MB`，GPT 类型 `e3c9e316-0b5c-4db8-817d-f92df00215ae`）。产品要求的最终布局是 **EFI + C + D(+E)**，没有 MSR，所以执行器把分区拆成**三段 diskpart + 两次复核**：

| 阶段 | 做什么 | 失败时 |
| --- | --- | --- |
| ① 清盘 | `select disk N` → `clean` → `convert gpt` → `exit`（MSR 就是这一步自动出现的，`tracker["diskpart_ran"]` 在这里置位） | 立即失败，不碰后面的步骤 |
| ② 删 MSR | PowerShell 按 GPT 类型查分区号 → `select disk N` / `select partition k` / `delete partition override` / `exit`，**单独一段脚本** | 退出码非 0 → 立即失败 |
| ②' 复查 | 再查一次：还查得到 MSR → 中止，**绝不继续建分区** | 报“MSR 分区删除失败：删除后仍然存在（分区 #k）” |
| ③ 建分区 | `select disk N` + `diskpart_lines(parts)`：EFI → C → D(+E)，最后一项不带 `size=` | 退出码非 0 → 立即失败 |
| ③' 复核 | 查分区表：分区数 == 计划、没有 Reserved、EFI 在盘首且大小对、盘上没有大块空闲 | 在跑 `dism`（最长 3 小时）**之前**就报错 |

为什么不在清盘脚本里直接写 `select partition 1` + `delete partition override`：万一某台机器没自动建 MSR，`select partition 1` 会报错，**整条 diskpart 脚本会中止**，装机直接失败。所以只对“查到的 MSR”动手：

- 查到 → 删掉并复查（删完后盘首空闲与后面合并，实测 EFI 依然落在 `offset=1 MB`，不会留 16 MB 空洞）；
- 查不到 → 不删任何东西，直接建分区；
- **查询本身失败（PowerShell 报错/超时）→ 中止任务并报错**，绝不猜一个分区号去删（猜错就是删掉真分区）；
- 查询带重试（3 次 × 300 ms）：`convert gpt` 刚结束时慢盘/RAID/USB 桥接上的分区枚举可能还没跟上，一次查不到就跳过删除会把 MSR 留在盘上。

为什么不靠 diskpart 的退出码判定成功：不同 build 对“某条命令失败”的退出码行为并不一致（19041 实测返回非 0，但别的版本有返回 0 的记录），所以第二段、第三段之后都各自查一次真实分区表。

代价：盘上没有 MSR 后，动态磁盘转换、部分内核内存转储、某些 BitLocker 配置会受限——这是产品明确接受的取舍。演练预览（`build_diskpart_preview()`）把三段都展示出来，MSR 那两行以 `REM` 形式出现（预览时不知道真实分区号）；第 4 步确认页也会多一行“MSR 分区 · 约 16 MB · 由 `convert gpt` 自动生成，执行时删除”。

## 最后一个数据分区永远吃满

**界面永远把最后一个数据分区发成 `"auto"`**（`selectionPayload()`），diskpart 收到的是不带 `size=` 的 `create partition primary`，尺寸由它自己算。所以：

- 界面上 D / E 的数字只是**预估**（`fitLayout()` 算出来的，用于显示和拖动），最后那个分区的输入框是只读的（`.szrow input[readonly]`）；
- 没有 E 时，D 就是那个 auto 分区 —— 也就是说“把 D 调小、留一块未分配”这个能力已经取消，盘总是被填满；
- 有 E 时，D 是显式分区（可编辑），E 是 auto；
- 容量校验（`partitions.check_layout()`）只算**显式项**：EFI + C +（有 E 时的 D）。显式项留 3 MB 余量（`LAYOUT_SLACK_MB`：GPT 盘首 1 MB + 盘尾 1 MB + 1 MB 冗余），实测 40 GB 盘显式合计 = 容量 − 2 MB 时 diskpart 成功、= 容量 − 1 MB 时最后一个 `create` 失败（`exit=-2147024809`）；
- 界面上不再有“未分配”这个概念：第 2 步进度条没有灰色段、第 4 步计划表没有“未分配”行，最后那个分区在计划表里显示成“剩余空间（约 X GB）”；
- **改 C（或改显式的 D）时，最后那个分区的预估值要立刻重算**（`state.syncAutoPartition()`，在输入框 `input`、失焦、以及每次渲染第 2 步前都会跑）。它的输入框是只读的、不会自己触发 `input` 事件，所以 `updateSizeVisual()` 还要把新值写回输入框——否则用户看到的是“C 改成 116，D 还停在 276.6，两个数加起来不等于整块盘”。

## 分区大小下限（后端也拦）

界面的第 2 步有表单校验（`MIN_SYS_GB = 60`、`MIN_DATA_GB = 20`、`MIN_EXTRA_GB = 1`），
但那只是**表单**：直连 `POST /api/prepare` 可以传 `c_size_gb = 1`，旧实现会一路排到
`dism` 才失败——而那时候盘已经清空了。现在后端也有同一套下限（`partitions.MIN_C_GB`
等，`tests/test_partition_layout.py` 会把两边的常量钉在一起），生效在两处：

1. `workflow._check_partition_fit()`：计划阶段就报“硬盘 N 分区过小：…”，`/api/prepare`
   直接失败，**不会生成 executable=true 的计划**（演练同样拦，避免“演练绿、真跑红”）；
2. `WindowsDiskRunner._run_steps()`：`plan_partitions` 之后、`_wipe_script` 之前再拦一次，
   保证绕过 `prepare` 直接塞 JobSpec 的调用方也不能在盘上建出 1 GB 的系统分区。

最后那个 auto 分区没有显式大小，按“容量 − 显式项 − `LAYOUT_SLACK_MB`”估算，够不够
它自己的下限同样会拦（与界面 `checkLayout()` 的判断一致）。

单测和小盘（8 GB VHDX）需要低于下限的布局时，必须在 payload 里显式写
`partition_info.allow_small_partitions: true`——界面永远不发这个字段。

## 演练与真实模式的判定

`SispService` 构造默认 `dry_run=True`，入口会覆盖其策略。`main.py` 的有效 dry-run 为命令行 `--dry-run` 与设置 `dryRunByDefault` 的逻辑或；`WorkflowController` 会把构造时的 dry-run 锁定为硬上限，后续 `start()` / `prepare()` 只能降级为演练，不能升级为真实执行。真实模式下，`prepare()` / `authorize_disks()` 会把本次勾选并确认过的盘加入服务白名单（没有显式白名单文件）。见[安全说明](security.md)。

## 前端结构

三个视图（主页面、系统安装、设置）+ 一个外壳，不引入框架、也没有构建步骤：

- `js/state.js` 是唯一可变状态：磁盘快照、选中项、设置草稿、任务、执行计划、向导进度；写状态后调 `notify()`。
- `js/view-*.js` 只读状态并返回/写入 DOM；`js/shell.js` 包掉菜单、工具栏、日志坞、状态栏、toast、弹窗、主题与密度；`js/derive.js` 是纯函数层（容量格式化、分区地图切片、大小校验），能在 `node` 里直接跑。
- 弹窗只剩两个（修复引导、磁盘检查），都在 `js/dialogs.js`；单盘分区工具（`/api/partition/*`）已于 2026-11 移除，分区只走装机向导。
- 主页面只读：分区地图与明细表都来自 `GET /api/disks` 的快照，不允许在这里发起写盘。
- 写盘只有一条链路：`POST /api/prepare` → `submit` → `confirm`。界面不自己拼 diskpart/dism 命令，也不自己算盘符：向导第 4 步显示的盘符来自 `plan.drive_letters`，第 2 步填的大小（含可选的额外数据分区 E）只是 `prepare` 的入参，后端 `_check_partition_fit` 还会再验一次。
- 进度不是前端“估”的：执行器通过 `on_step` 回调把每个步骤的状态交给 `JobManager`，变成 `job.step` 事件和 `Job.progress`（详见 [Web API 文档](web-api.md)）。

## 状态与部署

JobManager 的任务、订阅者和序号在内存中；GUI 服务必须单进程单 worker。Web API 与 SSE 细节见[Web API 文档](web-api.md)。测试使用注入依赖和替身，不能代表真实 Windows 写盘已端到端验证。
