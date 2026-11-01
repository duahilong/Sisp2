"""Web 视图入口：``python -m app.gui_web.main``。

与 ``app/gui/app.py`` 并列，互不影响；Sisp 的磁盘逻辑一行不改。

装配方式与 ``MainWindow`` 保持一致：
``WorkflowController()`` 零参构造，内部会用默认值造出
``SispService()`` / ``JobManager(event_dir=None)`` / ``run_preflight_checks``。

P0 补丁：设置存储在这里注入（``SettingsStore``），默认落在
``%APPDATA%\\Sisp\\settings.json``；传 ``--settings <path>`` 可改位置。
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from pathlib import Path

from app.gui.elevation import is_admin

MODULE = "app.gui_web"
UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
SW_SHOWNORMAL = 1


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


def _shim_script() -> str:
    """浏览器模式下注入的 pywebview 兼容层：window.pywebview.api -> HTTP。"""
    return """<script>
(function () {
  if (window.pywebview) { return; }
  function invoke(name, args) {
    return fetch('/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ args: args })
    }).then(function (r) { return r.json(); });
  }
  var api = new Proxy({}, {
    get: function (target, name) {
      if (name === 'then' || name === 'toJSON') { return undefined; }
      return function () { return invoke(String(name), Array.prototype.slice.call(arguments)); };
    }
  });
  window.pywebview = { api: api };
  window.addEventListener('DOMContentLoaded', function () {
    setTimeout(function () { window.dispatchEvent(new Event('pywebviewready')); }, 0);
  });
})();
</script>
"""


def _serve(api, port: int, host: str = "127.0.0.1") -> int:
    """浏览器模式：127.0.0.1:port 提供界面 + JSON API，用 Edge/Chromium 打开。"""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    html = UI_FILE.read_text(encoding="utf-8")
    shim = _shim_script()
    if "</head>" in html:
        html = html.replace("</head>", shim + "</head>", 1)
    else:
        html = shim + html
    index_bytes = html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "Sisp2Web/1.0"

        def log_message(self, fmt, *args):  # 安静一点，避免刷屏
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, index_bytes, "text/html; charset=utf-8")
            elif path == "/health":
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            else:
                self._send(404, b'{"ok":false,"error":{"code":"not-found","message":"not found"}}',
                           "application/json; charset=utf-8")

        def do_POST(self):
            name = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                payload = {}
            args = payload.get("args") if isinstance(payload, dict) else None
            if not isinstance(args, list):
                args = []
            method = getattr(api, name, None) if api is not None else None
            if not callable(method):
                body = {"ok": False, "error": {"code": "no-method", "message": f"后端未提供方法：{name}"}}
            else:
                try:
                    body = method(*args)
                except Exception as exc:  # noqa: BLE001 - 任何异常都回给界面
                    body = {"ok": False, "error": {"code": "backend-error", "message": str(exc)}}
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, data, "application/json; charset=utf-8")

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"界面已就绪：http://{host}:{port}/  （Ctrl+C 退出）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sisp2 Web 界面")
    parser.add_argument("-j", "--config", default=None, help="配置文件路径，默认 json/win11.json")
    parser.add_argument("--dry-run", action="store_true", help="演练：不真正分区/写盘")
    parser.add_argument("--mock", action="store_true", help="只显示界面，不连后端（用原型自带数据）")
    parser.add_argument("--elevate", action="store_true", help="需要时通过 UAC 提权重启")
    parser.add_argument("--debug", action="store_true", help="打开 devtools")
    parser.add_argument("--serve", action="store_true", help="浏览器模式：起本机 HTTP 服务，用 Edge/Chromium 打开界面（不依赖 pywebview）")
    parser.add_argument("--port", type=int, default=8765, help="浏览器模式端口，默认 8765")
    parser.add_argument("--host", default="127.0.0.1", help="浏览器模式监听地址，默认 127.0.0.1（测试机可改 0.0.0.0）")
    parser.add_argument("--settings", default=None, help="设置文件路径，默认 %%APPDATA%%\\Sisp\\settings.json")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=860)
    args = parser.parse_args(argv)

    if not UI_FILE.exists():
        print(f"找不到界面文件: {UI_FILE}", file=sys.stderr)
        print("先把原型复制为 app/gui_web/ui/index.html，见 ui/README.md", file=sys.stderr)
        return 2

    if args.elevate and not is_admin():
        if relaunch_as_admin(argv):
            return 0
        print("提权被取消，继续以普通权限运行（写盘会失败）", file=sys.stderr)

    api = None
    wf = None
    if not args.mock:
        api, wf = build_api(args.config, args.dry_run, args.settings)

    if args.serve:
        try:
            return _serve(api, args.port, args.host)
        finally:
            if api is not None:
                api.close()

    try:
        import webview  # type: ignore
    except ImportError:
        print("缺少依赖：pip install -r requirements-web.txt", file=sys.stderr)
        return 3

    webview.create_window(
        "硬盘工具箱",
        str(UI_FILE),
        js_api=api,
        width=args.width,
        height=args.height,
        min_size=(1024, 720),
    )
    try:
        webview.start(debug=args.debug)
    finally:
        if api is not None:
            api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
