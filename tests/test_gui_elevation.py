"""GUI 启动与 UAC 提权逻辑测试。

不真正弹 UAC、不启动事件循环：``ctypes``、``QApplication``、``MainWindow``
和消息框都被替换成假实现。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

pytest.importorskip("PySide6")

from app.gui import app as gui_app  # noqa: E402
from app.gui import elevation  # noqa: E402


def test_is_admin_handles_success_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        elevation.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: 1)),
    )
    assert elevation.is_admin() is True

    def broken() -> int:
        raise OSError("no shell32")

    monkeypatch.setattr(
        elevation.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=broken)),
    )
    assert elevation.is_admin() is False


def test_build_relaunch_arguments_for_source_and_frozen(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(sys, "argv", ["app/gui_main.py"])

    executable, params = elevation.build_relaunch_arguments(["--debug"])
    assert executable == r"C:\Python\python.exe"
    assert params == "-m app.gui --debug"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\SispGUI.exe")

    executable, params = elevation.build_relaunch_arguments([])
    assert executable == r"C:\Apps\SispGUI.exe"
    assert params == ""


def test_relaunch_as_admin_reports_success_and_cancel(monkeypatch) -> None:
    calls: list[tuple] = []

    def shell_execute(*args):
        calls.append(args)
        return 42

    monkeypatch.setattr(
        elevation.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=shell_execute)),
    )
    monkeypatch.setattr(elevation, "build_relaunch_arguments", lambda _argv: ("exe", "args"))

    assert elevation.relaunch_as_admin([]) is True
    assert calls[0][:4] == (None, "runas", "exe", "args")

    monkeypatch.setattr(
        elevation.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=lambda *args: 5)),
    )
    assert elevation.relaunch_as_admin([]) is False

    def broken(*args):
        raise OSError("shell failed")

    monkeypatch.setattr(
        elevation.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=broken)),
    )
    assert elevation.relaunch_as_admin([]) is False


class FakeApplication:
    instances: list["FakeApplication"] = []

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.quit_called = False
        self.exec_called = False
        FakeApplication.instances.append(self)

    def setApplicationName(self, _name: str) -> None:  # noqa: N802 - 模拟 Qt 接口
        pass

    def setApplicationDisplayName(self, _name: str) -> None:  # noqa: N802
        pass

    def setApplicationVersion(self, _version: str) -> None:  # noqa: N802
        pass

    def exec(self) -> int:
        self.exec_called = True
        return 0

    def quit(self) -> None:
        self.quit_called = True


class FakeWindow:
    def __init__(self) -> None:
        self.shown = False

    def show(self) -> None:
        self.shown = True


class FakeMessageBox:
    critical_calls: list[tuple] = []

    @staticmethod
    def critical(parent, title: str, text: str) -> None:
        FakeMessageBox.critical_calls.append((parent, title, text))


@pytest.fixture(autouse=True)
def reset_fakes(monkeypatch):
    FakeApplication.instances = []
    FakeMessageBox.critical_calls = []
    monkeypatch.setattr(gui_app, "QApplication", FakeApplication)
    monkeypatch.setattr(gui_app, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(gui_app, "MainWindow", FakeWindow)
    monkeypatch.setattr(gui_app, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gui_app, "reset_logging", lambda: None)
    monkeypatch.delenv("SISP_NO_ELEVATE", raising=False)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["SispGUI.exe"], None),
        (["SispGUI.exe", "--log-dir", r"D:\logs"], r"D:\logs"),
        (["SispGUI.exe", r"--log-dir=D:\logs"], r"D:\logs"),
        (["SispGUI.exe", "--log-dir", "   "], None),
        (["SispGUI.exe", "--log-dir"], None),
    ],
)
def test_log_dir_from_arguments(arguments, expected) -> None:
    assert gui_app._log_dir_from_arguments(arguments) == expected


def test_main_configures_file_logging_with_requested_dir(monkeypatch) -> None:
    monkeypatch.setattr(gui_app, "is_admin", lambda: True)
    calls: list[object] = []
    resets: list[bool] = []
    monkeypatch.setattr(
        gui_app,
        "configure_logging",
        lambda value=None: calls.append(value) or Path("Sisp.log"),
    )
    monkeypatch.setattr(gui_app, "reset_logging", lambda: resets.append(True))

    assert gui_app.main(["SispGUI.exe", "--log-dir", r"D:\logs"]) == 0

    assert calls == [r"D:\logs"]
    assert resets == [True]


def test_main_runs_window_when_admin(monkeypatch) -> None:
    monkeypatch.setattr(gui_app, "is_admin", lambda: True)

    assert gui_app.main(["SispGUI.exe"]) == 0

    application = FakeApplication.instances[-1]
    assert application.exec_called is True
    assert application.quit_called is False


def test_main_relaunches_when_not_admin(monkeypatch) -> None:
    monkeypatch.setattr(gui_app, "is_admin", lambda: False)
    monkeypatch.setattr(gui_app, "relaunch_as_admin", lambda _argv: True)

    assert gui_app.main(["SispGUI.exe", "--debug"]) == 0
    assert FakeApplication.instances == []
    assert FakeMessageBox.critical_calls == []


def test_main_shows_error_when_relaunch_fails(monkeypatch) -> None:
    monkeypatch.setattr(gui_app, "is_admin", lambda: False)
    monkeypatch.setattr(gui_app, "relaunch_as_admin", lambda _argv: False)

    assert gui_app.main(["SispGUI.exe"]) == 1
    assert FakeMessageBox.critical_calls
    assert FakeApplication.instances[-1].quit_called is True


def test_main_allows_unprivileged_debug_run(monkeypatch) -> None:
    monkeypatch.setattr(gui_app, "is_admin", lambda: False)
    monkeypatch.setenv("SISP_NO_ELEVATE", "1")

    def unexpected(_argv):
        raise AssertionError("提权不应被调用")

    monkeypatch.setattr(gui_app, "relaunch_as_admin", unexpected)

    assert gui_app.main(["SispGUI.exe"]) == 0
    assert FakeApplication.instances[-1].exec_called is True
