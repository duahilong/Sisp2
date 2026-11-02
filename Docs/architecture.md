# 架构与工作流

## 分层

- 入口：`app/cli.py` 与 `app/gui_web/main.py`。CLI 构造控制器并执行完整命令行流程；GUI 装配 WebApi、工作流和 FastAPI。
- HTTP/UI：`app/gui_web/server.py` 将 REST/SSE 转换为 WebApi 调用；`api.py` 包装响应信封、转发工作流并缓冲事件；`ui/index.html` 为单文件前端。
- 应用：`application/workflow.py` 管理会话、预检结果、扫描快照、计划及确认；`service.py` 提供扫描、配置检查、盘符分配与安全执行门；`jobs.py` 管理状态、调度、取消和事件。
- 执行与能力：`real_runner.py` 调用 Windows 系统工具；`preflight.py` 读配置并做启动检查；磁盘扫描、设置、盘符分配等位于 `modules/` 与 `drive_letters.py`。

## 一次工作流

1. `WorkflowController.start()` 运行预检、加载业务配置并扫描硬盘，应用系统盘/启动盘/离线/只读/系统盘符及型号排除规则。
2. `refresh_disks()` 可重新扫描；`check_config()` 单独重验配置。
3. `prepare()` 根据选盘与配置构造分区计划、盘符、身份快照及检查/阻断信息。GUI 会把设置中的默认分区大小用于补足业务配置缺失的字段（配置已有的值优先），并把 `extDiskpart` / `extDism` 作为真实执行器的程序路径。GUI 可对单盘覆盖部分分区/镜像参数。
4. `submit()` 默认创建等待确认的任务；`confirm()` 对无阻断的任务开始调度。
5. JobManager 调用服务执行任务。真实服务在执行前检查 dry-run 策略、白名单、身份复核及执行器；执行器按分区、镜像、引导、校验步骤调用系统命令。
6. 运行结果由任务状态和事件返回；取消为协作式，已运行的系统命令不会因此立即中断。

## CLI 与 GUI 的模式差异

`SispService` 构造默认 `dry_run=True`，但入口会覆盖其策略。CLI 只有传 `--dry-run` 才在演练分支停止提交；不传时意图进入真实执行，还要求 `--allow-disks`、管理员权限及其他检查。GUI `main.py` 的有效 dry-run 为命令行 `--dry-run` 与 `dryRunByDefault` 的逻辑或；`WorkflowController` 会把构造时的 dry-run 锁定为硬上限，后续 `start()` / `prepare()` 只能降级为演练，不能升级为真实执行。GUI 无显式白名单时，真实模式下 `prepare()` 会把所选盘加入服务白名单。见[安全说明](security.md)。

## 状态与部署

JobManager 的任务、订阅者和序号在内存中；GUI 服务必须单进程单 worker。Web API 与 SSE 细节见[Web API 文档](web-api.md)。测试使用注入依赖和替身，不能代表真实 Windows 写盘已端到端验证。
