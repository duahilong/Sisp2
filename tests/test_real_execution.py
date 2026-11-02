"""回归测试：真实执行路径必须真的可达。

覆盖三处曾经的断点（历史 bug）：

1. ``WorkflowController`` 从没把 ``dry_run`` 传给 ``SispService``（服务默认 True）；
2. ``JobManager`` 的默认执行器会另造一个 ``SispService()``（也是 True），
   控制器上挂的执行器因此被绕过；
3. 真实模式要求注入 ``real_runner``，而仓库里从来没有人注入。

这些测试全部用替身执行器，不碰 diskpart/dism。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from app.application.contracts import DiskInfo
from app.application.jobs import ExecutionPlan, JobManager, JobSpec
from app.application.real_runner import (
    WindowsDiskRunner,
    _CommandLog,
    build_real_runner,
    prune_runner_logs,
)
from app.application.service import SispService
from app.application.workflow import WorkflowController
from app.models import WorkerResult

_GIB = 1024**3
_PAYLOAD: dict[str, Any] = {
    "partition_info": {"efi_size_mb": 300, "c_size_gb": 30, "data_size_gb": "auto"},
    "image_info": {"image_path": r"D:\images\win11.wim", "image_index": 1},
}


def _disk(number: int = 1, *, selectable: bool = True) -> DiskInfo:
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
        is_selectable=selectable,
        protection_reasons=() if selectable else ("测试用：受保护",),
    )


class _FakeService:
    """只实现工作流用到的几个方法；executor 被调用时记一笔。"""

    def __init__(self, disks: list[DiskInfo], *, dry_run: bool = True) -> None:
        self.disks = disks
        self.dry_run = dry_run
        self.allowed_disk_numbers = frozenset()
        self.executed: list[Any] = []

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        letters = {number: {"efi": "E", "windows": "F", "data": "G"} for number in selected}
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        self.executed.append(spec)
        return "executed"


def _preflight(config_path=None) -> dict[str, Any]:
    return {
        "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
        "config_payload": _PAYLOAD,
    }


# ---------------------------------------------------------------- 装配（P0-1）
def test_real_mode_builds_service_with_runner_and_allowlist():
    controller = WorkflowController(dry_run=False, allowed_disk_numbers=[1, 2])

    assert controller.service.dry_run is False
    assert controller.service.allowed_disk_numbers == frozenset({1, 2})
    # 默认执行器必须是真实实现，不能再是 None（旧实现从不注入）。
    assert isinstance(controller.service._real_runner, WindowsDiskRunner)


def test_dry_run_mode_keeps_service_in_dry_run():
    controller = WorkflowController(dry_run=True, allowed_disk_numbers=[1])

    assert controller.service.dry_run is True
    assert controller.service.allowed_disk_numbers == frozenset({1})


def test_default_executor_delegates_to_session_service():
    controller = WorkflowController(dry_run=True)
    fake = _FakeService([])
    controller._service = fake

    spec = SimpleNamespace(dry_run=True)
    result = controller.job_manager._executor(spec, threading.Event())

    assert result == "executed"
    assert fake.executed == [spec]


def test_injected_service_keeps_its_dry_run_but_gets_allowlist():
    service = SispService(disk_scanner=lambda: [], dry_run=True)
    controller = WorkflowController(service=service, dry_run=False, allowed_disk_numbers=[3])

    controller._sync_service_policy(dry_run=False)

    # 外部注入的服务归调用方管，只同步白名单，不强行改 dry_run。
    assert service.dry_run is True
    assert service.allowed_disk_numbers == frozenset({3})


def test_custom_job_manager_is_respected():
    manager = JobManager()
    controller = WorkflowController(dry_run=True, job_manager=manager)

    assert controller.job_manager is manager


# ---------------------------------------------------------------- 计划（白名单）
def test_prepare_grants_only_selected_disks_when_no_explicit_allowlist():
    service = _FakeService([_disk(1)])
    controller = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
    )
    controller.start(None, dry_run=False)

    prepared = controller.prepare([1])

    assert service.allowed_disk_numbers == frozenset({1})
    checks = {check.key: check for check in prepared[0].plan.checks}
    assert checks["write_allowlist"].passed is True
    assert "选中" in checks["write_allowlist"].message


def test_prepare_keeps_explicit_allowlist_untouched():
    service = _FakeService([_disk(1), _disk(2)])
    controller = WorkflowController(
        service=service,
        dry_run=False,
        allowed_disk_numbers=[1],
        preflight_runner=_preflight,
    )
    controller.start(None, dry_run=False)

    prepared = controller.prepare([1])

    assert service.allowed_disk_numbers == frozenset({1})
    checks = {check.key: check for check in prepared[0].plan.checks}
    assert checks["write_allowlist"].message.startswith("白名单")


def test_real_plan_blocks_disk_outside_allowlist():
    service = _FakeService([_disk(1), _disk(2)])
    controller = WorkflowController(
        service=service,
        dry_run=False,
        allowed_disk_numbers=[2],
        preflight_runner=_preflight,
    )
    controller.start(None, dry_run=False)

    prepared = controller.prepare([1])

    assert prepared[0].plan.blockers
    assert any("白名单" in blocker for blocker in prepared[0].plan.blockers)


# ---------------------------------------------------------------- 执行器（P0-2）
def test_build_real_runner_returns_windows_runner():
    assert isinstance(build_real_runner(), WindowsDiskRunner)


def test_prune_runner_logs_removes_only_expired_runner_logs(tmp_path):
    old = tmp_path / "disk-runner-old.log"
    recent = tmp_path / "disk-runner-new.log"
    unrelated = tmp_path / "other.log"
    old.write_text("old")
    recent.write_text("new")
    unrelated.write_text("keep")
    import os
    import time
    os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))

    assert prune_runner_logs(tmp_path, 2) == 1
    assert not old.exists()
    assert recent.exists() and unrelated.exists()


def test_diskpart_script_layout():
    runner = WindowsDiskRunner()
    lines = runner._diskpart_script(
        disk_number=2,
        efi_mb=300,
        c_gb=30,
        data_gb=None,
        letters={"efi": "E", "windows": "F", "data": "G"},
        log=_CommandLog(None),
    )

    assert lines[0] == "select disk 2"
    assert "clean" in lines
    assert "convert gpt" in lines
    assert "create partition efi size=300" in lines
    assert "create partition primary size=30720" in lines  # 30 GB * 1024 MB
    assert "create partition primary" in lines  # 数据分区吃剩余空间
    assert lines[-1] == "exit"


def test_diskpart_script_uses_explicit_data_size():
    runner = WindowsDiskRunner()
    lines = runner._diskpart_script(
        disk_number=1,
        efi_mb=100,
        c_gb=10,
        data_gb=5,
        letters={"efi": "E", "windows": "F", "data": "G"},
        log=_CommandLog(None),
    )

    assert "create partition primary size=5120" in lines


def test_runner_reports_missing_image_without_touching_disks(tmp_path):
    runner = WindowsDiskRunner(
        diskpart=r"C:\nope\diskpart.exe",
        dism=r"C:\nope\dism.exe",
        bcdboot=r"C:\nope\bcdboot.exe",
    )
    spec = SimpleNamespace(
        disk_number=1,
        config_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(tmp_path / "missing.wim"), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "镜像文件不存在" in (result.error or "")
    assert result.steps == ()


def test_runner_fails_cleanly_when_diskpart_cannot_start(tmp_path):
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"not a real wim")
    runner = WindowsDiskRunner(
        diskpart=r"C:\nope\diskpart.exe",
        dism=r"C:\nope\dism.exe",
        bcdboot=r"C:\nope\bcdboot.exe",
    )
    spec = SimpleNamespace(
        disk_number=1,
        config_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "无法执行 diskpart" in (result.error or "")
    efi = next(step for step in result.steps if step.key == "efi")
    assert efi.failed is True


def test_runner_rejects_incomplete_letters(tmp_path):
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner()
    spec = SimpleNamespace(
        disk_number=1,
        config_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "缺少盘符分配" in (result.error or "")


def test_runner_honours_pre_cancelled_event(tmp_path):
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner()
    spec = SimpleNamespace(
        disk_number=1,
        config_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )
    cancel = threading.Event()
    cancel.set()

    result = runner.run(spec, cancel)

    assert result.success is False
    assert "已取消" in (result.error or "")


# ---------------------------------------------------------------- P0 回归
def test_forced_dry_run_cannot_be_upgraded():
    """构造时 dry_run=True 是硬上限，start/prepare 不能把它升级为真实写盘。"""
    seen: list[bool] = []

    def preflight(config_path=None, *, dry_run: bool = False) -> dict[str, Any]:
        seen.append(dry_run)
        return {"results": [], "config_payload": _PAYLOAD}

    controller = WorkflowController(
        service=_FakeService([_disk(1)], dry_run=True),
        dry_run=True,
        preflight_runner=preflight,
    )

    session = controller.start(None, dry_run=False)

    assert session.dry_run is True
    assert seen == [True]

    prepared = controller.prepare([1], dry_run=False)
    assert prepared[0].spec.dry_run is True
    assert prepared[0].plan.dry_run is True


def test_preflight_receives_effective_dry_run():
    """非强制模式也要把当前 dry-run 传给预检，dry-run 不要求管理员。"""
    seen: list[bool] = []

    def preflight(config_path=None, *, dry_run: bool = False) -> dict[str, Any]:
        seen.append(dry_run)
        return {"results": [], "config_payload": _PAYLOAD}

    controller = WorkflowController(
        service=_FakeService([_disk(1)], dry_run=False),
        dry_run=False,
        preflight_runner=preflight,
    )

    controller.start(None, dry_run=False)

    assert seen == [False]


def test_concurrent_confirm_only_runs_once():
    """两个并发的 confirm 请求只能启动一个执行器，避免重复清盘。"""
    calls: list[int] = []
    release = threading.Event()
    barrier = threading.Barrier(3)
    errors: list[str] = []

    def executor(spec: Any, cancel_event: threading.Event) -> WorkerResult:
        calls.append(spec.disk_number)
        release.wait(2)
        return WorkerResult(
            disk_number=spec.disk_number,
            success=True,
            message="ok",
            dry_run=True,
        )

    manager = JobManager(executor=executor)
    job = manager.submit(
        JobSpec(disk_number=1, dry_run=True),
        ExecutionPlan(disk_number=1),
    )

    def confirm() -> None:
        barrier.wait()
        try:
            manager.confirm_batch([job.id])
        except ValueError as exc:
            errors.append(str(exc))

    workers = [threading.Thread(target=confirm) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(2)

    release.set()
    manager.wait_all(2)

    assert calls == [1]
    assert len(errors) == 1


def test_duplicate_submit_for_same_disk_is_rejected():
    """同一块盘已有未结束任务时，第二次 submit 不能创建并发任务。"""
    manager = JobManager()
    spec = JobSpec(disk_number=1, dry_run=True)
    plan = ExecutionPlan(disk_number=1)
    manager.submit(spec, plan)

    with pytest.raises(ValueError, match="已有未结束任务"):
        manager.submit(spec, plan)


# ---------------------------------------------------------------- 界面设置接线
def test_partition_defaults_fill_only_missing_keys():
    def preflight(config_path=None):
        return {
            "results": [],
            "config_payload": {
                "partition_info": {"efi_size_mb": 300, "data_size_gb": "auto"},
                "image_info": {},
            },
        }

    controller = WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 20, "data_size_gb": 50},
    )
    controller.start(None, dry_run=True)

    info = controller.prepare([1])[0].spec.config_payload["partition_info"]

    assert info["efi_size_mb"] == 300      # 业务配置优先
    assert info["c_size_gb"] == 20         # 缺失字段用设置默认值
    assert info["data_size_gb"] == "auto"  # auto 不被默认值覆盖


def test_partition_defaults_apply_when_config_has_no_partition_info():
    def preflight(config_path=None):
        return {"results": [], "config_payload": {"image_info": {}}}

    controller = WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 20, "data_size_gb": None},
    )
    controller.start(None, dry_run=True)

    info = controller.prepare([1])[0].spec.config_payload["partition_info"]

    assert info == {"efi_size_mb": 100, "c_size_gb": 20}


def test_tool_paths_reach_default_real_runner():
    controller = WorkflowController(
        dry_run=False,
        tool_paths={"diskpart": r"C:\tools\diskpart.exe", "dism": r"C:\tools\dism.exe", "": ""},
    )

    runner = controller.service._real_runner

    assert runner.diskpart == r"C:\tools\diskpart.exe"
    assert runner.dism == r"C:\tools\dism.exe"


def test_build_real_runner_accepts_tool_paths():
    runner = build_real_runner(diskpart=r"C:\d\diskpart.exe", dism=r"C:\d\dism.exe")

    assert (runner.diskpart, runner.dism) == (r"C:\d\diskpart.exe", r"C:\d\dism.exe")


def test_prepare_does_not_accumulate_implicit_allowlist():
    service = _FakeService([_disk(1), _disk(2)])
    controller = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
    )
    controller.start(None, dry_run=False)

    controller.prepare([1])
    assert service.allowed_disk_numbers == frozenset({1})

    # 第二次只选 2：上一次隐式加入的 1 必须撤出，不能累积。
    controller.prepare([2])
    assert service.allowed_disk_numbers == frozenset({2})

    # 开新会话同样清空隐式授权。
    controller.start(None, dry_run=False)
    assert service.allowed_disk_numbers == frozenset()


# ---------------------------------------------------------------- EFI 盘符释放
def test_release_efi_letter_builds_diskpart_script():
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []

    def fake_diskpart(lines, log, timeout=600.0):
        seen.append(list(lines))
        return 0, "ok"

    runner._run_diskpart = fake_diskpart  # type: ignore[method-assign]
    runner._release_efi_letter(2, "E", _CommandLog(None))

    assert seen == [["select volume E", "remove letter=E", "exit"]]


def test_release_efi_letter_skips_without_letter():
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    runner._run_diskpart = lambda lines, log, timeout=600.0: (seen.append(list(lines)) or (0, "ok"))

    runner._release_efi_letter(2, None, _CommandLog(None))

    assert seen == []


def test_runner_releases_efi_letter_even_when_image_fails(tmp_path):
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    scripts: list[list[str]] = []

    def fake_diskpart(lines, log, timeout=600.0):
        scripts.append(list(lines))
        return 0, "ok"

    runner._run_diskpart = fake_diskpart  # type: ignore[method-assign]
    runner._apply_image = lambda image_path, index, target, log: (87, "bad image", 0.1)

    spec = SimpleNamespace(
        disk_number=2,
        config_payload={
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 3, "data_size_gb": "auto"},
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "应用镜像失败" in (result.error or "")
    assert scripts[0][:2] == ["select disk 2", "clean"]
    assert ["select volume E", "remove letter=E", "exit"] in scripts
