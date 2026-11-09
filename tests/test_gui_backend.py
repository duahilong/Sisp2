"""桌面模式的进程拆分：界面不提权，写盘能力在 UAC 拉起的后端子进程里。

回归的 BUG：exe 清单写 ``requireAdministrator``，WebView2 宿主跟着变成 High IL，
在部分机器上直接崩成 ``msedgewebview2 has stopped working`` +
``Error launching CrashSender.exe``（WebView2 的运行时只能以 Medium IL 启动，
降权那条路要借 explorer.exe 代劳）。修法见 ``app/gui_web/main.py`` 的模块文档。
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from argparse import Namespace

import pytest

from app.gui_web import main as gui


def _args(**kwargs) -> Namespace:
    """``main()`` 的默认参数快照，按需覆盖。"""
    base = {
        "settings": None,
        "debug": False,
        "backend": False,
        "parent_pid": None,
        "port": 8765,
        "host": "127.0.0.1",
        "serve": False,
        "elevate": False,
        "width": 1280,
        "height": 860,
    }
    base.update(kwargs)
    return Namespace(**base)


def test_backend_argv_marks_child_and_forwards_options() -> None:
    """后端子进程必须带上 ``--backend``、父 pid 和界面里选好的全部参数。"""
    argv = gui.backend_argv(
        _args(settings="D:/cfg/settings.json", debug=True),
        8901,
        4242,
    )
    assert argv[:2] == ["--backend", "--serve"]
    assert argv[argv.index("--parent-pid") + 1] == "4242"
    assert argv[argv.index("--port") + 1] == "8901"
    # 业务配置那一层已移除：子命令里不能再出现 -j/--config
    assert "-j" not in argv and "--config" not in argv
    # 演练模式已移除：子进程命令行里不能再出现这个开关
    assert "--dry-run" not in argv
    assert argv[argv.index("--settings") + 1] == "D:/cfg/settings.json"
    assert "--debug" in argv
    # 拆进程不能把写盘接口从回环地址上放出去
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert not any(item.startswith("0.0") for item in argv)  # 就是在断言没绑全网卡


def test_spawn_uses_hidden_window_and_own_pid(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake(argv=None, *, n_show=1):
        seen["argv"] = list(argv or [])
        seen["n_show"] = n_show
        return True

    monkeypatch.setattr(gui, "relaunch_as_admin", fake)
    assert gui.spawn_privileged_backend(_args(), 9999) is True
    assert seen["n_show"] == 0  # 后端不弹自己的窗口
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "--backend"
    assert argv[argv.index("--parent-pid") + 1] == str(os.getpid())


@pytest.mark.skipif(sys.platform != "win32", reason="父进程探活走 Win32 句柄")
def test_parent_alive_detects_dead_and_live() -> None:
    assert gui._parent_alive(os.getpid()) is False  # 自己不算“父进程”
    assert gui._parent_alive(0) is False

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert gui._parent_alive(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert gui._parent_alive(child.pid) is False


@pytest.mark.skipif(sys.platform != "win32", reason="父进程探活走 Win32 句柄")
def test_watch_parent_fires_when_parent_exits() -> None:
    """界面被关掉/被强杀时，提权后端必须能自己退出（界面没权限杀它）。"""
    fired: list[bool] = []
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    thread = gui.watch_parent(child.pid, lambda: fired.append(True), interval=0.05)
    thread.join(timeout=15)
    child.wait(timeout=10)
    assert fired == [True]


def test_webview_storage_path_is_stable_and_writable(monkeypatch, tmp_path) -> None:
    """WebView2 的 user data folder 要落在窗口进程自己的可写目录里。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = gui.webview_storage_path()
    assert path == str(tmp_path / "Sisp" / "WebView2")
    assert (tmp_path / "Sisp" / "WebView2").is_dir()


class _Event:
    def __iadd__(self, _other):
        return self


class _Window:
    width = 1280
    height = 860
    x = 0
    y = 0

    def __init__(self) -> None:
        self.events = types.SimpleNamespace(closing=_Event())


def _stub_webview(monkeypatch, captured: dict) -> None:
    def create_window(title, url, **_kwargs):
        captured["title"] = title
        captured["url"] = url
        return _Window()

    def start(**kwargs):
        captured["start"] = kwargs

    monkeypatch.setitem(
        sys.modules, "webview", types.SimpleNamespace(create_window=create_window, start=start)
    )


def test_desktop_window_process_does_not_build_workflow(monkeypatch, tmp_path) -> None:
    """提权后端拉起来之后，窗口进程**不建** WorkflowController。

    这一条是拆进程的核心：界面进程里不该有 diskpart 的入口，也不该另开一份
    业务会话（两份会话会让“界面里的计划”和“后端执行的计划”对不上）。
    """
    captured: dict = {}
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _stub_webview(monkeypatch, captured)
    monkeypatch.setattr(gui, "_check_port_free", lambda host, port: None)
    monkeypatch.setattr(gui, "spawn_privileged_backend", lambda args, port: True)
    monkeypatch.setattr(gui, "_wait_url", lambda url, timeout=25.0: url.endswith("/health"))
    monkeypatch.setattr(gui, "read_settings", lambda path: {})
    # 窗口进程的日志落在真实 %APPDATA% 下会污染用户目录，这里一律不落盘
    monkeypatch.setattr(gui, "configure_window_logging", lambda path, settings: None)
    monkeypatch.setattr(
        gui, "build_api", lambda *a, **k: pytest.fail("窗口进程不该建业务会话")
    )

    assert gui.main(["--port", "8791"]) == 0
    assert captured["url"] == "http://127.0.0.1:8791/"
    # UDF 指到 LOCALAPPDATA，别用 %TEMP% 下那份共享/易被清掉的目录
    assert captured["start"]["storage_path"] == str(tmp_path / "Sisp" / "WebView2")


def test_desktop_falls_back_to_local_backend_when_uac_rejected(monkeypatch, tmp_path) -> None:
    """用户点了“否”：界面照常打开，只是写盘会被预检拦住（不能白屏退出）。"""
    captured: dict = {}
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _stub_webview(monkeypatch, captured)
    monkeypatch.setattr(gui, "_check_port_free", lambda host, port: None)
    monkeypatch.setattr(gui, "spawn_privileged_backend", lambda args, port: False)
    monkeypatch.setattr(gui, "read_settings", lambda path: {})
    # 窗口进程的日志落在真实 %APPDATA% 下会污染用户目录，这里一律不落盘
    monkeypatch.setattr(gui, "configure_window_logging", lambda path, settings: None)

    closed: list[bool] = []
    monkeypatch.setattr(
        gui, "build_api", lambda *a, **k: (types.SimpleNamespace(close=lambda: closed.append(True)), object())
    )

    class _Server:
        should_exit = False

    servers: list[_Server] = []

    def fake_serve_in_thread(app, host, port):
        servers.append(_Server())
        return servers[-1], None

    monkeypatch.setattr(gui, "_serve_in_thread", fake_serve_in_thread)
    monkeypatch.setattr(gui, "_wait_until_ready", lambda url: True)
    monkeypatch.setitem(sys.modules, "app.gui_web.server", types.SimpleNamespace(create_app=lambda *a, **k: object()))

    assert gui.main(["--port", "8792"]) == 0
    assert captured["url"] == "http://127.0.0.1:8792/"
    assert closed == [True]  # 进程内服务被关掉
    assert servers and servers[0].should_exit is True
