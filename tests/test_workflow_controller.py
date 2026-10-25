"""第三阶段 3A：工作流适配层与批量调度测试。

这些测试不依赖 Qt、PowerShell 或真实磁盘：``SispService`` 使用假的扫描与
计划实现，``JobManager`` 使用同步 runner，批量调度使用可注入的 sleeper。
"""

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application.contracts import CheckItem, DiskInfo, SelectionPlan
from app.application.jobs import (
    DEFAULT_PLAN_STEPS,
    ExecutionPlan,
    JobManager,
    JobSpec,
    JobStatus,
)
from app.application.workflow import (
    SessionState,
    WorkflowController,
    WorkflowError,
)
from app.models import RunContext, WorkerResult
from app.modules.common.constants import ErrorCode

GB = 1024 ** 3


def make_disk(
    disk_number: int,
    *,
    selectable: bool = True,
    strong_identity: bool = True,
    reasons: tuple[str, ...] = (),
) -> dict:
    return {
        "disk_number": disk_number,
        "model": f"Target {disk_number}",
        "serial_number": f"SN-{disk_number}" if strong_identity else None,
        "unique_id": f"UID-{disk_number}" if strong_identity else None,
        "size_bytes": 500 * GB,
        "size_display": "465.66 GB",
        "partition_style": "GPT",
        "bus_type": "SATA",
        "drive_letters": [],
        "is_boot": False,
        "is_system": not selectable,
        "is_offline": False,
        "is_read_only": False,
        "is_selectable": selectable,
        "protection_reasons": list(reasons),
    }


def make_preflight(
    *,
    admin: bool = True,
    powershell: bool = True,
    payload: dict | None = None,
) -> dict:
    results = [
        {
            "name": "管理员权限检查",
            "passed": admin,
            "message": "已以管理员权限运行" if admin else "当前未以管理员权限运行",
        },
        {
            "name": "PowerShell 可用性检查",
            "passed": powershell,
            "message": "PowerShell 可正常调用" if powershell else "PowerShell 不可用",
        },
        {
            "name": "配置文件存在性检查",
            "passed": payload is not None,
            "message": "配置文件存在" if payload is not None else "配置文件不存在",
        },
        {
            "name": "配置文件可解析性检查",
            "passed": payload is not None,
            "message": "配置可解析" if payload is not None else "配置解析失败",
        },
    ]
    return {
        "all_passed": all(result["passed"] for result in results),
        "results": results,
        "config_payload": payload,
    }


def make_config() -> dict:
    return {
        "config_path": "D:\\config\\win11.json",
        "excluded_disk_names": [],
    }


class FakeService:
    """只实现 WorkflowController 使用到的应用服务方法。"""

    def __init__(self, disks: list[dict], *, plan_error: str | None = None) -> None:
        self.disks = disks
        self.plan_error = plan_error
        self.plan_calls: list[tuple[int, ...]] = []

    def scan_protected_disks(self, excluded_names=None) -> list[DiskInfo]:
        _ = excluded_names
        return [DiskInfo.from_summary(disk) for disk in self.disks]

    def build_selection_plan(
        self,
        config_payload,
        disk_summaries,
        selected_disk_numbers,
        **kwargs,
    ) -> SelectionPlan:
        _ = kwargs
        self.plan_calls.append(tuple(selected_disk_numbers))
        if self.plan_error:
            raise ValueError(self.plan_error)

        selectable = {
            disk["disk_number"]: disk
            for disk in disk_summaries
            if disk.get("is_selectable", True)
        }
        for number in selected_disk_numbers:
            if number not in selectable:
                raise ValueError(f"硬盘编号 {number} 不可选择: 受保护")

        pool = ["E", "F", "G", "H", "I", "J", "K", "L", "M", "N"]
        allocations: dict[int, dict[str, str]] = {}
        index = 0
        for number in selected_disk_numbers:
            allocations[number] = {
                "efi": pool[index],
                "windows": pool[index + 1],
                "data1": pool[index + 2],
                "data2": pool[index + 3],
            }
            index += 4
        return SelectionPlan(
            selected_disk_numbers=tuple(selected_disk_numbers),
            forbidden_drive_letters=(),
            drive_letter_allocations=allocations,
        )


def successful_runner(spec: JobSpec, context: RunContext) -> None:
    context.start_step("初始化")
    context.reporter.step_started(1, 7, "初始化硬盘")
    context.record_step("初始化", True, "初始化完成")
    context.reporter.step_finished(1, 7, "初始化硬盘", True, "初始化完成")


def build_controller(
    disks: list[dict] | None = None,
    *,
    payload: dict | None = None,
    dry_run: bool = False,
    admin: bool = True,
    plan_error: str | None = None,
    runner=successful_runner,
    sleeper=None,
) -> WorkflowController:
    service = FakeService(disks or [make_disk(1)], plan_error=plan_error)
    manager = JobManager(runner=runner, sleeper=sleeper or time.sleep)
    controller = WorkflowController(
        service=service,
        job_manager=manager,
        preflight_runner=lambda _path: make_preflight(
            admin=admin,
            payload=payload if payload is not None else make_config(),
        ),
        dry_run=dry_run,
        worker_interval=6.0,
        max_parallel=1,
    )
    return controller


def make_job_spec(disk_number: int) -> JobSpec:
    return JobSpec(
        disk_number=disk_number,
        config_payload={"config_path": "D:\\config\\win11.json"},
        expected_identity={
            "disk_number": disk_number,
            "unique_id": f"UID-{disk_number}",
            "serial_number": f"SN-{disk_number}",
            "model": f"Target {disk_number}",
            "size_bytes": 500 * GB,
        },
        drive_letters={"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
    )


# --------------------------------------------------------------------- 会话
def test_start_builds_session_with_checks_and_disks() -> None:
    controller = build_controller(
        [
            make_disk(1),
            make_disk(2, selectable=False, reasons=("系统盘",)),
        ]
    )

    session = controller.start("D:\\config\\win11.json")

    assert isinstance(session, SessionState)
    assert session.ready is True
    assert [check.key for check in session.checks] == [
        "admin",
        "powershell",
        "config_exists",
        "config_parse",
    ]
    assert len(session.disks) == 2
    assert [disk.disk_number for disk in session.selectable_disks] == [1]
    assert session.config_path == "D:\\config\\win11.json"
    assert session.dry_run is False
    assert controller.session is session


def test_start_records_failed_checks_as_blockers() -> None:
    controller = build_controller(admin=False)

    session = controller.start()

    assert session.ready is False
    assert session.can_prepare is True
    assert any("管理员权限检查" in blocker for blocker in session.blockers)
    # 权限失败不影响只读扫描，界面仍可展示硬盘。
    assert [disk.disk_number for disk in session.disks] == [1]


def test_start_handles_preflight_exception() -> None:
    manager = JobManager(runner=successful_runner)

    def failing_preflight(_path):
        raise RuntimeError("probe boom")

    controller = WorkflowController(
        service=FakeService([make_disk(1)]),
        job_manager=manager,
        preflight_runner=failing_preflight,
    )

    session = controller.start()

    assert session.ready is False
    assert session.can_prepare is False
    assert session.config_payload is None
    # 配置不可用时仍然做只读扫描，方便界面展示硬盘，但不应用排除规则。
    assert [disk.disk_number for disk in session.disks] == [1]
    assert any("probe boom" in blocker for blocker in session.blockers)


def test_start_without_config_scan_failure_and_no_selectable_warning() -> None:
    controller = build_controller(
        [make_disk(3, selectable=False, reasons=("启动盘",))],
        payload={},
    )
    # payload 为空字典时仍视为配置可用，但扫描结果全部受保护。
    session = controller.start()

    assert session.config_payload == {}
    assert session.ready is True
    assert session.warnings
    assert "没有可选硬盘" in session.warnings[0]

    def broken_scan(_excluded=None):
        raise OSError("scan failed")

    controller._service.scan_protected_disks = broken_scan  # type: ignore[attr-defined]
    session = controller.start()
    assert any("硬盘扫描失败" in blocker for blocker in session.blockers)


def test_refresh_disks_rescans_and_clears_prepared_jobs() -> None:
    controller = build_controller([make_disk(1)])
    controller.start()
    controller.prepare([1])
    assert controller.prepared_jobs

    controller._service.disks = [make_disk(1), make_disk(2)]  # type: ignore[attr-defined]
    disks = controller.refresh_disks()

    assert [disk.disk_number for disk in disks] == [1, 2]
    assert controller.session is not None
    assert [disk.disk_number for disk in controller.session.disks] == [1, 2]
    assert controller.prepared_jobs == ()


def test_refresh_disks_requires_started_session() -> None:
    controller = build_controller()
    with pytest.raises(WorkflowError, match="尚未启动"):
        controller.refresh_disks()


# --------------------------------------------------------------------- 计划
def test_prepare_builds_plan_for_multiple_disks() -> None:
    controller = build_controller([make_disk(1), make_disk(2)])
    controller.start()

    prepared = controller.prepare([1, 2])

    assert [item.spec.disk_number for item in prepared] == [1, 2]
    assert [item.plan.disk_number for item in prepared] == [1, 2]
    first = prepared[0]
    assert first.plan.steps == DEFAULT_PLAN_STEPS
    assert first.plan.identity["unique_id"] == "UID-1"
    assert first.plan.model == "Target 1"
    assert first.plan.size_display == "465.66 GB"
    assert first.plan.drive_letters == {
        "efi": "E",
        "windows": "F",
        "data1": "G",
        "data2": "H",
    }
    assert prepared[1].plan.drive_letters["efi"] == "I"
    assert first.plan.can_execute is True
    assert first.plan.blockers == ()
    assert [check.key for check in first.plan.checks] == [
        "selectable",
        "identity",
        "drive_letters",
    ]
    assert any("破坏性操作" in warning for warning in first.plan.warnings)
    assert any("协作式" in warning for warning in first.plan.warnings)
    assert controller.prepared_jobs == tuple(prepared)
    assert first.spec.expected_identity == first.plan.identity


def test_prepare_requires_start_and_selection() -> None:
    controller = build_controller()
    with pytest.raises(WorkflowError, match="尚未启动"):
        controller.prepare([1])

    controller.start()
    with pytest.raises(WorkflowError, match="尚未选择"):
        controller.prepare([])


def test_prepare_wraps_selection_errors() -> None:
    controller = build_controller(plan_error="盘符不足")
    controller.start()

    with pytest.raises(WorkflowError, match="无法生成执行计划.*盘符不足"):
        controller.prepare([1])


def test_prepare_dry_run_skips_strong_identity_requirement() -> None:
    controller = build_controller([make_disk(1, strong_identity=False)], dry_run=True)
    controller.start()

    prepared = controller.prepare([1])

    plan = prepared[0].plan
    assert plan.dry_run is True
    assert prepared[0].spec.dry_run is True
    assert plan.blockers == ()
    identity_check = next(check for check in plan.checks if check.key == "identity")
    assert identity_check.passed is True
    assert "dry-run" in identity_check.message


def test_prepare_blocks_weak_identity_when_writing() -> None:
    controller = build_controller([make_disk(1, strong_identity=False)])
    controller.start()

    prepared = controller.prepare([1])
    plan = prepared[0].plan

    assert plan.dry_run is False
    assert plan.can_execute is False
    assert any("UniqueId" in blocker for blocker in plan.blockers)
    identity_check = next(check for check in plan.checks if check.key == "identity")
    assert identity_check.passed is False


# --------------------------------------------------------------------- 执行
def test_submit_and_confirm_runs_jobs() -> None:
    controller = build_controller([make_disk(1), make_disk(2)])
    controller.start()
    controller.prepare([1, 2])

    jobs = controller.submit()

    assert [job.status for job in jobs] == [
        JobStatus.WAITING_CONFIRMATION,
        JobStatus.WAITING_CONFIRMATION,
    ]
    assert controller.results() == []

    controller.confirm()
    controller.wait_all(timeout=10)

    assert all(job.status == JobStatus.SUCCEEDED for job in jobs)
    results = controller.results()
    assert [result.disk_number for result in results] == [1, 2]
    assert all(result.success for result in results)
    assert all(result.error_code == ErrorCode.SUCCESS for result in results)


def test_submit_without_confirmation_starts_jobs() -> None:
    controller = build_controller()
    controller.start()
    controller.prepare([1])

    jobs = controller.submit(require_confirmation=False)
    controller.wait_all(timeout=10)

    assert jobs[0].status == JobStatus.SUCCEEDED


def test_submit_requires_prepared_jobs() -> None:
    controller = build_controller()
    controller.start()
    with pytest.raises(WorkflowError, match="没有可提交的任务"):
        controller.submit()


def test_confirm_rejects_plan_with_blockers() -> None:
    controller = build_controller([make_disk(1, strong_identity=False)])
    controller.start()
    controller.prepare([1])
    jobs = controller.submit()

    with pytest.raises(WorkflowError, match="阻断项"):
        controller.confirm()

    assert jobs[0].status == JobStatus.WAITING_CONFIRMATION
    with pytest.raises(ValueError, match="阻断项"):
        controller.job_manager.confirm(jobs[0].id)
    assert jobs[0].status == JobStatus.WAITING_CONFIRMATION


def test_submit_without_confirmation_rejects_blockers_before_creating_jobs() -> None:
    controller = build_controller([make_disk(1, strong_identity=False)])
    controller.start()
    controller.prepare([1])

    with pytest.raises(WorkflowError, match="阻断项"):
        controller.submit(require_confirmation=False)

    assert controller.jobs() == []
    assert controller.job_manager.jobs() == []


def test_cancel_waiting_jobs() -> None:
    controller = build_controller()
    controller.start()
    controller.prepare([1])
    jobs = controller.submit()

    cancelled = controller.cancel(reason="用户放弃")

    assert cancelled == [jobs[0].id]
    assert jobs[0].status == JobStatus.CANCELLED
    results = controller.results()
    assert results[0].error_code == ErrorCode.CANCELLED
    # 已结束任务不再重复取消。
    assert controller.cancel([jobs[0].id]) == []


def test_select_jobs_rejects_unknown_id() -> None:
    controller = build_controller()
    controller.start()
    with pytest.raises(WorkflowError, match="任务不存在"):
        controller.confirm(["missing"])


def test_execution_plan_serializes_checks_and_blockers() -> None:
    plan = ExecutionPlan(
        disk_number=1,
        checks=(CheckItem("identity", "硬盘强身份", False, "缺少身份"),),
        blockers=("缺少身份",),
        warnings=("破坏性操作",),
    )

    data = plan.to_dict()

    assert data["can_execute"] is False
    assert data["blockers"] == ["缺少身份"]
    assert data["checks"][0]["key"] == "identity"
    assert plan.can_execute is False


# --------------------------------------------------------------------- 批量调度
def test_start_batch_applies_interval_between_sequential_jobs() -> None:
    sleeps: list[float] = []
    manager = JobManager(runner=successful_runner, sleeper=sleeps.append)

    jobs = manager.start_batch(
        [make_job_spec(1), make_job_spec(2), make_job_spec(3)],
        interval=6.0,
        max_parallel=1,
    )
    manager.wait_all(timeout=10)

    assert [job.status for job in jobs] == [JobStatus.SUCCEEDED] * 3
    assert sleeps == [6.0, 6.0]


def test_start_batch_respects_max_parallel() -> None:
    lock = threading.Lock()
    started: list[int] = []
    active = 0
    max_active = 0
    two_started = threading.Event()
    release = threading.Event()

    def blocking_runner(spec: JobSpec, context: RunContext) -> None:
        nonlocal active, max_active
        with lock:
            started.append(spec.disk_number)
            active += 1
            max_active = max(max_active, active)
            if len(started) == 2:
                two_started.set()
        release.wait(5)
        with lock:
            active -= 1

    manager = JobManager(runner=blocking_runner)
    jobs = manager.start_batch(
        [make_job_spec(1), make_job_spec(2), make_job_spec(3)],
        interval=0.0,
        max_parallel=2,
    )

    assert two_started.wait(2) is True
    assert started == [1, 2]
    assert all(job.status == JobStatus.RUNNING for job in jobs[:2])
    assert jobs[2].status == JobStatus.WAITING_CONFIRMATION

    release.set()
    manager.wait_all(timeout=10)

    assert [job.status for job in jobs] == [JobStatus.SUCCEEDED] * 3
    assert started == [1, 2, 3]
    assert max_active == 2


def test_start_batch_rejects_blocked_plan_before_submitting() -> None:
    manager = JobManager(runner=successful_runner)
    plan = ExecutionPlan(disk_number=1, blockers=("盘符分配不完整",))

    with pytest.raises(ValueError, match="阻断项"):
        manager.start_batch([make_job_spec(1)], [plan], interval=0.0)

    assert manager.jobs() == []


def test_confirm_batch_rejects_blocked_job() -> None:
    manager = JobManager(runner=successful_runner)
    plan = ExecutionPlan(disk_number=1, blockers=("缺少身份",))
    job = manager.submit(make_job_spec(1), plan)

    with pytest.raises(ValueError, match="阻断项"):
        manager.confirm_batch([job.id])

    assert job.status == JobStatus.WAITING_CONFIRMATION


def test_confirm_batch_validation() -> None:
    manager = JobManager(runner=successful_runner)
    with pytest.raises(ValueError, match="max_parallel"):
        manager.confirm_batch([], max_parallel=0)
    with pytest.raises(ValueError, match="interval"):
        manager.confirm_batch([], interval=-1)
    assert manager.confirm_batch([]) == []


def test_wait_all_returns_result_for_all_jobs() -> None:
    manager = JobManager(runner=successful_runner)
    manager.start_batch(
        [make_job_spec(1), make_job_spec(2)],
        interval=0.0,
        max_parallel=2,
    )

    jobs = manager.wait_all(timeout=10)

    assert len(jobs) == 2
    assert all(isinstance(job.result, WorkerResult) for job in jobs)
