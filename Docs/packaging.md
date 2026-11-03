# 打包成 exe

把 Sisp2 打包成**单文件、免安装**的 Windows 程序。产物不写注册表、不需要
Python 环境，双击即用。

## 产物

| 文件 | 说明 | 典型体积 |
| --- | --- | --- |
| `dist/Sisp2.exe` | 图形界面（无控制台窗口），双击打开「硬盘工具箱」 | ≈ 18 MB |
| `dist/Sisp2-CLI.exe` | 命令行，参数与 `python -m app.cli` 完全一致 | ≈ 9 MB |

两个 exe 都是 onefile：启动时会把自己解到 `%TEMP%` 下的临时目录再运行，
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
| 界面 `index.html`、默认配置模板 | exe 内部资源 | 只读，随包发布 |
| 业务配置 `json/win11.json` | **exe 同目录** | 首次运行自动从模板复制一份，之后可随意编辑 |
| 设置 `settings.json` | `%APPDATA%\Sisp\` | 与源码运行完全一致，exe 升级不会丢 |
| 窗口位置 `window.json` | `%APPDATA%\Sisp\` | 受 `rememberWindow` 控制 |
| 执行日志、`app.log` | 设置里的 `logDir` | 无控制台窗口时，这里是唯一的诊断出口 |
| 解包目录 | `%TEMP%\_MEIxxxx` | onefile 临时目录，退出后自动清理 |

如果 exe 安装在 `C:\Program Files\` 之类没有写权限的目录，复制配置会失败，
程序改用包内模板继续运行（只读）；这种情况下用 `-j` 指定别处的配置即可。

## 提权

程序不会自动要管理员权限：

- 判断「有没有管理员」是 `app/gui/elevation.py` 的 `IsUserAnAdmin()`；
- 需要提权时用 `--elevate`：`Sisp2.exe --elevate` / `Sisp2-CLI.exe --elevate`，
  内部走 `ShellExecuteW(..., "runas", ...)`，冻结后重启的是 exe 自身；
- 不提权也能跑：预检会把「管理员权限检查」标成失败，写盘会失败，dry-run 照常。

## 命令行用法

```powershell
.\Sisp2-CLI.exe --list                          # 只列出硬盘
.\Sisp2-CLI.exe --dry-run                       # 演练，不写盘
.\Sisp2-CLI.exe --allow-disks 1 --test-mode     # 真实写盘（还需管理员）
.\Sisp2-CLI.exe --json --list                   # 机器可读输出
.\Sisp2-CLI.exe --settings D:\cfg\settings.json # 指定设置文件
```

退出码与源码运行一致：`0` 成功、`1` 内部错误、`2` 预检/阻断、
`3` 执行失败、`4` 策略拒绝、`130` 用户中断。

图形界面也可以走浏览器模式：`Sisp2.exe --serve --port 8765`，然后自己用浏览器
打开 `http://127.0.0.1:8765/`（默认只监听回环地址）。

## 验证记录

打包后的 exe 在专用测试机（Windows 10 22H2 / Python 3.15 源码版对照）与本机
（Windows 11 / Python 3.14.6）都跑过：

| 检查 | 结果 |
| --- | --- |
| `Sisp2-CLI.exe --list` / `--json --list` | 退出码 0，硬盘列表与源码一致 |
| `Sisp2-CLI.exe --dry-run` | 退出码 0，五步流程全过，未提交任何写盘命令 |
| `Sisp2-CLI.exe --allow-disks 0` | 退出码 2（无管理员/受保护盘按设计拒绝） |
| `Sisp2-CLI.exe --bogus` | 退出码 4，与源码版一致 |
| `Sisp2.exe --serve` REST 冒烟 | 会话 → 扫描 → 预检 → 计划 → 提交/确认 → 设置读写全通 |
| `Sisp2.exe` 桌面窗口 | 窗口标题「硬盘工具箱」，界面完整渲染，右上角显示「已连接后端」 |
| 首次运行配置 | 自动在 exe 同目录生成 `json/win11.json` |

## 已知限制

- **未签名**：会触发 SmartScreen「未知发布者」，也会被杀软按“打包的 Python
  程序”对待。正式分发建议买代码签名证书并对产物签名。
- **onefile 启动慢**：每次启动都要解包，若在意启动速度可改成 onedir
  （`EXE(..., exclude_binaries=True)` + `COLLECT`），代价是变成一个文件夹。
- **`app.log` 是唯一日志出口**：`--windowed` 没有控制台，`print` 的内容会进
  `os.devnull`，排查问题请看设置里 `logDir` 下的 `app.log`。
- **不能跨平台**：产物含 Windows 专属的 WebView2 / WinForms 组件。
