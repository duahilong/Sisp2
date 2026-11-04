# Sisp2 v1.0.1 打包产物测试报告

- 报告日期：2026-11-04
- 被测对象：`dist/Sisp2.exe`（单文件 GUI，版本 1.0.1）
- 测试机器：`windows-mcp-vm`（`CHINAMI-IS74ND6`）
- 结论：**全部通过（API 用例 23/23、界面冒烟 0 问题）**，未发现产品缺陷

---

## 1. 被测物

| 项 | 值 |
| --- | --- |
| 文件 | `Sisp2.exe` |
| 大小 | 18,716,836 B（17.8 MB） |
| SHA256 | `12a941b09224a0afcee92c9c32bd30f629e09bd13b843d4b213968ea84e23ebd` |
| 文件版本 | 1.0.1.0（ProductVersion 1.0.1.0） |
| 来源 | 本机 `main @ e4b61c3`（tag `v1.0.1`）用 Python 3.14.6 + PyInstaller 6.22.2 打包 |
| 内置资源 | `ui/index.html`、`ui/css`、`ui/js`（10 个 ES 模块）、`json/win11.example.json` |

传输方式与校验：

1. 本机 `python -m http.server 8970 --directory dist` 起临时文件服务；
2. 本机 Windows 防火墙临时放行 **仅 `10.2.0.153`（VM）** 访问 TCP 8970；
3. VM 侧 `WebClient` 下载（`Invoke-WebRequest` 会走系统代理而失败，见 §6）；
4. 下载后 SHA256 与本地逐字节一致（`12A941B0…3EBD`）；
5. 传输完成后立即删除防火墙规则并关停临时服务。

---

## 2. 环境

| 项 | 值 |
| --- | --- |
| OS | Windows 10 22H2（`Windows-10-10.0.19045`） |
| 磁盘 | Disk 0：`GeIL G7T 1TB`（931.5 GB，GPT，系统盘，含 C:/D:/E:）<br>Disk 1：`GeIL P3A 2TB`（1863 GB，GPT，**本轮写盘目标**） |
| 测试镜像 | `D:\sisp-test\test.wim`，dism 索引 1（名称 `SispTest`，70,287,008 B） |
| 测试驱动 | Python 3.15.2（只跑 REST 测试脚本）、Edge（界面冒烟）、Node.js v26（`tools/ui_smoke.mjs`） |
| 配置载体 | `D:\sisp-test\v101\settings.json`（v1.0.1 起以界面设置为主，业务 JSON 降级为可选预设） |
| 服务进程 | 演练实例 `--serve --port 8895 --dry-run`；真实实例 `--serve --port 8897`（两者用同一份 settings） |

测试工作目录：`D:\sisp-test\v101\`（exe、settings、脚本、日志、截图、结果 JSON 都在这里）

---

## 3. 测试方法

- **只用 REST 驱动**：脚本 `run_tests.py` 直接打 `/api/*`，不手点界面，保证可重复；区分
  「演练模式」与「真实模式」两轮，用例按模式自动跳过（`SKIP`）。
- **界面单独冒烟**：`tools/ui_smoke.mjs` 用 Chrome DevTools 协议驱动 Edge，走主页面 →
  四步向导 → 设置页 → 菜单 → 修复引导弹窗，断言关键节点并截图。
- **写盘目标**：真实写盘一律打在 Disk 1（专用测试盘，已确认可清空）；Disk 0 全程只读。
- 结果落盘：`results-dry.json`（16 例）、`results-real.json`（23 例）、`shots-dry/`（10 张）。

---

## 4. 用例与结果

### 4.1 总表（同一脚本，两种模式）

`演练` = 对 `--dry-run` 实例执行；`真实` = 对真实模式实例执行。

| ID | 用例 | 演练 | 真实 |
| --- | --- | --- | --- |
| A1 | `GET /health` 返回 `{"ok":true}` | ✅ | ✅ |
| A2 | `/api/info` 报 1.0.1，且 `dry_run` 与进程模式一致（演练 True / 真实 False） | ✅ | ✅ |
| A3 | 打包后的静态资源可访问：`/` 内联引用 `/static/js/main.js`，js 含 `import`，css 含 `:root` | ✅ | ✅ |
| A4 | `/api/settings` 34 项、6 个分组、12 个路径键，含新增 `efiDefaultMb` / `imageDefaultIndex` / `excludedDiskNames` | ✅ | ✅ |
| A5 | `PUT /api/settings` 写入镜像、分区默认值、日志目录、主题密度 | ✅ | ✅ |
| A6 | `POST /api/settings/export` 导出可携带的 JSON | ✅ | ✅ |
| A7 | `POST /api/settings/import` 把刚导出的文件回灌（幂等） | ✅ | ✅ |
| B1 | `POST /api/session`：8 项预检、2 块盘、**无阻断项**（设置的 `sysWim` 压过配置文件占位路径） | ✅ | ✅ |
| B2 | `/api/disks` 卷标编码正确（安装前 `["Windows","Data"]`，安装后同） | ✅ | ✅ |
| B3 | `POST /api/images` 用 dism 读出 `test.wim` 索引（1 / SispTest） | ✅ | ✅ |
| B4 | `GET /api/boot/report` 给出 `boot_ok` + `orphan_esp`，不写盘 | ✅ | ✅ |
| C1 | `POST /api/partition/plan` 永远演练：`dry_run=true`，只出 5 行 diskpart 脚本 | ✅ | ✅ |
| C2 | ESP/MSR/恢复分区拒绝删除（`E_PARTITION`：分区 2 是 EFI 系统分区，拒绝操作） | ✅ | ✅ |
| H1 | 危险操作缺确认文本 → `E_CONFIRM` | ✅ | ✅ |
| H2 | 不存在的任务 → `E_NOT_FOUND` | ✅ | ✅ |
| H3 | 受保护的系统盘：计划带阻断项、不可执行 | ✅ | ✅ |
| D1 | 真实模式计划：镜像与分区大小来自设置（300 MB / 120 GB / 300 GB，镜像 index 1），盘符 F/G/J | — | ✅ |
| E1 | 真实整盘安装：prepare → submit → confirm（确认文本 = 盘号） | — | ✅ |
| E2 | 六步执行结果：`efi/windows/data/image/boot/verify` 全 `ok`，进度 6/6 = 100%，用时 3.1 s | — | ✅ |
| E3 | 任务日志尾部可读（`/api/jobs/{id}/log?tail=`，249 行） | — | ✅ |
| F1 | 安装后分区结构：GPT + MSR 15.98 MB + ESP 300 MB + 120 GB NTFS「Windows」+ 300 GB NTFS「Data」 | — | ✅ |
| G1 | 真实分区操作：新建 50 GB NTFS 分区，diskpart `exit=0`，日志含输出 | — | ✅ |
| G2 | 新分区出现在磁盘列表（5 个分区，含 50.00 GB） | — | ✅ |

- 演练模式：**16 / 16 通过**（7 例按模式跳过）
- 真实模式：**23 / 23 通过**（0 跳过）

### 4.2 界面冒烟（`tools/ui_smoke.mjs` → Edge，对 8895 演练实例）

```
ok=true, problems=[]
firstPaint: tree=13, rows=5, segments=3, chipDisks="2 块磁盘", fatal=""
step1: 2 张磁盘卡（1 张置灰）  step2: 1 张分区卡 / 2 个输入框
step3: 1 个镜像下拉           step4: 9 行计划表 + 开始按钮置灰（有阻断项时）
settings: 34 行（与后端 34 项一致）   boot: 修复引导弹窗 1 条 finding
截图: 10 张，54–113 KB，全部非空白
```

另外在真实模式实例（8897）上截取了实际桌面画面：标题栏 `硬盘工具箱 v1.0.1`、
Disk 1 显示 5 个分区（MSR / EFI 300 MB / G: Windows 120 GB / J: Data 300 GB / 50 GB）、
分区地图与图例（`共 5 个分区`）、未分配 1.36 TB，与 API 数据一致。

---

## 5. 关键证据

**安装任务 `disk1-87539e0b`（真实模式）**

```text
status=ok  percent=100/100  total=6  duration=3.127s
steps: efi=ok windows=ok data=ok image=ok boot=ok verify=ok
log:   D:\sisp-test\v101\logs\disk-runner-20261104-092706-8460.log（249 行）
tail:  [09:27:09] 已释放 EFI 盘符 F
       [09:27:09] 任务结束：success=True 用时 3.1s
```

**执行器日志目录（`logDir` 生效）**

```text
logs/app.log                                    含 "真实执行日志: …disk-runner…"，logDir 生效
logs/disk-runner-20261104-092706-8460.log       11,864 B，六步全过程
logs/partition-disk1-20261104-092716.log        517 B，含 diskpart 脚本与输出
D:\sisp-test\v101\tmp                           0 个残留文件（diskpart 脚本用完即删）
```

**最终分区状态（Disk 1）**

```text
PartitionNumber  Size(GB)  Type       DriveLetter
1                0.016     Reserved   -
2                0.293     System     -
3                120       Basic      G:
4                300       Basic      J:
5                50        Basic      -
```

**Disk 0（系统盘）**：4 个分区、C: `WIN10` 150 GB，未参与任何写盘，全程只读。

---

## 6. 观察与已知限制

### 属预期、不是缺陷

- `boot/report` 出现 `orphan_esp`（warn）：测试镜像是从 `C:\Windows\Boot` 捕的合成 WIM，
  盘上没有 `Windows` 目录，所以「ESP 存在但没找到 Windows」是正确判断；
- ESP/C/D 的 `file_system` 在列表里为 `null`：盘符已释放，`Get-Volume` 读不到卷，
  这是扫描器的既有口径（界面按 GPT 类型显示 EFI 并用紫色区分）；
- 首次运行会在 exe 旁生成 `json/win11.json`（内容 = `win11.example.json` 模板），
  其中镜像是占位路径 `D:\images\win11.wim`；v1.0.1 的行为是**设置优先**，所以填了
  `sysWim` 后会话不再报镜像阻断（本轮 B1 已验证）。

### 测试过程中遇到的问题（环境/脚本，非产品）

1. VM 上 `Invoke-WebRequest` 访问 `http://10.2.0.165:8970` 报「无法连接到远程服务器」，
   而 `Test-NetConnection` 同端口通——系统代理所致；改用 `System.Net.WebClient` 并
   `$wc.Proxy = $null` 后正常。
2. 测试脚本自身两处断言写错（第一轮 14/16）：
   - A3 把 HTML 当 JSON 解析 → 改为 `raw=True` 读取并按字符串断言；
   - A5 误以为 `PUT /api/settings` 回 `{settings:{…}}`，实际回的是扁平设置字典。
   修正后演练 16/16、真实 23/23。

### 本轮未覆盖

- **未签名**：SmartScreen 会提示「未知发布者」，本轮直接运行（未走下载拦截流程）；
- 未验证真实 Windows 装机后的可启动性（用的是合成 WIM）；
- 未测桌面窗口模式（pywebview），本轮全部通过 `--serve` + Edge；
- 未测多盘并发安装、安装中途取消、断网/断电等异常路径；
- 未做长时间稳定性（连续多次安装）与内存/句柄泄漏观察。

---

## 7. 结论与建议

- v1.0.1 打包产物的启动、静态资源、设置读写与导入导出、镜像探测、引导报告、
  分区计划/执行、整盘安装（含进度与日志）在真实硬件上全部符合预期；
- 写盘相关的安全边界仍然有效：受保护系统盘、ESP 保护、确认文本、白名单、
  强身份校验都在真实模式下验证过；
- 建议后续补：真实装机镜像的可启动性验证（用真 Windows ISO/WIM 装一台再引导）、
  多盘并发与取消中断、以及给产物签名。
