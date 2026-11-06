"""源码运行与 PyInstaller 打包运行共用的路径解析。

三种场景要同时成立：

- ``python -m app.gui_web.main``：仓库根目录既是只读资源根，也是可写目录；
- onefile exe：只读资源在 ``sys._MEIPASS`` 临时目录，可写目录是 exe 所在目录；
- onedir exe：只读资源在 ``<exe目录>/_internal``（PyInstaller 6 起 ``_MEIPASS``
  指向它），可写目录仍是 exe 所在目录。

界面文件通过这里取路径，避免打包后 ``__file__`` 指向不存在的路径导致
「找不到界面文件」。

（这里曾经还有 ``default_config_path()`` / ``ensure_default_config()``，
服务于「业务 JSON 配置文件」那一层。那一层整条移除后，这两个函数没有调用方，
一并删掉——装机参数只来自界面设置。）
"""

from __future__ import annotations

import sys
from pathlib import Path

UI_RELATIVE = Path("app") / "gui_web" / "ui" / "index.html"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """可写根目录：源码运行时是仓库根，冻结后是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """随包只读资源根目录（onefile 是解包临时目录，onedir 是 ``_internal``）。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        return Path(base) if base else app_root()
    return app_root()


def ui_path() -> Path:
    """前端单文件界面的位置。"""
    return bundle_root() / UI_RELATIVE
