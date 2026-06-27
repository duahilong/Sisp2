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
  - 多个硬盘选择时，为每个硬盘启动独立 PowerShell worker 窗口
  - worker 窗口只处理一个硬盘，并重新执行运行前检查和磁盘保护校验
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

## 尚未完成的内容
- 整体完成验证与结果汇总

## 运行环境
- Windows
- 可正常调用 PowerShell
- 需要管理员权限运行
- 已安装项目依赖

## 安装依赖
```powershell
py -m pip install -r requirements.txt
```

当前已声明依赖：
- `wcwidth>=0.2.13`

## PyInstaller 打包支持

项目已兼容 PyInstaller 打包，可编译为单独的 exe 文件。

**打包方式：**
```powershell
# 使用打包脚本
.\build.bat

# 或手动执行打包命令
pyinstaller --onefile --name Sisp --console --add-data "json;json" app\main.py
```

**打包后目录结构：**
```
发布目录/
├── Sisp.exe                  # PyInstaller 打包后的 exe
├── json/
│   └── win11.json            # 配置文件（外部可修改）
└── Sw/
    ├── ghost64.exe           # 外部工具
    └── bcdboot.exe           # 外部工具
```

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

- 选择单个硬盘时，当前窗口直接执行后续流程。
- 选择多个硬盘时，主窗口为每个硬盘启动一个独立 PowerShell worker 窗口。
- 每个 worker 窗口只处理一个硬盘。
- 每个 worker 会重新执行运行前检查、重新读取配置、重新扫描硬盘并重新应用磁盘保护。
- 主程序自动启动 worker 时，会同时传入目标硬盘的 `UniqueId`、`SerialNumber`、型号和容量。
- worker 重新扫描硬盘后，会对传入的硬盘身份信息进行二次确认；若硬盘编号对应的设备与主窗口选中的设备不一致，则中止执行。
- 主窗口启动多个 worker 时，会在相邻 worker 之间等待 3 秒，降低多个 USB 硬盘同时初始化和分区时的存储服务抖动。
- worker 窗口执行完成后默认保持打开，方便查看结果。

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

| 字段 | 类型 | 单位 | 必填 | 当前是否校验路径存在 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `description` | string | - | 是 | 否 | 配置描述 |
| `win_gho` | string | - | 是 | 否 | Windows Ghost 镜像路径 |
| `efi_size` | number | MB | 是 | 否 | EFI 分区大小 |
| `c_size` | number | GB | 是 | 否 | C 分区大小 |
| `software_file` | string | - | 是 | 否 | 待拷贝的软件目录路径 |
| `gho_exe` | string | - | 是 | 否 | `ghost64.exe` 路径 |
| `bcd_exe` | string | - | 是 | 否 | `bcdboot.exe` 路径 |
| `excluded_disk_names` | string[] | - | 是 | 否 | 排除/保护的硬盘型号列表 |

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
├── Docs/                   # 项目文档
├── Sw/                     # 外部软件目录
├── app/                    # 源代码目录
│   ├── modules/
│   │   ├── common/         # 公共模块（PowerShell 执行等）
│   │   ├── config_loader/  # 配置读取模块
│   │   ├── disk_info/      # 硬盘信息模块
│   │   ├── disk_initializer/ # 硬盘初始化模块
│   │   ├── disk_partitioner/ # 分区和格式化模块
│   │   ├── ghost_writer/   # 镜像写入模块
│   │   ├── directory_copier/ # 目录拷贝模块
│   │   ├── boot_creator/   # 引导记录创建模块
│   │   ├── initialization_validator/ # 初始化结果验证模块
│   │   ├── partition_validator/ # 分区和格式化结果验证模块
│   │   └── user_interaction/ # 用户交互模块
│   ├── main.py             # 主程序入口、主控流程和 worker 模式
│   └── preflight.py        # 运行前检查
├── json/                   # JSON 配置目录
├── tests/                  # 测试目录
├── requirements.txt        # 依赖列表
└── README.md               # 项目说明
```

## 测试
当前已包含默认安全测试，主要覆盖：

- 配置读取
- 运行前检查
- 硬盘信息
- 用户交互
- 磁盘保护
- 硬盘初始化
- 初始化结果验证
- 分区和格式化
- 分区和格式化结果验证
- 镜像写入
- 目录拷贝
- 引导记录创建
- 多硬盘 worker 主流程

默认安全测试入口：

```powershell
py .\tests\run_all.py
```

默认安全测试使用模拟磁盘数据，不读取或修改本机真实硬盘。

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
- 任一步失败后必须中止后续破坏性操作，并输出失败原因
- 初始化和分区步骤必须在 PowerShell 层再次校验目标硬盘不是系统盘、启动盘、离线盘或只读盘

## 相关文档
- `Docs/项目整体.md`
- `Docs/功能及模块设计架构.md`
- `Docs/引用外部软件信息.md`
