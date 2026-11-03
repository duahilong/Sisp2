# 安全边界与已知风险

Sisp2 的真实写盘会清空目标盘。以下描述的是当前代码行为，不是风险消除承诺。

## 写盘门禁

`SispService.execute_spec()` 对真实任务要求五道闸门：任务不是 dry-run、服务自身不是 dry-run、盘号在 `allowed_disk_numbers` 中、任务身份含 UniqueId 或 SerialNumber 且执行前重扫匹配、目标盘在执行那一刻仍可选（重新扫描后按 `protectSystemDisk` / `blockUnverified` 重算保护原因）。任一不通即 `PermissionError`；最后一道保证「计划阶段可选」不会因为中途换盘/挂载而失效。`WorkflowController` 默认装配 `WindowsDiskRunner`，但服务构造本身不等于用户确认。

确认环节有两道人工闸门，都由设置控制：`confirmBeforeStart` 决定界面是否要求先勾选「已核对目标磁盘」才能提交/确认；`typeToConfirm` 决定 `confirm()` 是否必须收到与盘号或型号匹配的 `confirm_text`（`1`、`disk 1`、`硬盘1`、型号等，忽略大小写与空白）。存在阻断项的计划会先被拒绝，提示里带不可选原因。CLI 非交互，以 `--allow-disks` 作为等价确认。

CLI 默认 `dry_run=False`（参数默认关闭），但仅当显式 `--dry-run` 时会停止任务提交；真实路径要求 `--allow-disks`，另有管理员权限、预检、受保护盘和身份检查。GUI 默认 dry-run 也是关闭的；其有效模式由 CLI `--dry-run` 与 `dryRunByDefault` 取逻辑或决定，并且构造时的 dry-run 是硬上限：后续界面或 API 的 `start_session(dry_run=false)`、`prepare(dry_run=false)` 不能关闭它。GUI 没有显式白名单时，真实模式的 `prepare()` 只会把**本次选中的盘**加入服务白名单；上一次 `prepare()` 隐式加入且本次未选中的盘会被撤出，新会话也会清空这些隐式授权。因此 GUI 选盘不是独立于该策略的额外白名单来源，使用者必须特别核对盘号与身份。

系统/启动盘、离线盘、只读盘、含系统盘符的盘及型号命中 `excluded_disk_names` 的盘会被标为不可选。保护判断和执行前身份复核不能替代备份、人工核盘或隔离测试。

`protectSystemDisk` 与 `blockUnverified` 可以从设置里关掉（CLI `--settings` 同样生效）。关掉后系统盘会重新变成「可选」，`blockUnverified=false` 也允许弱身份盘进入计划；此时唯一剩下的拦截是白名单、身份重扫和人工确认。请把这看作“拆安全网”，只在隔离测试机或专门的装机环境里使用。

同一块盘同时只允许一个未结束任务：重复 `submit` 会被拒绝；`confirm_batch()` 在锁内唯一认领任务，重复确认不会再次启动执行器。这用于防止双击、HTTP 重试或并发调用导致同一块盘被并发清空。

## Web 暴露面（高风险）

服务默认监听 `127.0.0.1`。loopback 模式启用 Host 名称校验，允许 `localhost`、`127.0.0.1`、`::1`（含方括号形式）；它用于缓解异常 Host/DNS rebinding 场景，**不是身份认证、授权或 CSRF 防护的等价物**。同源托管且不启用 CORS 也不构成认证。

`--host` 可改监听地址。非 loopback 绑定会关闭 Host 校验，当前没有鉴权；可从网络访问的接口包括任务准备、确认等写盘相关操作。将服务绑定到 `0.0.0.0` 或其他非 loopback 地址应视为高风险，不要在不可信网络暴露。

## 事件与日志

WebApi 事件缓冲最多保留 5000 个事件；旧事件淘汰后游标补发不完整。每条 SSE 连接队列上限为 2000，满时丢弃新事件。SSE 首帧及轮询补发依赖 WebApi 进程内缓冲，不能保证完整补发。重启服务也会丢失该内存缓冲；JobManager 的可选 JSONL 事件落盘不是 WebApi 游标补发来源。

真实执行可记录命令、输出及错误日志（CLI 用 `--log-dir`，也给设置里的 `logDir` 兜底；GUI 使用设置中的 `logDir`）；日志可能包含路径、配置和系统信息，须按敏感数据保护。桌面进程另写 `app.log`（RotatingFileHandler，2 MB × 3），启动时按 `logRetentionDays` 清理过期的 `disk-runner-*.log` 与 `app.log.*`。窗口位置写在设置文件同目录的 `window.json`（`rememberWindow=false` 时不写也不恢复），不包含业务数据。取消是协作式，运行中的步骤通常先完成；用户已请求取消的任务最终状态记为 cancelled，执行器返回的失败详情仍保留在结果里。

## 预检边界

预检检查管理员状态（dry-run 下不要求）、PowerShell、配置文件解析及系统盘剩余空间。只有配置中存在非空 `image_info.image_path` 时才检查镜像文件；缺少路径不保证预检发现。真实执行器随后会检查镜像路径并拒绝缺失或不存在的文件。

带上设置（GUI 总是，CLI 在给 `--settings` 时）会额外检查：填写的外部工具（`extWimlib` / `extVentoy` / `ext7z`）是否存在、`sysIsoDir` / `sysWim` / `sysBoot` / `sysUnattend` / `sysDrivers` / `sysTemp` 的类型与存在性（`sysTemp` 会用完即建）。这些检查只证明路径存在，不证明镜像可引导或工具兼容。

## 验证边界

当前测试主要使用替身扫描器、执行器和注入依赖，覆盖安全分支及命令构造，不应描述为真实 Windows 磁盘端到端验证。不要在含重要数据的物理盘上试运行。
