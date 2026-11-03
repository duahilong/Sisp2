"""源码运行与 PyInstaller 打包运行共用的路径解析。

三种场景要同时成立：

- ``python -m app.gui_web.main``：仓库根目录既是只读资源根，也是可写目录；
- onefile exe：只读资源在 ``sys._MEIPASS`` 临时目录，可写目录是 exe 所在目录；
- onedir exe：只读资源在 ``<exe目录>/_internal``（PyInstaller 6 起 ``_MEIPASS``
  指向它），可写目录仍是 exe 所在目录。

界面文件、默认业务配置模板都通过这里取路径，避免打包后 ``__file__`` 指向
不存在的路径导致「找不到界面文件」。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 默认业务配置在包内的相对位置；exe 目录里同名的文件优先。
DEFAULT_CONFIG_RELATIVE = Path("json") / "win11.json"
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


def default_config_path() -> Path:
    """默认业务配置：exe 目录优先（用户可改），其次随包模板。"""
    preferred = app_root() / DEFAULT_CONFIG_RELATIVE
    if preferred.is_file():
        return preferred
    bundled = bundle_root() / DEFAULT_CONFIG_RELATIVE
    return bundled if bundled.is_file() else preferred


def ensure_default_config() -> Path:
    """返回可用的默认配置路径；冻结版首次运行会把模板复制到 exe 目录。

    复制失败（例如装在 Program Files 且没有写权限）不算错误，直接用随包
    模板：至少能起来，用户可用 ``-j`` 指定别处的配置。
    """
    target = app_root() / DEFAULT_CONFIG_RELATIVE
    if target.is_file():
        return target
    template = bundle_root() / DEFAULT_CONFIG_RELATIVE
    if is_frozen() and template.is_file():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, target)
            return target
        except OSError:
            pass
    return template if template.is_file() else target
