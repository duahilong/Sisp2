"""Web 视图入口：``python -m app.gui_web.main``。

两种运行模式，**共用同一条通信链路**（HTTP + REST + SSE）：

- 桌面模式（默认）：后台线程起本机 HTTP 服务，pywebview 窗口直接加载
  ``http://127.0.0.1:<port>/``——不再注入 ``js_api``，前端一律走 fetch；
- 浏览器模式（``--serve``）：前台运行同一个服务，用 Edge/Chromium 打开。

装配方式与之前一致：``WorkflowController()`` 零参构造，内部会用默认值造出
``SispService()`` / ``JobManager(event_dir=None)`` / ``run_preflight_checks``。
设置存储在这里注入（``SettingsStore``），默认落在
``%APPDATA%\\Sisp\\settings.json``；传 ``--settings <path>`` 可改位置。
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from app.gui.elevation import is_admin
from app.resources import ui_path

MODULE = "app.gui_web"
UI_FILE = ui_path()
SW_SHOWNORMAL = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


def _ensure_std_streams() -> None:
    """窗口模式下 ``sys.stdout``/``sys.stderr`` 是 ``None``。

    PyInstaller 的 ``--windowed`` 程序没有控制台，任何 ``print(..., file=sys.stderr)``
    或 argparse 的出错提示都会直接抛 ``AttributeError``。这里换成空设备，
    让所有既有代码按“没有控制台”的方式安静工作；真正的诊断信息走 ``app.log``。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        try:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        except OSError:  # 极端情况下连空设备都打不开
            setattr(sys, name, io.StringIO())


def _level_of(name: object) -> int:
    return _LOG_LEVELS.get(str(name or "info").lower(), logging.INFO)


def configure_logging(log_dir: str | None, level: object) -> Path | None:
    """把 ``app`` 日志输出到控制台 + ``<logDir>/app.log``。

    界面设置里的 ``logDir`` / ``logLevel`` 就作用在这里：没有这两个设置
    之前，桌面进程的日志只去了 stderr，关掉窗口就什么都没了。
    """
    logger = logging.getLogger("app")
    logger.setLevel(_level_of(level))
    if not log_dir:
        return None
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "app.log"
    except OSError:
        return None

    for handler in list(logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            if Path(handler.baseFilename) == target.resolve():
                handler.setLevel(_level_of(level))
                return target
            logger.removeHandler(handler)
            handler.close()

    handler = logging.handlers.RotatingFileHandler(
        target,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(_level_of(level))
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    return target


def _window_state_path(settings_path: str | None) -> Path:
    """窗口几何信息跟设置文件放在同一目录（默认 ``%APPDATA%\\Sisp``）。"""
    try:
        from app.modules.settings.service import default_settings_path

        base = Path(settings_path) if settings_path else default_settings_path()
    except ImportError:
        base = Path(settings_path) if settings_path else Path.home() / ".config" / "Sisp" / "settings.json"
    return base.parent / "window.json"


def load_window_geometry(settings_path: str | None, settings: dict | None) -> dict[str, int] | None:
    """读回上次的窗口位置/大小；``rememberWindow`` 关掉时一律不恢复。"""
    if settings is not None and not bool(settings.get("rememberWindow", True)):
        return None
    path = _window_state_path(settings_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    geometry: dict[str, int] = {}
    for key in ("width", "height", "x", "y"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            geometry[key] = value
    if geometry.get("width", 0) < 800 or geometry.get("height", 0) < 600:
        return None
    return geometry


def save_window_geometry(settings_path: str | None, geometry: dict[str, int] | None) -> None:
    """保存窗口几何；写不了就算了，不影响退出。"""
    if not geometry:
        return
    path = _window_state_path(settings_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(geometry, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def relaunch_as_admin(argv: list[str] | None = None) -> bool:
    """通过 UAC 重新拉起 ``python -m app.gui_web``；失败返回 False。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        executable, params = sys.executable, subprocess.list2cmdline(arguments)
    else:
        executable, params = sys.executable, subprocess.list2cmdline(["-m", MODULE, *arguments])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, None, SW_SHOWNORMAL
        )
    except Exception:  # noqa: BLE001
        return False
    return result > 32


def build_api(config_path: str | None, dry_run: bool, settings_path: str | None = None):
    """构造 WorkflowController + WebApi，并立刻建立会话。"""
    from app.application.workflow import WorkflowController
    from app.modules.settings.service import build_store
    from app.gui_web.api import WebApi

    store = build_store(settings_path)
    settings = store.get()
    # 界面里的「演练模式」开关优先于命令行参数
    effective_dry_run = bool(dry_run) or bool(settings.get("dryRunByDefault"))

    log_dir = str(settings.get("logDir") or "").strip() or None
    level = str(settings.get("logLevel", "info")).lower()
    configure_logging(log_dir, level)
    if log_dir:
        from app.application.real_runner import prune_runner_logs

        prune_runner_logs(log_dir, int(settings.get("logRetentionDays", 30)))
    wf = WorkflowController(
        dry_run=effective_dry_run,
        log_dir=log_dir,
        partition_defaults=store.partition_info(),
        tool_paths={
            "diskpart": settings.get("extDiskpart"),
            "dism": settings.get("extDism"),
        },
        protect_system_disk=bool(settings.get("protectSystemDisk", True)),
        block_unverified=bool(settings.get("blockUnverified", True)),
        type_to_confirm=bool(settings.get("typeToConfirm", True)),
        # 预检与执行器可选项都从设置里取最新值（改完设置不用重建控制器）。
        settings_provider=store.get,
    )
    wf.start(config_path, dry_run=effective_dry_run)
    return WebApi(wf, settings=store), wf


def _check_port_free(host: str, port: int) -> str | None:
    """端口已被占用时返回错误文本，空闲返回 None。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        return f"端口 {port} 已被占用：{exc}。可用 --port 换一个端口。"
    finally:
        sock.close()
    return None


def _make_server(app, host: str, port: int):
    """构造 uvicorn Server（单进程单 worker，任务状态在内存里，不能多开）。"""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)


def _wait_until_ready(url: str, server, timeout: float = 10.0) -> bool:
    """轮询 /health 直到服务就绪或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:  # noqa: S310 - 本机回环地址
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.15)
    return False


def _serve(app, host: str, port: int) -> int:
    """浏览器模式：前台运行 HTTP 服务，用 Edge/Chromium 打开界面。"""
    server = _make_server(app, host, port)
    print(f"界面已就绪：http://{host}:{port}/  （Ctrl+C 退出）", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


def _serve_in_thread(app, host: str, port: int):
    """桌面模式：后台线程运行 HTTP 服务，返回 (server, thread)。"""
    server = _make_server(app, host, port)
    thread = threading.Thread(target=server.run, name="sisp-web-server", daemon=True)
    thread.start()
    return server, thread


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()
    parser = argparse.ArgumentParser(description="Sisp2 Web 界面")
    parser.add_argument("-j", "--config", default=None, help="配置文件路径，默认 json/win11.json")
    parser.add_argument("--dry-run", action="store_true", help="演练：不真正分区/写盘")
    parser.add_argument("--elevate", action="store_true", help="需要时通过 UAC 提权重启")
    parser.add_argument("--debug", action="store_true", help="打开 devtools")
    parser.add_argument("--serve", action="store_true", help="浏览器模式：前台起本机 HTTP 服务，用 Edge/Chromium 打开界面（不依赖 pywebview）")
    parser.add_argument("--port", type=int, default=8765, help="服务端口，默认 8765")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（改 0.0.0.0 会把写盘接口暴露给局域网，慎用）")
    parser.add_argument("--settings", default=None, help="设置文件路径，默认 %%APPDATA%%\\Sisp\\settings.json")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=860)
    args = parser.parse_args(argv)

    if not UI_FILE.exists():
        print(f"找不到界面文件: {UI_FILE}", file=sys.stderr)
        return 2

    if args.elevate and not is_admin():
        if relaunch_as_admin(argv):
            return 0
        print("提权被取消，继续以普通权限运行（写盘会失败）", file=sys.stderr)

    api, wf = build_api(args.config, args.dry_run, args.settings)

    # ---------------------------------------------------------- 服务装配
    from app.gui_web.server import create_app

    host_check = args.host in _LOOPBACK_HOSTS
    app = create_app(api, wf, host_check=host_check)

    if not host_check:
        print(
            f"警告：服务监听在 {args.host}，写盘接口对网络可达；"
            "当前没有鉴权，请勿在不可信网络里这样运行。",
            file=sys.stderr,
        )

    port_error = _check_port_free(args.host, args.port)
    if port_error:
        print(port_error, file=sys.stderr)
        api.close()
        return 4

    # ---------------------------------------------------------- 浏览器模式
    if args.serve:
        try:
            return _serve(app, args.host, args.port)
        finally:
            api.close()

    # ---------------------------------------------------------- 桌面模式
    try:
        import webview  # type: ignore
    except ImportError:
        print("缺少依赖：pip install -r requirements-web.txt", file=sys.stderr)
        api.close()
        return 3

    server, _thread = _serve_in_thread(app, args.host, args.port)
    if not _wait_until_ready(f"http://{args.host}:{args.port}/health", server):
        print("本机 HTTP 服务启动超时，无法打开窗口。", file=sys.stderr)
        server.should_exit = True
        api.close()
        return 1
    url = f"http://{args.host}:{args.port}/"

    geometry = load_window_geometry(args.settings, api.settings_data()) or {}
    window = webview.create_window(
        "硬盘工具箱",
        url,
        width=int(geometry.get("width") or args.width),
        height=int(geometry.get("height") or args.height),
        x=geometry.get("x"),
        y=geometry.get("y"),
        min_size=(1024, 720),
    )

    def _remember_geometry(*_args, **_kwargs) -> None:
        """关窗时保存位置/大小（只受 rememberWindow 控制）。"""
        try:
            if not bool(api.settings_data().get("rememberWindow", True)):
                return
            save_window_geometry(
                args.settings,
                {
                    "width": int(window.width),
                    "height": int(window.height),
                    "x": int(window.x),
                    "y": int(window.y),
                },
            )
        except Exception:  # noqa: BLE001 - 记不住窗口位置不能影响退出
            pass

    try:
        window.events.closing += _remember_geometry
    except Exception:  # noqa: BLE001 - 旧版 pywebview 没有 events 挂钩
        pass

    try:
        webview.start(debug=args.debug)
    finally:
        _remember_geometry()
        server.should_exit = True
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
