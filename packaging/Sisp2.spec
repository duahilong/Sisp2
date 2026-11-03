# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：产出单文件图形界面 exe ``dist/Sisp2.exe``。

（命令行入口已经移除，全部能力都在界面里：装机向导、硬盘分区、修复引导。）

打包内容包括：

- 界面文件 ``app/gui_web/ui/index.html``、业务配置模板 ``json/win11.example.json``
  作为 datas 打进包内，运行时由 ``app/resources.py`` 解析；
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
    (os.path.join(ROOT, "json", "win11.example.json"), "json"),
]

# uvicorn 按字符串名加载实现（loops/protocols/lifespan），静态分析看不到
UVICORN_HIDDEN = collect_submodules("uvicorn")

# pywebview 在 Windows 上按顺序尝试后端，都是惰性 import
GUI_HIDDEN = UVICORN_HIDDEN + [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]

EXCLUDES = ["tkinter", "pytest", "_pytest", "tests", "PyInstaller"]

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

# 构建模式：默认 onefile（单个 exe，启动时解包到 %TEMP%）；
# 设 ``SISP2_BUILD_MODE=onedir`` 则产出目录版（不解包，启动快数倍）。
# 由 ``packaging/build.py --onedir`` 设置，不直接手改。
ONEDIR = os.environ.get("SISP2_BUILD_MODE", "").strip().lower() == "onedir"

gui_exe = EXE(
    gui_pyz,
    gui.scripts,
    gui.binaries if ONEDIR else [],
    gui.datas if ONEDIR else [],
    [],
    name="Sisp2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口；诊断信息走 app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
    exclude_binaries=ONEDIR,
)

if ONEDIR:
    # onedir：exe 只是引导器，依赖与资源放在 exe 同目录（_internal/）
    Sisp2 = COLLECT(
        gui_exe,
        gui.binaries,
        gui.datas,
        strip=False,
        upx=False,
        name="Sisp2",
    )
