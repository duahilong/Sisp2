"""Windows 提权（UAC）相关的通用工具。

只依赖标准库（``ctypes`` / ``subprocess`` / ``sys``），不 import 任何业务模块，
因此被 GUI 入口（``app.gui_web.main``）
和单元测试安全地使用。

典型用法::

    from app.gui.elevation import is_admin, relaunch_as_admin

    if not is_admin() and relaunch_as_admin():
        raise SystemExit(0)   # UAC 已经拉起新进程，本进程退出

注意：``IsUserAnAdmin()`` 只回答“当前进程是否持有管理员令牌”，
不代表某块磁盘可写；磁盘是否可写仍由工作流（预检 + 保护规则）判断。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

# ShellExecuteW 的 nShowCmd 参数：正常显示窗口
SW_SHOWNORMAL = 1

# ShellExecuteW 的返回值：小于等于 32 都表示失败
SE_ERR_FNF = 2
SE_ERR_ACCESSDENIED = 5
SE_HINSTANCE_SUCCESS_MIN = 32


def is_admin() -> bool:
    """当前进程是否以管理员权限运行。

    通过 ``shell32.IsUserAnAdmin`` 判断；在非 Windows 平台、或者调用本身失败
    （例如受限环境里 ``ctypes.windll`` 不存在）时一律返回 ``False``，绝不抛异常。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 判断失败按“非管理员”处理，避免入口崩溃
        return False


def relaunch_as_admin(
    argv: list[str] | None = None,
    *,
    module: str | None = None,
    executable: str | None = None,
) -> bool:
    """通过 UAC 以管理员身份重新拉起当前程序。

    参数
    ----
    argv:
        要传给新进程的参数列表；``None`` 表示沿用当前进程的 ``sys.argv[1:]``。
    module:
        以 ``python -m <module>`` 方式重启时要用的模块名；``None`` 时自动推断
        （``python -m app.gui_web.main`` 会推断出 ``app.gui_web.main``）。
    executable:
        要启动的解释器/可执行文件；``None`` 表示 ``sys.executable``。

    返回
    ----
    ``True`` = UAC 已接受并成功拉起新进程；``False`` = 用户取消或调用失败。
    调用方在拿到 ``True`` 后应当结束当前进程，避免两个实例同时跑。

    实现用 ``ShellExecuteW(None, "runas", ...)``，判断标准是返回值大于 32。
    """
    arguments = [str(item) for item in (sys.argv[1:] if argv is None else argv)]

    try:
        if getattr(sys, "frozen", False):
            # 打包成 exe：直接重启自身，命令行参数原样传过去
            target = executable or sys.executable
            parameters = subprocess.list2cmdline(arguments)
        else:
            target = executable or sys.executable
            module_name = module or _current_module_name()
            if module_name:
                payload = ["-m", module_name, *arguments]
            else:
                script = sys.argv[0] if sys.argv else ""
                payload = [script, *arguments] if script.endswith(".py") else list(arguments)
            parameters = subprocess.list2cmdline(payload)

        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, parameters, None, SW_SHOWNORMAL
        )
    except Exception:  # noqa: BLE001 - 提权失败只返回 False，由调用方决定怎么提示
        return False

    try:
        return int(result) > SE_HINSTANCE_SUCCESS_MIN
    except (TypeError, ValueError):
        return False


def _current_module_name() -> str | None:
    """推断当前进程的模块名，供 ``python -m <module>`` 方式重启。

    ``python -m app.gui_web.main`` 运行时 ``__main__.__spec__.name`` 就是该模块名；
    直接跑脚本文件时没有模块名，返回 ``None``（调用方改用脚本路径重启）。
    """
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", None)
    if not name:
        name = getattr(main, "__package__", None)
    if not name or name == "__main__":
        return None
    return str(name)
