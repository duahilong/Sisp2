# Sisp

一个面向 Windows 的系统安装辅助项目。

Sisp 使用 Python、PowerShell 和外部工具，围绕硬盘信息获取、磁盘初始化、分区格式化、系统镜像写入、目录拷贝与引导创建等流程进行阶段化开发。

## 当前状态
当前已完成**第一阶段基础能力**，并已进入真实磁盘初始化、分区格式化与镜像写入阶段。当前已实现范围为：

- 程序启动
- 运行前检查
- JSON 配置读取
- 硬盘信息获取
- 终端展示硬盘信息
- 用户选择硬盘
- 初始化硬盘
- 验证初始化结果
- 硬盘分区和格式化
- 验证分区和格式化结果
- 调用 `ghost64.exe` 写入镜像
- 镜像写入结果验证
- 拷贝目录到特定位置
- 拷贝结果验证
- 调用 `bcdboot.exe` 创建引导记录
- 引导结果验证



## v0.2.0 可靠性、质量与体验改进

本次迭代围绕 P0（磁盘安全）、P1（可靠性与可观测性）、P2（代码质量与测试）、P3（体验与文档）完成了以下改造：

- **P0 磁盘安全**
  - 目标盘必须至少具备 `UniqueId` 或 `SerialNumber` 之一；初始化/分区 PowerShell 脚本在 `Clear-Disk` / `New-Partition` 前断言身份，Ghost/拷贝/bcdboot 前重新校验。
  - 当前运行中所有已占用盘符（包括选中盘自身旧盘符）本轮禁用；不再调用全局 `mountvol /d`，每个 `New-Partition` 前检查盘符空闲，Ghost/拷贝/bcdboot 前检查盘符归属。
  - GHO、Ghost/bcdboot、源目录、配置、日志/结果目录、程序目录和 Python 解释器若位于任一目标盘，清盘前拒绝执行。
- **P1 可靠性/可观测性**
  - 新增文件日志，默认写入 `%LOCALAPPDATA%\Sisp\logs`，按运行轮转，记录 PowerShell 脚本、退出码、输出和每个步骤的耗时。
  - worker 结果写入 JSON 文件，主窗口统一汇总成功/失败、错误码、步骤和日志路径。
  - worker 改为直接启动 Python 进程（独立控制台），不再通过 PowerShell 包裹 Python 命令，减少引号转义和编码问题。
  - 扫描、初始化、分区等 PowerShell 调用增加分级超时；Ctrl+C 返回取消错误码。
  - Ghost 写入前重新校验镜像、Ghost、bcdboot 和拷贝源路径。
- **P2 代码质量与测试**
  - 拆分 `app/main.py`：新增 `cli.py`、`orchestrator.py`、`drive_letters.py`、`worker_launcher.py`、`logging_setup.py`、`results.py`、`models.py`、`paths.py`、`version.py`。
  - 新增 worker 结果、日志、dry-run、CLI、相对路径、磁盘安全等测试；当前共 178 个测试。
  - 引入 `pyproject.toml`、`ruff`、`pytest-cov`、`pre-commit` 和 GitHub Actions CI。
  - `Sisp.spec` 成为打包事实来源，`build.bat` 和 CI 都直接使用该文件。
- **P3 体验与文档**
  - 新增 `--version`、`--list-disks`、`--check-config`、`--dry-run`、`--log-dir`、`--worker-interval`、`--worker-result-timeout` 等命令行参数。
  - 新增分步进度提示、单盘执行结束后的退出等待、worker 结果汇总。
  - 新增 `Docs/故障排查.md` 和 `Docs/测试矩阵.md`，并更新 README。

## 当前已实现内容
- 主程序入口可运行
- 支持通过 `-j` 指定 JSON 配置文件路径
- 运行前检查
  - 管理员权限检查
  - PowerShell 可用性检查
  - 配置文件存在性检查
  - 配置文件可解析性检查
- JSON 配置读取与基础校验
- 硬盘完整信息获取
- 面向终端展示的硬盘摘要输出
- 无外框对齐表格展示
- 用户硬盘选择解析
  - 单个编号
  - 范围输入，如 `1-3`
  - 多个编号输入，如 `1,3,5` 或 `1 3 5`
  - 中文逗号输入
  - `a` 选择全部可选磁盘
  - `q` 退出
- 磁盘保护与选择拦截
  - 系统盘保护
  - 启动盘保护
  - 离线盘保护
  - 只读盘保护
  - 配置中 `excluded_disk_names` 命中的硬盘保护
  - 终端表格显示磁盘状态
- 多硬盘 worker 执行模式
  - 单个硬盘选择时，在当前窗口直接执行后续流程
  - 多个硬盘选择时，为每个硬盘启动独立 Python worker 窗口
  - worker 窗口只处理一个硬盘，并重新执行运行前检查和磁盘保护校验
  - worker 将结果写入 JSON 文件，主窗口等待并输出统一汇总
- 可观测性与结果汇总
  - 文件日志记录 PowerShell 脚本、退出码、输出和步骤耗时
  - worker 结果汇总成功/失败、错误码、步骤状态和日志路径
  - 支持 `--dry-run` 预览将执行的脚本/命令，不产生写盘操作
- 初始化硬盘
- 验证初始化结果
- 硬盘分区和格式化
- 验证分区和格式化结果
- 默认安全测试
- 调用 `ghost64.exe` 写入镜像
- 镜像写入结果验证
- 拷贝目录到特定位置
- 拷贝结果验证
- 调用 `bcdboot.exe` 创建引导记录
- 引导结果验证

当前真实磁盘操作使用 Windows PowerShell Storage 模块实现。初始化阶段使用 `Clear-Disk` 清除目标硬盘数据，并根据清盘后的分区表状态决定是否执行 `Initialize-Disk -PartitionStyle GPT`；分区阶段使用 `New-Partition` 和 `Format-Volume` 创建并格式化 EFI、Windows、Data1、Data2 分区。

为提升 USB 硬盘和多 worker 场景下的稳定性，分区模块会在每次创建分区前刷新目标硬盘状态，并基于 `LargestFreeExtent` 判断最大连续可用空间是否足够。`Initialize-Disk` 会自动创建 MSR 分区，分区器会在创建新分区前删除 MSR 分区，释放的空间被后续分区自动利用。最终磁盘上不再有 MSR 分区，EFI 分区大小直接使用配置字段 `efi_size`。

## 尚未完成/后续可扩展
- 真实磁盘环境下的完整回归与验收
- 更细粒度的交互式确认、失败回滚和恢复策略
- 将较长的 PowerShell 脚本进一步参数化到独立 `.ps1` 文件

## 运行环境
- Windows
- Python 3.12 及以上（源码运行）
- 可正常调用 PowerShell
- 需要管理员权限运行
- 已安装项目依赖

## 安装依赖
```powershell
py -m pip install -r requirements.txt
```

开发/测试依赖：
```powershell
py -m pip install -r requirements-dev.txt
```

当前已声明依赖：
- `wcwidth>=0.2.13`

当前已声明开发依赖：
- `pytest`、`pytest-cov`
- `ruff`
- `pre-commit`
- `pyinstaller`（打包）

## PyInstaller 打包支持

项目已兼容 PyInstaller 打包。默认使用 onedir 模式输出发布目录，避免单文件自解压 exe 被 Windows 智能应用控制误判拦截。

**打包方式：**
```powershell
# 安装打包工具
py -m pip install pyinstaller

# 使用打包脚本
.\build.bat

# 或手动执行打包命令（Sisp.spec 是打包配置的事实来源）
py -m PyInstaller --clean --noconfirm Sisp.spec
```

`build.bat` 会优先使用环境变量 `SISP_PYTHON` 指定的 Python；未指定时依次查找 `py` 和 `python`。如果当前机器没有 `py` 命令，可先设置：

```powershell
$env:SISP_PYTHON = "C:\Path\To\python.exe"
.\build.bat
```

**打包后目录结构：**
```
dist/Sisp/
├── Sisp.exe                  # 程序入口
├── _internal/                # PyInstaller onedir 运行依赖目录，必须保留
├── json/
│   └── win11.json            # 配置文件（外部可修改）
└── Sw/
    ├── ghost64.exe           # 外部工具（存在时由 build.bat 自动复制）
    └── bcdboot.exe           # 外部工具（存在时由 build.bat 自动复制）
```

`_internal/` 是 PyInstaller onedir 模式生成的运行依赖目录，必须随 `Sisp.exe` 一起发布，不能只复制 `Sisp.exe` 单独运行。

`build.bat` 会自动复制 `json/win11.json` 到发布目录；若本地 `Sw/ghost64.exe` 和 `Sw/bcdboot.exe` 存在，也会自动复制到 `dist/Sisp/Sw/`。若外部工具不存在，需要在发布前手动补齐。

**打包后使用方式：**
```powershell
Sisp.exe -j D:\Sisp\json\win11.json
```

**兼容性说明：**
- 代码通过 `sys.frozen` 检测打包环境
- 打包后使用 `sys.executable` 启动 worker 进程
- 配置文件和外部工具路径使用绝对路径

## 运行方式
默认读取 `json/win11.json`：

```powershell
py .\app\main.py
```

指定配置文件：

```powershell
py .\app\main.py -j .\json\win11.json
```

也支持绝对路径：

```powershell
py .\app\main.py -j D:\Code-Project\Sisp2\json\win11.json
```

worker 模式用于处理单个指定硬盘。它主要由主程序在多硬盘执行时自动启动，也可用于单盘调试：

```powershell
py .\app\main.py --worker-disk 2 -j .\json\win11.json
```

执行规则：

- 选择单个硬盘时，当前窗口直接执行后续流程，结束前等待用户按 Enter 退出。
- 选择多个硬盘时，主窗口为每个硬盘启动一个独立 Python worker 窗口。
- 每个 worker 窗口只处理一个硬盘。
- 每个 worker 会重新执行运行前检查、重新读取配置、重新扫描硬盘并重新应用磁盘保护。
- 主程序自动启动 worker 时，会同时传入目标硬盘的 `UniqueId`、`SerialNumber`、型号和容量。
- worker 重新扫描硬盘后，会对传入的硬盘身份信息进行二次确认；若硬盘编号对应的设备与主窗口选中的设备不一致，则中止执行。
- 主窗口启动多个 worker 时，默认在相邻 worker 之间等待 6 秒，可通过 `--worker-interval` 调整，降低多个 USB 硬盘同时初始化和分区时的存储服务抖动。
- worker 将结果写入 `--worker-result-file` 指定的 JSON 文件；主窗口等待结果并输出汇总。
- worker 会继承主窗口的 `--log-dir` / `--no-log` 日志配置。
- worker 窗口执行完成后默认保持打开，方便查看结果；主窗口可通过 `--worker-result-timeout` 设置等待上限。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `-j <path>` | 指定 JSON 配置文件路径 |
| `--version` | 输出程序版本 |
| `--list-disks` | 只读取并列出硬盘信息，不执行写盘操作 |
| `--check-config` | 检查配置文件、关键路径和文件/目录类型 |
| `--dry-run` | 打印将执行的 PowerShell 脚本/命令，不执行写盘操作；仍会校验配置和关键路径 |
| `--log-dir <dir>` | 指定日志目录，默认 `%LOCALAPPDATA%\Sisp\logs` |
| `--no-log` | 关闭文件日志 |
| `--worker-interval <seconds>` | 多盘 worker 启动间隔，默认 6 秒 |
| `--result-dir <dir>` | worker 结果文件目录 |
| `--worker-result-timeout <seconds>` | 主窗口等待 worker 结果的最长时间，默认 6 小时 |
| `--worker-disk <n>` | worker 模式：处理指定硬盘 |
| `--worker-result-file <path>` | worker 模式：结果 JSON 文件路径 |

示例：

```powershell
# 查看版本
py .\app\main.py --version

# 只检查配置
py .\app\main.py --check-config -j .\json\win11.json

# 只列出硬盘
py .\app\main.py --list-disks

# 预览执行计划（不会写盘）
py .\app\main.py --dry-run -j .\json\win11.json

# 自定义日志目录和多盘启动间隔
py .\app\main.py --log-dir D:\Sisp\logs --worker-interval 10
```

## 日志与结果文件

- 文件日志默认位置：`%LOCALAPPDATA%\Sisp\logs\sisp_<时间戳>_<进程号>.log`
- 单个日志文件最大 5 MB，保留 5 个备份。
- 日志包含 PowerShell 脚本、退出码、stdout/stderr、每个步骤的通过/失败和耗时。
- 可用 `--log-dir` 指定目录，或用 `--no-log` 关闭文件日志。
- 多盘模式下，worker 结果文件默认位于临时目录 `Sisp\results\<时间戳>_<pid>\disk_<n>.json`，也可用 `--result-dir` 指定。
- 主窗口会在所有 worker 结果返回后打印汇总，例如：

```text
================================================================================
Sisp 执行结果汇总
================================================================================
硬盘 2: 成功 - 当前阶段处理完成
  步骤: 初始化=通过，初始化验证=通过，分区格式化=通过，分区验证=通过，Ghost 镜像写入=通过，目录拷贝=通过，引导创建=通过
  日志: C:\Users\...\AppData\Local\Sisp\logs\sisp_....log
硬盘 3: 失败 [错误码 3] - 硬盘分区和格式化失败: 硬盘 3: ...
--------------------------------------------------------------------------------
合计: 2 块 | 成功 1 | 失败 1
```

## 配置说明
当前代码已校验的关键字段包括：

- `description`
- `win_gho`
- `efi_size`
- `c_size`
- `software_file`
- `gho_exe`
- `bcd_exe`
- `excluded_disk_names`

配置字段说明：

| 字段 | 类型 | 单位 | 必填 | 路径存在性校验时机 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `description` | string | - | 是 | 否 | 配置描述 |
| `win_gho` | string | - | 是 | 执行前校验 | Windows Ghost 镜像路径 |
| `efi_size` | number | MB | 是 | 否 | EFI 分区大小 |
| `c_size` | number | GB | 是 | 否 | C 分区大小 |
| `software_file` | string | - | 是 | 执行前校验 | 待拷贝的软件目录路径 |
| `gho_exe` | string | - | 是 | 执行前校验 | `ghost64.exe` 路径 |
| `bcd_exe` | string | - | 是 | 执行前校验 | `bcdboot.exe` 路径 |
| `excluded_disk_names` | string[] | - | 是 | 否 | 排除/保护的硬盘型号列表 |

配置解析阶段只校验必要字段和基础类型。程序在开始执行真实磁盘处理前，会校验 `win_gho`、`software_file`、`gho_exe`、`bcd_exe` 指向的路径是否存在，并检查文件/目录类型，避免清盘后才发现资源路径错误。Ghost 写入前还会再次校验一次。

配置中的相对路径会相对 JSON 配置文件所在目录解析；绝对路径保持不变。例如配置文件在 `D:\Sisp\json\win11.json`，`win_gho` 为 `images\win.gho` 时，实际读取 `D:\Sisp\json\images\win.gho`。

配置加载后会整理为结构化数据，包含：
- 配置文件路径
- 原始配置
- 分区信息
- 功能开关信息
- 镜像信息
- 外部软件路径信息
- 目录拷贝信息
- 排除硬盘名称列表

## 代码结构
```text
Sisp2/
├── Docs/                   # 项目文档（故障排查、测试矩阵等）
├── Sw/                     # 外部软件目录
├── app/                    # 源代码目录
│   ├── cli.py              # 命令行解析、诊断命令和 main()
│   ├── orchestrator.py     # 单盘/多盘/worker 流程编排
│   ├── disk_safety.py      # 身份断言、目标盘资源/盘符归属检查
│   ├── drive_letters.py    # 盘符分配与解析
│   ├── worker_launcher.py  # 独立 Python worker 启动
│   ├── logging_setup.py    # 文件日志与 PowerShell 日志
│   ├── results.py          # worker 结果读写与汇总
│   ├── models.py           # RunContext / WorkerResult 等数据模型
│   ├── paths.py            # 应用根目录
│   ├── version.py          # 版本号
│   ├── main.py             # 轻量入口与兼容性 re-export
│   ├── preflight.py        # 运行前检查
│   └── modules/            # 各功能模块（配置、磁盘、Ghost、引导等）
├── json/                   # JSON 配置目录
├── tests/                  # 测试目录
├── Sisp.spec               # PyInstaller 打包配置（事实来源）
├── build.bat               # 本地打包脚本
├── pyproject.toml          # 项目元数据、ruff/coverage 配置
├── requirements.txt        # 运行依赖
├── requirements-dev.txt    # 开发/测试依赖
└── README.md               # 项目说明
```

## 测试
当前共 178 个测试，主要覆盖：

- 配置读取、相对路径解析、文件/目录类型校验
- 运行前检查
- 硬盘信息
- 用户交互
- 磁盘保护
- 目标盘身份、路径→磁盘解析、目标盘资源冲突、盘符归属
- 硬盘初始化
- 初始化结果验证
- 分区和格式化
- 分区和格式化结果验证
- 镜像写入
- 目录拷贝
- 引导记录创建
- 多硬盘 worker 主流程
- worker 结果文件与汇总
- 文件日志、PowerShell 超时/异常日志
- dry-run、`--check-config`、`--list-disks`、`--version`
- CLI 参数传递和 worker 直接 Python 启动

测试基于 pytest 组织（`pytest.ini` 已配置，根目录 `conftest.py` 负责导入路径），可直接运行：

```powershell
py -m pytest
```

带覆盖率：

```powershell
py -m pytest -o addopts="" --cov=app --cov-report=term-missing
```

代码检查：

```powershell
py -m ruff check app tests
```

默认安全测试入口（等价于 pytest 全量运行，并额外生成无效输入场景报告 `tests/.output/invalid_input_results.txt`）：

```powershell
py .\tests\run_all.py
```

默认安全测试使用模拟磁盘数据，不读取或修改本机真实硬盘。当前覆盖率约 84%，CI 中设置了 70% 的最低门槛。

以下脚本会读取本机真实磁盘信息，仅作为手动诊断工具使用，不包含在默认安全测试入口中：

```powershell
py .\tests\test_disk_info_raw.py
```

该脚本只读取磁盘信息，不执行初始化、分区、格式化或写入操作。

默认安全测试会覆盖初始化、分区和验证逻辑，但通过 mock 和脚本文本断言完成，不会对真实硬盘执行 `Clear-Disk`、`Initialize-Disk`、`New-Partition` 或 `Format-Volume`。

## 风险提示
后续目标涉及真实磁盘操作，开发时应重点关注：

- 系统盘保护
- 引导盘保护
- 排除盘名策略
- 每一步执行结果验证
- 执行日志与失败排障能力

当前真实磁盘操作策略：用户选择可选硬盘后直接进入后续模块，依赖磁盘保护机制和执行模块内的硬性安全校验。应遵守以下安全规则：

- 默认禁止操作系统盘、启动盘和配置中 `excluded_disk_names` 命中的硬盘
- 破坏性操作前必须显示目标硬盘编号、型号、容量和盘符
- 目标硬盘必须至少具备 `UniqueId` 或 `SerialNumber` 之一；缺失时拒绝写盘
- 初始化和分区步骤必须在 PowerShell 层再次校验目标硬盘身份、系统盘/启动盘/离线盘/只读盘状态，身份断言紧邻 `Clear-Disk` / `New-Partition`
- 当前运行中已占用的盘符（包括选中硬盘自身的旧盘符）本轮不得复用；分区前逐盘符确认未被占用，Ghost/目录拷贝/bcdboot 前再次确认盘符仍属于目标硬盘
- GHO、Ghost/bcdboot、软件源目录、配置文件、日志/结果目录、程序目录和 Python 解释器不得位于任何目标硬盘上；检查在清盘前完成
- 任一步失败后必须中止后续破坏性操作，并输出失败原因

## 相关文档
- `Docs/项目整体.md`
- `Docs/功能及模块设计架构.md`
- `Docs/引用外部软件信息.md`
- `Docs/故障排查.md`
- `Docs/测试矩阵.md`
