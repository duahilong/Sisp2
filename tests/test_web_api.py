"""Web API（REST + SSE）集成测试。

用 ``fastapi.testclient.TestClient`` 打真实路由；工作流侧用替身 service +
假预检，不碰磁盘、不碰 PowerShell——与 ``test_real_execution.py`` 同一套手法。

跑法：
    python -m pytest tests/test_web_api.py -q
"""

from __future__ import annotations

import contextlib
import json
import tempfile
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

_FAKE_IMAGE_PATH = Path(tempfile.gettempdir()) / "sisp-test-web-api.GHO"
_FAKE_IMAGE_PATH.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))

_GIB = 1024**3
# 单测用小尺寸（C 30 GB）测算术：显式放宽后端下限（真实布局 C ≥ 60 GB）。
# 任务载荷现在只有两个来源：设置页（构造时的 ``partition_defaults`` 快照 +
# 每次现读的 ``settings_provider``）。这里就按这两条路注入，不再有“预检顺带
# 返回一份 config_payload”的旁路。
_SETTINGS: dict[str, Any] = {
    # 计划会按 payload 重算镜像可用性（后缀 + 存在性 + Ghost 文件头），
    # 所以这里用一个真实存在的 .GHO 占位文件。
    "sysImageA": str(_FAKE_IMAGE_PATH),
}
_PARTITION_DEFAULTS: dict[str, Any] = {
    "efi_size_mb": 300,
    "c_size_gb": 30,
    "data_size_gb": None,  # None = 吃掉剩余空间（等价于设置里的 "auto"）
    "allow_small_partitions": True,
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
    """只实现工作流用到的几个方法；执行器是替身，不会碰盘。"""

    def __init__(self, disks: list[DiskInfo]) -> None:
        self.disks = disks
        self.allowed_disk_numbers = frozenset()

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def scan_protected_snapshot(self) -> tuple[list[DiskInfo], tuple[str, ...]]:
        return list(self.disks), ()

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        letters = {number: {"efi": "E", "windows": "F", "data": "G"} for number in selected}
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        return WorkerResult(disk_number=int(getattr(spec, "disk_number", 0) or 0), success=True, message="ok")



def _preflight() -> dict[str, Any]:
    return {
        "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
    }


def _build_client(
    tmp_path: Path, *, host_check: bool = False, log_dir: Path | None = None
) -> tuple[TestClient, WorkflowController]:
    wf = WorkflowController(
        service=_FakeService([_disk(1), _disk(2)]),
        preflight_runner=_preflight,
        partition_defaults=_PARTITION_DEFAULTS,
        settings_provider=lambda: dict(_SETTINGS),
    )
    wf.start()
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
    session = _unwrap(client.post("/api/session", json={}))
    assert len(session["disks"]) == 2

    disks = _unwrap(client.get("/api/disks"))
    assert {disk["disk_number"] for disk in disks} == {1, 2}


def test_config_check_endpoint_is_gone(client: TestClient) -> None:
    """业务配置那一层移除后，重新校验配置的接口也一并删掉。"""
    response = client.post("/api/config/check", json={})
    assert response.status_code in (404, 405)


# ---------------------------------------------------------------- 任务全流程
def test_full_job_flow_over_rest(client: TestClient, tmp_path: Path) -> None:
    prepared = _unwrap(client.post("/api/prepare", json={"selection": [1]}))
    assert len(prepared) == 1
    assert prepared[0]["plan"]["blockers"] == []

    jobs = _unwrap(client.post("/api/submit"))
    assert len(jobs) == 1
    job_id = jobs[0]["id"]
    assert jobs[0]["status"] == "waiting_confirmation"

    confirmed = _unwrap(client.post("/api/confirm", json={"job_ids": [job_id]}))
    assert confirmed[0]["id"] == job_id

    # 替身执行器很快；轮询等待完成
    deadline = time.monotonic() + 5
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = _unwrap(client.get(f"/api/jobs/{job_id}"))
        if job["status"] in ("ok", "failed", "cancelled", "skipped"):
            break
        time.sleep(0.05)
    assert job["status"] == "ok", job

    # 事件缓冲里应该能捞到这条任务的创建与结束事件
    polled = _unwrap(client.post("/api/events/poll", json={"cursor": 0, "limit": 100}))
    types = [event["type"] for event in polled["events"]]
    assert "job.created" in types
    assert "job.finished" in types
    assert any(j["id"] == job_id for j in polled["jobs"])


def test_duplicate_submit_is_rejected(client: TestClient) -> None:
    _unwrap(client.post("/api/prepare", json={"selection": [1]}))
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
            json={"selection": {"1": {"c_size": 20, "data_size": 30}}},
        )
    )
    assert len(prepared) == 1
    info = prepared[0]["spec"]["task_payload"]["partition_info"]
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
    # 回包与 GET 同形：界面保存后直接用它刷新本地快照
    assert updated["settings"]["sysDefaultGb"] == 300
    assert updated["settings"]["theme"] == "dark"
    assert "warnings" in updated

    bad = client.put("/api/settings", json={"theme": "neon"}).json()
    assert bad["ok"] is False
    assert bad["error"]["code"] == "E_SETTINGS"

    reset = _unwrap(client.delete("/api/settings"))
    assert reset["settings"]["sysDefaultGb"] == 120
    assert reset["settings"]["theme"] == "light"


def test_dialog_pick_returns_path_without_saving(client: TestClient, monkeypatch) -> None:
    """选择框必须走本进程的原生弹框，且只交回路径、不落盘。

    回归用例一：以前这里调 ``webview.windows[0].create_file_dialog``，而桌面模式下
    窗口在父进程、API 在提权子进程，子进程永远拿不到窗口，界面点「浏览…」只
    能得到「窗口尚未创建」。
    回归用例二：曾经选完就写盘，界面又同时显示「未保存的改动」—— 现在只回填
    草稿，由「保存设置」统一落盘。
    """
    from app.gui_web import native_dialog

    _unwrap(client.put("/api/settings", json={"sysImageA": str(_FAKE_IMAGE_PATH)}))
    seen: dict[str, Any] = {}

    def fake_pick(kind: str = "file", **kwargs: Any) -> str:
        seen["kind"] = kind
        seen.update(kwargs)
        return "D:\\images\\Win11.GHO"

    monkeypatch.setattr(native_dialog, "pick_path", fake_pick)
    data = _unwrap(client.post("/api/dialog/pick", json={"key": "sysImageA", "kind": "file"}))
    assert data == {"path": "D:\\images\\Win11.GHO", "cancelled": False}
    assert seen["kind"] == "file"
    # 当前值当起始位置传给对话框
    assert str(seen["initial"]) == str(_FAKE_IMAGE_PATH)
    # 选中不落盘：设置里的值还是原来的
    assert _unwrap(client.get("/api/settings"))["settings"]["sysImageA"] == str(_FAKE_IMAGE_PATH)


def test_probe_follows_the_draft_path(client: TestClient) -> None:
    """探测能就着传进来的草稿路径跑，保存前角标也描述刚选的值。"""
    _unwrap(client.put("/api/settings", json={"sysImageA": str(_FAKE_IMAGE_PATH)}))
    assert _unwrap(client.post("/api/settings/probe", json={"key": "sysImageA"}))["state"] == "ok"

    missing = str(_FAKE_IMAGE_PATH.parent / "nope.GHO")
    result = _unwrap(client.post("/api/settings/probe", json={"key": "sysImageA", "path": missing}))
    assert result["state"] == "miss"
    assert result["path"] == missing

    # 草稿是真文件就报 ok（即使磁盘上不是这个值）
    other = _FAKE_IMAGE_PATH.parent / "sisp-test-web-api-draft.GHO"
    other.write_bytes(bytes([0xFE, 0xEF]))
    draft = _unwrap(client.post("/api/settings/probe", json={"key": "sysImageA", "path": str(other)}))
    assert draft["state"] == "ok"
    assert draft["path"] == str(other)

    # 没传 path 时退回已保存的值
    assert _unwrap(client.post("/api/settings/probe", json={"key": "sysImageA", "path": None}))["state"] == "ok"


def test_images_returns_configured_versions(client: TestClient) -> None:
    """``/api/images`` 不带 path 时返回设置里的两个槽位（各自带文件头校验）。

    这一步以前是「扫目录列文件」；改成两个具名槽位后，向导第 3 步只认这两个。
    """
    good = _FAKE_IMAGE_PATH
    missing = good.parent / "sisp-test-missing-b.GHO"
    if missing.exists():
        missing.unlink()
    _unwrap(
        client.put(
            "/api/settings",
            json={
                "sysImageA": str(good),
                "sysImageALabel": "Windows 10 22H2",
                "sysImageB": str(missing),
                "sysImageBLabel": "Windows 11 23H2",
            },
        )
    )

    data = _unwrap(client.post("/api/images", json={}))
    versions = data["versions"]
    assert [item["id"] for item in versions] == ["A", "B"]
    assert versions[0]["label"] == "Windows 10 22H2"
    assert versions[0]["ok"] is True
    assert versions[0]["path"] == str(good)
    # B 指着一个不存在的文件：不通过，但要把原因带回去给界面显示
    assert versions[1]["ok"] is False
    assert versions[1]["path"] == str(missing)
    # 默认选中第一个可用版本
    assert data["default"] == str(good)


def test_images_without_settings_returns_empty_list(client: TestClient) -> None:
    data = _unwrap(client.post("/api/images", json={}))
    assert data["versions"] == []
    assert data["default"] == ""


def test_dialog_pick_cancel_reports_cancelled(client: TestClient, monkeypatch) -> None:
    from app.gui_web import native_dialog

    monkeypatch.setattr(native_dialog, "pick_path", lambda *a, **kw: None)
    assert _unwrap(client.post("/api/dialog/pick", json={"kind": "dir"})) == {"cancelled": True}


def test_dialog_pick_reports_picker_failure(client: TestClient, monkeypatch) -> None:
    from app.gui_web import native_dialog

    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("找不到 powershell.exe，无法弹出选择框")

    monkeypatch.setattr(native_dialog, "pick_path", boom)
    body = client.post("/api/dialog/pick", json={"kind": "dir"}).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PICK"
    assert "powershell" in body["error"]["message"]


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
                            json={"selection": [1]},
                        )
                        producer.post(base + "/api/submit")
                except Exception:  # noqa: BLE001, S110 - 测试里失败由帧断言兜底
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
            steps=[SimpleNamespace(key="boot", status="ok", details={"log_path": log_path})]
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


def test_job_log_tracks_appends_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续轮询只申请增量：新行能看到、总数正确（接口契约不变）。"""
    logs = tmp_path / "logs"
    logs.mkdir()
    log_file = logs / "disk-runner-20260101-000000-1.log"
    log_file.write_text("a\nb\n", encoding="utf-8")
    client, wf = _build_client(tmp_path, log_dir=logs)
    monkeypatch.setattr(
        wf.job_manager, "get", lambda job_id: _fake_job(job_id, str(log_file))
    )

    first = _unwrap(client.get("/api/jobs/any/log?tail=5"))
    assert first["lines"] == ["a", "b"]
    assert first["total"] == 2

    with log_file.open("a", encoding="utf-8") as handle:
        handle.write("c\nd\n")
    second = _unwrap(client.get("/api/jobs/any/log?tail=2"))
    assert second["lines"] == ["c", "d"]
    assert second["total"] == 4


# ---------------------------------------------------------------- 危险操作确认

def test_resolve_identity_prefers_caller_then_scans(tmp_path: Path, monkeypatch) -> None:
    """界面只传盘号时，闸门也要拿到这块盘的身份（执行前复核用）。

    以前这里是 ``identity or {}``，于是**真实分区操作和修复引导全部被拒**——
    在打包好的 exe 上实测才会发现（本地替身拦不住真实子进程）。
    """
    _client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=build_store(tmp_path / "settings.json"))

    # 调用方显式给了身份：原样返回，不再扫盘
    assert api._resolve_identity(1, {"unique_id": "abc"}) == {"unique_id": "abc"}

    # 没给：现场扫一次，把强标识补齐
    monkeypatch.setattr(
        "app.gui_web.api._scan_summary",
        lambda number: {
            "disk_number": number,
            "unique_id": f"eui-{number}",
            "serial_number": "SN-1",
            "model": "Test Disk",
            "size_bytes": 1024,
            "size_display": "1 KB",
        },
    )
    resolved = api._resolve_identity(1, None)
    assert resolved["unique_id"] == "eui-1"
    assert resolved["serial_number"] == "SN-1"
    assert resolved["model"] == "Test Disk"

    # 盘不存在（扫描抛错）时退回空身份，由闸门自己报原因
    def boom(number):
        raise PermissionError(f"硬盘 {number} 当前不存在")

    monkeypatch.setattr("app.gui_web.api._scan_summary", boom)
    assert api._resolve_identity(9, None) == {}

def test_export_settings_writes_file_and_roundtrips(tmp_path: Path) -> None:
    store = build_store(tmp_path / "settings.json")
    store.update({"sysDefaultGb": 200, "sysImageALabel": "A 版本"})
    _client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=store)

    target = tmp_path / "out" / "sisp2-settings.json"
    data = api.export_settings(str(target))
    assert data["ok"] is True, data
    assert target.is_file()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["settings"]["sysDefaultGb"] == 200
    assert written["settings"]["sysImageALabel"] == "A 版本"


def test_export_without_path_uses_settings_folder(tmp_path: Path) -> None:
    store = build_store(tmp_path / "settings.json")
    _client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=store)
    data = api.export_settings(None)
    assert data["ok"] is True
    exported = Path(data["data"]["path"])
    assert exported.is_file()
    assert exported.parent == (tmp_path / "settings.json").parent


def test_import_settings_applies_and_validates(tmp_path: Path) -> None:
    store = build_store(tmp_path / "settings.json")
    _client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=store)

    good = tmp_path / "good.json"
    good.write_text(
        json.dumps({"version": 1, "settings": {"sysDefaultGb": 333, "theme": "dark"}}),
        encoding="utf-8",
    )
    result = api.import_settings(str(good))
    assert result["ok"] is True, result
    assert store.get()["sysDefaultGb"] == 333
    assert store.get()["theme"] == "dark"
    # 导入回包也带 settings/warnings，界面直接拿它刷新快照
    assert result["data"]["settings"]["sysDefaultGb"] == 333
    assert result["data"]["source"].endswith("good.json")

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"settings": {"sysDefaultGb": 5}}), encoding="utf-8")
    rejected = api.import_settings(str(bad))
    assert rejected["ok"] is False
    assert rejected["error"]["detail"]["field"] == "sysDefaultGb"
    # 整体拒绝：合法的 theme 也不能被写进去
    assert store.get()["sysDefaultGb"] == 333


def test_import_settings_rejects_missing_or_broken_file(tmp_path: Path) -> None:
    store = build_store(tmp_path / "settings.json")
    _client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=store)

    assert api.import_settings(str(tmp_path / "nope.json"))["ok"] is False
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    body = api.import_settings(str(broken))
    assert body["ok"] is False
    assert "JSON" in body["error"]["message"]


def test_import_settings_accepts_inline_content(tmp_path: Path) -> None:
    store = build_store(tmp_path / "settings.json")
    _client, wf = _build_client(tmp_path)
    api = WebApi(wf, settings=store)
    body = api.import_settings(None, json.dumps({"efiDefaultMb": 260}))
    assert body["ok"] is True, body
    assert store.get()["efiDefaultMb"] == 260


# ---------------------------------------------------- 设置导出 / 导入（v1.0.1）
def test_settings_export_writes_portable_file(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    target = tmp_path / "exported.json"

    data = _unwrap(client.post("/api/settings/export", json={"path": str(target)}))

    assert Path(data["path"]) == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["settings"]["sysDefaultGb"] == 120


def test_settings_import_roundtrip(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    _unwrap(client.put("/api/settings", json={"sysDefaultGb": 200}))
    target = tmp_path / "exported.json"
    _unwrap(client.post("/api/settings/export", json={"path": str(target)}))

    _unwrap(client.put("/api/settings", json={"sysDefaultGb": 60}))
    assert _unwrap(client.get("/api/settings"))["settings"]["sysDefaultGb"] == 60

    _unwrap(client.post("/api/settings/import", json={"path": str(target)}))
    assert _unwrap(client.get("/api/settings"))["settings"]["sysDefaultGb"] == 200


def test_settings_import_rejects_broken_file(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    body = client.post("/api/settings/import", json={"path": str(broken)}).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "E_SETTINGS"


# ---------------------------------------------------------------- 日志导出
def test_export_logs_writes_a_readable_zip(tmp_path: Path, monkeypatch) -> None:
    """导出接口：任意保存位置生成可读 ZIP，且之后能定位这个确切文件。"""
    import os
    import zipfile

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(os, "startfile", lambda _path: None, raising=False)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    client, _wf = _build_client(tmp_path, log_dir=log_dir)
    _unwrap(client.post("/api/prepare", json={"selection": [1]}))
    _unwrap(client.post("/api/submit"))
    # 任务还没写完/已收尾都可能：造一份"按盘号兜底能找到"的日志
    running = log_dir / "disk-runner-d1-test-disk-20261109-1200-1-abcdef.log"
    running.write_text("[12:00:01] 任务开始\n", encoding="utf-8")

    target = tmp_path / "outside-home" / "bundle.zip"
    data = _unwrap(client.post("/api/logs/export", json={"path": str(target)}))

    assert Path(data["path"]) == target
    assert data["jobs"] == 1
    assert data["size"] > 0
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert {"README.txt", "summary.txt", "summary.json"} <= names
        summary = archive.read("summary.txt").decode("utf-8")
        assert "test disk（磁盘 1）" in summary, summary
        assert any(name.startswith("logs/disk-runner-d1-") for name in names)

    revealed = _unwrap(client.post("/api/reveal", json={"path": str(target)}))
    assert Path(revealed["path"]) == target


def test_export_logs_without_jobs_is_a_clear_error(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    body = client.post("/api/logs/export", json={"path": str(tmp_path / "x.zip")}).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_NO_JOBS"


def test_reveal_rejects_paths_outside_home_and_logs(tmp_path: Path) -> None:
    client, _wf = _build_client(tmp_path)
    body = client.post("/api/reveal", json={"path": r"C:\Windows\win.ini"}).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PATH"


# ---------------------------------------------------------------- 会话缓存（启动优化）
def test_session_is_served_from_cache(tmp_path: Path) -> None:
    """后台已经热身好的会话不能因为界面再调一次就重扫一遍盘。

    启动阶段那次扫描（PowerShell 冷启动约 1 秒）以前会被 ``POST /api/session``
    再算一遍，界面首屏白等两秒。
    """
    client, wf = _build_client(tmp_path)
    calls: list[int] = []
    original = wf.start

    def spy():
        calls.append(1)
        return original()

    wf.start = spy  # type: ignore[method-assign]

    session = _unwrap(client.post("/api/session", json={}))
    assert len(session["disks"]) == 2
    assert calls == [], "已有会话就该直接返回缓存"
    assert wf.session is not None


def test_session_force_reruns(tmp_path: Path) -> None:
    """显式 ``{"force": true}`` 才重新预检 + 扫盘（“重新开始一次会话”）。"""
    client, wf = _build_client(tmp_path)
    calls: list[int] = []
    original = wf.start

    def spy():
        calls.append(1)
        return original()

    wf.start = spy  # type: ignore[method-assign]
    _unwrap(client.post("/api/session", json={"force": True}))
    assert calls == [1]


def test_warm_session_fills_the_session(tmp_path: Path) -> None:
    """``warm_session()`` 就是“后台把会话跑起来”，/health 不必等它。"""
    wf = WorkflowController(
        service=_FakeService([_disk(1)]),
        preflight_runner=_preflight,
        partition_defaults=_PARTITION_DEFAULTS,
        settings_provider=lambda: dict(_SETTINGS),
    )
    api = WebApi(wf, settings=build_store(tmp_path / "settings.json"))
    assert wf.session is None, "构造 WebApi 不该顺手扫盘"
    data = api.warm_session()
    assert data["ok"] is True
    assert wf.session is not None
    # 再调一次走缓存（不重复扫盘）
    again = api.start_session()
    assert again["ok"] is True


def test_warm_session_failure_is_reported_not_raised(tmp_path: Path) -> None:
    """热身失败不能让进程崩：界面调 /api/session 时会拿到 E_START。"""
    wf = WorkflowController(
        service=_FakeService([_disk(1)]),
        preflight_runner=_preflight,
        partition_defaults=_PARTITION_DEFAULTS,
        settings_provider=lambda: dict(_SETTINGS),
    )

    def boom():
        raise RuntimeError("扫描炸了")

    wf.start = boom  # type: ignore[method-assign]
    api = WebApi(wf, settings=build_store(tmp_path / "settings.json"))
    data = api.warm_session()
    assert data["ok"] is False
    assert data["error"]["code"] == "E_START"
    assert "扫描炸了" in data["error"]["message"]
