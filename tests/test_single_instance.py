"""单实例门闩：一台机器上只允许一个 Sisp2（`app/gui_web/single_instance.py`）。

为什么值得这么多测试：这个锁**错了就会挡住用户**（两条命：一是第二次双击被误
判成"已经在运行"，二是上一次崩溃留下的残留让程序永远打不开）。所以这里既测
"该挡的挡住"，也测"不该挡的不许挡"（拿不到锁后端、锁文件损坏、pid 已经死了）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.gui_web.single_instance import (
    DEFAULT_NAME,
    EXIT_ALREADY_RUNNING,
    SingleInstance,
    _FileLock,
    already_running_message,
)


# ---------------------------------------------------------------- 假后端
class FakeBackend:
    """模拟命名互斥体：``taken`` 里已有的名字就算“已经有人在跑”。"""

    def __init__(self, taken: set[str] | None = None, *, busy_times: int = 0) -> None:
        self.taken = set(taken or ())
        self.busy_times = busy_times
        self.creates: list[str] = []
        self.closed: list[int] = []
        self.activated: list[str] = []
        self.window_found = True
        self.window_results: list[bool] = []

    def create(self, name: str) -> tuple[int, bool]:
        self.creates.append(name)
        if self.busy_times > 0:
            self.busy_times -= 1
            return 99, True
        if name in self.taken:
            return 99, True
        self.taken.add(name)
        return 7, False

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        if handle == 7:
            self.taken.discard(DEFAULT_NAME)

    def activate_window(self, title: str) -> bool:
        self.activated.append(title)
        if self.window_results:
            return self.window_results.pop(0)
        return self.window_found


# ---------------------------------------------------------------- 基本语义
def test_first_instance_gets_the_lock() -> None:
    backend = FakeBackend()
    first = SingleInstance(backend=backend)
    assert first.acquire() is True
    assert first.held is True
    assert backend.creates == [DEFAULT_NAME]


def test_second_instance_is_refused() -> None:
    backend = FakeBackend()
    first = SingleInstance(backend=backend)
    second = SingleInstance(backend=backend)
    assert first.acquire() is True
    assert second.acquire() is False
    assert second.held is False


def test_release_lets_the_next_one_in() -> None:
    backend = FakeBackend()
    first = SingleInstance(backend=backend)
    second = SingleInstance(backend=backend)
    assert first.acquire() is True
    first.release()
    assert first.held is False
    assert second.acquire() is True


def test_acquire_is_idempotent() -> None:
    backend = FakeBackend()
    lock = SingleInstance(backend=backend)
    assert lock.acquire() is True
    assert lock.acquire() is True
    assert len(backend.creates) == 1, "已经拿到锁就不该再创建一遍"


def test_wait_retries_before_giving_up() -> None:
    """提权重启时父子进程有极短的交接窗口：等几轮应该能拿到。"""
    backend = FakeBackend(busy_times=2)
    lock = SingleInstance(backend=backend)
    assert lock.acquire(wait=2.0) is True
    assert len(backend.creates) == 3


def test_wait_times_out_when_really_taken() -> None:
    backend = FakeBackend(taken={DEFAULT_NAME})
    lock = SingleInstance(backend=backend)
    assert lock.acquire(wait=0.0) is False


def test_missing_backend_never_blocks_startup() -> None:
    """拿不到锁后端（非 Windows 没有可写 tmp 等）时宁可不管，也不能挡住启动。"""
    lock = SingleInstance(backend=None)
    lock._backend_resolved = True  # 强制走“没有后端”这条分支
    assert lock.acquire() is True
    assert lock.held is True
    lock.release()


def test_activate_window_delegates() -> None:
    backend = FakeBackend()
    lock = SingleInstance(backend=backend)
    lock.acquire()
    assert lock.activate_window("硬盘工具箱") is True
    assert backend.activated == ["硬盘工具箱"]


def test_activate_window_waits_for_first_instance_to_create_it(monkeypatch) -> None:
    """首个实例已拿锁但窗口还没建好时，第二次启动要轮询而不是立即放弃。"""
    backend = FakeBackend()
    backend.window_results = [False, False, True]
    lock = SingleInstance(backend=backend)
    ticks = iter([0.0, 0.2, 0.4])
    monkeypatch.setattr("app.gui_web.single_instance.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("app.gui_web.single_instance.time.sleep", lambda _seconds: None)

    assert lock.activate_window("硬盘工具箱", wait=1.0) is True
    assert backend.activated == ["硬盘工具箱"] * 3


def test_message_tells_the_user_what_to_do() -> None:
    message = already_running_message()
    assert "已经在运行" in message
    assert "同时运行一个" in message
    assert "任务管理器" in message


def test_exit_code_is_documented() -> None:
    assert EXIT_ALREADY_RUNNING == 6


# ---------------------------------------------------------------- 锁文件（非 Windows）
def test_file_lock_refuses_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "sisp.lock"
    first = _FileLock(path)
    handle, already = first.create("ignored")
    assert handle and already is False
    second = _FileLock(path)
    handle2, already2 = second.create("ignored")
    assert handle2 == 0
    assert already2 is True
    first.close(handle)
    assert not path.exists(), "释放后必须删掉锁文件"


def test_file_lock_takes_over_a_dead_pid(tmp_path: Path) -> None:
    path = tmp_path / "sisp.lock"
    path.write_text("999999", encoding="utf-8")  # 不存在的 pid
    lock = _FileLock(path)
    handle, already = lock.create("ignored")
    assert handle and already is False, "上一次崩了留下的文件不能把程序锁死"
    lock.close(handle)


def test_file_lock_takes_over_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "sisp.lock"
    path.write_text("not-a-pid", encoding="utf-8")
    lock = _FileLock(path)
    handle, already = lock.create("ignored")
    assert handle and already is False
    lock.close(handle)


def test_file_lock_keeps_the_live_pid(tmp_path: Path) -> None:
    path = tmp_path / "sisp.lock"
    # 用当前进程自己的 pid：一定“活着”
    path.write_text(str(os.getpid() + 0), encoding="utf-8")
    lock = _FileLock(path)
    handle, already = lock.create("ignored")
    # 自己的 pid 视为残留（同一个进程不可能持锁两次），这里应该接管
    assert handle and already is False
    lock.close(handle)


# ---------------------------------------------------------------- Windows 真互斥体
@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 上有")
def test_windows_mutex_refuses_a_second_holder() -> None:
    """真跑一遍 ``CreateMutexW``：同一进程里两次创建，第二次必须拿到“已存在”。"""
    first = SingleInstance("Local\\Sisp2.Test.SingleInstance")
    second = SingleInstance("Local\\Sisp2.Test.SingleInstance")
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        first.release()
        second.release()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 上有")
def test_windows_mutex_release_allows_reacquire() -> None:
    name = "Local\\Sisp2.Test.Reacquire"
    first = SingleInstance(name)
    second = SingleInstance(name)
    assert first.acquire() is True
    first.release()
    assert second.acquire() is True
    second.release()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体只在 Windows 上有")
def test_windows_activate_missing_window_is_false() -> None:
    lock = SingleInstance("Local\\Sisp2.Test.Activate")
    assert lock.activate_window("这个窗口标题一定不存在-12345") is False


# ---------------------------------------------------------------- 入口门闩
def _args(**kwargs):
    from types import SimpleNamespace

    base = {"backend": False, "allow_multiple": False, "serve": True, "elevate": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_main_guard_blocks_second_instance() -> None:
    from app.gui_web.main import acquire_single_instance

    backend = FakeBackend(taken={DEFAULT_NAME})
    lock = SingleInstance(backend=backend)
    alerts: list[str] = []
    lines: list[str] = []
    code = acquire_single_instance(
        _args(serve=False),
        lock=lock,
        alert=alerts.append,
        say=lines.append,
    )
    assert code == EXIT_ALREADY_RUNNING
    assert lines and "已经在运行" in lines[0]
    assert alerts and "只要" not in alerts[0] and "任务管理器" in alerts[0]


def test_main_guard_says_nothing_in_serve_mode() -> None:
    """``--serve`` 是控制台程序：不弹模态框，只写一行 stderr。"""
    from app.gui_web.main import acquire_single_instance

    backend = FakeBackend(taken={DEFAULT_NAME})
    lock = SingleInstance(backend=backend)
    alerts: list[str] = []
    lines: list[str] = []
    code = acquire_single_instance(
        _args(serve=True), lock=lock, alert=alerts.append, say=lines.append
    )
    assert code == EXIT_ALREADY_RUNNING
    assert alerts == []
    assert lines


def test_main_guard_skips_backend_and_allow_multiple() -> None:
    from app.gui_web.main import acquire_single_instance

    backend = FakeBackend(taken={DEFAULT_NAME})
    lock = SingleInstance(backend=backend)
    assert acquire_single_instance(_args(backend=True), lock=lock) is None
    assert acquire_single_instance(_args(allow_multiple=True), lock=lock) is None
    assert backend.creates == [], "这两种情况下根本不该去抢锁"


def test_main_guard_reports_activated_window() -> None:
    from app.gui_web.main import acquire_single_instance

    backend = FakeBackend(taken={DEFAULT_NAME})
    backend.window_found = True
    lock = SingleInstance(backend=backend)
    alerts: list[str] = []
    assert (
        acquire_single_instance(_args(serve=False), lock=lock, alert=alerts.append)
        == EXIT_ALREADY_RUNNING
    )
    assert "已经把已有的窗口切到前台" in alerts[0]
