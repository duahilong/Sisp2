# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：产出单文件图形界面 exe ``dist/Sisp2.exe``。

（命令行入口已经移除，全部能力都在界面里：装机向导、硬盘分区、修复引导。）

打包内容包括：

- 界面文件 ``app/gui_web/ui/index.html`` 及其 css/js 作为 datas 打进包内，
  运行时由 ``app/resources.py`` 解析；
- ``uvicorn`` 的 loop / protocol / lifespan 实现是动态导入的，必须整包收进来；
- pywebview 的平台后端（WinForms + WebView2）也是惰性导入。

构建：``python packaging/build.py``（会先确保图标存在再调用本文件）。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
ICON = os.path.join(SPECPATH, "sisp2.ico")
VERSION_FILE = os.path.join(SPECPATH, "version_info.txt")

UI_ROOT = os.path.join(ROOT, "app", "gui_web", "ui")
UI_DEST = os.path.join("app", "gui_web", "ui")

DATAS = [
    (os.path.join(UI_ROOT, "index.html"), UI_DEST),
    # 界面是 ES 模块：样式和脚本都必须进包，否则 exe 里只剩一个空壳。
    (os.path.join(UI_ROOT, "css"), os.path.join(UI_DEST, "css")),
    (os.path.join(UI_ROOT, "js"), os.path.join(UI_DEST, "js")),
]

# uvicorn 按字符串名加载实现（loops/protocols/lifespan），静态分析看不到
UVICORN_HIDDEN = collect_submodules("uvicorn")

# pywebview 在 Windows 上按顺序尝试后端，都是惰性 import
GUI_HIDDEN = UVICORN_HIDDEN + [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]

EXCLUDES = [
    "tkinter",
    "pytest",
    "_pytest",
    "tests",
    "PyInstaller",
    # pywebview 在 Windows 上只用 winforms + edgechromium，其他后端不打进包
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "gi",
    "cefpython3",
    "sip",
    # 本工具一行都不用、但会被构建机上无关的包拖进来的大件；不排掉的话
    # 同一份源码在“装了 numpy/Pillow 的机器”上会打出 57 MB，干净机器只有 18 MB
    "numpy",
    "PIL",
    "tzdata",
    "bcrypt",
]

COMMON = dict(
    pathex=[ROOT],
    datas=DATAS,
    excludes=EXCLUDES,
    upx=False,
    noarchive=False,
    strip=False,
)

gui = Analysis(
    [os.path.join(ROOT, "app", "gui_web", "main.py")],
    hiddenimports=GUI_HIDDEN,
    **COMMON,
)

gui_pyz = PYZ(gui.pure)

gui_exe = EXE(
    gui_pyz,
    gui.scripts,
    gui.binaries,
    gui.datas,
    [],
    name="Sisp2",
    # 不要写 requireAdministrator：WebView2 跑不了 High IL 进程（它要把
    # msedgewebview2.exe 降到 Medium IL，而这条降权路径要借 explorer.exe），
    # 强行提权就会崩成 “msedgewebview2 has stopped working”。
    # 需要管理员的写盘动作由界面通过 UAC 拉起的后端子进程（--backend）承担。
    uac_admin=False,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # onefile 的解包目录：默认（None）会解到 %TEMP%\_MEIxxxx。%TEMP% 在部分机器上
    # 被组策略/沙箱/杀软管着，bootloader 会直接弹 “Could not create temporary
    # directory!”（连 CreateDirectoryW 都失败，重试 5 次就放弃）。这里固定到
    # %LOCALAPPDATA%\Sisp\run：与 exe 位置无关、用户一定可写、不会被临时目录清理
    # 工具随手删掉。bootloader 会用 ExpandEnvironmentStringsW 展开这个变量并自动建目录。
    # 注意：不能写相对路径（会按“当前工作目录”解析），也不能写 exe 同目录
    # （exe 装在 Program Files 之类只读位置时就解不开了）。
    runtime_tmpdir="%LOCALAPPDATA%\\Sisp\\run",
    console=False,  # 无控制台窗口；诊断信息走 app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)
