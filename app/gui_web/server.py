"""REST + SSE 服务层：把 ``WebApi`` 平移成 HTTP 接口。

职责边界（与 ``api.py`` 一致）：本模块只做「HTTP 协议 ↔ WebApi 方法」的
转换，不写任何磁盘、PowerShell 或业务判断逻辑。所有响应沿用
``WebApi`` 的信封：``{"ok": True, "data": ...}`` / ``{"ok": False, "error": ...}``。

三类端点：

- **REST 管操作**：``POST /api/session``、``GET /api/disks``、
  ``POST /api/prepare`` / ``submit`` / ``confirm`` / ``cancel``、设置读写等；
- **SSE 管事件推送**：``GET /api/events`` 把 ``JobManager.subscribe()`` 的
  事件流推给前端，替代原先的 1.2 秒轮询。断线重连靠 SSE 标准的
  ``Last-Event-ID``（即 ``JobEvent.seq``）+ ``poll_events`` 的游标缓冲补发；
- **静态托管**：``GET /`` 返回 ``ui/index.html``，前后端同源，不需要 CORS。

两个刻意的实现选择：

- 业务路由全部用**同步 ``def``** 声明：FastAPI 会自动把它们放进线程池，
  不会用 60 秒的 PowerShell 扫描卡住事件循环（SSE 心跳不受影响）；
- **Host 头校验**：默认只接受 loopback 主机名，防 DNS rebinding——
  本机 HTTP 服务的经典攻击面。测试或非 loopback 绑定可显式关闭。

部署约束：必须**单进程单 worker** 运行（``JobManager`` 的任务状态在内存里，
多 worker 会把状态撕开）。``main.py`` 里已写死。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

_LOGGER = logging.getLogger(__name__)

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"

# Host 头里允许出现的主机名（不含端口）。
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# SSE：单个连接的事件队列上限；极端情况下满队丢弃新事件，
# 前端仍可用 POST /api/events/poll 按游标补齐。
_SSE_QUEUE_SIZE = 2000
_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_BACKLOG_LIMIT = 1000


def _sse_frame(data: dict[str, Any], *, event_id: int | None = None) -> str:
    """把一份载荷编码成 SSE 帧；``id`` 供浏览器重连时回传 Last-Event-ID。"""
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def create_app(
    api: Any,
    wf: Any,
    *,
    host_check: bool = True,
) -> FastAPI:
    """装配 FastAPI 应用。

    ``api`` 是 ``WebApi`` 实例，``wf`` 是对应的 ``WorkflowController``；
    ``host_check`` 为 True 时拒绝非 loopback 的 Host 头（TestClient 等场景应传 False）。
    """
    app = FastAPI(title="Sisp2 Web API", docs_url="/docs", redoc_url=None)

    # ------------------------------------------------------------- 安全
    if host_check:

        @app.middleware("http")
        async def check_host_header(request: Request, call_next):  # noqa: ANN001, ANN202
            host = request.headers.get("host", "")
            # 去掉端口；IPv6 形如 [::1]:8765。
            hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]", 1)[0] + "]"
            hostname = hostname.strip().lower()
            if hostname and hostname not in LOOPBACK_HOSTNAMES:
                return JSONResponse(
                    {"ok": False, "error": {"code": "E_HOST", "message": "非法 Host 头"}},
                    status_code=403,
                )
            return await call_next(request)

    # ------------------------------------------------------------- 静态
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/info")
    def app_info() -> dict[str, Any]:
        return api.app_info()

    # ------------------------------------------------------------- 会话与磁盘
    @app.post("/api/session")
    def start_session(body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        return api.start_session(body.get("config_path"), bool(body.get("dry_run")))

    @app.get("/api/disks")
    def refresh_disks() -> dict[str, Any]:
        return api.refresh_disks()

    @app.post("/api/config/check")
    def check_config(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return api.check_config((body or {}).get("config_path"))

    # ------------------------------------------------------------- 计划与任务
    @app.post("/api/prepare")
    def prepare(body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        return api.prepare(body.get("selection"), body.get("dry_run"))

    @app.post("/api/submit")
    def submit() -> dict[str, Any]:
        return api.submit()

    @app.post("/api/confirm")
    def confirm(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return api.confirm((body or {}).get("job_ids"))

    @app.post("/api/cancel")
    def cancel(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return api.cancel((body or {}).get("job_id"))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return api.get_job(job_id)

    # ------------------------------------------------------------- 事件：轮询兜底
    @app.post("/api/events/poll")
    def poll_events(body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        return api.poll_events(int(body.get("cursor") or 0), int(body.get("limit") or 300))

    # ------------------------------------------------------------- 事件：SSE 推送
    @app.get("/api/events")
    async def events(request: Request, cursor: int = 0) -> StreamingResponse:
        """SSE 事件流。

        首帧是 ``snapshot``（当前游标之后的事件补发 + 任务列表），随后每个
        ``JobEvent`` 推一帧，帧载荷::

            {"cursor": <seq>, "event": {...}, "jobs": [...]}

        浏览器自动重连时带 ``Last-Event-ID``（上一帧的 seq），服务端据此
        从游标缓冲补发断线期间的事件。空闲时每 15 秒发一行 ``: ping`` 心跳。
        """
        if wf is None:
            raise ValueError("create_app() 需要 WorkflowController 实例")

        last_event_id = request.headers.get("last-event-id") or ""
        if last_event_id.strip().isdigit():
            cursor = max(int(cursor), int(last_event_id.strip()))

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SSE_QUEUE_SIZE)

        def offer(item: dict[str, Any]) -> None:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                _LOGGER.warning("SSE 事件队列已满，丢弃事件 seq=%s", item.get("seq"))

        def on_event(event: Any) -> None:
            # 在 JobManager 的工作线程里被调用：只能做线程安全的投递。
            try:
                data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
            except Exception:  # noqa: BLE001 - 事件序列化失败不能拖垮工作线程
                return
            loop.call_soon_threadsafe(offer, data)

        unsubscribe = wf.job_manager.subscribe(on_event)

        def jobs_snapshot() -> list[dict[str, Any]]:
            return [job.to_dict() for job in wf.jobs()]

        async def stream():  # noqa: ANN202
            try:
                # 首帧：快照 + 断线期间积压的事件（复用 poll_events 的游标缓冲）。
                backlog = api.poll_events(cursor, _SSE_BACKLOG_LIMIT)["data"]
                yield _sse_frame(
                    {
                        "type": "snapshot",
                        "cursor": backlog.get("cursor", cursor),
                        "events": backlog.get("events", []),
                        "jobs": jobs_snapshot(),
                    }
                )
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=_SSE_HEARTBEAT_SECONDS
                        )
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    seq = item.get("seq")
                    yield _sse_frame(
                        {"cursor": seq, "event": item, "jobs": jobs_snapshot()},
                        event_id=seq if isinstance(seq, int) else None,
                    )
            finally:
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------- 设置
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return api.get_settings()

    @app.put("/api/settings")
    def update_settings(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return api.update_settings(body or {})

    @app.delete("/api/settings")
    def reset_settings() -> dict[str, Any]:
        return api.reset_settings()

    @app.post("/api/settings/probe")
    def probe_path(body: dict[str, Any] | None = None) -> dict[str, Any]:
        return api.probe_path(str((body or {}).get("key") or ""))

    @app.post("/api/settings/probe_all")
    def probe_all_paths() -> dict[str, Any]:
        return api.probe_all_paths()

    @app.post("/api/settings/open_folder")
    def open_settings_folder() -> dict[str, Any]:
        return api.open_settings_folder()

    @app.post("/api/dialog/pick")
    def pick_path(body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        return api.pick_path(str(body.get("key") or ""), str(body.get("kind") or "file"))

    return app
