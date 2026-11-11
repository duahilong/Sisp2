"""单实例锁：**一台电脑上只允许跑一个 Sisp2**。

为什么必须限制：任务队列、写盘白名单、会话（本次扫描的盘与保护判断）全部在
**进程内存**里。开两个实例的后果不是“多一个窗口”，而是：

- 两个进程各自扫盘、各自算计划，用户可能在一份计划里点了「开始安装」，
  又在另一份里对同一块盘点一次——``cmd`` 层没有人去串行化它们；
- 两份界面看到的任务状态、日志、``partitionSettleMs`` 等待全都不一致，
  出问题时的现场（导出包）只能看到一半。

实现用 **Windows 命名互斥体**（``CreateMutexW``）：内核对象，进程一退出
（哪怕是崩溃/被结束）系统就自动释放，不会留下需要手工清理的“锁文件”。
非 Windows 环境退回独占锁文件（开发机用；本项目正式运行环境是 Windows）。

第二个实例启动时不会静默消失：先把已经打开的窗口叫到前台（``ShowWindow`` +
``SetForegroundWindow``），再告诉用户“已经有一个在运行了”，然后以退出码
:data:`EXIT_ALREADY_RUNNING` 结束。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

#: 互斥体名字。带版本号，避免和别的程序撞名；``Local\`` 前缀 = 每个登录会话一个，
#: 同一台机器上的两个用户各自可以开一份（他们本来也互相看不到对方的窗口）。
DEFAULT_NAME = "Local\\Sisp2.SingleInstance.v1"
#: 窗口标题（和 ``main._run_desktop`` 里 ``webview.create_window`` 的标题一致）。
WINDOW_TITLE = "硬盘工具箱"
#: 第二个实例的退出码（4 = 端口被占，5 = 渲染内核不满足，6 = 已经在运行）。
EXIT_ALREADY_RUNNING = 6

_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9
_SW_SHOWNORMAL = 1


# ---------------------------------------------------------------- Windows 互斥体
class _WinMutex:
    """``CreateMutexW`` 的最小封装（只在 Windows 上可用）。"""

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]

    def create(self, name: str) -> tuple[int, bool]:
        """创建/打开互斥体，返回 ``(句柄, 是否已经存在)``。"""
        handle = self._kernel32.CreateMutexW(None, True, name)
        already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS  # type: ignore[attr-defined]
        return int(handle or 0), bool(already)

    def close(self, handle: int) -> None:
        try:
            self._kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001, S110 - 关句柄失败不影响退出
            pass

    def activate_window(self, title: str) -> bool:
        """把标题匹配的窗口叫到前台；找不到返回 ``False``。"""
        try:
            hwnd = self._user32.FindWindowW(None, title)
            if not hwnd:
                return False
            self._user32.ShowWindow(hwnd, _SW_RESTORE)
            self._user32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001 - 叫不动就算了，用户还能自己切
            return False
        else:
            return True


# ---------------------------------------------------------------- 锁文件（非 Windows）
class _FileLock:
    """独占锁文件：``O_CREAT|O_EXCL`` 建文件，里面写 pid。

    进程被强杀时文件会残留，所以读到已有文件时要检查那个 pid 还活着没有；
    已经不在就当成旧文件接管（避免“上一次崩了以后永远打不开”）。
    """

    def __init__(self, path: Path | None = None) -> None:
        base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.path = Path(path) if path else base / "sisp2-single-instance.lock"
        self._fd: int | None = None

    def create(self, name: str) -> tuple[int, bool]:
        for _attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                if self._stale():
                    self._drop()
                    continue
                return 0, True
            except OSError:
                # 锁文件都建不了（目录只读等）：当成“没在跑”，绝不让锁挡住启动
                return 0, False
            os.write(fd, str(os.getpid()).encode("ascii"))
            self._fd = fd
            return fd, False
        return 0, True

    def _stale(self) -> bool:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            pid = int(text)
        except (OSError, ValueError):
            return True
        if pid <= 0 or pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    def _drop(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def close(self, handle: int) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._drop()

    def activate_window(self, title: str) -> bool:
        return False


def _backend():
    """按平台挑实现；挑不出来返回 ``None``（退化成“不限制”，绝不挡住启动）。"""
    try:
        if sys.platform == "win32":
            return _WinMutex()
        return _FileLock()
    except Exception:  # noqa: BLE001 - 拿不到内核 API 也不能让程序打不开
        return None


# ---------------------------------------------------------------- 对外接口
class SingleInstance:
    """一次进程生命周期内的单实例门闩。

    用法::

        lock = SingleInstance()
        if not lock.acquire():
            ...  # 已经有一个在跑
    """

    def __init__(self, name: str = DEFAULT_NAME, *, backend=None) -> None:
        self.name = name
        self._backend = backend
        self._backend_resolved = backend is not None
        self._handle: int | None = None
        self.held = False

    def _ops(self):
        if not self._backend_resolved:
            self._backend = _backend()
            self._backend_resolved = True
        return self._backend

    def acquire(self, *, wait: float = 0.0) -> bool:
        """抢占实例锁；已被占用返回 ``False``。

        ``wait`` 秒内会重试（提权重启时父子进程有极短的交接窗口：父进程刚
        释放、子进程才拿到锁）。
        """
        if self.held:
            return True
        ops = self._ops()
        if ops is None:
            # 拿不到锁后端（非 Windows 且没有可写 tmp 等）：按“没在跑”处理。
            self.held = True
            return True
        deadline = time.monotonic() + max(0.0, float(wait))
        while True:
            handle, already = ops.create(self.name)
            if handle and not already:
                self._handle = handle
                self.held = True
                return True
            if handle:
                ops.close(handle)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def release(self) -> None:
        """释放锁（进程退出时系统也会自动释放，这里只是让路径显式）。"""
        if self._handle is not None:
            ops = self._ops()
            if ops is not None:
                ops.close(self._handle)
            self._handle = None
        self.held = False

    def activate_window(self, title: str = WINDOW_TITLE) -> bool:
        """把已经打开的窗口叫到前台（找不到返回 ``False``）。"""
        ops = self._ops()
        if ops is None:
            return False
        return bool(ops.activate_window(title))


def already_running_message() -> str:
    """第二个实例给用户看的说明（含该怎么做）。"""
    return (
        "硬盘工具箱已经在运行了。\n\n"
        "为了避免两个窗口同时对硬盘做分区/写盘，程序只允许同时运行一个。\n\n"
        "· 已经打开的窗口应该已经切到前台；\n"
        "· 如果看不到，请在任务管理器里结束“Sisp2.exe”后再重新打开。"
    )
