"""管理员权限检测与 UAC 重新启动。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from collections.abc import Sequence

SW_SHOWNORMAL = 1
ERROR_CANCELLED = 5


def is_admin() -> bool:
    """当前进程是否具有管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def build_relaunch_arguments(argv: Sequence[str] | None = None) -> tuple[str, str]:
    """返回 ``(可执行文件, 参数字符串)``，兼容源码与 PyInstaller。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(arguments)

    params = subprocess.list2cmdline(["-m", "app.gui", *arguments])
    return sys.executable, params


def relaunch_as_admin(argv: Sequence[str] | None = None) -> bool:
    """通过 UAC 重新启动当前程序；用户取消或启动失败时返回 ``False``。"""
    executable, params = build_relaunch_arguments(argv)
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            params,
            None,
            SW_SHOWNORMAL,
        )
    except Exception:
        return False
    return result > 32
