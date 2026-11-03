"""Web API（REST + SSE）集成测试。

用 ``fastapi.testclient.TestClient`` 打真实路由；工作流侧用替身 service +
假预检，不碰磁盘、不碰 PowerShell——与 ``test_real_execution.py`` 同一套手法。

跑法：
    python -m pytest tests/test_web_api.py -q
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
uvicorn = pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient

from app.application.contracts import DiskInfo
from app.application.workflow import WorkflowController
from app.gui_web.api import WebApi
from app.gui_web.server import create_app
from app.models import WorkerResult
from app.modules.settings.service import build_store

_GIB = 1024**3
_PAYLOAD: dict[str, Any] = {
    "partition_info": {"efi_size_mb": 300, "c_size_gb": 30, "data_size_gb": "auto"},
    "image_info": {"image_path": r"D:\images\win11.wim", "image_index": 1},
}


def _disk(number: int = 1) -> DiskInfo:
    return DiskInfo(
        disk_number=number,
        model="test disk",
        serial_number=None,
        unique_id=f"unique-{number}",
        size_bytes=60 * _GIB,
        size_display="60 GB",
        partition_style="RAW",
        bus_type="NVMe",
        drive_letters=(),
        is_boot=False,
        is_system=False,
        is_offline=False,
        is_read_only=False,
        is_selectable=True,
        protection_reasons=(),
    )


class _FakeService:
    """只实现工作流用到的几个方法；真实执行不会被触发（dry_run=True）。"""

    def __init__(self, disks: list[DiskInfo]) -> None:
        self.disks = disks
        self.dry_run = True
        self.allowed_disk_numbers = frozenset()

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        letters = {number: {"efi": "E", "windows": "F", "data": "G"} for number in selected}
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        return WorkerResult.dry_run_plan(int(getattr(spec, "disk_number", 0) or 0))

    def check_config(self, config_path=None):
        return SimpleNamespace(passed=True, message="配置可解析", error_code=0)


def _preflight(config_path=None) -> dict[str, Any]:
    return {
        "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
        "config_payload": _PAYLOAD,
    }


def _build_client(
    tmp_path: Path, *, host_check: bool = False, log_dir: Path | None = None
) -> tuple[TestClient, WorkflowController]:
    wf = WorkflowController(
        service=_FakeService([_disk(1), _disk(2)]),
        dry_run=True,
        preflight_runner=_preflight,
    )
    wf.start(None, dry_run=True)
    store = build_store(tmp_path / "settings.json")
    api = WebApi(wf, settings=store, log_dir=log_dir)
    app = create_app(api, wf, host_check=host_check)
    return TestClient(app), wf


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return _build_client(tmp_path)[0]


def _unwrap(response) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


# ---------------------------------------------------------------- 静态与信息
def test_health_and_index(client: TestClient) -> None:
    assert client.get("/health").json() == {"ok": True}
    index = client.get("/")
    assert index.status_code == 200
    assert "硬盘工具箱" in index.text


def test_app_info(client: TestClient) -> None:
    data = _unwrap(client.get("/api/info"))
    assert data["dry_run"] is True
    assert "python" in data


# ---------------------------------------------------------------- Host 校验
def test_host_check_rejects_foreign_host(tmp_path: Path) -> None:
    app_client, _wf = _build_client(tmp_path, host_check=True)
    response = app_client.get("/health")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E_HOST"


def test_host_check_allows_loopback(tmp_path: Path) -> None:
    app_client, _wf = _build_client(tmp_path, host_check=True)
    response = app_client.get("/health", headers={"host": "localhost:8765"})
    assert response.status_code == 200


# ---------------------------------------------------------------- 会话与磁盘
def test_session_and_disks(client: TestClient) -> None:
    session = _unwrap(client.post("/api/session", json={"config_path": None, "dry_run": True}))
    assert len(session["disks"]) == 2
    assert session["dry_run"] is True

    disks = _unwrap(client.get("/api/disks"))
    assert {disk["disk_number"] for disk in disks} == {1, 2}


def test_session_cannot_upgrade_forced_dry_run(tmp_path: Path) -> None:
    """控制器以 dry-run 启动后，API 传 dry_run=false 也不能升级为真实模式。"""
    client, wf = _build_client(tmp_path)

    session = _unwrap(client.post("/api/session", json={"config_path": None, "dry_run": False}))

    assert session["dry_run"] is True
    assert wf.service.dry_run is True


def test_check_config(client: TestClient) -> None:
    data = _unwrap(client.post("/api/config/check", json={"config_path": None}))
    assert data["passed"] is True


# ---------------------------------------------------------------- 任务全流程
def test_full_job_flow_over_rest(client: TestClient, tmp_path: Path) -> None:
    prepared = _unwrap(client.post("/api/prepare", json={"selection": [1], "dry_run": True}))
    assert len(prepared) == 1
    assert prepared[0]["plan"]["blockers"] == []

    jobs = _unwrap(client.post("/api/submit"))
    assert len(jobs) == 1
    job_id = jobs[0]["id"]
    assert jobs[0]["status"] == "waiting_confirmation"

    confirmed = _unwrap(client.post("/api/confirm", json={"job_ids": [job_id]}))
    assert confirmed[0]["id"] == job_id

    # dry-run 执行很快；轮询等待完成
    deadline = time.monotonic() + 5
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = _unwrap(client.get(f"/api/jobs/{job_id}"))
        if job["status"] in ("ok", "failed", "cancelled", "skipped"):
            break
        time.sleep(0.05)
    assert job["status"] == "ok", job
    assert job["result"]["dry_run"] is True

    # 事件缓冲里应该能捞到这条任务的创建与结束事件
    polled = _unwrap(client.post("/api/events/poll", json={"cursor": 0, "limit": 100}))
    types = [event["type"] for event in polled["events"]]
    assert "job.created" in types
    assert "job.finished" in types
    assert any(j["id"] == job_id for j in polled["jobs"])


def test_duplicate_submit_is_rejected(client: TestClient) -> None:
    _unwrap(client.post("/api/prepare", json={"selection": [1], "dry_run": True}))
    _unwrap(client.post("/api/submit"))

    body = client.post("/api/submit").json()

    assert body["ok"] is False
    assert body["error"]["code"] == "E_SUBMIT"


def test_prepare_accepts_per_disk_override_mapping(client: TestClient) -> None:
    """第 4 步发的是 ``{盘号: {c_size, data_size}}``，不是数组。

    这条形态是 `WorkflowController.prepare` 的 Mapping 分支：数组只用来报
    “要装哪几块盘”，覆盖参数必须按盘分开传，否则 ``int(dict)`` 会炸。
    """
    prepared = _unwrap(
        client.post(
            "/api/prepare",
            json={"selection": {"1": {"c_size": 20, "data_size": 30}}, "dry_run": True},
        )
    )
    assert len(prepared) == 1
    info = prepared[0]["spec"]["config_payload"]["partition_info"]
    assert info["c_size_gb"] == 20
    assert info["data_size_gb"] == 30
    assert prepared[0]["spec"]["disk_number"] == 1


def test_prepare_rejects_unknown_disk(client: TestClient) -> None:
    body = client.post("/api/prepare", json={"selection": [99]}).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PREPARE"


# ---------------------------------------------------------------- 设置
def test_settings_roundtrip(client: TestClient) -> None:
    data = _unwrap(client.get("/api/settings"))
    assert data["settings"]["sysDefaultGb"] == 120

    updated = _unwrap(client.put("/api/settings", json={"sysDefaultGb": 300, "theme": "dark"}))
    assert updated["sysDefaultGb"] == 300
    assert updated["theme"] == "dark"

    bad = client.put("/api/settings", json={"theme": "neon"}).json()
    assert bad["ok"] is False
    assert bad["error"]["code"] == "E_SETTINGS"

    reset = _unwrap(client.delete("/api/settings"))
    assert reset["sysDefaultGb"] == 120

    probed = _unwrap(client.post("/api/settings/probe_all"))
    assert "extDism" in probed


# ---------------------------------------------------------------- SSE
@contextlib.contextmanager
def _live_server(app: Any):
    """在后台线程起真实 uvicorn，返回基地址。

    starlette 1.x 的 ``TestClient`` 会等整个 ASGI 应用跑完才返回，无法读取
    无限 SSE 响应；因此 SSE 测试必须走真实网络栈。服务使用 ``port=0`` 让系统
    分配空闲端口，避免与开发中的 8765 冲突。
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn 未能在 10 秒内启动")
            if not thread.is_alive():
                raise RuntimeError("uvicorn 启动线程已退出")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_sse_streams_snapshot_and_events(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    frames: list[dict[str, Any]] = []

    try:
        with _live_server(client.app) as base:
            def produce_event() -> None:
                # 连接建立后制造一个任务事件，让 SSE 推出第二帧
                time.sleep(0.4)
                try:
                    with httpx.Client(timeout=5.0, trust_env=False) as producer:
                        producer.post(
                            base + "/api/prepare",
                            json={"selection": [1], "dry_run": True},
                        )
                        producer.post(base + "/api/submit")
                except Exception:  # noqa: BLE001 - 测试里失败由帧断言兜底
                    pass

            producer = threading.Thread(target=produce_event, daemon=True)
            producer.start()

            deadline = time.monotonic() + 10
            buffer = ""
            with httpx.Client(timeout=httpx.Timeout(5.0, read=10.0), trust_env=False) as http:
                try:
                    with http.stream("GET", base + "/api/events?cursor=0") as response:
                        assert response.status_code == 200
                        assert response.headers["content-type"].startswith("text/event-stream")
                        for chunk in response.iter_text():
                            buffer += chunk
                            while "\n\n" in buffer:
                                raw, buffer = buffer.split("\n\n", 1)
                                for line in raw.splitlines():
                                    if line.startswith("data: "):
                                        frames.append(json.loads(line[len("data: "):]))
                            if len(frames) >= 2 or time.monotonic() > deadline:
                                break
                except httpx.HTTPError as exc:  # 超时必须报断言，不能挂住测试套件
                    raise AssertionError(f"读取 SSE 失败：{exc}；已收到帧={frames}") from exc
            producer.join(timeout=5)
    finally:
        client.close()

    assert frames, "没有收到任何 SSE 帧"
    assert frames[0]["type"] == "snapshot"
    assert "jobs" in frames[0]
    # 第二帧：submit 产生的任务事件，带 jobs 快照与游标
    event_frames = [frame for frame in frames[1:] if frame.get("event")]
    assert event_frames, frames
    assert event_frames[0]["event"]["type"] == "job.created"
    assert event_frames[0]["cursor"] >= 1
    assert isinstance(event_frames[0]["jobs"], list)


# ---------------------------------------------------------------- 静态资源
def test_static_assets_are_served(client: TestClient) -> None:
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert ":root" in css.text

    js = client.get("/static/js/main.js")
    assert js.status_code == 200
    assert "import" in js.text
    # 浏览器按 MIME 判断模块类型，必须是 javascript
    assert "javascript" in js.headers.get("content-type", "").lower()


# ---------------------------------------------------------------- 任务日志
def _fake_job(job_id: str, log_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=job_id,
        result=SimpleNamespace(
            steps=[SimpleNamespace(key="verify", status="ok", details={"log_path": log_path})]
        ),
    )


def test_job_log_unknown_job(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    body = client.get("/api/jobs/nope/log").json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_NOT_FOUND"


def test_job_log_reads_tail_inside_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    log_file = logs / "disk-runner-20260101-000000-1.log"
    log_file.write_text(chr(10).join(f"line {index}" for index in range(20)), encoding="utf-8")
    client, wf = _build_client(tmp_path, log_dir=logs)
    monkeypatch.setattr(
        wf.job_manager, "get", lambda job_id: _fake_job(job_id, str(log_file))
    )

    data = _unwrap(client.get("/api/jobs/any/log?tail=3"))
    assert data["path"] == str(log_file.resolve())
    assert data["total"] == 20
    assert data["lines"] == ["line 17", "line 18", "line 19"]


def test_job_log_refuses_path_outside_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "app.log"
    outside.write_text("secret", encoding="utf-8")
    client, wf = _build_client(tmp_path, log_dir=logs)
    monkeypatch.setattr(
        wf.job_manager, "get", lambda job_id: _fake_job(job_id, str(outside))
    )

    body = client.get("/api/jobs/any/log").json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PATH"


def test_job_log_rejects_non_log_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    sneaky = logs / "id_rsa.txt"
    sneaky.write_text("secret", encoding="utf-8")
    client, wf = _build_client(tmp_path, log_dir=logs)
    monkeypatch.setattr(
        wf.job_manager, "get", lambda job_id: _fake_job(job_id, str(sneaky))
    )

    body = client.get("/api/jobs/any/log").json()
    assert body["error"]["code"] == "E_PATH"


# ---------------------------------------------------------------- 危险操作确认
def test_partition_execute_requires_typed_confirmation(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    body = client.post(
        "/api/partition/execute",
        json={"disk_number": 1, "actions": [{"kind": "create", "size_gb": 1}]},
    ).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_CONFIRM"
    assert "确认" in body["error"]["message"]


def test_partition_execute_rejects_bad_disk_number(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    body = client.post("/api/partition/execute", json={"disk_number": "x"}).json()
    assert body["error"]["code"] == "E_PARTITION"


def test_delete_confirmed_accepts_several_spellings(tmp_path: Path) -> None:
    store = build_store(tmp_path / "settings.json")
    client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=store)
    identity = {"model": "Samsung SSD 990"}
    for text in ("1", "disk1", "disk 1", "硬盘1", "硬盘 1", "samsung ssd 990", " SAMSUNG  SSD 990 "):
        assert api._delete_confirmed(1, identity, text) is True, text
    assert api._delete_confirmed(1, identity, None) is False
    assert api._delete_confirmed(1, identity, "2") is False

    store.update({"confirmDelete": False})
    assert api._delete_confirmed(1, identity, None) is True


def test_session_without_dry_run_keeps_process_mode(tmp_path: Path) -> None:
    """界面刷新会话时不传 dry_run，不能把演练模式的进程切成真实写盘。"""
    client, wf = _build_client(tmp_path)
    session = _unwrap(client.post("/api/session", json={}))
    assert session["dry_run"] is True
    assert wf.service.dry_run is True
