# 测试与真实写盘验证

本项目会清空目标硬盘。测试分三层，**任何真实写盘都只能在一次性虚拟磁盘或专用测试 VM 上进行**。

## 1. 本机/CI：逻辑与安全分支

```powershell
python -m pip install -r requirements-web.txt -r requirements-dev.txt
python -m ruff check .          # 静态检查（规则与豁免见 pyproject.toml）
python -m compileall -q app tests tools packaging
python -m pytest tests/ -q
```

- `ruff check` 目前是 **All checks passed**；规则集在 `pyproject.toml` 里显式声明，
  中文注释里的全角标点（`RUF001`/`RUF002`/`RUF003`）与长错误文案（`TRY003`）
  是有意豁免的；
- `ruff format --check` 尚未纳入门禁（存量格式差异较多），需要时单独跑，不要与
  `ruff check` 混为一谈；
- 测试使用替身扫描器/执行器和注入依赖，不碰真实磁盘；
- SSE 测试会在后台线程启动真实 uvicorn（随机空闲端口），因此依赖 `uvicorn`；
- 覆盖重点：任务状态机、重复提交、取消、演练硬上限、盘符规划、设置校验与 BOM、写盘闸门、SSE 帧。
- 当前基线：194 passed（专用 VM Windows 10 22H2 + Python 3.15.2 同口径）。其中前端相关三组：
  `tests/test_ui_assets.py`（资源引用、薄壳、设置页字段对账、CSS 与纯函数约定）、
  `tests/test_partition_boot_image.py`（分区/引导/镜像三个模块的纯逻辑）、
  `tests/test_web_api.py` 新增的静态托管、日志尾部、分区确认、会话模式等用例。

## 1.1 前端：静态检查与浏览器冒烟

前端没有构建步骤，所以检查也分两层：

```powershell
python tools/ui_check.py                                               # node --check + 引用 + derive.js 行为
node tools/ui_smoke.mjs --url http://127.0.0.1:8765/ --out build/shots  # headless Chrome 走一遍
```

- `tools/ui_check.py`：每个 `ui/js/*.js` 过 `node --check`；`index.html` 里的 `/static/...`
  引用必须存在；相对 `import` 必须能解析；把 `derive.js` 复制成 `.mjs` 在 node 里真实跑
  一遍，断言容量格式化、分区切片、盘符建议、大小校验（含 EFI 预留）。
  `tests/test_ui_assets.py::test_frontend_static_check` 会代调它。
- `tools/ui_smoke.mjs`：CDP 直连 headless Chrome（不依赖 Playwright），依次走主页面、
  四步向导、设置页、操作菜单和修复引导弹窗，收集 `Runtime.exceptionThrown` /
  `Log.entryAdded`，截图 10 张，并对 `.dsel`/`.tgt`/`select[data-image]`/计划行数/
  `#startBtn`/`.set-row` 做计数断言；**截图小于 20 KB 视为空白直接判失败**（曾经因为
  `.fatal` 的 `display:flex` 盖掉了 `[hidden]`，整块遮罩把界面糊成纯背景色，十张截图
  全是空白而且脚本还“通过”了）。
- 两项都需要 PATH 里有 `node`；没有时 `ui_check.py` 打印跳过并以 0 退出，
  `test_frontend_static_check` 用 `skipif` 跳过。

## 2. 专用 VM：只读与 dry-run

推荐在专用 Windows VM 上验证真实 PowerShell 扫描与 GUI：

```powershell
python -m app.gui_web.main --serve --dry-run --port 8765
```

检查项：
- 磁盘列表、系统盘/启动盘保护、盘符分配、缺镜像/容量不足的阻断；
- `GET /health`、`/api/info`、`/api/disks`、`/api/prepare`；
- `--dry-run`（或 `dryRunByDefault`）会话下，API 传 `dry_run:false` 仍应保持演练；
- 重复 `submit` 返回 `E_SUBMIT`。

## 3. 一次性 VHDX：真实写盘（推荐）

不碰系统盘和物理盘，测完直接 detach/删除。需要管理员权限。

### 3.1 创建并挂载测试盘

```powershell
New-Item -ItemType Directory -Force D:\sisp-test | Out-Null
@'
create vdisk file="D:\sisp-test\sisp-test.vhdx" maximum=8192 type=expandable
select vdisk file="D:\sisp-test\sisp-test.vhdx"
attach vdisk
exit
'@ | Set-Content $env:TEMP\vhd.txt -Encoding ASCII
diskpart /s $env:TEMP\vhd.txt
Get-Disk   # 找到新出现的 "Msft Virtual Disk"（RAW、File Backed Virtual）
```

### 3.2 准备一个合法测试 WIM

真实执行器要求镜像存在且能被 dism 应用。没有 install.wim 时，可以用
`dism /Capture-Image` 从一个小目录生成。若还想验证 `bcdboot`，源目录至少需要：

- `Windows\Boot`（从 `C:\Windows\Boot` 复制）
- `Windows\System32\bootstr.dll`
- `Windows\System32\bcd.dll`
- `Windows\System32\config\BCD-Template`
- `Windows\System32\zh-CN\bootstr.dll.mui`（按系统语言选择 MUI）

```powershell
# 组装 D:\sisp-test\src 后
dism /English /Capture-Image /ImageFile:D:\sisp-test\test.wim `
  /CaptureDir:D:\sisp-test\src /Name:SispTest /Compress:fast
```

> 这个 WIM 只用于验证命令链路，不是可启动的 Windows 镜像；真正验证装机可启动性需要官方 install.wim。

### 3.3 配置与执行

```json
{
  "partition_info": { "efi_size_mb": 100, "c_size_gb": 3, "data_size_gb": "auto" },
  "image_info": { "image_path": "D:\\sisp-test\\test.wim", "image_index": 1 },
  "excluded_disk_names": []
}
```

```powershell
python -m app.gui_web.main -j D:\sisp-test\config.json --serve --port 8765
```

在界面里走四步向导（勾选虚拟盘 → 分区大小 → 选镜像与版本 → 生成计划并确认）。
命令行入口已移除：真实执行的授权来自“界面勾选 + 输入确认文本”，
护栏来自设置里的 `typeToConfirm` / `protectSystemDisk`。（历史记录里的 `--test-mode` 只允许
虚拟盘，该开关随命令行入口一起移除。）
预期六步全部成功：EFI / Windows / Data 分区 → dism 应用 → bcdboot → verify。

GUI 真实路径（需要管理员）：

```powershell
python -m app.gui_web.main --serve -j D:\sisp-test\config.json `
  --settings D:\sisp-test\settings.json
# 然后 POST /api/prepare -> /api/submit -> /api/confirm
```

`typeToConfirm=true`（默认）时确认请求要带 `confirm_text`，值为盘号或磁盘型号：

```json
{"job_ids": ["disk1-xxxx"], "confirm_text": "1"}
```

### 3.4 清理

```powershell
@'
select vdisk file="D:\sisp-test\sisp-test.vhdx"
detach vdisk
exit
'@ | Set-Content $env:TEMP\detach.txt -Encoding ASCII
diskpart /s $env:TEMP\detach.txt
```

## 已通过的真实 VM 验证（2026-11）

- Windows 10 22H2 + Python 3.15.2：`pytest tests/ -q` → 106 passed；
- 真实执行在 8GB VHDX 上六步全部成功，`bcdboot` 写入 EFI，`verify` 识别 GPT/分区；
- 失败路径：错误镜像索引（dism 87）→ image 失败、boot/verify 跳过、退出码 3；缺镜像 → 预检阻断、退出码 2；容量不足 → 计划阶段阻断、退出码 2；受保护系统盘 → 退出码 4；
- 安全闸门：真实身份通过；错误 UniqueId / 白名单外 / 服务 dry-run / 弱身份全部 `PermissionError`；受保护盘的计划会带阻断项，`confirm` 拒绝执行；
- `typeToConfirm` 走 REST 实测：`/api/confirm` 不带文本、空白文本均返回 `E_CONFIRM`；盘号不匹配（`99`）与型号不匹配（`GeIL`）也返回 `E_CONFIRM`；`" disk 1 "` 这种带空格写法被接受并真的跑完六步；
- `protectSystemDisk=false` 实测：`GET /api/disks` 中 Disk 0 变回可选、`prepare` 可生成 dry-run 计划；开启时 Disk 0 不可选且计划带「系统盘、启动盘、包含系统盘符 C:」阻断项。真实写盘未在系统盘上验证，也不应该验证；
- 设置接通实测：`sysTemp` 被用作 dism/diskpart 子进程 `TMP`/`TEMP`，diskpart 脚本文件也落在这个目录；预检多出「外部工具可用性检查」「设置路径有效性检查」两项（故意填错路径 → 退出码 2）；
- 日志：执行日志目录取设置里的 `logDir`；桌面进程还会在 `logDir` 下写 `app.log`（含真实执行日志路径）；`logRetentionDays` 启动清理同时删掉过期的 `disk-runner-*.log` 和 `app.log.*`（实测 6 → 4 个文件）；
- 窗口记忆：关窗后 `window.json` 写入 `{"width":1280,"height":860,"x":182,"y":182}`；预先写入 1024×700 @ (300,240) 后重启，窗口按该位置恢复；`rememberWindow=false` 时既不恢复也不写文件；
- 连续多次真实执行后盘符池稳定；EFI 盘符在任务结束后自动释放，无幽灵盘符。

### 界面重构验证（ui-rebuild 分支）

- 当前 `pytest -q` 为 **189 passed**；
- `tools/ui_check.py` 通过：10 个 ES 模块语法正常、`/static` 引用全部落地、
  `derive.js` 在 node 里跑通（含 EFI 预留的容量校验）；
- VM 上没有 Chrome，`tools/ui_smoke.mjs` 自动改用 Edge 跑通：10 张截图都在 50 KB 以上，
  主页面 `tree=9`、设置页 34 行、第 4 步计划表 9 行、修复引导弹窗有 1 条 finding；
- 新增接口在 VM 上用真实数据验证：`/api/images` 读出 `test.wim` 的索引 1（SispTest）、
  `/api/boot/report` 给出 `boot_ok` + ESP 分区 1、`/api/partition/plan` 在“未分配空间为 0”
  时回明确中文原因、格式化演练只生成 5 行 diskpart 脚本、
  `/api/partition/execute` 不带确认文本时 `E_CONFIRM`、盘号不存在时 `E_PARTITION`、
  `/api/jobs/{id}/log` 在没配 `logDir` 时回 `message` 而不是报错；
- **真实写盘走了新代码路径**：新建 8 GB 动态 VHDX → Disk 2，`prepare(dry_run=false)`
  给出 `executable=true`，`confirm(confirm_text="2")` 后六步全绿，
  `progress` 为 `6/6 = 100%`、`result.steps` 六步 `ok`，分区为
  MSR 15.98 MB / ESP 300 MB / C 4 GB NTFS / D 3 GB NTFS（GPT）；跑完 detach 并删除 VHDX，
  Disk 0/1 的分区与卷（C 150 GB、D/E 390.7 GB、F 1863 GB）完全没变；
- 演练流程（`--serve --dry-run` + 2 TB 数据盘，完整走 prepare→submit→confirm）：`completed` 恰为六个阶段，
  演练附加的 `diskpart_preview` 只发事件、不计入进度。

### 由此修复的问题

- 设置文件 UTF-8 BOM 导致整份设置被忽略（`logDir` 看似保存却不生效）；
- EFI 隐藏卷盘符在 `clean` 后残留，下一次 `assign letter` 失败（现已释放 + 规划阶段避开）；
- 取消中的任务被执行器失败结果覆盖为 failed；
- 向导第 3 步换了镜像后，计划仍挂着会话级的「镜像文件不存在」阻断，永远无法开始；
- 演练只上报 4 个阶段，`diskpart_preview` 还会被算进进度（显示成 7/6）。
- `.fatal` 的 `display:flex` 盖掉 `[hidden]`，一块空遮罩把整个界面糊成纯背景色；
- 默认数据分区大小只按“总容量 − 系统分区”算，没给 EFI/对齐留余量，第 4 步一生成计划就容量不足。

### 打包 exe 专项验证（2026-11，2 TB 测试盘）

测试机装了专用于测试的 2 TB NVMe（Disk 1），本轮的**真实写盘全部打在这块盘上**，
系统盘（Disk 0）只做只读扫描。

- 从 `main` 现打现测（Python 3.15.2 / PyInstaller 6.22.3）：`Sisp2.exe` 18.1 MB（当时还带 `Sisp2-CLI.exe`，现已移除）；
- `Sisp2.exe --serve`（真机 + 设置文件）：`/api/info` 报 `1.0.0 / 3.15.2 / dry_run=False`，
  `/static/js/main.js`、`/static/css/app.css` 均 200，设置 34 项、6 个分组、12 个路径键；
- 浏览器冒烟（`tools/ui_smoke.mjs` → Edge）：`ok=true`，10 张截图 53–105 KB，
  磁盘树按分区数增长、设置页 34 行、第 4 步计划表 9 行（真实模式显示“真实写盘”）；
- **真实整盘安装**（走 REST：prepare → submit → confirm，盘号 1、C 120 GB、D 300 GB、
  镜像用 `D:\sisp-test\test.wim` 索引 1）：六步全绿、`progress` 6/6=100%、
  `result.steps` 六步 `ok`，完成后分区为 MSR 15.98 MB / ESP 300 MB / C 120 GB NTFS
  「Windows」/ D 300 GB NTFS「Data」，GPT；
- 执行日志：`logDir` 下生成 `disk-runner-*.log`（11.8 KB），`/api/jobs/{id}/log?tail=`
  能取到尾部（含「已释放 EFI 盘符 G」「任务结束：success=True」），`app.log` 记录路径；
- **真实分区操作**（新增的 partition 模块）：`/api/partition/plan` 只出 5 行脚本、
  `dry_run=true`；`/api/partition/execute` 真实创建 50 GB NTFS 分区并格式化成功，
  日志含 diskpart 输出；`/api/partition/plan` 对 ESP 一律拒绝（“分区 2 是 EFI 系统分区”）；
- 修复引导：`/api/boot/report` 给出 `boot_ok` + `orphan_esp`（合成镜像没有 `Windows` 目录，
  属预期）；`/api/boot/repair` 对没有 Windows 目录的盘返回明确原因，且不改动任何分区。

### 只有打包后才暴露的问题（已修）

在源码模式下看不到的差异，靠 exe + 真机才浮出来：

- **卷标乱码**：PowerShell 5.1 的重定向输出按 cp936 写字节，而「系统」的 cp936 字节
  `CF B5 CD B3` 恰好是合法 UTF-8 → 界面里显示成 `ϵͳ`。修法是在脚本前挂
  `[Console]::OutputEncoding = UTF8`，不是事后猜编码；
- **真实分区/修复引导被判“缺少 UniqueId”**：界面只传盘号，闸门却要求期望身份里有强标识；
  现在没传身份时只读补扫一次；
- **真实分区/修复引导被判“不在白名单”**：`prepare()` 才会授权，单盘操作不经过它；
  新增 `WorkflowController.authorize_disks()`（界面确认即授权，显式白名单与演练模式不动）；
- **diskpart 脚本残留在 temp 目录**：`mkstemp` 的 fd 没关，Windows 上 `unlink` 因占用失败；
- **分区日志没有 diskpart 输出**：攒行日志在写完最后一行后没再 flush。

## 3.1 VM 实测记录（2026-11-03，界面设置接管业务配置之后）

测试机：`DESKTOP-FODEEKK`，Windows 10 22H2（19045），Python 3.15.2，node v26.13.0。
代码版本 `1ad74ab`。

| 检查 | 结果 |
| --- | --- |
| `python -m ruff check .` | All checks passed |
| `python -m compileall -q app tests tools packaging` | ok |
| `python tools/ui_check.py` | 10 个模块通过 |
| `python -m pytest tests/ -q` | **197 passed**（6.2s） |
| 浏览器冒烟（dry-run，3 块盘） | `ok=true`，10 张截图，树 10 节点，设置页 34 行 |
| 浏览器冒烟（真实模式，写盘后） | `ok=true`，树 14 节点（4 个分区），计划表 9 行，无阻断 |
| **真实整盘安装**（200 GB 可扩展 VHDX，走 REST） | **六步全绿**：EFI 100 MB(E:) → Windows 120 GB(H:) → 数据 70 GB(I:) → dism 应用 test.wim → bcdboot → 校验 GPT/4 分区 |

### 这台机器上的两个坑

- **40 GB 的系统副盘不够用**：默认 C=120 GB，`sysDefaultGb` 的下限是 60 GB，
  所以不到 ~81 GB 的盘永远过不了体积校验（第 2 步「下一步」直接禁用）。测整盘安装
  请用 ≥200 GB 的可扩展 VHDX（`create vdisk maximum=204800 type=expandable`，实际占用很小）。
- **合成 WIM 的 `Windows\System32` 为空时，bcdboot 一定失败**
  （`BFSVC Error: BCD strings MUI load failure ... bootstr.dll`）。这是 §3.2 那条
  “源目录至少需要”的清单没抄全时的表现：按清单补上 `Boot`、`System32ootstr.dll`、
  `bcd.dll`、`config\BCD-Template`、`zh-CNootstr.dll.mui` 重新 capture 后，
  `test-full.wim`（67 MB 源，30 MB WIM）跑出六步全绿，ESP 里有 1.5 MB 的
  `EFI\Microsoft\Bootootmgfw.efi`；
- 临时绕法：把设置里的「引导文件目录」`sysBoot` 指向真实 Windows 目录（`C:\Windows`）
  也能让 boot 步通过（当时就是靠它把失败步跑成绿的），但那是兜底路径，不是常规用法。

### 由此修掉的两个产品缺陷

1. `_apply_image_defaults` 只在镜像路径为空时兜底 → 模板里的占位路径
   （`json/win11.example.json` 的 `D:\images\win11.wim`）压住了设置里的 `sysWim`，
   用户在设置页填了真镜像仍然一直看到「镜像文件不存在」。现在无 `-j` 时设置覆盖配置。
2. 预检自己读配置算镜像，绕过了上面那条覆盖规则 → 会话仍然带阻断项。
   `preflight` 改为与运行时同一套优先级（显式 `-j` 才配置优先）。

## 4. 打包产物（exe）

`Docs/packaging.md` 记录构建方式；验证时按“新打出来的 exe 就是用户手里的东西”
对待，不只看源码：

```powershell
python packaging/build.py
.\build\smoke.py 8890        # 先起 .\dist\Sisp2.exe --serve --port 8890 --dry-run
node tools/ui_smoke.mjs --url http://127.0.0.1:8890/ --out build/shots-exe   # 打包后的界面冒烟
```

- 早期版本验证过命令行产物（退出码 0/2/4 与源码一致），该入口已删除，现阶段只验证 GUI 产物；
- 打出来的 exe 在 8 GB 一次性 VHDX 上通过界面完成六步真实写盘，
  日志证明 `sysTemp`、`logDir` 等设置经设置文件传入后照常生效；
- `Sisp2.exe --serve` 下 `/api/info` 报 `version=1.0.0`，桌面窗口能正常渲染；
- 界面拆成 ES 模块后，从源码重打并把 `tools/ui_smoke.mjs` 指向 `Sisp2.exe --serve`：
  `ok=true`、10 张截图全部 50 KB 以上、磁盘树 / 设置页 / 四步向导都能点（说明
  `ui/css`、`ui/js` 确实进了包）；
- 冻结后的路径解析由 `tests/test_resources.py` 覆盖（源码 / onefile / onedir、
  exe 旁配置优先、复制失败回退），版本号与 spec 一致性由 `tests/test_packaging.py`
  覆盖。

## 已知限制

- 上述 VHD 测试使用的是最小合成 WIM，未验证真实 Windows 装机的可启动性；
- 未在物理盘上测试，也不应在有数据的盘上测试；
- 协作式取消只能在步骤边界停止；取消中的真实 dism 不会被打断；
- 运行中强杀进程可能留下盘符/分区中间状态（依赖下次规划避开幽灵盘符）。

## 安全红线

1. 真实写盘前确认目标是**一次性虚拟盘**或专用测试机磁盘；
2. 真实执行前在界面上核对盘号/型号并输入确认文本，并确认目标盘不在禁止名单里；
3. 生产环境优先 `--dry-run` / `dryRunByDefault`；
4. 不要在含重要数据的物理盘上运行任何真实执行测试。
