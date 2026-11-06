"""分区 / 装机解耦的回归测试：阶段契约、编排停止条件、预览同源、API 预览。

这些用例守护三件事：

1. **分区阶段独立可测**：只给 ``DiskpartShell`` 替身就能跑，不需要 dism/bcdboot；
2. **停止条件只在编排层**：分区验收不过 → 系统组一次系统命令都不发；
3. **预览与真跑同源**：``/api/partition/preview`` 的脚本就是真实执行器会跑的行。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import (
    create_partition_lines,
    msr_delete_lines,
    plan_partitions,
    preview_segments,
    preview_text,
    wipe_lines,
)
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.phases.partition import PartitionPhase
from app.application.phases.system import BootPhase, ImagePhase, SystemPhase
from app.application.pipeline import JobPipeline
from app.application.steps import (
    GROUP_OF,
    STEP_GROUPS,
    STEP_KEYS,
    group_progress,
    phases_payload,
)
from tests.fakes import FakeDiskpartShell, FakeProcess, FakeVerify, build_snapshot

PAYLOAD = {
    "partition_info": {
        "efi_size_mb": 100,
        "c_size_gb": 3,
        "data_size_gb": "auto",
        "allow_small_partitions": True,
    },
    "image_info": {"image_path": "D:/images/win11.wim", "image_index": 1},
}
LETTERS = {"efi": "E", "windows": "F", "data": "G"}


def _parts(payload=None, letters=None):
    return plan_partitions(payload or PAYLOAD, letters=letters or LETTERS)


def _ctx(tmp_path, *, parts=None, letters=None, options=None, cancel=False, on_step=None):
    image = tmp_path / "tiny.wim"
    if not image.exists():
        image.write_bytes(b"x")
    return PhaseContext(
        disk_number=1,
        parts=parts or _parts(),
        letters=letters or LETTERS,
        image_path=str(image),
        image_index=1,
        cancel=threading.Event(),
        log=CommandLog(None),
        options=options or {},
        on_step=on_step,
    )


# ---------------------------------------------------------------- 步骤契约
def test_step_contract_has_seven_steps_with_layout():
    """「分区验收」是独立一步：分区组 4 步 + 系统组 2 步 + 校验组 1 步。"""
    assert STEP_KEYS == ("efi", "windows", "data", "layout", "image", "boot", "verify")
    assert STEP_GROUPS["partition"] == ("efi", "windows", "data", "layout")
    assert GROUP_OF["layout"] == "partition"
    assert GROUP_OF["image"] == "system"
    assert GROUP_OF["verify"] == "verify"


def test_phases_payload_is_declarative():
    phases = phases_payload()
    assert [phase["key"] for phase in phases] == ["partition", "system", "verify"]
    assert phases[0]["title"] == "硬盘分区"
    assert phases[0]["steps"] == ["efi", "windows", "data", "layout"]
    assert phases[0]["stop_on_failure"] is True


def test_group_progress_ignores_extra_steps():
    """演练期附加步骤（如 diskpart_preview）不能把分组进度顶满。"""
    groups = group_progress(["efi", "windows", "data", "layout", "diskpart_preview"], None)
    assert groups["partition"]["percent"] == 100
    assert groups["system"]["percent"] == 0
    assert groups["verify"]["percent"] == 0


# ---------------------------------------------------------------- 分区阶段独立
def test_partition_phase_runs_with_only_diskpart_shell(tmp_path):
    """分区阶段只依赖 DiskpartShell：不需要 dism/bcdboot 也能跑完。"""
    parts = _parts()
    shell = FakeDiskpartShell(msr=None, query_handler=lambda disk: build_snapshot(parts))
    phase = PartitionPhase(shell)

    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is True
    assert [result.key for result in outcome.results] == ["efi", "windows", "data", "layout"]
    assert outcome.disk_touched is True
    assert outcome.efi_partition == 1


def _code_identifiers(source: str) -> set[str]:
    """收集源码里的"会被执行的"标识符与字符串常量，跳过文档字符串。"""
    import ast

    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
            found.add(node.module or "")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.add(node.value)
    return found


def test_partition_phase_source_does_not_know_system_stage():
    """源码级护栏：分区阶段不许出现 dism / bcdboot（解耦不是靠注释维持的）。"""
    source = Path("app/application/phases/partition.py").read_text(encoding="utf-8")
    identifiers = {name.lower() for name in _code_identifiers(source)}
    for forbidden in ("dism", "bcdboot"):
        assert not any(forbidden in name for name in identifiers), (
            f"分区阶段代码里不该引用 {forbidden}"
        )


def test_partitions_module_is_pure():
    """布局/判据/脚本生成必须是纯函数：不 spawn、不 subprocess。"""
    source = Path("app/application/partitions.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "import os"):
        assert forbidden not in source


def test_layout_is_a_separate_step_with_verification_details(tmp_path):
    parts = _parts()
    shell = FakeDiskpartShell(msr=None, query_handler=lambda disk: build_snapshot(parts))
    outcome = PartitionPhase(shell).run(_ctx(tmp_path))

    layout = next(result for result in outcome.results if result.key == "layout")
    assert layout.title.startswith("分区验收")
    assert layout.details["check"] == "partition_phase"
    assert layout.details["efi_partition"] == 1


# ---------------------------------------------------------------- 编排停止条件
class _RecordingPhase:
    def __init__(self, group, ok=True, message="", results=(), cancelled=False):
        self.group = group
        self._ok = ok
        self._message = message
        self._results = tuple(results)
        self._cancelled = cancelled
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return PhaseOutcome(
            ok=self._ok,
            results=self._results,
            message=self._message,
            cancelled=self._cancelled,
        )


def _pipeline(tmp_path, *, partition, system, verify=None):
    return JobPipeline(
        partition=partition,
        system=system,
        verify=verify,
    )


def test_pipeline_skips_system_when_partition_fails(tmp_path):
    """分区失败 → 系统组一次都不调用，镜像/引导/校验都标 skipped。"""
    partition = _RecordingPhase(
        "partition",
        ok=False,
        message="分区验收不通过：大小不符",
        results=(PhaseResult.failed("layout", "分区验收不通过：大小不符"),),
    )
    system = _RecordingPhase("system")
    verify = _RecordingPhase("verify")
    pipeline = _pipeline(tmp_path, partition=partition, system=system, verify=verify)

    result = pipeline.run(_ctx(tmp_path))

    assert result.ok is False
    assert system.calls == 0
    assert verify.calls == 0
    statuses = {item.key: item.status for item in result.results}
    assert statuses["layout"] == "failed"
    assert statuses["image"] == "skipped"
    assert statuses["boot"] == "skipped"
    assert statuses["verify"] == "skipped"
    assert "未执行" in next(item for item in result.results if item.key == "image").message


def test_pipeline_skips_verify_when_system_fails(tmp_path):
    partition = _RecordingPhase(
        "partition",
        results=(
            PhaseResult.ok("efi"),
            PhaseResult.ok("windows"),
            PhaseResult.ok("data"),
            PhaseResult.ok("layout"),
        ),
    )
    system = _RecordingPhase(
        "system", ok=False, message="写引导失败（bcdboot 退出码 2）",
        results=(PhaseResult.ok("image"), PhaseResult.failed("boot", "bcdboot 失败")),
    )
    verify = _RecordingPhase("verify")
    pipeline = _pipeline(tmp_path, partition=partition, system=system, verify=verify)

    result = pipeline.run(_ctx(tmp_path))

    assert result.ok is False
    assert verify.calls == 0
    statuses = {item.key: item.status for item in result.results}
    assert statuses["boot"] == "failed"
    assert statuses["verify"] == "skipped"


def test_pipeline_cancel_after_partition_skips_system(tmp_path):
    partition = _RecordingPhase(
        "partition",
        results=(PhaseResult.ok("layout"),),
    )
    system = _RecordingPhase("system")
    pipeline = _pipeline(tmp_path, partition=partition, system=system)

    ctx = _ctx(tmp_path)
    ctx.cancel.set()
    result = pipeline.run(ctx)

    assert result.ok is False
    assert system.calls == 0
    assert "任务已取消" in (result.failure or "")
    statuses = {item.key: item.status for item in result.results}
    assert statuses["image"] == "skipped"


# ---------------------------------------------------------------- 预览同源
def test_preview_segments_share_the_real_line_generators():
    """预览的行**就是**真实脚本的行（不是另抄一份）。"""
    parts = _parts()
    segments = preview_segments(1, parts)
    assert segments[0]["lines"] == wipe_lines(1)
    assert segments[1]["lines"][0] == "select disk 1"
    assert segments[2]["lines"] == create_partition_lines(1, parts)
    assert msr_delete_lines(1, None) == ["select disk 1", "exit"]


def test_preview_text_contains_every_real_line():
    parts = _parts()
    text = preview_text(2, parts)
    for line in [*wipe_lines(2), *create_partition_lines(2, parts)]:
        assert line in text.splitlines()


def test_service_preview_uses_same_generator():
    from app.application.service import SispService

    service = SispService(disk_scanner=list, dry_run=True)
    parts = _parts()
    text = service.build_diskpart_preview(1, PAYLOAD, LETTERS)
    for line in create_partition_lines(1, parts):
        assert line in text.splitlines()


# ---------------------------------------------------------------- 系统组仍可独立用
def test_system_phase_runs_with_only_process_shell(tmp_path):
    parts = _parts()
    process = FakeProcess()
    system = SystemPhase(
        ImagePhase(process, dism="dism.exe"), BootPhase(process, bcdboot="bcdboot.exe")
    )
    outcome = system.run(_ctx(tmp_path, parts=parts))

    assert outcome.ok is True
    assert [result.key for result in outcome.results] == ["image", "boot"]
    assert process.calls[0][2] == "/Apply-Image"
    assert process.calls[1][0].endswith("bcdboot.exe")


def test_verify_phase_can_be_swapped(tmp_path):
    """校验组是可替换的：装配一个替身进 pipeline，不碰 PowerShell。"""
    partition = _RecordingPhase("partition", results=(PhaseResult.ok("layout"),))
    system = _RecordingPhase("system", results=(PhaseResult.ok("image"), PhaseResult.ok("boot")))
    verify = FakeVerify(ok=False, message="EFI 分区里没有 bootmgfw.efi")
    result = _pipeline(tmp_path, partition=partition, system=system, verify=verify).run(_ctx(tmp_path))

    assert result.ok is False
    assert "bootmgfw.efi" in (result.failure or "")
    assert verify.calls == 1


# ---------------------------------------------------------------- API 预览
def test_prepare_returns_declarative_phases(tmp_path):
    pytest.importorskip("fastapi")
    from tests.test_web_api import _build_client

    client, _wf = _build_client(tmp_path)
    prepared = client.post(
        "/api/prepare", json={"selection": {"1": {"c_size": 20, "data_size": 30}}}
    ).json()["data"]
    phases = prepared[0]["plan"]["phases"]
    assert [phase["key"] for phase in phases] == ["partition", "system", "verify"]
    assert phases[0]["steps"] == ["efi", "windows", "data", "layout"]


def test_partition_preview_returns_three_segments(tmp_path):
    pytest.importorskip("fastapi")
    from tests.test_web_api import _build_client

    client, _wf = _build_client(tmp_path)
    client.post("/api/prepare", json={"selection": [1]})

    body = client.post("/api/partition/preview", json={"disk_number": 1}).json()
    assert body["ok"] is True
    item = body["data"][0]
    assert [segment["key"] for segment in item["segments"]] == ["wipe", "msr", "create"]
    assert item["script"].startswith("select disk 1")
    assert "create partition efi" in item["script"]


def test_partition_preview_requires_prepare(tmp_path):
    pytest.importorskip("fastapi")
    from tests.test_web_api import _build_client

    client, _wf = _build_client(tmp_path)
    body = client.post("/api/partition/preview", json={"disk_number": 1}).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PREPARE"


def test_partition_preview_unknown_disk(tmp_path):
    pytest.importorskip("fastapi")
    from tests.test_web_api import _build_client

    client, _wf = _build_client(tmp_path)
    client.post("/api/prepare", json={"selection": [1]})
    body = client.post("/api/partition/preview", json={"disk_number": 2}).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_PREPARE"


# ---------------------------------------------------------------- 进度事件
def _recording_ctx(tmp_path, *, parts=None):
    """带 ``on_step`` 记录的 ctx（进度链路的最小验证装置）。"""
    events: list[tuple[str, str, str]] = []

    def on_step(key: str, status: str, message: str = "") -> None:
        events.append((key, status, message))

    ctx = _ctx(tmp_path, parts=parts, on_step=on_step)
    return ctx, events


def test_pipeline_reports_every_step_through_on_step(tmp_path):
    """每个步骤都要有 ``on_step`` 事件（界面进度条依赖这条链路）。

    回归用例：阶段只产出 ``PhaseResult``，如果编排层不把它翻译成进度事件，
    真实任务会一直停在第一条 running 事件上（进度 0%、分组进度全空）。
    """
    partition = _RecordingPhase(
        "partition",
        results=(
            PhaseResult.ok("efi"),
            PhaseResult.ok("windows"),
            PhaseResult.ok("data"),
            PhaseResult.ok("layout"),
        ),
    )
    system = _RecordingPhase(
        "system", results=(PhaseResult.ok("image"), PhaseResult.ok("boot"))
    )
    verify = _RecordingPhase("verify", results=(PhaseResult.ok("verify"),))
    pipeline = _pipeline(tmp_path, partition=partition, system=system, verify=verify)
    ctx, events = _recording_ctx(tmp_path)

    pipeline.run(ctx)

    assert [key for key, status, _ in events if status != "running"] == list(STEP_KEYS)


def test_pipeline_reports_skipped_steps_through_on_step(tmp_path):
    """失败被跳过的步骤也要上报：否则分组的“未执行”在界面上出不来。"""
    partition = _RecordingPhase(
        "partition",
        ok=False,
        message="分区验收不通过：大小不符",
        results=(PhaseResult.failed("layout", "分区验收不通过：大小不符"),),
    )
    system = _RecordingPhase("system")
    pipeline = _pipeline(tmp_path, partition=partition, system=system)
    ctx, events = _recording_ctx(tmp_path)

    pipeline.run(ctx)

    statuses = {key: status for key, status, _ in events}
    assert statuses["layout"] == "failed"
    assert statuses["image"] == "skipped"
    assert statuses["verify"] == "skipped"


def test_precheck_missing_tools_reports_all_steps(tmp_path):
    """缺系统工具时七个步骤都标 skipped，并且都上报进度。"""
    pipeline = _pipeline(
        tmp_path, partition=_RecordingPhase("partition"), system=_RecordingPhase("system")
    )
    ctx, events = _recording_ctx(tmp_path)

    result = pipeline.precheck(ctx, missing_tools=("dism.exe",))

    assert result is not None
    assert len(result.results) == len(STEP_KEYS)
    assert [key for key, _, _ in events] == list(STEP_KEYS)


def test_real_runner_fills_job_progress(tmp_path):
    """真实执行器（替身壳）+ JobManager：进度必须是 7/7、分组进度齐全。

    这是 VM 上真实写盘暴露出来的回归：状态是 ok、步骤结果是全的，但
    ``job.progress`` 里 ``completed`` 为空、三组进度全 0（阶段只产出结果，
    没人把它翻译成进度事件）。
    """
    from app.application.jobs import ExecutionPlan, JobManager, JobSpec, JobStatus
    from app.application.real_runner import WindowsDiskRunner

    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    payload = {
        "partition_info": dict(PAYLOAD["partition_info"]),
        "image_info": {"image_path": str(image), "image_index": 1},
    }
    parts = _parts(payload)
    runner = WindowsDiskRunner(
        shell=FakeDiskpartShell(query_handler=lambda disk: build_snapshot(parts)),
        process=FakeProcess(),
        verify=FakeVerify(),
        log_dir=str(tmp_path),
    )
    manager = JobManager(executor=runner.run)
    job = manager.submit(
        JobSpec(
            disk_number=2,
            config_payload=payload,
            drive_letters=LETTERS,
            dry_run=False,
        ),
        ExecutionPlan(disk_number=2, dry_run=False),
    )
    manager.confirm_batch([job.id])
    manager.wait_all(10)

    assert job.status is JobStatus.OK
    assert job.progress["total"] == len(STEP_KEYS)
    assert job.progress["completed"] == list(STEP_KEYS)
    assert job.progress["percent"] == 100
    groups = job.progress["groups"]
    assert groups["partition"]["done"] is True
    assert groups["system"]["percent"] == 100
    assert groups["verify"]["percent"] == 100
