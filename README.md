# Sisp2

Windows 硬盘装机工具：自动把一块空白 / 待重装的硬盘完成 **清盘 → GPT 分区（EFI + Windows + Data 三分区）→ dism 应用 WIM 镜像 → bcdboot 写入 UEFI 引导 → 校验** 的全流程。

本工具只用 Python 标准库，底层调用 Windows 自带命令完成全部磁盘操作，不依赖任何第三方运行时包。

---

## 特性

- **双入口**：命令行（`python -m app.cli`）用于批量 / 自动化；Web 图形界面（`python -m app.gui_web.main`）用于交互操作。两者共用同一套应用层逻辑。
- **多层纵深安全**：默认 dry-run，真实写盘必须通过四道安全闸门（显式关闭 dry-run + 盘号白名单 + 硬盘身份比对 + 注入执行器）。
- **协作式取消**：正在执行的分区 / 写镜像不会被强行打断，取消在「当前步骤完成后」生效。
- **完整执行日志**：真实执行的每条命令、退出码、输出都会写入日志文件，方便排查。
- **可测试**：磁盘、PowerShell、配置加载等外部依赖全部可注入，单元测试完全绕过真实磁盘。

---

## 安装

要求 **Python 3.10+**（使用了 `X | None` 等新语法），且只能在 Windows 上运行。

```powershell
# 命令行入口不需要任何第三方包，主体只用标准库
python -m pip install -r requirements.txt

# 图形界面（可选）安装 Web 服务与桌面窗口依赖
python -m pip install -r requirements-web.txt
```

> 磁盘扫描、分区、格式化、引导修复全部通过 Windows 自带命令完成（PowerShell / diskpart / dism / bcdboot），无第三方运行时依赖。
> 界面是「REST + SSE」的本机 HTTP 服务（FastAPI + uvicorn，只监听 127.0.0.1），API 文档见 `Docs/web-api.md`。

---

## 快速开始

### 命令行（CLI）

```powershell
# 1. 查看硬盘列表（只读，不写盘）
python -m app.cli --list

# 2. 演练：生成执行计划，不提交任务，绝不写盘
python -m app.cli --dry-run

# 3. 真实执行（必须先提供盘号白名单，且需管理员权限）
python -m app.cli --allow-disks 1,2

# 4. 指定业务配置文件 + JSON 输出（便于脚本集成）
python -m app.cli -j json/win11.json --allow-disks 1 --json
```

### 图形界面（GUI）

```powershell
# 桌面窗口（后台起本机 HTTP 服务，pywebview 加载）
python -m app.gui_web.main

# 浏览器模式（前台起服务，用 Edge/Chromium 打开，不依赖 pywebview）
python -m app.gui_web.main --serve

# 演练模式 / 提权 / 指定端口
python -m app.gui_web.main --dry-run --elevate --serve --port 8765
```

界面启动后可访问接口文档：`http://127.0.0.1:8765/docs`。

---

## 业务配置文件

业务配置描述「装系统要用的镜像、分区大小、排除名单」，由运维维护，默认位于 `json/win11.json`：

```json
{
  "partition_info": {
    "efi_size_mb": 300,
    "c_size_gb": 30,
    "data_size_gb": "auto"
  },
  "image_info": {
    "image_path": "D:\\images\\win11-pro-23h2.wim",
    "image_index": 1
  },
  "excluded_disk_names": ["Samsung SSD 980 PRO 1TB"]
}
```

- `partition_info.efi_size_mb`：EFI 分区大小（MB）
- `partition_info.c_size_gb`：Windows 分区大小（GB）
- `partition_info.data_size_gb`：数据分区大小（GB）或 `"auto"`（剩余空间）
- `image_info.image_path`：要应用的 WIM 镜像绝对路径
- `image_info.image_index`：镜像索引（默认 1）
- `excluded_disk_names`：型号命中此名单的硬盘将被保护（不可选中）

> **注意**：请先把 `image_info.image_path` 改成你机器上实际存在的镜像文件路径。

除 `json/win11.json` 外，也可以用 `-j / --config` 指定其他路径，或用环境变量 `SISP_CONFIG` 指定。

---

## 运行前检查（preflight）

动任何硬盘之前，工具会先检查：

- 当前是否以管理员身份运行（dry-run 模式下不要求）
- PowerShell 是否可用
- 配置文件是否存在、能否解析
- 镜像文件是否存在
- 系统盘剩余空间是否充足（≥ 2GB）

任何一项不通过都会变成阻断项，界面据此禁用「开始」。

---

## 安全设计

真实写盘是**破坏性操作**，本工具默认 dry-run，真实执行必须通过以下全部闸门：

1. **显式关闭 dry-run**：`SispService(dry_run=False)`
2. **盘号白名单**：目标硬盘编号必须在 `allowed_disk_numbers` 内（CLI 用 `--allow-disks` 指定，GUI 按会话中用户勾选授权）
3. **硬盘身份一致性**：执行任务前会重新扫描，比对 UniqueId / SerialNumber 是否与确认时一致，防止「扫描时是 A 盘、执行时被换成 B 盘」
4. **注入执行器**：必须显式注入 `real_runner`（真实实现是 `WindowsDiskRunner`），否则拒绝执行

另外：

- 系统盘 / 启动盘 / 离线 / 只读 / 含系统盘符 / 命中排除名单的硬盘会被自动保护（`is_selectable=False`）
- 盘符分配只用 E–Z，排除系统盘符和已占用盘符
- 取消是协作式的：正在执行的步骤跑完后才停
- CLI 有独立退出码：`0` 成功、`2` 预检失败、`3` 执行失败、`4` 参数或安全策略拒绝

---

## 架构

```
入口层    app/cli.py (5步流程)      app/gui_web/main.py (桌面窗 / 浏览器，统一走 HTTP)
             └──────────┬──────────────────────┘
服务层               app/gui_web/server.py     (FastAPI 路由：REST 管操作 + SSE 管事件)
                    app/gui_web/api.py        (WebApi：转发 + 信封 + 事件缓冲)
                        │
适配层               app/application/workflow.py  (WorkflowController)
                        │  start → refresh → prepare → submit/confirm/cancel
应用层               app/application/service.py   (SispService — 门面 / 安全闸门)
                    app/application/jobs.py       (JobManager — 状态机 / 并发 / 取消 / 事件)
                    app/application/real_runner.py (WindowsDiskRunner — 真实执行)
契约层              app/application/contracts.py  (DiskInfo / PartitionInfo / ConfigCheckResult...)
                    app/models.py                 (StepResult / WorkerResult)
能力层              app/modules/disk_info        (PowerShell 扫描)
                    app/modules/settings         (界面设置持久化)
                    app/modules/common           (run_powershell / 编码 / 步骤输出)
                    app/drive_letters.py         (盘符分配)
                    app/preflight.py             (运行前检查)
                    app/orchestrator.py          (硬盘身份快照)
```

**核心设计**：

- **依赖注入**：`SispService` 的 `real_runner` / `disk_scanner` / `powershell` 全部可注入；`WorkflowController` 的 `preflight_runner` / `job_manager` 可注入。
- **契约与实现分离**：契约层只装可 JSON 序列化的数据，`to_dict()` 结果可直接进 HTTP 响应 / 日志。
- **GUI 与业务解耦**：`WebApi` 只做「转发 + 信封包装 + 事件缓冲」，一行磁盘逻辑都没有；
  `server.py` 只做「HTTP 协议 ↔ WebApi 方法」转换，REST 管操作、SSE 管事件推送（详见 `Docs/web-api.md`）。
- **界面设置与业务配置分离**：界面设置（语言 / 主题 / 默认分区大小 / 外部程序路径等）存在 `%APPDATA%\Sisp\settings.json`，由 `app.modules.settings` 管理，不会污染业务配置。

---

## 测试

```powershell
python -m pytest tests/ -q
```

目前共 **41 个测试**，全部用替身执行器，不碰 diskpart / dism：

- `tests/test_real_execution.py` — 回归测试，验证真实执行路径可达、安全闸门、取消、diskpart 脚本布局等。
- `tests/test_settings_service.py` — 设置模块单元测试（加载 / 校验 / 持久化 / 路径探测）。
- `tests/test_web_api.py` — Web API 集成测试（REST 端点、Host 校验、任务全流程、SSE 事件流；另需 `httpx`）。

---

## 目录结构

```
Sisp2/
├── app/
│   ├── cli.py                    # 命令行入口（python -m app.cli）
│   ├── drive_letters.py          # 盘符分配
│   ├── orchestrator.py           # 硬盘身份快照与比对
│   ├── preflight.py              # 运行前检查
│   ├── models.py                 # StepResult / WorkerResult 结果模型
│   ├── application/              # 应用层（workflow / service / jobs / real_runner）
│   ├── gui/                      # GUI 公共代码（目前只有提权工具）
│   ├── gui_web/                  # Web 视图（FastAPI 服务 + pywebview 窗口 + 单文件 HTML 界面）
│   │   ├── main.py               # 入口（桌面 / 浏览器两种模式，统一走 HTTP）
│   │   ├── server.py             # FastAPI 路由层（REST + SSE + Host 校验）
│   │   ├── api.py                # WebApi — 界面调用的后端方法
│   │   └── ui/index.html         # 界面前端（单文件）
│   └── modules/                  # 能力层（disk_info / settings / common / partition_validator）
├── json/win11.json               # 业务配置（镜像、分区、排除名单）
├── requirements.txt              # 运行时依赖（实际只用标准库）
├── requirements-web.txt          # GUI 依赖（fastapi / uvicorn / pywebview）
├── Docs/web-api.md               # Web API 与前后端分离架构文档
├── tests/                        # 测试
└── README.md
```

---

## 常见问题

**运行时报「需要管理员权限」？**

磁盘清盘 / 分区 / 写引导需要管理员权限。请以管理员身份运行 PowerShell / CMD，或加 `--elevate` 通过 UAC 重新拉起进程。

**真实写盘被拒绝（退出码 4）？**

确认你已提供 `--allow-disks` 白名单，且目标硬盘未被保护规则命中（系统盘 / 启动盘 / 离线 / 只读等）。

**GUI 打不开窗口？**

确认已安装 `pywebview`（`pip install -r requirements-web.txt`），或改用浏览器模式：`python -m app.gui_web.main --serve`（不需要 pywebview）。

**提示「端口已被占用」？**

8765 端口被别的程序占了，换一个：`python -m app.gui_web.main --serve --port 8766`。
