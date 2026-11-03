# 打包成 exe

把 Sisp2 打包成**单文件、免安装**的 Windows 程序。产物不写注册表、不需要
Python 环境，双击即用。

## 产物

| 文件 | 说明 | 实测体积 |
| --- | --- | --- |
| `dist/Sisp2.exe` | 图形界面（无控制台窗口），双击打开「硬盘工具箱」 | 17.8 MB（Python 3.14.6）/ 18.1 MB（Python 3.15.2） |

命令行产物 `Sisp2-CLI.exe` 已随 `app/cli.py` 一起移除；扫盘、装机、分区、修复引导、
镜像索引都在界面里完成。

产物是 onefile：启动时会把自己解到 `%TEMP%` 下的临时目录再运行，
所以**首次启动约 1–3 秒**（杀毒软件首次扫描时更久），属正常现象。

## 构建

```powershell
python -m pip install -r requirements-build.txt
python packaging/build.py
```

- `python packaging/build.py --no-clean`：跳过 PyInstaller `--clean`，快一些；
- `python packaging/build.py --clean-only`：只清理 `dist/` 与 `build/pyinstaller/`；
- 图标缺失时会自动调用 `packaging/make_icon.py` 生成 `packaging/sisp2.ico`；
- 版本号只有 `app/version.py` 一个来源，`packaging/version_info.txt` 必须与之
  一致，`tests/test_packaging.py` 会检查。

构建必须**在 Windows 上、用目标 Python 版本**执行（onefile 里带的是当前解释器
的 `python3xx.dll`）。本地实测：Python 3.14.6 + PyInstaller 6.22.2。

## 运行时的文件位置

打包后 `__file__` 不再指向源码目录，路径统一由 `app/resources.py` 解析：

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 界面 `ui/index.html` + `ui/css` + `ui/js`、默认配置模板 | exe 内部资源 | 只读，随包发布（spec 的 `DATAS` 必须整目录带上，否则 exe 只剩空壳） |
| 业务配置 `json/win11.json` | **exe 同目录** | 首次运行自动从模板复制一份，之后可随意编辑 |
| 设置 `settings.json` | `%APPDATA%\Sisp\` | 与源码运行完全一致，exe 升级不会丢；设置页可导出/导入换机 |
| 窗口位置 `window.json` | `%APPDATA%\Sisp\` | 受 `rememberWindow` 控制 |
| 执行日志、`app.log` | 设置里的 `logDir` | 无控制台窗口时，这里是唯一的诊断出口 |
| 解包目录 | `%TEMP%\_MEIxxxx` | onefile 临时目录，退出后自动清理 |

如果 exe 安装在 `C:\Program Files\` 之类没有写权限的目录，复制配置会失败，
程序改用包内模板继续运行（只读）；这种情况下用 `-j` 指定别处的配置即可。

## 提权

程序不会自动要管理员权限：

- 判断「有没有管理员」是 `app/gui/elevation.py` 的 `IsUserAnAdmin()`；
- 需要提权时用 `--elevate`：`Sisp2.exe --elevate`，
  内部走 `ShellExecuteW(..., "runas", ...)`，冻结后重启的是 exe 自身；
- 不提权也能跑：预检会把「管理员权限检查」标成失败，写盘会失败，dry-run 照常。

## 运行方式

```powershell
.\Sisp2.exe                       # 桌面窗口（默认）
.\Sisp2.exe --dry-run             # 强制演练，永不写盘
.\Sisp2.exe --serve --port 8765   # 浏览器模式，自己开浏览器访问 127.0.0.1:8765
.\Sisp2.exe --settings D:\cfg\settings.json   # 指定设置文件
```

首启会在 exe 同目录生成可编辑的 `json/win11.json`（模板 `json/win11.example.json`），
但**没有这个文件也能正常用**：分区大小、默认镜像、禁止操作的硬盘型号都在界面设置里。

## 验证记录

产物在两台机器上都验过：开发机（Windows 11 / Python 3.14.6 / PyInstaller 6.22.2）
和专用测试机（Windows 10 22H2 / Python 3.15.2 / PyInstaller 6.22.3，从仓库源码
现打）。

**真实写盘（测试机，8 GB 虚拟盘，测完即卸）**

通过界面「系统安装」四步向导（勾选磁盘 → 分区大小 → 选镜像与版本 → 生成计划并确认）
在同一块虚拟盘上跑完，等价于旧的命令行真实执行。

六步全绿：创建 EFI 100 MB（F:）→ Windows 3 GB（G:）→ 数据分区（H:）→
应用 `test.wim` → 写 UEFI 引导 → 校验分区（#2=100 MB System、#3(G:)=3.00 GB、
#4(H:)=4.89 GB）。执行日志落在设置指定的 `D:\sisp-test\gui-logs\`，日志里能看到
`diskpart.exe /s D:\sisp-test\tmp\sisp-diskpart-*.txt`——说明 `sysTemp` 这类设置
在打包版里同样生效；跑完 `tmp` 目录为空，EFI 盘符已释放，固件启动项未新增。

**图形界面**

| 检查 | 结果 |
| --- | --- |
| `Sisp2.exe --serve --port 8890 --dry-run` | `/health` 200、`/api/info` 版本 1.0.0、会话 8 项预检、设置 34 个键、系统盘按保护规则不可选 |
| `Sisp2.exe` 桌面窗口（开发机） | 窗口标题「硬盘工具箱」，磁盘列表完整渲染，右上角「已连接后端」 |
| 首次运行 | 自动在 exe 同目录生成可编辑的 `json/win11.json` |
| `pytest tests/ -q` | 测试机 189 passed（含 `test_resources` / `test_packaging`） |

界面拆成 ES 模块之后，spec 的 `DATAS` 必须把 `ui/css`、`ui/js` 整目录带上，
否则打出来的 exe 只会显示一个没有样式、也没有脚本的空壳。`tests/test_packaging.py`
里钉了这条（`test_spec_bundles_static_assets`），打包后也用浏览器引擎实测过：

| 检查 | 结果 |
| --- | --- |
| `GET /static/js/main.js`、`/static/css/app.css` | 都返回 200（证明资源真的进了包） |
| 打包后的界面冒烟（`tools/ui_smoke.mjs` 对 `Sisp2.exe --serve`） | `ok=true`，10 张截图全部 54–105 KB，磁盘树 9 个节点、设置页 34 行、第 4 步计划表 9 行 |
| `Sisp2.exe --serve` 走 REST 真实整盘安装（2 TB 测试盘） | 六步全绿、`progress` 6/6、分区为 MSR/ESP 300 MB/C 120 GB/D 300 GB，日志 11.8 KB 可读 |
| 同一 exe 的真实分区操作 | `/api/partition/execute` 建 50 GB NTFS 分区成功，日志含 diskpart 输出，temp 目录无残留 |

## 已知限制

- **未签名**：会触发 SmartScreen「未知发布者」，也会被杀软按“打包的 Python
  程序”对待。正式分发建议买代码签名证书并对产物签名。
- **onefile 启动慢**：每次启动都要解包，若在意启动速度可改成 onedir
  （`EXE(..., exclude_binaries=True)` + `COLLECT`），代价是变成一个文件夹。
- **`app.log` 是唯一日志出口**：`--windowed` 没有控制台，`print` 的内容会进
  `os.devnull`，排查问题请看设置里 `logDir` 下的 `app.log`。
- **不能跨平台**：产物含 Windows 专属的 WebView2 / WinForms 组件。
