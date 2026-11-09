"""内核探测（``app/gui/webengine.py``）的纯逻辑测试。

这里只碰注入的假 reader，不读真实注册表：判定结果必须能在任何机器上复现，
包括“老 Win10 没装 WebView2”这种真实故障机的形态。
"""

from __future__ import annotations

import winreg

import pytest

from app.gui.webengine import (
    DOTNET_SUBKEY,
    MIN_DOTNET_RELEASE,
    MIN_WEBVIEW2_MAJOR,
    WEBVIEW2_CLIENTS,
    WEBVIEW2_DOWNLOAD_URL,
    inspect_engine,
    major_version,
    parse_version,
)

RUNTIME_GUID = WEBVIEW2_CLIENTS[0][0]
MINOR_CLIENT_GUID = WEBVIEW2_CLIENTS[1][0]


class FakeRegistry:
    """按 ``(subkey 后缀, 值名)`` 查表；只关心「读得到 / 读不到」。"""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.rows: list[tuple[int, str, str]] = []

    def __call__(self, hive: int, subkey: str, name: str) -> str | None:
        self.rows.append((hive, subkey, name))
        for suffix, value in self.values.items():
            if subkey.endswith(suffix) and name == ("Release" if suffix == DOTNET_SUBKEY else "pv"):
                return value
        return None


def _good(**overrides: object) -> FakeRegistry:
    values: dict[str, str] = {
        DOTNET_SUBKEY: "533325",  # .NET Framework 4.8
        RUNTIME_GUID: "157.0.4341.42",
    }
    values.update(overrides)  # type: ignore[arg-type]
    return FakeRegistry(values)


# ------------------------------------------------------------------ 版本解析


def test_parse_version_handles_real_and_broken_values() -> None:
    assert parse_version("157.0.4341.42") == (157, 0, 4341, 42)
    assert parse_version("86.0.622.0") == (86, 0, 622, 0)
    assert parse_version("0") == (0,)
    assert parse_version("") is None
    assert parse_version(None) is None
    # 尾部有脏字符时只取能解析的前缀，不抛异常
    assert parse_version("120.0.1-beta") == (120, 0, 1)
    assert parse_version("abc") is None


def test_major_version() -> None:
    assert major_version("157.0.4341.42") == 157
    assert major_version(None) is None


# ------------------------------------------------------------------ 正常机器


def test_chromium_when_runtime_and_dotnet_are_present() -> None:
    report = inspect_engine(_good())
    assert report.chromium is True
    assert report.engine == "chromium"
    assert report.webview2_version == "157.0.4341.42"
    assert report.dotnet_release == 533325
    assert report.problems == ()
    assert "chromium" in report.summary()


def test_beta_channel_also_counts() -> None:
    """WebView2 的 Beta/Dev/Canary 与正式版等价（pywebview 也认这四个 GUID）。"""
    read = FakeRegistry({DOTNET_SUBKEY: "533325", MINOR_CLIENT_GUID: "160.0.100.0"})
    report = inspect_engine(read)
    assert report.chromium is True
    assert report.webview2_version == "160.0.100.0"


def test_explicit_runtime_path_bypasses_registry() -> None:
    """``WEBVIEW2_RUNTIME_PATH`` 指定了目录时 pywebview 不看注册表。"""
    report = inspect_engine(FakeRegistry({}), webview2_path=r"C:\WebView2")
    assert report.chromium is True


# ------------------------------------------------------------------ 故障机器


def test_mshtml_when_runtime_missing() -> None:
    """这是老 Win10 最常见的样子：没装 Evergreen 运行时。"""
    report = inspect_engine(FakeRegistry({DOTNET_SUBKEY: "533325"}))
    assert report.chromium is False
    assert report.engine == "mshtml"
    assert report.webview2_version is None
    assert any("WebView2" in item for item in report.problems)


def test_mshtml_when_dotnet_too_old() -> None:
    """Windows 10 1507/1516 自带 .NET 4.6：即使装了 WebView2 也会退回 IE。"""
    report = inspect_engine(_good(**{DOTNET_SUBKEY: "393295"}))  # 4.6.1
    assert report.chromium is False
    assert any("4.6.2" in item for item in report.problems)


def test_mshtml_when_dotnet_key_missing() -> None:
    report = inspect_engine(
        FakeRegistry({RUNTIME_GUID: "157.0.4341.42"})
    )
    assert report.chromium is False
    assert report.dotnet_release is None
    assert any(".NET" in item for item in report.problems)


def test_version_gate_is_major_only_like_pywebview() -> None:
    """pywebview 实际只比主版本；这里必须一致，否则会拒掉本来能用的机器。"""
    assert inspect_engine(_good(**{RUNTIME_GUID: f"{MIN_WEBVIEW2_MAJOR}.0.622.0"})).chromium is True
    assert inspect_engine(_good(**{RUNTIME_GUID: f"{MIN_WEBVIEW2_MAJOR}.0.1.0"})).chromium is True
    older = MIN_WEBVIEW2_MAJOR - 1
    too_old = inspect_engine(_good(**{RUNTIME_GUID: f"{older}.0.9999.0"}))
    assert too_old.chromium is False
    assert any("太旧" in item for item in too_old.problems)


def test_garbage_registry_value_is_treated_as_missing() -> None:
    report = inspect_engine(_good(**{RUNTIME_GUID: "not-a-version"}))
    assert report.chromium is False
    assert any("WebView2" in item for item in report.problems)


# ------------------------------------------------------------------ 提示文案


def test_message_explains_what_to_install() -> None:
    text = inspect_engine(FakeRegistry({})).message()
    assert "Chromium" in text
    assert WEBVIEW2_DOWNLOAD_URL in text
    assert "--serve" in text  # 浏览器模式的替代办法也要给出来
    assert "app.log" in text  # 指向日志，便于进一步排查
    # 不能中文乱码、不能是空串
    assert len(text) > 200


def test_summary_and_dict_are_log_friendly() -> None:
    report = inspect_engine(_good())
    payload = report.to_dict()
    assert payload["engine"] == "chromium"
    assert payload["problems"] == []
    assert "WebView2" in report.summary()


def test_report_message_used_as_dialog_text_has_no_format_braces() -> None:
    """消息框用 ``MessageBoxW``：正文里出现 ``{}`` 不会被格式化，但也别引入迷惑内容。"""
    text = inspect_engine(FakeRegistry({DOTNET_SUBKEY: "533325"})).message()
    assert "{" not in text and "}" not in text


def test_min_dotnet_matches_pywebview() -> None:
    """394802 = .NET Framework 4.6.2，与 pywebview ``_is_chromium`` 同源。"""
    assert MIN_DOTNET_RELEASE == 394802
    assert winreg.HKEY_LOCAL_MACHINE  # 常量存在即可（避免导入被优化掉）


# ------------------------------------------------------------------ 入口行为


def _desktop_args(**overrides: object):
    import argparse

    base = {
        "settings": None,
        "port": 8765,
        "backend": False,
        "debug": False,
        "width": 1280,
        "height": 860,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_window_logging_writes_engine_line(tmp_path) -> None:
    """窗口进程也要落盘：以前内核回退/UAC 被拒都只走 stderr，日志里查不到。"""
    import logging

    import app.gui_web.main as entry

    logger = logging.getLogger("app")
    before = list(logger.handlers)
    try:
        target = entry.configure_window_logging(None, {"logDir": str(tmp_path), "logLevel": "info"})
        assert target is not None and target.is_file()
        logging.getLogger("app.gui_web.main").info("渲染内核检查通过：内核=chromium")
        for handler in logger.handlers:
            handler.flush()
        assert "渲染内核检查通过" in target.read_text(encoding="utf-8")
    finally:
        for handler in set(logger.handlers) - set(before):
            logger.removeHandler(handler)
            handler.close()


def test_run_desktop_refuses_to_open_window_on_ie_engine(monkeypatch) -> None:
    """内核不满足时必须“弹框 + 退出码 5”，不能给一个点不动的窗口。"""
    import sys
    import types

    import app.gui_web.main as entry
    from app.gui.webengine import EngineReport

    alerts: list[str] = []
    report = EngineReport(
        engine="mshtml",
        webview2_version=None,
        webview2_client=None,
        dotnet_release=393295,
        problems=("没有检测到 Microsoft Edge WebView2 运行时（需要主版本 ≥ 86）",),
    )
    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace())
    monkeypatch.setattr(entry, "read_settings", lambda _path: {})
    monkeypatch.setattr(entry, "configure_window_logging", lambda _path, _settings: None)
    monkeypatch.setattr(entry, "inspect_engine", lambda: report)
    monkeypatch.setattr(entry, "alert_blocking", lambda text, title="": alerts.append(text))
    monkeypatch.setattr(
        entry, "spawn_privileged_backend", lambda *_a, **_k: pytest.fail("不该拉起后端")
    )

    assert entry._run_desktop(_desktop_args()) == 5
    assert len(alerts) == 1
    assert "WebView2" in alerts[0]


def test_run_desktop_proceeds_when_engine_is_chromium(monkeypatch) -> None:
    """内核正常时继续往下走（这里停在拉起后端那一步，不建真实窗口）。"""
    import sys
    import types

    import app.gui_web.main as entry
    from app.gui.webengine import EngineReport

    called: list[object] = []
    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace())
    monkeypatch.setattr(entry, "read_settings", lambda _path: {})
    monkeypatch.setattr(entry, "configure_window_logging", lambda _path, _settings: None)
    monkeypatch.setattr(entry, "inspect_engine", lambda: inspect_engine(_good()))
    monkeypatch.setattr(entry, "alert_blocking", lambda *_a, **_k: pytest.fail("不该弹框"))
    monkeypatch.setattr(entry, "_check_port_free", lambda host, port: None)
    def _spawn(*_args, **_kwargs) -> bool:
        called.append("spawn")
        return True  # 走到“等提权后端就绪”那条分支，不建真实窗口

    monkeypatch.setattr(entry, "spawn_privileged_backend", _spawn)
    monkeypatch.setattr(entry, "_wait_url", lambda url, timeout=25.0: False)

    assert entry._run_desktop(_desktop_args()) == 1  # 后端没就绪 → 1，而不是 5
    assert called == ["spawn"]
    assert isinstance(EngineReport("chromium", None, None, None, ()).engine, str)


def test_window_logger_survives_freezing() -> None:
    """冻结成 exe 后入口的 ``__name__`` 是 ``"__main__"``。

    用它取 logger 会落到 root（root 没有 handler），窗口侧辛苦加的日志一条都
    写不进 ``app.log`` —— 这个坑在 VM 上跑真实 exe 才暴露出来，所以这里同时
    钉住“不用 __name__”和“名字必须是 app 的子 logger”。
    """
    from pathlib import Path

    import app.gui_web.main as entry

    source = Path(entry.__file__).read_text(encoding="utf-8")
    assert "logging.getLogger(__name__)" not in source
    assert entry.LOG.name == "app.gui_web.main"
    assert entry.LOG.name.startswith("app.")
    assert entry.LOG.propagate is True


def test_window_log_reaches_app_file_handler(tmp_path) -> None:
    """``configure_window_logging`` + ``LOG`` 必须真的写进同一个 app.log。"""
    import logging

    import app.gui_web.main as entry

    logger = logging.getLogger("app")
    before = list(logger.handlers)
    before_level = logger.level
    try:
        target = entry.configure_window_logging(None, {"logDir": str(tmp_path), "logLevel": "info"})
        assert target is not None
        entry.LOG.info("窗口侧日志应当落到 app.log")
        for handler in logger.handlers:
            handler.flush()
        assert "窗口侧日志应当落到 app.log" in target.read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(before_level)
