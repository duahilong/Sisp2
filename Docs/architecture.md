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

**分区布局是显式模型**：`app/application/partitions.py` 的 `plan_partitions()` 把 `partition_info` 解析成 `[EFI, Windows, Data(, Data2)]`，真实执行器拼 diskpart 脚本、容量预检、dry-run 预览、界面第 4 步摘要都消费同一份结果（以前这三处各写一遍，加一个分区要改三处）。向导第 2 步可以再加一个数据分区（E）：此时 payload 用 `data_partitions` 列表逐项给大小与文件系统（NTFS/exFAT），剩余容量保持未分配，界面会把它算出来并标注。盘符按角色分配：`efi` / `windows` / `data` / `data2`，每块盘 3 个或 4 个。

第 2 步的容量条有两条可拖分隔条：**C|D** 改系统分区（变化由 D 吸收，E 不动），**D|E** 只调整两个数据分区的比例（D+E 总量不变）。两者都做了下限钳制（C ≥ 60 GB、D ≥ 20 GB、E ≥ 1 GB）并向下取整，所以拖动**永远不会超过硬盘真实容量**；加 E 时默认与 D 平分剩余容量。E 的文件系统不在界面上单独选择——它与 D 一致（`dataFs()`：跟随设置 `defaultFs`，非 NTFS/exFAT 一律回落 NTFS）。
4. `submit()` 默认创建等待确认的任务；`confirm()` 对无阻断的任务开始调度。确认通过（勾选 + 盘号/型号）后会按任务盘号重新补齐隐式白名单——刷新页面会重开会话并撤销隐式授权，不补就会“确认时报白名单为空”。
5. JobManager 调用服务执行任务。真实服务在执行前检查 dry-run 策略、白名单、身份复核及执行器；执行器按分区、镜像、引导、校验步骤调用系统命令，并在任务结束后释放 EFI 盘符（避免隐藏卷盘符在下次清盘后残留成幽灵盘符）。**`verify` 判定“装完能不能开机”，不只数分区**：分区表是 GPT、分区数 ≥ 计划分区数（三分区布局是 3，加 E 是 4；盘上还有 diskpart 自动建的 16MB MSR，所以用“至少”判定）、EFI 分区里真的有 `EFI\Microsoft\Boot\bootmgfw.efi`（或 `EFI\BOOT\BOOTX64.EFI`）、系统分区里真的有 `Windows\System32\config\SYSTEM`（或 `ntoskrnl.exe`）；缺一项就判失败，并把证据写进步骤 `details`。
6. 运行结果由任务状态和事件返回；取消为协作式，已运行的系统命令不会因此立即中断。

## 演练与真实模式的判定

`SispService` 构造默认 `dry_run=True`，入口会覆盖其策略。`main.py` 的有效 dry-run 为命令行 `--dry-run` 与设置 `dryRunByDefault` 的逻辑或；`WorkflowController` 会把构造时的 dry-run 锁定为硬上限，后续 `start()` / `prepare()` 只能降级为演练，不能升级为真实执行。真实模式下，`prepare()` / `authorize_disks()` 会把本次勾选并确认过的盘加入服务白名单（没有显式白名单文件）。见[安全说明](security.md)。

## 前端结构

三个视图（主页面、系统安装、设置）+ 一个外壳，不引入框架、也没有构建步骤：

- `js/state.js` 是唯一可变状态：磁盘快照、选中项、设置草稿、任务、执行计划、向导进度；写状态后调 `notify()`。
- `js/view-*.js` 只读状态并返回/写入 DOM；`js/shell.js` 包掉菜单、工具栏、日志坞、状态栏、toast、弹窗、主题与密度；`js/derive.js` 是纯函数层（容量格式化、分区地图切片、盘符建议、大小校验），能在 `node` 里直接跑。
- 主页面只读：分区地图与明细表都来自 `GET /api/disks` 的快照，不允许在这里发起写盘。
- 写盘只有一条链路：`POST /api/prepare` → `submit` → `confirm`。界面不自己拼 diskpart/dism 命令，也不自己算盘符：向导第 4 步显示的盘符来自 `plan.drive_letters`，第 2 步填的大小（含可选的额外数据分区 E）只是 `prepare` 的入参，后端 `_check_partition_fit` 还会再验一次。
- 进度不是前端“估”的：执行器通过 `on_step` 回调把每个步骤的状态交给 `JobManager`，变成 `job.step` 事件和 `Job.progress`（详见 [Web API 文档](web-api.md)）。

## 状态与部署

JobManager 的任务、订阅者和序号在内存中；GUI 服务必须单进程单 worker。Web API 与 SSE 细节见[Web API 文档](web-api.md)。测试使用注入依赖和替身，不能代表真实 Windows 写盘已端到端验证。
