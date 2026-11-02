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
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from app.gui.elevation import is_admin

MODULE = "app.gui_web"
UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
SW_SHOWNORMAL = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
    # 界面里的「演练模式」开关优先于命令行参数
    effective_dry_run = bool(dry_run) or bool(store.get().get("dryRunByDefault"))

    wf = WorkflowController(dry_run=effective_dry_run)
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
    parser = argparse.ArgumentParser(description="Sisp2 Web 界面")
    parser.add_argument("-j", "--config", default=None, help="配置文件路径，默认 json/win11.json")
    parser.add_argument("--dry-run", action="store_true", help="演练：不真正分区/写盘")
    parser.add_argument("--mock", action="store_true", help="只显示界面，不连后端（用原型自带数据）")
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

    api = None
    wf = None
    if not args.mock:
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
        if api is not None:
            api.close()
        return 4

    # ---------------------------------------------------------- 浏览器模式
    if args.serve:
        try:
            return _serve(app, args.host, args.port)
        finally:
            if api is not None:
                api.close()

    # ---------------------------------------------------------- 桌面模式
    try:
        import webview  # type: ignore
    except ImportError:
        print("缺少依赖：pip install -r requirements-web.txt", file=sys.stderr)
        if api is not None:
            api.close()
        return 3

    if args.mock:
        # mock：没有后端，直接加载本地文件，界面走原型自带数据。
        url = str(UI_FILE)
        server = None
    else:
        server, _thread = _serve_in_thread(app, args.host, args.port)
        if not _wait_until_ready(f"http://{args.host}:{args.port}/health", server):
            print("本机 HTTP 服务启动超时，无法打开窗口。", file=sys.stderr)
            server.should_exit = True
            if api is not None:
                api.close()
            return 1
        url = f"http://{args.host}:{args.port}/"

    webview.create_window(
        "硬盘工具箱",
        url,
        width=args.width,
        height=args.height,
        min_size=(1024, 720),
    )
    try:
        webview.start(debug=args.debug)
    finally:
        if server is not None:
            server.should_exit = True
        if api is not None:
            api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
