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
| `language` | `zh-CN` | `zh-CN` / `en-US`；只做持久化，界面文案目前固定为简体中文 |
| `theme` | `light` | `light` / `dark` / `system`；读取后与设置页修改时立即切换 `data-theme` |
| `density` | `comfortable` | `compact` / `comfortable`；切换 `data-density`，紧凑模式收紧卡片内边距、表格行距与字号 |
| `rememberWindow` | `true` | 布尔值；关窗时把窗口位置/大小写入设置文件同目录的 `window.json`，关闭该开关则不保存也不恢复 |
| `confirmBeforeStart` | `true` | 布尔值；实际执行模式下，未勾选「我已核对目标磁盘」时提交/确认按钮禁用，并在提交前弹一次确认框 |
| `typeToConfirm` | `true` | 布尔值；真实执行前 `/api/confirm` 必须带匹配的 `confirm_text`（盘号如 `1` 或磁盘型号，忽略大小写与空白），否则 `E_CONFIRM`。CLI 非交互，以 `--allow-disks` 作为确认 |
| `protectSystemDisk` | `true` | 布尔值；控制系统盘/启动盘/含系统盘符的盘是否不可选，执行层闸门复用同一判定 |
| `blockUnverified` | `true` | 布尔值；控制缺少 `UniqueId`/`SerialNumber` 的盘是否不可选，执行层闸门同样生效 |

这三个开关是**热生效**的：保存后无需重启，下一次「重新扫描硬盘」、生成计划、提交或确认都会重新读取设置；扫描与执行层（`SispService`）的开关会同步刷新，不会出现界面说可选、执行层仍拒绝的不一致。
| `sysDefaultGb` | `120` | 整数 60–2000；作为 `partition_info.c_size_gb` 的默认值，仅在业务 JSON 未提供该字段时生效 |
| `dataDefaultGb` | `auto` | `auto` 或整数 20–100000；作为 `partition_info.data_size_gb` 的默认值，仅补缺失字段，`auto` 不写入固定值 |
| `extDiskpart`、`extDism` | 空字符串 | 路径型字段；作为真实执行器的程序路径，空值回退 PATH / System32 探测 |
| `extWimlib`、`extVentoy`、`ext7z` | 空字符串 | 路径型字段；参与运行前检查：一旦填写就必须存在，否则预检不通过（不驱动执行流程） |
| `sysTemp` | 空字符串 | 目录；作为 dism/diskpart 子进程的 `TMP`/`TEMP`，不存在会自动创建；与 `logDir` 相同会被拒绝 |
| `sysUnattend` | 空字符串 | 应答文件；真实执行时追加 `dism /Apply-Unattend:<文件>` |
| `sysDrivers` | 空字符串 | 目录；真实执行时追加 `dism /Add-Driver:<目录> /Recurse` |
| `sysBoot` | 空字符串 | 目录；设置后作为 `bcdboot` 的源目录（镜像里没有 `Windows\Boot` 时的兜底） |
| `sysWim`、`sysIsoDir` | 空字符串 | 文件/目录；只参与运行前检查（存在性/类型），不改变镜像来源 |
| `dryRunByDefault` | `false` | 布尔值；GUI 启动时与 `--dry-run` 取逻辑或，影响会话模式 |
| `logRetentionDays` | `30` | 整数 1–3650；启动时清理 `logDir` 下超过该天数的 `disk-runner-*.log` 与 `app.log.*` 备份 |
| `logDir` | 空字符串 | 真实执行器命令日志目录；桌面进程还会在其中写 `app.log`（2 MB × 3 轮转） |
| `logLevel` | `info` | `debug` / `info` / `warn` / `error`；设置 `app` logger 级别，桌面进程同时落盘到 `logDir/app.log` |

CLI 加 `--settings <path>` 后，上表的安全开关（`protectSystemDisk`、`blockUnverified`）、执行器可选项（`sysTemp`、`sysUnattend`、`sysDrivers`、`sysBoot`）、外部工具与系统文件路径同样生效，运行前检查会多出「外部工具可用性检查」「设置路径有效性检查」两项。

仍然只做持久化的字段：`language`（界面文案未做多语言）。其余字段均已接入运行时。

`sysDefaultGb + dataDefaultGb` 若两者为数字且总和不大于 60 会被拒绝；`sysTemp` 与 `logDir` 相同也会被拒绝。路径探测只报告存在状态或系统内置命令，不会验证业务兼容性；预检的单项通过也不代表镜像/引导真的可用。
