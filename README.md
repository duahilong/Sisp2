# Sisp2

Sisp2 是面向 Windows 的硬盘装机工具，可规划并执行清盘、GPT EFI/Windows/Data 分区、用 Ghost 还原 GHO 镜像、写入 UEFI 引导及分区结果校验。真实执行会破坏目标盘全部数据；在隔离环境外使用前必须逐项核对目标盘。

## 环境与安装

要求 Windows 与 Python 3.10+。磁盘逻辑只用标准库和系统工具；界面另需 FastAPI、uvicorn、pywebview（仅桌面窗口需要 pywebview）。

桌面窗口还需要 **Microsoft Edge WebView2 运行时（主版本 ≥ 86）** 与 **.NET Framework 4.6.2+**：界面是 ES 模块，缺这两样时 pywebview 会退回 IE 内核，界面一行脚本都跑不起来。缺的时候程序现在会直接弹框说明原因并退出（不再给一个点不动的窗口）；老 Win10 请先装 WebView2，或用 `--serve` 配 Edge/Chrome。详见 `Docs/打包与分发.md`。

```powershell
python -m pip install -r requirements.txt
# 可选：GUI 服务及桌面窗口
python -m pip install -r requirements-web.txt
```

## 启动

```powershell
# 桌面窗口（双击/默认）
python -m app.gui_web.main
# 浏览器模式
python -m app.gui_web.main --serve
```

扫盘、装机向导、修复引导、镜像文件头校验全部在界面里完成，命令行已经不是必需入口（`app.cli` 已移除）。**分区只在装机向导里做**（整盘重建 EFI + 系统 + 数据分区）：单盘级的新建/格式化/删分区属于破坏性操作，请用 Windows 磁盘管理（`diskmgmt.msc`）——历史上那套单盘分区接口（`/api/partition/*`）已于 2026-11 移除，只保留只读的 `POST /api/partition/preview`。启动即真实模式：**确认后会把选中的磁盘重新分区**。要看“会执行什么”，用只读的计划与脚本预览——向导第 4 步、`POST /api/prepare`、`POST /api/partition/preview` 都不执行命令；真正写盘只发生在 `POST /api/confirm` 之后。启动 GUI/服务不等同于安全操作，请先备份并核对盘号与身份。

**只允许开一个实例**：第二次启动会先把已打开的窗口叫到前台，然后以退出码 6 退出
（`--serve` 只打一行提示；排障/自动化测试可加 `--allow-multiple`）。
GUI 默认监听 `127.0.0.1:8765`；可用 `--host`、`--port` 改变监听参数。接口文档默认在 `/docs`。非 loopback 监听没有鉴权，属于高风险配置，详见[安全边界](Docs/安全边界.md)及[Web API](Docs/Web接口与部署.md)。

## 打包成 exe

不想装 Python 的话，可以直接打成单文件 exe（免安装、双击即用）：

```powershell
python -m pip install -r requirements-build.txt
python packaging/build.py
# → dist/Sisp2.exe（图形界面，约 19 MB）
```

exe 的参数与源码运行一致（`--serve` / `--settings` 等）；设置保存在 `%APPDATA%\Sisp`，没有额外的配置文件要维护。细节（提权、日志位置、体积、未签名等限制）见[打包说明](Docs/打包与分发.md)。

## 配置

**全部配置都在界面「设置」里**：EFI/系统/数据分区大小、默认镜像位置与版本、外部工具路径（Ghost / bcdboot）、安全开关。数据分区的文件系统固定为 NTFS（不可选）。

设置默认保存于 `%APPDATA%\Sisp\settings.json`（无 `APPDATA` 时为用户目录下 `.config/Sisp/settings.json`）；GUI 可用 `--settings` 指定位置。

> 旧版本的「业务 JSON 配置文件」（`json/win11.json`、`-j/--config`、`SISP_CONFIG`）已整条移除。同一个参数以前有两处来源、优先级还取决于是否显式传了 `-j`，实测出现过「界面里看着 60 GB、实际按 30 GB 拒绝」的困惑。现在只有一处来源：设置页。每个字段的真实行为与生效时机见[配置说明](Docs/配置说明.md)。

## 当前安全行为与限制

- `SispService` 没有演练开关：不是 `--serve` 的只读模式，也没有“只看不写”的降级；执行层在拿不到真实执行器时直接拒绝，绝不静默跳过。
- 真实模式下，工作流会把本次在界面上勾选并确认过的硬盘加入服务白名单（没有独立的白名单文件）；第 4 步的勾选「我明白这些分区会被删除」就是授权动作。
- 写盘要过三道闸门：白名单、执行前重扫比对身份（有 UniqueId/SerialNumber 比标识，没有比盘号+容量+型号）、执行瞬间仍可选；受保护硬盘不能操作。
- 第 4 步的勾选「我明白这些分区会被删除」是唯一的人工授权动作：勾上并点「开始安装」即提交，不再弹第二次确认框（`confirmBeforeStart` 已删除）。
- Web 默认 loopback。Host 校验仅校验 Host 名称、不是身份认证；改为非 loopback 监听时没有鉴权，网络可达的写盘接口具有高风险。
- SSE 重放依赖进程内有限缓冲；队列满会丢弃新事件，缓冲淘汰也会使旧事件无法补发，不能保证完整事件历史。
- 预检只在设置里至少填了一个系统槽位（`sysImageA` / `sysImageB`）时检查镜像文件是否存在与文件头；一个都没填时预检不报，真实执行器仍会拒绝缺少镜像路径。

详见[安全边界](Docs/安全边界.md)。

## 出问题怎么排查（日志与导出）

每次任务都会在设置里的 `logDir`（默认 `%APPDATA%\Sisp\logs`）写一份
`disk-runner-d<N>-<型号>-<时间>-<进程>-<随机>.log`。日志是给人和机器都能读的：

- **抬头**写清是哪块盘（`===== KINGSTON SNV3S1000G（磁盘 3） =====` + 型号/容量/序列号/唯一标识/盘符）；
- 中间按 `① 硬盘分区` / `② 写入系统` / `③ 收尾` 分段，每步一行 `步骤 ✓/✗/-`；
- 只读查询（PowerShell 脚本几十行）只记一行说明，不刷屏；
- **结尾有结论块**（成功/失败、用时、失败步骤、下一步），失败原因用型号说话，而不是只有 `disk1`。

**安装页底部有「⤓ 导出日志」**（每块盘的日志弹窗里也有）：它把这一轮任务打成一个 zip，
弹原生保存框（默认落桌面），内容：

```
Sisp2-日志-20261110-0923.zip
├── README.txt      这个包是什么、先看哪个、怎么回传
├── summary.txt     人读结论表：型号/容量/每步结果/失败步骤原文/诊断/分区计划
├── summary.json    机器读版本（贴 issue 也行）
├── settings.json   当时的设置快照（只有本机路径）
├── logs/           原始执行日志
└── artifacts/      ghosterr-*.txt 之类（有才带）
```

遇到失败直接把这个 zip 发出来即可；`summary.txt` 末尾还附了排查顺序（关掉该盘的
资源管理器窗口与杀软实时扫描、换数据线/直连口、看 SMART C7 是否在涨）。

## 测试

```powershell
python -m pip install -r requirements-web.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

测试主要依赖替身与注入实现，目标是验证逻辑而非真实设备写盘；`app/safety_guard.py` 会在
执行前拦下 diskpart/format/bcdboot/ghost64 -clone/写盘 cmdlet 等危险命令（`ghost64 -ver`、`dism /Get-*` 这类只读查询除外，详见[测试指南](Docs/测试指南.md)）。Web API 测试需要 `httpx`；SSE 测试会在后台线程启动真实 uvicorn（随机空闲端口），因此需要 `uvicorn`。不要在真实磁盘环境中尝试写盘测试。

仓库的 `.github/workflows/windows-tests.yml` 会在 GitHub 的 Windows runner 上对 Python 3.10 / 3.12
运行上述非破坏性测试、ruff 与 `compileall`。main 推送且两个版本全部通过后，会继续用 Python 3.12
构建 `Sisp2-beta.exe`、生成 SHA256，并更新同一个 `beta` 滚动预发布 Release；失败时不会替换旧产物。
完整版本号为 ``app/version.py`` 的基础版本加 GitHub 运行编号，例如 `1.5.0-beta.8`；
同一个值会写入关于页面、exe 的 FileVersion/ProductVersion 和 Release 标题。
PR 与手动触发只检查，不发布。连续推送只让最新提交发布，避免旧构建覆盖新构建。
**云端全绿只证明替身覆盖的逻辑与兼容性，不能替代隔离 VM 上的真实清盘、多盘串行和写盘验收。**

## 设计原型

[`design/`](design/) 目录包含 `disk-tools-app.html` 与 `disk-tools-prototype.html` 两个可点击 HTML 界面原型，用于设计参考；原型不连接本项目后端，也不执行磁盘操作。正式运行界面位于 `app/gui_web/ui/index.html`。

## 版本与定版

当前版本：**v1.5.0**（1.2.0 起移除演练模式，启动即真实写盘；预览改用只读的
`prepare` + `/api/partition/preview`）。1.3.0 加了「安装页一键导出日志」与
「日志用型号说话」；1.4.0 把「建分区」与「格式化」拆成两段并加了
`format.com /X` 兜底；1.4.1 让 `format` 一律带 diskpart 的 `override`
（强制卸载卷），并修好「ESP 没有可见盘符导致兜底永远被拒」的死结；
1.5.0 加了**单实例**（一台电脑只许开一个，第二次双击以退出码 6 结束）并把
启动改成「窗口先出来、会话后台热身 + 首屏只扫一次盘」。
功能语义以 tag 上的代码与文档为准；变更历史见 [CHANGELOG.md](CHANGELOG.md)。

- 版本号只有一处来源：`app/version.py` 的 `__version__`，`packaging/version_info.txt`
  与它由 `tests/test_packaging.py` 强校验（界面标题、`/api/info`、exe 的 FileVersion 都用它）。
- 定版就是一个**注释 tag**：tag 指向的提交树 = 那一版的完整快照（源码 + 文档），
  所以「回到某一版」不需要额外备份：`git checkout v1.1.0`，或 `git archive v1.1.0 -o sisp-1.1.0.zip`（1.2.1 同理）。
- 打新版（例：`1.1.1` 只修 bug）：改 `app/version.py` 与 `packaging/version_info.txt` →
  在 VM 上跑 `ruff` / `pytest` / `tools/ui_check.py` / `ui_smoke` → 本地 `python packaging/build.py` →
  提交 → `git tag -a v1.1.1 -m "..."` → `git push origin main --tags`。
- 发布产物不入库（`.gitignore` 跳过 `dist/`）。每次 main 推送通过全部检查后，自动把
  `Sisp2-beta.exe` 与 `Sisp2-beta.exe.sha256` 上传到 `beta` 滚动预发布，并将 `beta` tag 移到对应提交；
  固定下载地址为 `https://github.com/duahilong/Sisp2/releases/download/beta/Sisp2-beta.exe`。
  同一提交也在 Actions Artifacts 保留 7 天。正式稳定版仍使用 `v*` 注释 tag 单独发布。

## 文档索引

共 9 篇：**6 篇主文档**（架构、配置、Web/部署、安全、测试、打包）、1 篇已归档的改造实施记录、
1 份事故报告、1 份历史测试快照。`Docs/` 下的文件名全部是中文；旧英文名（`architecture.md` /
`security.md` / `web-api.md` / `testing.md` / `packaging.md` / `configuration.md`）已不存。
快照类文档只作对照，**当前行为以代码和主文档为准**。

| 文档 | 内容 |
| --- | --- |
| [架构与工作流](Docs/架构与工作流.md) | 分层、一次工作流、分区/MSR/盘符/验收、确认与执行的边界 |
| [配置说明](Docs/配置说明.md) | 唯一的设置来源、每个字段的真实行为与生效时机 |
| [Web接口与部署](Docs/Web接口与部署.md) | 启动与部署、路由、Host 校验、SSE、前端目录与开发约定 |
| [安全边界](Docs/安全边界.md) | 写盘门禁、Web 暴露面、事件与日志、预检边界 |
| [测试指南](Docs/测试指南.md) | 三层测试、护栏规则、VM 真实写盘流程、历史测试报告（附录 A） |
| [多盘并发改造实施文档](Docs/多盘并发改造实施文档.md) | 🗄 已归档：多盘流水线（分区串行 + 镜像交错）的方案、改动清单、测试与验收记录；当前行为以[架构与工作流](Docs/架构与工作流.md)为准 |
| [打包与分发](Docs/打包与分发.md) | 构建、产物、提权链、运行方式、启动耗时、已知限制 |
| [事故报告-2026-11-06-磁盘被格式化](Docs/事故报告-2026-11-06-磁盘被格式化.md) | 一次测试真实清盘事故的完整复盘、护栏修复、测试风险审计 |
| [历史测试记录](Docs/历史测试记录.md) | 各时期 VM/产物实测快照（含 v1.0.1 打包报告），仅作对照 |
