"""第二阶段：JobManager、实时事件、取消与计划确认门禁测试。"""

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application.cancellation import CancellationToken, JobCancelled
from app.application.events import (
    EventLog,
    JobEvent,
    JobEventType,
    read_events,
)
from app.application.jobs import (
    DEFAULT_PLAN_STEPS,
    ExecutionPlan,
    JobManager,
    JobSpec,
    JobStatus,
)
from app.application.reporting import ConsoleReporter, MemoryReporter
from app.models import RunContext
from app.modules.common.constants import ErrorCode
from app.modules.common.service import ConfigError
from app.orchestrator import run_single_disk_flow


def make_spec(disk_number: int = 1, dry_run: bool = False) -> JobSpec:
    return JobSpec(
        disk_number=disk_number,
        config_payload={"config_path": "D:\\config\\win11.json"},
        expected_identity={
            "disk_number": disk_number,
            "unique_id": f"UID-{disk_number}",
            "serial_number": f"SN-{disk_number}",
            "model": "Target Disk",
            "size_bytes": 500 * 1024 ** 3,
        },
        drive_letters={"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
        dry_run=dry_run,
    )


def successful_runner(spec: JobSpec, context: RunContext) -> None:
    context.start_step("初始化")
    context.reporter.line("fake output")
    context.reporter.step_started(1, 7, "初始化硬盘")
    context.record_step("初始化", True, "初始化完成")
    context.reporter.step_finished(1, 7, "初始化硬盘", True, "初始化完成")


def test_cancellation_token_lifecycle() -> None:
    token = CancellationToken()
    assert token.cancelled is False
    token.raise_if_cancelled()

    token.cancel()
    assert token.cancelled is True
    with pytest.raises(JobCancelled):
        token.raise_if_cancelled()

    token.reset()
    assert token.cancelled is False


def test_event_roundtrip_and_event_log(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    first = JobEvent(
        job_id="job-1",
        sequence=1,
        type=JobEventType.CREATED.value,
        timestamp="2026-01-01T00:00:00.000",
        payload={"plan": {"disk_number": 1}},
    )
    second = JobEvent(
        job_id="job-1",
        sequence=2,
        type=JobEventType.STARTED.value,
        timestamp="2026-01-01T00:00:01.000",
    )

    log.append(first)
    log.append(second)

    events = read_events(path, strict=True)
    assert [event.sequence for event in events] == [1, 2]
    assert events[0].payload == {"plan": {"disk_number": 1}}
    assert JobEvent.from_dict(first.to_dict()) == first


def test_read_events_skips_broken_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"job_id":"job-1","sequence":1,"type":"job.created","timestamp":"x"}\n'
        "not-json\n"
        '{"job_id":"job-1","sequence":2,"type":"job.started","timestamp":"x"}\n',
        encoding="utf-8",
    )

    assert [event.sequence for event in read_events(path)] == [1, 2]
    with pytest.raises(ValueError):
        read_events(path, strict=True)


def test_execution_plan_from_disk() -> None:
    disk = {
        "disk_number": 3,
        "model": "Disk 3",
        "serial_number": "SN-3",
        "unique_id": "UID-3",
        "size_bytes": 1000,
        "size_display": "1000 B",
    }
    plan = ExecutionPlan.from_disk(
        disk,
        config_payload={"config_path": "D:\\win.json"},
        drive_letters={"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
        warnings=["测试警告"],
    )

    assert plan.disk_number == 3
    assert plan.steps == DEFAULT_PLAN_STEPS
    assert plan.identity["unique_id"] == "UID-3"
    assert plan.model == "Disk 3"
    assert plan.drive_letters["windows"] == "F"
    assert plan.config_path == "D:\\win.json"
    assert plan.warnings == ("测试警告",)
    assert plan.to_dict()["steps"] == list(DEFAULT_PLAN_STEPS)


def test_submit_waits_for_confirmation(tmp_path: Path) -> None:
    manager = JobManager(runner=successful_runner, event_dir=tmp_path)
    job = manager.submit(make_spec())

    assert job.status == JobStatus.WAITING_CONFIRMATION
    assert job.result is None
    assert job.thread is None

    event_types = [event.type for event in manager.events(job.id)]
    assert event_types == [JobEventType.CREATED.value, JobEventType.AWAITING_CONFIRMATION.value]

    manager.confirm(job.id)
    manager.wait(job.id, timeout=5)

    assert job.status == JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result.success is True
    assert job.result.error_code == ErrorCode.SUCCESS
    assert job.result.steps[0].name == "初始化"
    assert job.steps == job.result.steps
    assert job.started_at is not None
    assert job.finished_at is not None

    events = manager.events(job.id)
    types = [event.type for event in events]
    assert types[0] == JobEventType.CREATED.value
    assert JobEventType.CONFIRMED.value in types
    assert JobEventType.STARTED.value in types
    assert JobEventType.LOG.value in types
    assert JobEventType.STEP_STARTED.value in types
    assert JobEventType.STEP_FINISHED.value in types
    assert types[-2:] == [JobEventType.SUCCEEDED.value, JobEventType.FINISHED.value]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))

    step_finished = next(event for event in events if event.type == JobEventType.STEP_FINISHED.value)
    assert step_finished.payload["passed"] is True

    saved = read_events(tmp_path / f"{job.id}.jsonl", strict=True)
    assert [event.sequence for event in saved] == [event.sequence for event in events]
    assert [event.type for event in saved] == types


def test_submit_can_skip_confirmation() -> None:
    manager = JobManager(runner=successful_runner)
    job = manager.submit(make_spec(), require_confirmation=False)
    manager.wait(job.id, timeout=5)

    assert job.status == JobStatus.SUCCEEDED
    types = [event.type for event in manager.events(job.id)]
    assert JobEventType.CONFIRMED.value in types


def test_plan_disk_number_must_match() -> None:
    manager = JobManager(runner=successful_runner)
    plan = ExecutionPlan(disk_number=2)
    with pytest.raises(ValueError, match="计划硬盘编号"):
        manager.submit(make_spec(1), plan=plan)


def test_job_failure_maps_sisp_error() -> None:
    def failing_runner(spec: JobSpec, context: RunContext) -> None:
        raise ConfigError("配置错误")

    manager = JobManager(runner=failing_runner)
    job = manager.submit(make_spec(), require_confirmation=False)
    manager.wait(job.id, timeout=5)

    assert job.status == JobStatus.FAILED
    assert job.result is not None
    assert job.result.success is False
    assert job.result.error_code == ErrorCode.CONFIG_ERROR
    assert job.result.message == "配置错误"
    types = [event.type for event in manager.events(job.id)]
    assert types[-2:] == [JobEventType.FAILED.value, JobEventType.FINISHED.value]


def test_job_failure_maps_unexpected_error() -> None:
    def crashing_runner(spec: JobSpec, context: RunContext) -> None:
        raise RuntimeError("boom")

    manager = JobManager(runner=crashing_runner)
    job = manager.submit(make_spec(), require_confirmation=False)
    manager.wait(job.id, timeout=5)

    assert job.status == JobStatus.FAILED
    assert job.result is not None
    assert job.result.error_code == ErrorCode.UNEXPECTED_ERROR
    assert "RuntimeError: boom" in job.result.message


def test_cancel_before_confirmation() -> None:
    manager = JobManager(runner=successful_runner)
    job = manager.submit(make_spec())

    assert manager.cancel(job.id, reason="用户放弃") is True
    assert job.status == JobStatus.CANCELLED
    assert job.result is not None
    assert job.result.error_code == ErrorCode.CANCELLED
    assert job.error == "用户放弃"
    types = [event.type for event in manager.events(job.id)]
    assert JobEventType.CANCEL_REQUESTED.value in types
    assert types[-2:] == [JobEventType.CANCELLED.value, JobEventType.FINISHED.value]

    with pytest.raises(ValueError, match="不允许确认"):
        manager.confirm(job.id)
    assert manager.cancel(job.id) is False


def test_cancel_running_job_is_cooperative() -> None:
    started = threading.Event()

    def slow_runner(spec: JobSpec, context: RunContext) -> None:
        started.set()
        while not context.cancel_token.cancelled:
            time.sleep(0.01)
        context.raise_if_cancelled()

    manager = JobManager(runner=slow_runner)
    job = manager.submit(make_spec())
    manager.confirm(job.id)

    assert started.wait(2) is True
    assert manager.cancel(job.id) is True
    manager.wait(job.id, timeout=5)

    assert job.status == JobStatus.CANCELLED
    assert job.result is not None
    assert job.result.error_code == ErrorCode.CANCELLED
    types = [event.type for event in manager.events(job.id)]
    assert JobEventType.CANCEL_REQUESTED.value in types
    assert types[-2:] == [JobEventType.CANCELLED.value, JobEventType.FINISHED.value]


def test_subscribe_and_unsubscribe() -> None:
    received: list[JobEvent] = []
    manager = JobManager(runner=successful_runner)
    unsubscribe = manager.subscribe(received.append)

    first = manager.submit(make_spec(1), require_confirmation=False)
    manager.wait(first.id, timeout=5)
    assert received
    assert {event.job_id for event in received} == {first.id}

    unsubscribe()
    received.clear()
    second = manager.submit(make_spec(2), require_confirmation=False)
    manager.wait(second.id, timeout=5)
    assert received == []


def test_unknown_job_operations() -> None:
    manager = JobManager(runner=successful_runner)
    with pytest.raises(KeyError):
        manager.confirm("missing")
    with pytest.raises(KeyError):
        manager.cancel("missing")
    with pytest.raises(KeyError):
        manager.events("missing")
    with pytest.raises(KeyError):
        manager.wait("missing")


def test_memory_reporter_records_step_outcomes(capsys) -> None:
    memory = MemoryReporter()
    memory.step_finished(1, 7, "初始化硬盘", True, "完成")
    assert memory.steps[0].name == "初始化硬盘"
    assert memory.steps[0].passed is True
    assert memory.steps[0].message == "完成"

    ConsoleReporter().step_finished(1, 7, "初始化硬盘", True, "完成")
    assert capsys.readouterr().out == ""


def test_run_single_disk_flow_checks_cancel_before_scan(monkeypatch) -> None:
    token = CancellationToken()
    token.cancel()

    def unexpected_scan():
        raise AssertionError("已取消时不应扫描硬盘")

    monkeypatch.setattr("app.orchestrator.scan_disk_summaries", unexpected_scan)

    with pytest.raises(JobCancelled):
        run_single_disk_flow(1, {}, dry_run=True, cancel_token=token)


def test_default_runner_executes_dry_run(tmp_path: Path, monkeypatch) -> None:
    """验证 JobManager 默认 runner 与 orchestrator 的接线。"""
    image = tmp_path / "win.gho"
    ghost = tmp_path / "ghost64.exe"
    bcd = tmp_path / "bcdboot.exe"
    source = tmp_path / "source"
    for path in (image, ghost, bcd):
        path.write_bytes(b"x")
    source.mkdir()

    config_payload = {
        "config_path": str(tmp_path / "win11.json"),
        "partition_info": {"efi_size_mb": 100, "c_size_gb": 100},
        "image_info": {"image_path": str(image)},
        "software_paths": {"ghost64_path": str(ghost), "bcdboot_path": str(bcd)},
        "copy_info": {"source_dir": str(source)},
        "excluded_disk_names": [],
    }
    disk = {
        "disk_number": 1,
        "model": "Target 1",
        "serial_number": "SN-1",
        "unique_id": "UID-1",
        "size_bytes": 500 * 1024 ** 3,
        "size_display": "465.66 GB",
        "partition_style": "GPT",
        "bus_type": "SATA",
        "drive_letters": [],
        "is_boot": False,
        "is_system": False,
        "is_offline": False,
        "is_read_only": False,
    }
    monkeypatch.setattr("app.orchestrator.scan_disk_summaries", lambda: [disk])
    monkeypatch.setattr("app.orchestrator.resolve_path_disk_number", lambda _: 0)

    manager = JobManager(event_dir=tmp_path / "events")
    spec = JobSpec(
        disk_number=1,
        config_payload=config_payload,
        expected_identity={
            "disk_number": 1,
            "unique_id": "UID-1",
            "serial_number": "SN-1",
            "model": "Target 1",
            "size_bytes": 500 * 1024 ** 3,
        },
        drive_letters={"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
        dry_run=True,
    )
    job = manager.submit(spec, require_confirmation=False)
    manager.wait(job.id, timeout=10)

    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result is not None
    assert job.result.dry_run is True
    assert any(step.name == "dry-run" for step in job.result.steps)

    log_text = "\n".join(
        str(event.payload.get("text") or "")
        for event in manager.events(job.id)
        if event.type == JobEventType.LOG.value
    )
    assert "Dry-run 执行计划" in log_text
    assert "未执行任何磁盘操作" in log_text
