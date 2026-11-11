"""Web 视图入口：``python -m app.gui_web.main``。

两种运行模式，**共用同一条通信链路**（HTTP + REST + SSE）：

- 桌面模式（默认）：**窗口进程不提权**，它会用 UAC 拉起一个提权后端子进程
  （``--backend --serve --parent-pid <本进程 pid>``），窗口加载
  ``http://127.0.0.1:<port>/``——不再注入 ``js_api``，前端一律走 fetch；
- 浏览器模式（``--serve``）：前台运行同一个服务，用 Edge/Chromium 打开。

为什么窗口进程不能提权：WebView2 的运行时不支持跑在高完整性（High IL）进程里，
它必须把 ``msedgewebview2.exe`` 降到 Medium IL 启动，而这条降权路径要借
``explorer.exe`` 代劳（见 WebView2Feedback #2984 与微软 “Develop secure WebView2
apps”）。以前 exe 清单写 ``requireAdministrator``，于是 Windows 上偶尔会直接崩成
``msedgewebview2 has stopped working`` + ``Error launching CrashSender.exe``。
现在清单是 ``asInvoker``：界面在普通权限下跑，需要管理员的写盘动作全在提权后端里。

装配方式与之前一致：``WorkflowController()`` 零参构造，内部会用默认值造出
``SispService()`` / ``JobManager()`` / ``run_preflight_checks``。
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
from app.gui.webengine import inspect_engine
from app.gui_web.single_instance import (
    EXIT_ALREADY_RUNNING,
    SingleInstance,
    already_running_message,
)
from app.resources import ui_path

MODULE = "app.gui_web"
# 冻结成 exe 之后入口脚本的 __name__ 是 "__main__"，用它取 logger 会落到 root
# （root 没有 handler），窗口侧的日志就一条都写不进 app.log。这里写死名字，
# 和 configure_logging() 挂在 "app" 上的文件 handler 对齐。
LOG = logging.getLogger("app.gui_web.main")
UI_FILE = ui_path()
SW_SHOWNORMAL = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 等父进程退出用的最小权限：只用 SYNCHRONIZE，不碰它的内存/句柄表
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
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
            # 进程退出才需要关闭：这里替换的是 sys.stdout/stderr 本身
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
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


def relaunch_as_admin(argv: list[str] | None = None, *, n_show: int = SW_SHOWNORMAL) -> bool:
    """通过 UAC 重新拉起 ``python -m app.gui_web``；失败返回 False。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        executable, params = sys.executable, subprocess.list2cmdline(arguments)
    else:
        executable, params = sys.executable, subprocess.list2cmdline(["-m", MODULE, *arguments])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, None, int(n_show)
        )
    except Exception:  # noqa: BLE001
        return False
    return result > 32


def backend_argv(args, port: int, parent_pid: int) -> list[str]:
    """提权后端子进程的命令行。

    界面把用户选的设置/调试开关原样传下去，所以“界面里看到的计划”和
    “后端执行用的参数”是同一份——没有两套启动条件。
    """
    argv = [
        "--backend",
        "--serve",
        "--host", "127.0.0.1",
        "--port", str(int(port)),
        "--parent-pid", str(int(parent_pid)),
    ]
    if args.settings:
        argv += ["--settings", str(args.settings)]
    if args.debug:
        argv.append("--debug")
    return argv


def spawn_privileged_backend(args, port: int) -> bool:
    """UAC 拉起提权后端，返回是否成功启动（用户拒绝 / 调用失败返回 False）。

    ``SW_HIDE`` 让子进程不弹自己的窗口：它是纯后端，界面在调用方这边。
    """
    return relaunch_as_admin(backend_argv(args, port, os.getpid()), n_show=0)


def _parent_alive(pid: int) -> bool:
    """父进程（界面）是否还活着。

    自己就是父进程号或拿不到句柄一律当“已经没了”：宁可让后端早退，
    也不要留下一个没人管的提权服务。
    """
    pid = int(pid)
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    except Exception:  # noqa: BLE001 - 非 Windows / 受限环境
        return False
    if not handle:
        return False
    try:
        # WAIT_OBJECT_0 == 0 表示已经结束；WAIT_TIMEOUT 表示还活着
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def watch_parent(parent_pid: int, on_exit, interval: float = 0.5) -> threading.Thread:
    """盯着父进程，它一退出就回调（用来让提权后端跟着退出）。

    界面进程没有权限结束一个提权进程（``PROCESS_TERMINATE`` 拿不到），所以
    清理只能由后端自己完成：父进程没了就自己退。这样即使界面被强杀、
    崩掉，也不会在后台留一个持管理员令牌的 HTTP 服务。
    """
    def _loop() -> None:
        while _parent_alive(parent_pid):
            time.sleep(interval)
        try:
            on_exit()
        except Exception:  # noqa: BLE001, S110 - 回调出错不能让线程崩在日志里
            pass

    thread = threading.Thread(target=_loop, name="sisp-parent-watch", daemon=True)
    thread.start()
    return thread


def configure_window_logging(settings_path: str | None, settings: dict | None) -> Path | None:
    """窗口进程也把日志写进 ``app.log``。

    以前只有 ``build_api()`` 里配日志，而桌面模式下业务都在提权后端里跑，
    窗口进程一句日志都没有：内核回退、UAC 被拒、服务起不来，全都只走 stderr，
    而打包产物 ``console=False``，stderr 直接进空设备——用户拿到的只有
    “窗口打开了但连不上”，日志目录里什么都没有。这里让窗口侧也落一份盘。

    两个进程写同一个文件是刻意的：用户点的“打开日志目录”只有一个 ``app.log``。
    窗口侧每次启动只写几行，滚动切分的概率可以忽略。
    """
    data = settings or {}
    log_dir = str(data.get("logDir") or "").strip()
    if not log_dir:
        try:
            from app.application.command_log import default_log_dir

            log_dir = str(default_log_dir())
        except Exception:  # noqa: BLE001 - 拿不到默认目录就不写文件，不阻塞开窗
            return None
    return configure_logging(log_dir, data.get("logLevel", "info"))


def alert_blocking(message: str, title: str = "硬盘工具箱") -> None:
    """模态消息框：窗口打不开时必须让用户看见原因，而不是静默退出。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x00040000)
    except Exception:  # noqa: BLE001, S110 - 弹不出框也不能让进程崩在入口
        pass


def read_settings(settings_path: str | None) -> dict:
    """窗口进程只读设置里跟自己有关的几项（窗口几何、rememberWindow）。

    注意这里**不建 WorkflowController**：桌面模式下所有业务状态都在提权后端里，
    窗口进程不该另开一份会话。
    """
    try:
        from app.modules.settings.service import build_store

        return dict(build_store(settings_path).get() or {})
    except Exception:  # noqa: BLE001 - 设置读不出来不能挡住开窗口
        return {}


def webview_storage_path() -> str | None:
    """给 WebView2 一个窗口进程自己可写的 user data folder。

    默认（``private_mode``）pywebview 会把 UDF 放在 %TEMP% 下，而 ``%TEMP%`` 被
    清理/重定向、或把多份 pywebview 应用的缓存指到一起时，crashpad（``CrashSender``）
    起不来就会弹“Error launching CrashSender.exe”。放到 ``%LOCALAPPDATA%\\Sisp\\WebView2``
    里既稳定又不会跟别的 pywebview 应用抢同一个目录。
    """
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Sisp" / "WebView2"
        base.mkdir(parents=True, exist_ok=True)
        return str(base)
    except OSError:
        return None


def build_api(settings_path: str | None = None):
    """构造 WorkflowController + WebApi，并在后台把会话热起来。

    返回值是 ``(api, wf)``。注意：会话（预检 + 扫盘）不再在这里同步跑完——
    同步跑会让 uvicorn 晚一秒多才 bind，界面就要多等一秒多才看得到窗口。
    """
    from app.application.workflow import WorkflowController
    from app.gui_web.api import WebApi
    from app.modules.settings.service import build_store

    store = build_store(settings_path)
    settings = store.get()

    # 设置里留空时也要有日志目录：否则任务跑完一份 disk-runner-*.log 都没有，
    # 界面上的日志面板永远是空的（%APPDATA%\\Sisp\\logs）。
    log_dir = str(settings.get("logDir") or "").strip()
    if not log_dir:
        from app.application.command_log import default_log_dir

        log_dir = str(default_log_dir())
    level = str(settings.get("logLevel", "info")).lower()
    configure_logging(log_dir, level)
    from app.application.real_runner import prune_runner_logs

    prune_runner_logs(log_dir, int(settings.get("logRetentionDays", 30)))
    wf = WorkflowController(
        log_dir=log_dir,
        partition_defaults=store.partition_info(),
        tool_paths={
            "ghost": settings.get("extGhost"),
            "bcdboot": settings.get("extBcdboot"),
        },
        protect_system_disk=bool(settings.get("protectSystemDisk", True)),
        # 预检与执行器可选项都从设置里取最新值（改完设置不用重建控制器）。
        settings_provider=store.get,
    )
    api = WebApi(wf, settings=store, log_dir=log_dir)
    # 会话（运行前检查 + 扫盘，约 1 秒，主要是 PowerShell 冷启动）**在后台热身**：
    # 以前这里同步 wf.start()，结果是 uvicorn 要等扫描跑完才 bind，界面白白多等
    # 一秒多才看到窗口。现在 /health 立刻可用，界面先出骨架，扫描同时进行。
    _warm_session(api)
    return api, wf


def _warm_session(api) -> threading.Thread:
    """后台把会话跑起来（失败不抛：前端调 /api/session 时会重试并拿到真错误）。"""
    def _run() -> None:
        try:
            api.warm_session()
        except Exception as exc:  # noqa: BLE001 - 热身失败留给 /api/session 报错
            LOG.warning("会话预热失败（前端会重试）：%s", exc)

    thread = threading.Thread(target=_run, name="sisp-session-warm", daemon=True)
    thread.start()
    return thread


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


def _wait_until_ready(url: str, timeout: float = 10.0) -> bool:
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


def _wait_url(url: str, timeout: float = 25.0) -> bool:
    """轮询 ``/health`` 直到 200 或超时（提权子进程要重新解包一次 onefile，慢）。"""
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


def _serve(app, host: str, port: int, parent_pid: int | None = None) -> int:
    """浏览器模式：前台运行 HTTP 服务，用 Edge/Chromium 打开界面。

    ``parent_pid`` 非空时（被界面以 UAC 拉起的后端），父进程一退出就跟着退出。
    """
    server = _make_server(app, host, port)
    if parent_pid:
        watch_parent(parent_pid, lambda: setattr(server, "should_exit", True))
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


def _run_serve(args) -> int:
    """浏览器模式（也用于被界面拉起的提权后端）。"""
    api, wf = build_api(args.settings)

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

    try:
        return _serve(app, args.host, args.port, parent_pid=args.parent_pid)
    finally:
        api.close()


def _run_desktop(args) -> int:
    """桌面模式：窗口进程不提权，写盘能力在 UAC 拉起的后端子进程里。

    退出码：3 = 缺 pywebview；4 = 端口被占；5 = 渲染内核不满足（WebView2/.NET）。
    """
    try:
        import webview  # type: ignore
    except ImportError:
        print("缺少依赖：pip install -r requirements-web.txt", file=sys.stderr)
        return 3

    settings = read_settings(args.settings)
    log_file = configure_window_logging(args.settings, settings)
    log = LOG
    if log_file is not None:
        log.info("界面进程启动，日志文件：%s", log_file)

    # 内核必须是 Chromium：退回 IE（MSHTML）时界面脚本一行都执行不了，
    # 用户看到的是“窗口开了但连不上”，没有任何提示。宁可明确拒绝开窗。
    engine = inspect_engine()
    if engine.chromium:
        log.info("渲染内核检查通过：%s", engine.summary())
    else:
        log.error("渲染内核不满足，拒绝打开窗口：%s", "; ".join(engine.problems))
        print(engine.message(), file=sys.stderr)
        alert_blocking(engine.message())
        return 5

    port_error = _check_port_free("127.0.0.1", args.port)
    if port_error:
        log.error("端口不可用：%s", port_error)
        print(port_error, file=sys.stderr)
        return 4

    url = f"http://127.0.0.1:{args.port}/"
    health = f"{url}health"
    api = None
    server = None
    try:
        # “界面”和“能写盘的进程”分开：WebView2 只在普通权限进程里跑。
        if not args.backend and spawn_privileged_backend(args, args.port):
            log.info("已拉起提权后端，等待 /health 就绪…")
            print("已拉起提权后端，等待就绪…", flush=True)
            if not _wait_url(health):
                log.error("提权后端启动超时（%s 没等到 /health）", health)
                print("提权后端启动超时（没等到 /health）。", file=sys.stderr)
                return 1
        else:
            # 提权被取消：退回进程内服务，界面照常打开；写盘会被预检拦住。
            log.warning("提权被取消或不可用：退回普通权限的进程内服务，真实写盘会被预检拒绝")
            print("提权被取消：以普通权限运行，真实写盘会被预检拒绝。", file=sys.stderr)
            api, wf = build_api(args.settings)
            from app.gui_web.server import create_app

            app = create_app(api, wf, host_check=True)
            server, _thread = _serve_in_thread(app, "127.0.0.1", args.port)
            if not _wait_until_ready(health):
                log.error("本机 HTTP 服务启动超时（%s），无法打开窗口", health)
                print("本机 HTTP 服务启动超时，无法打开窗口。", file=sys.stderr)
                return 1

        log.info("打开窗口：%s", url)
        geometry = load_window_geometry(args.settings, settings) or {}
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
                # 现读一次设置：用户可能在本次运行里刚刚关掉 rememberWindow
                if not bool(read_settings(args.settings).get("rememberWindow", True)):
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
            except Exception:  # noqa: BLE001, S110 - 记不住窗口位置不能影响退出
                pass

        try:
            window.events.closing += _remember_geometry
        except Exception:  # noqa: BLE001, S110 - 旧版 pywebview 没有 events 挂钩
            pass

        try:
            webview.start(debug=args.debug, storage_path=webview_storage_path())
        finally:
            log.info("界面进程退出")
            _remember_geometry()
    finally:
        # 后端是提权进程，界面这边结束不了它（拿不到 PROCESS_TERMINATE），
        # 它会由父进程 watchdog 自己退；这里只清理自己名下的服务。
        if server is not None:
            server.should_exit = True
        if api is not None:
            api.close()
    return 0


#: 进程级的门闩对象：同一个进程里多次调用 ``main()``（测试、重启逻辑）不算多开。
_INSTANCE_LOCK: SingleInstance | None = None


def _instance_lock() -> SingleInstance:
    global _INSTANCE_LOCK  # noqa: PLW0603 - 进程级单例，就是这个用途
    if _INSTANCE_LOCK is None:
        _INSTANCE_LOCK = SingleInstance()
    return _INSTANCE_LOCK


def acquire_single_instance(args, *, lock=None, alert=None, say=None) -> int | None:
    """单实例门闩：返回 ``None`` = 可以继续；返回数字 = 直接以该退出码结束。

    为什么"只有用户面前的那个进程"拿锁：提权后端（``--backend``）是本实例的
    一部分，让它也去抢锁会把自己锁在外面。提权重启时父子进程有极短的交接
    窗口，所以 ``--elevate`` 路径上给几秒钟等父进程放手。
    """
    if getattr(args, "backend", False) or getattr(args, "allow_multiple", False):
        return None
    lock = lock or _instance_lock()
    if lock.acquire(wait=3.0 if getattr(args, "elevate", False) else 0.0):
        return None
    activated = False
    try:
        activated = bool(lock.activate_window())
    except Exception:  # noqa: BLE001 - 叫不动已有的窗口不影响后续提示
        activated = False
    message = already_running_message()
    if activated:
        message = message.replace(
            "· 已经打开的窗口应该已经切到前台；", "· 已经把已有的窗口切到前台；"
        )
    (say or _say_stderr)("硬盘工具箱已经在运行了（只允许一个实例）。")
    if not getattr(args, "serve", False):
        (alert or alert_blocking)(message)
    return EXIT_ALREADY_RUNNING


def _say_stderr(text: str) -> None:
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001, S110 - 没有 stderr（窗口模式）也不能崩
        pass


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()
    parser = argparse.ArgumentParser(description="Sisp2 Web 界面")
    parser.add_argument("--elevate", action="store_true", help="需要时通过 UAC 提权重启（主要给 --serve 用）")
    parser.add_argument("--debug", action="store_true", help="打开 devtools")
    parser.add_argument("--serve", action="store_true", help="浏览器模式：前台起本机 HTTP 服务，用 Edge/Chromium 打开界面（不依赖 pywebview）")
    parser.add_argument("--port", type=int, default=8765, help="服务端口，默认 8765")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（改 0.0.0.0 会把写盘接口暴露给局域网，慎用）")
    parser.add_argument("--settings", default=None, help="设置文件路径，默认 %%APPDATA%%\\Sisp\\settings.json")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="允许多开（排障/自动化测试用；正常使用不要加，两个实例会同时扫盘并抢同一块盘）",
    )
    # 下面两个是内部参数：界面给自己拉起的提权后端用，不对外。
    parser.add_argument("--backend", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not UI_FILE.exists():
        print(f"找不到界面文件: {UI_FILE}", file=sys.stderr)
        return 2

    if args.elevate and args.serve and not is_admin():
        if relaunch_as_admin(argv):
            return 0
        print("提权被取消，继续以普通权限运行（写盘会失败）", file=sys.stderr)

    blocked = acquire_single_instance(args)
    if blocked is not None:
        return blocked

    if args.serve:
        return _run_serve(args)
    return _run_desktop(args)


if __name__ == "__main__":
    raise SystemExit(main())
