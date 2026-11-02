# 配置说明

Sisp2 有两类配置：业务配置描述装机输入；界面设置保存 GUI 偏好。二者独立。

## 业务 JSON

默认 `json/win11.json`。优先级为显式 `-j/--config`、环境变量 `SISP_CONFIG`、默认仓库路径。相对路径按当前工作目录解析。顶层应为 JSON 对象。

```json
{
  "partition_info": {
    "efi_size_mb": 300,
    "c_size_gb": 30,
    "data_size_gb": "auto"
  },
  "image_info": {
    "image_path": "D:\\images\\win11.wim",
    "image_index": 1
  },
  "excluded_disk_names": ["Example SSD"]
}
```

| 字段 | 当前校验/用途 |
|---|---|
| `partition_info` | 可省略；存在时须为对象。`efi_size_mb`、`c_size_gb` 若提供须为正数。 |
| `partition_info.data_size_gb` | 可空、`""`、`"auto"` 或数字；结构校验允许非负数（错误文案称正数），实际 runner 将其转整数，0/缺省按剩余空间处理。 |
| `image_info` | 可省略；存在时须为对象。`image_path` 可省略/空值，若提供须为字符串；`image_index` 未由此结构校验器验证。 |
| `excluded_disk_names` | 可省略；若提供须为数组（加载器接受 list/tuple；JSON 实际为数组）。型号包含名单文字（不区分大小写）时保护该盘。 |
| 其他键 | 结构校验允许并透传；具体是否被业务读取须逐项核实。 |

预检只有非空镜像路径时检查文件存在。缺路径可能通过该项预检，但真实执行器会在写盘步骤前拒绝缺少/无效镜像路径。分区容量适配检查在计划阶段进行，不能仅凭 JSON 解析成功判断配置可执行。

## GUI 设置

默认 `%APPDATA%\Sisp\settings.json`；若没有 `APPDATA`，用 `~/.config/Sisp/settings.json`。启动参数 `--settings` 可指定文件。保存格式为 `{"version":1,"settings":{...}}`。读取时坏字段逐项忽略并记录 warning；更新 patch 是整体校验，任一字段非法则不保存；未知字段拒绝。

| 字段 | 默认值 | 校验/已确认行为 |
|---|---:|---|
| `language` | `zh-CN` | `zh-CN` / `en-US` |
| `theme` | `light` | `light` / `dark` / `system` |
| `density` | `comfortable` | `compact` / `comfortable` |
| `rememberWindow` | `true` | 布尔值；业务接入待核实 |
| `confirmBeforeStart` | `true` | 布尔值；workflow 本身默认提交需确认，但此字段是否控制 UI 行为待核实 |
| `typeToConfirm` | `true` | 布尔值；是否接入执行确认流程待核实 |
| `sysDefaultGb` | `120` | 整数 60–2000；作为 `partition_info.c_size_gb` 的默认值，仅在业务 JSON 未提供该字段时生效 |
| `dataDefaultGb` | `auto` | `auto` 或整数 20–100000；作为 `partition_info.data_size_gb` 的默认值，仅补缺失字段，`auto` 不写入固定值 |
| `extDiskpart`、`extDism` | 空字符串 | 路径型字段；作为真实执行器的程序路径，空值回退 PATH / System32 探测 |
| `extWimlib`、`extVentoy`、`ext7z` | 空字符串 | 路径型字段；当前只做路径探测，未接入执行流程 |
| `sysIsoDir`、`sysWim`、`sysBoot`、`sysUnattend`、`sysDrivers`、`sysTemp` | 空字符串 | 文件/目录路径与探测；对装机流程的接入待核实。 |
| `protectSystemDisk` | `true` | 布尔值；当前服务确有系统盘保护，但该设置是否控制保护逻辑待核实。 |
| `dryRunByDefault` | `false` | 布尔值；GUI 启动时与 `--dry-run` 取逻辑或，影响会话模式。 |
| `blockUnverified` | `true` | 布尔值；具体业务接入待核实，不应视为已启用额外门禁。 |
| `logRetentionDays` | `30` | 整数 1–3650；GUI 启动时清理 `logDir` 下超过该天数的 `disk-runner-*.log` |
| `logDir` | 空字符串 | 真实执行器命令日志目录；GUI 启动时按 `logRetentionDays` 清理其中的本工具日志 |
| `logLevel` | `info` | `debug` / `info` / `warn` / `error`；设置 `app` logger 级别，当前未配置文件 handler，GUI 进程日志不落盘 |

已接入运行时的字段（其余字段只做持久化与路径探测）：`theme`、`dryRunByDefault`、`sysDefaultGb`、`dataDefaultGb`、`extDiskpart`、`extDism`、`logDir`、`logRetentionDays`、`logLevel`。

`sysDefaultGb + dataDefaultGb` 若两者为数字且总和不大于 60 会被拒绝；`sysTemp` 与 `logDir` 相同也会被拒绝。路径探测只报告存在状态或系统内置命令，不会验证业务兼容性。设置项存在不代表功能已接入。
