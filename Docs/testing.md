# 测试与真实写盘验证

本项目会清空目标硬盘。测试分三层，**任何真实写盘都只能在一次性虚拟磁盘或专用测试 VM 上进行**。

## 1. 本机/CI：逻辑与安全分支

```powershell
python -m pip install -r requirements-web.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

- 测试使用替身扫描器/执行器和注入依赖，不碰真实磁盘；
- SSE 测试会在后台线程启动真实 uvicorn（随机空闲端口），因此依赖 `uvicorn`；
- 覆盖重点：任务状态机、重复提交、取消、演练硬上限、盘符规划、设置校验与 BOM、CLI 输出、SSE 帧。
- 当前基线：170 passed（本机 Python 3.14；专用 VM 上 Python 3.15.2 同口径）。其中前端相关三组：
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
python -m app.cli --list
python -m app.cli --dry-run
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
python -m app.cli -j D:\sisp-test\config.json `
  --allow-disks <VHD 盘号> --test-mode `
  --log-dir D:\sisp-test\cli-logs
```

`--test-mode` 只允许虚拟盘（VHD 的 `Msft Virtual Disk` / `File Backed Virtual` 会被识别）。
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
- CLI/GUI 真实执行在 8GB VHDX 上六步全部成功，`bcdboot` 写入 EFI，`verify` 识别 GPT/分区；
- 失败路径：错误镜像索引（dism 87）→ image 失败、boot/verify 跳过、退出码 3；缺镜像 → 预检阻断、退出码 2；容量不足 → 计划阶段阻断、退出码 2；受保护系统盘 → 退出码 4；
- 安全闸门：真实身份通过；错误 UniqueId / 白名单外 / 服务 dry-run / 弱身份全部 `PermissionError`；受保护盘的计划会带阻断项，`confirm` 拒绝执行；
- `typeToConfirm` 走 REST 实测：`/api/confirm` 不带文本、空白文本均返回 `E_CONFIRM`；盘号不匹配（`99`）与型号不匹配（`GeIL`）也返回 `E_CONFIRM`；`" disk 1 "` 这种带空格写法被接受并真的跑完六步；
- `protectSystemDisk=false` 实测：`GET /api/disks` 中 Disk 0 变回可选、`prepare` 可生成 dry-run 计划；开启时 Disk 0 不可选且计划带「系统盘、启动盘、包含系统盘符 C:」阻断项。真实写盘未在系统盘上验证，也不应该验证；
- 设置接通实测：`sysTemp` 被用作 dism/diskpart 子进程 `TMP`/`TEMP`，diskpart 脚本文件也落在这个目录；预检多出「外部工具可用性检查」「设置路径有效性检查」两项（故意填错路径 → 退出码 2）；
- 日志：CLI 未给 `--log-dir` 时用设置里的 `logDir`；桌面进程在 `logDir` 下写 `app.log`（含真实执行日志路径）；`logRetentionDays` 启动清理同时删掉过期的 `disk-runner-*.log` 和 `app.log.*`（实测 6 → 4 个文件）；
- 窗口记忆：关窗后 `window.json` 写入 `{"width":1280,"height":860,"x":182,"y":182}`；预先写入 1024×700 @ (300,240) 后重启，窗口按该位置恢复；`rememberWindow=false` 时既不恢复也不写文件；
- 连续多次真实执行后盘符池稳定；EFI 盘符在任务结束后自动释放，无幽灵盘符。

### 界面重构验证（ui-rebuild 分支）

- 本机与专用 VM（Python 3.15.2）`pytest -q` 均 **170 passed**；
- `tools/ui_check.py` 通过：10 个 ES 模块语法正常、`/static` 引用全部落地、
  `derive.js` 在 node 里跑通（含 EFI 预留的容量校验）；
- VM 上没有 Chrome，`tools/ui_smoke.mjs` 自动改用 Edge 跑通：10 张截图都在 50 KB 以上，
  主页面 `tree=9`、设置页 31 行、第 4 步计划表 9 行、修复引导弹窗有 1 条 finding；
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
- CLI 无法指定执行日志目录；取消中的任务被执行器失败结果覆盖为 failed；
- 向导第 3 步换了镜像后，计划仍挂着会话级的「镜像文件不存在」阻断，永远无法开始；
- 演练只上报 4 个阶段，`diskpart_preview` 还会被算进进度（显示成 7/6）。
- `.fatal` 的 `display:flex` 盖掉 `[hidden]`，一块空遮罩把整个界面糊成纯背景色；
- 默认数据分区大小只按“总容量 − 系统分区”算，没给 EFI/对齐留余量，第 4 步一生成计划就容量不足。

## 4. 打包产物（exe）

`Docs/packaging.md` 记录构建方式；验证时按“新打出来的 exe 就是用户手里的东西”
对待，不只看源码：

```powershell
python packaging/build.py
.\dist\Sisp2-CLI.exe --list
.build\smoke.py 8890        # 先起 .\dist\Sisp2.exe --serve --port 8890 --dry-run
```

- 专用 VM 上用 Python 3.15.2 现打现测：`--list` / `--dry-run` / `--allow-disks 0`
  的退出码（0 / 2 / 4）与源码版一致；
- 打出来的 `Sisp2-CLI.exe --allow-disks 1` 在 8 GB 一次性 VHDX 上完成了六步真实写盘，
  日志证明 `sysTemp`、`logDir` 等设置经设置文件传入后照常生效；
- `Sisp2.exe --serve` 下 `/api/info` 报 `version=1.0.0`，桌面窗口能正常渲染；
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
2. 始终使用 `--allow-disks`（CLI）并在 GUI 中核对盘号/身份；
3. 生产环境优先 `--dry-run` / `dryRunByDefault`；
4. 不要在含重要数据的物理盘上运行任何真实执行测试。
