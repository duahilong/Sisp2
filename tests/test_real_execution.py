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
from app.application.jobs import ExecutionPlan, Job, JobManager, JobSpec, JobStatus
from app.application.partitions import plan_partitions
from app.application.real_runner import (
    STEP_OK,
    STEP_TITLES,
    RunnerError,
    StepResult,
    WindowsDiskRunner,
    _CommandLog,
    build_real_runner,
    prune_runner_logs,
)
from app.application.service import SispService
from app.application.workflow import WorkflowController
from app.models import WorkerResult

_GIB = 1024**3
# 单测用小尺寸（C 30 GB）测算术：显式放宽后端下限（真实布局 C ≥ 60 GB）。
_PAYLOAD: dict[str, Any] = {
    "partition_info": {
        "efi_size_mb": 300,
        "c_size_gb": 30,
        "data_size_gb": "auto",
        "allow_small_partitions": True,
    },
    "image_info": {"image_path": r"D:\images\win11.wim", "image_index": 1},
}


def _disk(number: int = 1, *, selectable: bool = True, size_gb: int = 60) -> DiskInfo:
    return DiskInfo(
        disk_number=number,
        model="test disk",
        serial_number=None,
        unique_id=f"unique-{number}",
        size_bytes=size_gb * _GIB,
        size_display=f"{size_gb} GB",
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
    service = SispService(disk_scanner=list, dry_run=True)
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


def test_image_blocker_helper_is_pure(tmp_path):
    """``_image_blocker`` 只看 payload 里的镜像路径，不碰磁盘。"""
    from app.application.workflow import _image_blocker

    exists = tmp_path / "ok.wim"
    exists.write_bytes(b"wim")
    assert _image_blocker(None) is None
    assert _image_blocker({"image_info": {}}) is None
    assert _image_blocker({"image_info": {"image_path": str(exists)}}) is None
    missing = _image_blocker({"image_info": {"image_path": str(tmp_path / "no.wim")}})
    assert missing and "镜像文件不存在" in missing


def test_per_disk_image_override_recomputes_image_blocker(tmp_path):
    """第 3 步换了镜像之后，计划里的“镜像文件不存在”必须跟着重算。

    会话级预检只在 ``start()`` 里按配置文件算一次；如果计划直接沿用那份阻断项，
    用户在第 3 步选了一个真实存在的镜像也永远开始不了——第 4 步会一直挂着阻断。
    """
    good = tmp_path / "ok.wim"
    good.write_bytes(b"wim")

    def preflight(config_path=None):
        return {
            "results": [
                {
                    "name": "镜像文件存在性检查",
                    "passed": False,
                    "message": r"镜像文件不存在: D:\images\missing.wim",
                }
            ],
            "config_payload": {
                "partition_info": {"c_size_gb": 30, "allow_small_partitions": True},
                "image_info": {"image_path": r"D:\images\missing.wim"},
            },
        }

    controller = WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        image_indexes=lambda path: [1],
    )
    controller.start(None, dry_run=True)

    stale = controller.prepare({1: {}})[0]
    assert any("镜像文件不存在" in item for item in stale.plan.blockers)

    fixed = controller.prepare({1: {"win_gho": str(good)}})[0]
    assert not any("镜像文件不存在" in item for item in fixed.plan.blockers)
    assert fixed.spec.config_payload["image_info"]["image_path"] == str(good)
    assert tuple(fixed.plan.blockers) == ()
    assert fixed.plan.executable is True


def test_authorize_disks_grants_single_disk_operations():
    """分区/修复引导不经过 prepare，必须自己授权，否则真实执行被白名单挡掉。"""
    service = _FakeService([_disk(1), _disk(2)])
    controller = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
    )
    controller.start(None, dry_run=False)

    assert service.allowed_disk_numbers == frozenset()
    controller.authorize_disks([1])
    assert service.allowed_disk_numbers == frozenset({1})
    # 再授权另一块盘是追加，不是覆盖
    controller.authorize_disks([2])
    assert service.allowed_disk_numbers == frozenset({1, 2})


def test_authorize_disks_respects_explicit_allowlist_and_dry_run():
    service = _FakeService([_disk(1), _disk(2)])
    controller = WorkflowController(
        service=service,
        dry_run=False,
        allowed_disk_numbers=[1],
        preflight_runner=_preflight,
    )
    controller.start(None, dry_run=False)

    controller.authorize_disks([2])
    assert service.allowed_disk_numbers == frozenset({1})  # 显式白名单不动

    dry_service = _FakeService([_disk(1)])
    dry = WorkflowController(service=dry_service, dry_run=True, preflight_runner=_preflight)
    dry.start(None, dry_run=True)
    dry.authorize_disks([1])
    assert dry_service.allowed_disk_numbers == frozenset()  # 演练模式不授权


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
    """分区脚本分三段：清盘+转 GPT → 删 MSR → 建分区（各段独立校验）。"""
    runner = WindowsDiskRunner()
    log = _CommandLog(None)
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 300, "c_size_gb": 30}},
        letters={"efi": "E", "windows": "F", "data": "G"},
    )

    wipe = runner._wipe_script(disk_number=2, log=log)
    assert wipe == ["select disk 2", "clean", "convert gpt", "exit"]

    delete = runner._msr_delete_script(disk_number=2, msr_partition=1, log=log)
    assert delete == ["select disk 2", "select partition 1", "delete partition override", "exit"]

    layout = runner._diskpart_script(disk_number=2, parts=parts, log=log)
    assert layout[0] == "select disk 2"
    assert "delete partition override" not in layout  # MSR 在上一段已删
    assert "create partition efi size=300" in layout
    assert "create partition primary size=30720" in layout  # 30 GB * 1024 MB
    assert "create partition primary" in layout  # 最后一项吃剩余空间
    assert layout[-1] == "exit"


def test_diskpart_script_lets_last_data_partition_eat_remaining_space():
    """最后一个数据分区可以不带 size：diskpart 用掉全部剩余空间。

    这比“总容量 − C − EFI − 对齐余量”更准，也不会出现“界面上刚好、
    diskpart 报空间不足”——所以界面的准备页面（selectionPayload）在把盘填满时
    就发 ``"auto"``。
    """
    runner = WindowsDiskRunner()
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 100,
                "c_size_gb": 120,
                "data_partitions": [
                    {"size_gb": 200, "file_system": "NTFS"},
                    {"size_gb": "auto", "file_system": "NTFS"},
                ],
            }
        },
        letters={"efi": "E", "windows": "F", "data": "G", "data2": "H"},
    )
    lines = runner._diskpart_script(disk_number=1, parts=parts, log=_CommandLog(None))

    assert "create partition primary size=122880" in lines          # C 120 GB
    assert "create partition primary size=204800" in lines          # D 200 GB
    # 只有最后一个数据分区不带 size，且它是最后一个 create 行
    creates = [line for line in lines if line.startswith("create partition primary")]
    assert creates == ["create partition primary size=122880",
                       "create partition primary size=204800",
                       "create partition primary"]
    assert "assign letter=H" in lines


def test_diskpart_script_uses_explicit_data_size():
    runner = WindowsDiskRunner()
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 100, "c_size_gb": 10, "data_size_gb": 5}},
        letters={"efi": "E", "windows": "F", "data": "G"},
    )
    lines = runner._diskpart_script(disk_number=1, parts=parts, log=_CommandLog(None))

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
def test_partition_defaults_settings_win_by_default():
    """没有显式 ``-j`` 时界面设置优先：设置页改了什么，计划就用什么。"""
    def preflight(config_path=None):
        return {
            "results": [],
            "config_payload": {
                "partition_info": {"efi_size_mb": 300, "data_size_gb": "auto"},
                "image_info": {},
            },
        }

    controller = WorkflowController(
        service=_FakeService([_disk(1, size_gb=1000)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 120, "data_size_gb": 30},
    )
    controller.start(None, dry_run=True)

    info = controller.prepare([1])[0].spec.config_payload["partition_info"]

    assert info["efi_size_mb"] == 100      # 设置值覆盖配置
    assert info["c_size_gb"] == 120
    assert info["data_size_gb"] == 30


def test_partition_defaults_config_wins_when_config_explicit():
    """显式 ``-j/--config`` 的会话保持“配置优先”，设置只补缺失字段。"""
    def preflight(config_path=None):
        return {
            "results": [],
            "config_payload": {
                "partition_info": {"efi_size_mb": 300, "data_size_gb": "auto"},
                "image_info": {},
            },
        }

    controller = WorkflowController(
        service=_FakeService([_disk(1, size_gb=1000)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 120, "data_size_gb": 30},
    )
    session = controller.start("preset.json", dry_run=True)
    assert session.config_explicit is True

    info = controller.prepare([1])[0].spec.config_payload["partition_info"]

    assert info["efi_size_mb"] == 300      # 业务配置优先
    assert info["c_size_gb"] == 120        # 缺失字段用设置默认值
    assert info["data_size_gb"] == "auto"  # auto 不被默认值覆盖


def test_partition_defaults_apply_when_config_has_no_partition_info():
    def preflight(config_path=None):
        return {"results": [], "config_payload": {"image_info": {}}}

    controller = WorkflowController(
        service=_FakeService([_disk(1, size_gb=1000)]),
        dry_run=True,
        preflight_runner=preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 120, "data_size_gb": None},
    )
    controller.start(None, dry_run=True)

    info = controller.prepare([1])[0].spec.config_payload["partition_info"]

    assert info == {"efi_size_mb": 100, "c_size_gb": 120}


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
def _esp_payload(number=1):
    return {
        "partition_style": "GPT",
        "largest_free": 0,
        "partitions": [
            {
                "number": number,
                "offset_bytes": 1024 * 1024,
                "size_bytes": 100 * 1024 * 1024,
                "type": "System",
                "gpt_type": "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}",
                "drive_letter": None,
            }
        ],
    }


def test_option_flag_reads_bools_leniently():
    """release_efi_letter 是布尔开关：缺省/怪值都按默认，只有明确的假才是假。"""
    from app.application.real_runner import _option_flag

    assert _option_flag({}, "release_efi_letter") is True
    assert _option_flag({"release_efi_letter": True}, "release_efi_letter") is True
    assert _option_flag({"release_efi_letter": False}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "false"}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "0"}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "off"}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "1"}, "release_efi_letter") is True
    assert _option_flag({"release_efi_letter": "也许"}, "release_efi_letter") is True
    assert _option_flag(None, "release_efi_letter") is True


def test_release_efi_letter_switch_off_keeps_letter():
    """关掉 releaseEfiLetter → 一条 remove 都不发，只在日志里说明。"""
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    log = _CommandLog(None)
    runner._run_diskpart = lambda lines, log_, timeout=600.0, **kwargs: (
        seen.append(list(lines)) or (0, "ok")
    )

    runner._release_efi_letter_if_enabled(
        disk_number=2,
        letter="E",
        log=log,
        options={"release_efi_letter": False},
        tracker={"diskpart_ran": True, "efi_partition": 1},
    )

    assert seen == []
    assert "已关闭 EFI 盘符释放" in log.summary()


def test_release_efi_letter_switch_on_removes_letter():
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    log = _CommandLog(None)
    runner._run_diskpart = lambda lines, log_, timeout=600.0, **kwargs: (
        seen.append(list(lines)) or (0, "ok")
    )
    runner._esp_partition_number = lambda disk, log_: 1  # type: ignore[method-assign]

    runner._release_efi_letter_if_enabled(
        disk_number=2,
        letter="E",
        log=log,
        options={"release_efi_letter": True},
        tracker={"diskpart_ran": True, "efi_partition": 1},
    )

    assert seen == [["select disk 2", "select partition 1", "remove letter=E", "exit"]]


def test_release_efi_letter_switch_off_does_not_even_query():
    """关掉开关时连 ESP 分区号都不查（不碰盘，性能与安全都更好）。"""
    runner = WindowsDiskRunner()
    log = _CommandLog(None)

    def boom(*_a, **_k):
        raise AssertionError("关掉开关时不该执行任何 diskpart")

    runner._run_diskpart = boom  # type: ignore[method-assign]
    runner._esp_partition_number = boom  # type: ignore[method-assign]

    runner._release_efi_letter_if_enabled(
        disk_number=2,
        letter="E",
        log=log,
        options={"release_efi_letter": False},
        tracker={"diskpart_ran": True},
    )
    assert "已关闭 EFI 盘符释放" in log.summary()


def test_release_efi_letter_switch_skips_when_disk_untouched():
    """任务根本没动过盘（前置失败）→ 开关开着也不去释放。"""
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    runner._run_diskpart = lambda lines, log, timeout=600.0, **kwargs: (
        seen.append(list(lines)) or (0, "ok")
    )

    runner._release_efi_letter_if_enabled(
        disk_number=2,
        letter="E",
        log=_CommandLog(None),
        options={"release_efi_letter": True},
        tracker={"diskpart_ran": False},
    )
    assert seen == []


def test_release_efi_letter_uses_disk_and_partition_not_letter():
    """按“磁盘号 + 分区号”释放，不再 ``select volume <字母>``。

    实测（Win10 19045）ESP 的临时字母在 ``Get-Partition -DriveLetter`` /
    ``Get-Volume`` / ``Win32_Volume`` 里都查不到（只有 ``Win32_LogicalDisk`` 看得到），
    所以“先按字母查归属”必然查空 → 跳过释放 → 幽灵盘符残留。改成定位 ESP 分区后，
    字母被谁占用都不影响：操作对象是确定的分区。
    """
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []

    def fake_diskpart(lines, log, timeout=600.0, **kwargs):
        seen.append(list(lines))
        return 0, "ok"

    runner._run_diskpart = fake_diskpart  # type: ignore[method-assign]
    runner._esp_partition_number = lambda disk, log: 1  # type: ignore[method-assign]
    runner._release_efi_letter(2, "E", _CommandLog(None))

    assert seen == [
        ["select disk 2", "select partition 1", "remove letter=E", "exit"]
    ]


def test_release_efi_letter_reuses_partition_number_from_layout_check():
    """布局复核时已经知道 ESP 是第几个分区，收尾直接用，不再查一次。"""
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    runner._run_diskpart = lambda lines, log, timeout=600.0, **kwargs: (
        seen.append(list(lines)) or (0, "ok")
    )

    def boom(disk, log):
        raise AssertionError("不该再查分区表")

    runner._esp_partition_number = boom  # type: ignore[method-assign]
    runner._release_efi_letter(3, "F", _CommandLog(None), efi_partition=4)

    assert seen == [["select disk 3", "select partition 4", "remove letter=F", "exit"]]


def test_release_efi_letter_skips_when_no_esp_found():
    """盘上没有 ESP（还没分区 / 分区表被改过）→ 不动手。"""
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    log = _CommandLog(None)
    runner._run_diskpart = lambda lines, log_, timeout=600.0, **kwargs: (
        seen.append(list(lines)) or (0, "ok")
    )
    runner._esp_partition_number = lambda disk, log_: None  # type: ignore[method-assign]

    runner._release_efi_letter(2, "E", log)

    assert seen == []
    assert "找不到 ESP 分区" in log.summary()


def test_release_efi_letter_skips_when_query_fails():
    """查不到 ESP 也不动手：宁可留幽灵盘符，也不误操作。"""
    from app.application.real_runner import RunnerError

    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    log = _CommandLog(None)
    runner._run_diskpart = lambda lines, log_, timeout=600.0, **kwargs: (
        seen.append(list(lines)) or (0, "ok")
    )

    def boom(disk, log_):
        raise RunnerError("powershell 不可用")

    runner._esp_partition_number = boom  # type: ignore[method-assign]

    runner._release_efi_letter(2, "E", log)

    assert seen == []
    assert "已跳过" in log.summary()


def test_esp_partition_number_matches_gpt_type():
    """按 GPT 类型找 ESP，不依赖盘符（ESP 是隐藏卷，可能没有可读字母）。"""
    runner = WindowsDiskRunner(powershell="powershell.exe")
    runner._query_partitions = lambda disk, log: _esp_payload(4)  # type: ignore[method-assign]
    assert runner._esp_partition_number(2, _CommandLog(None)) == 4

    runner._query_partitions = lambda disk, log: {  # type: ignore[method-assign]
        "partition_style": "GPT",
        "partitions": [
            {"number": 1, "type": "Basic", "gpt_type": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"}
        ],
    }
    assert runner._esp_partition_number(2, _CommandLog(None)) is None


def test_release_efi_letter_skips_without_letter():
    runner = WindowsDiskRunner()
    seen: list[list[str]] = []
    runner._run_diskpart = lambda lines, log, timeout=600.0, **kwargs: (seen.append(list(lines)) or (0, "ok"))

    runner._release_efi_letter(2, None, _CommandLog(None))

    assert seen == []


def test_runner_keeps_efi_letter_when_switch_off(tmp_path):
    """同一个任务，release_efi_letter=False → 全流程一条 remove letter 都没有。"""
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    scripts: list[list[str]] = []
    runner._run_diskpart = lambda lines, log, timeout=600.0, **kwargs: (
        scripts.append(list(lines)) or (0, "ok")
    )
    runner._find_msr_partition = lambda disk_number, log, **_: None  # type: ignore[method-assign]
    runner._verify_partitions_after_create = (  # type: ignore[method-assign]
        lambda disk, parts, log: (True, "ok", {"efi_partition": 1})
    )
    runner._apply_image = lambda image_path, index, target, log, **kwargs: (87, "bad image", 0.1)

    spec = SimpleNamespace(
        disk_number=2,
        config_payload={
            "partition_info": {
                "efi_size_mb": 100,
                "c_size_gb": 3,
                "data_size_gb": "auto",
                "allow_small_partitions": True,
            },
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
        runner_options={"release_efi_letter": False},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert not any("remove letter" in line for script in scripts for line in script)


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

    def fake_diskpart(lines, log, timeout=600.0, **kwargs):
        scripts.append(list(lines))
        return 0, "ok"

    runner._run_diskpart = fake_diskpart  # type: ignore[method-assign]
    runner._find_msr_partition = lambda disk_number, log, **_: None  # type: ignore[method-assign]
    runner._verify_partitions_after_create = (  # type: ignore[method-assign]
        lambda disk, parts, log: (True, "ok", {"efi_partition": 1})
    )
    runner._apply_image = lambda image_path, index, target, log, **kwargs: (87, "bad image", 0.1)

    spec = SimpleNamespace(
        disk_number=2,
        config_payload={
            "partition_info": {
                "efi_size_mb": 100,
                "c_size_gb": 3,
                "data_size_gb": "auto",
                # 单测用小尺寸：显式放宽后端下限（C ≥ 60 GB）
                "allow_small_partitions": True,
            },
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "应用镜像失败" in (result.error or "")
    assert scripts[0][:2] == ["select disk 2", "clean"]
    # 收尾按“磁盘 + 分区号”释放，不按字母选卷
    assert ["select disk 2", "select partition 1", "remove letter=E", "exit"] in scripts


# ----------------------------------------------------------- MSR 删除（两段式）


def _tiny_image(tmp_path):
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    return image


def _tiny_spec(image, data_size="auto"):
    return SimpleNamespace(
        disk_number=2,
        config_payload={
            "partition_info": {
                "efi_size_mb": 100,
                "c_size_gb": 3,
                "data_size_gb": data_size,
                "allow_small_partitions": True,
            },
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )


def _msr_runner(**stubs):
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    for name, value in stubs.items():
        setattr(runner, name, value)
    return runner


def _msr_payload(number=1):
    return {
        "partition_style": "GPT",
        "largest_free": 0,
        "partitions": [
            {
                "number": number,
                "size_bytes": 16 * 1024 * 1024,
                "offset_bytes": 17 * 1024,
                "type": "Reserved",
                "gpt_type": "{e3c9e316-0b5c-4db8-817d-f92df00215ae}",
                "drive_letter": None,
            }
        ],
    }


def test_find_msr_partition_reads_partition_number():
    runner = _msr_runner(
        _query_partitions=lambda disk, log: _msr_payload(3),
    )
    assert runner._find_msr_partition(2, _CommandLog(None)) == 3


def test_find_msr_partition_returns_none_without_msr():
    """有些盘 ``convert gpt`` 之后没有 MSR：不能瞎删，直接返回 None。"""
    runner = _msr_runner(_query_partitions=lambda disk, log: _empty_disk())
    assert runner._find_msr_partition(2, _CommandLog(None)) is None


def test_find_msr_partition_retries_before_giving_up():
    """枚举慢：头两次查不到、第三次才出现 → 仍然要认出来（不能就此跳过删除）。"""
    calls: list[int] = []

    def flaky(disk, log):
        calls.append(1)
        return _msr_payload(1) if len(calls) >= 3 else _empty_disk()

    runner = _msr_runner(_query_partitions=flaky)
    assert runner._find_msr_partition(2, _CommandLog(None)) == 1
    assert len(calls) == 3


def test_find_msr_partition_raises_when_query_fails():
    """查询失败要中止装机，而不是猜一个分区号去删。"""

    def boom(disk, log):
        raise RunnerError("Get-Partition : 拒绝访问")

    runner = _msr_runner(_query_partitions=boom)
    with pytest.raises(RunnerError):
        runner._find_msr_partition(2, _CommandLog(None))


def _ok_pipeline(**overrides):
    """把 dism / bcdboot / verify 都换成“成功”，只留分区阶段的真实逻辑。"""
    stubs = {
        "_apply_image": lambda image_path, index, target, log, **kwargs: (0, "ok", 0.1),
        "_write_boot": lambda letters, log, **kwargs: (0, "ok", 0.1),
        "_verify": lambda disk_number, log, letters, **kwargs: StepResult(
            key="verify", title=STEP_TITLES["verify"], status=STEP_OK, message="ok"
        ),
    }
    stubs.update(overrides)
    return stubs


def test_run_steps_deletes_msr_then_verifies_before_creating_partitions(tmp_path):
    """清盘 → 删 MSR（独立脚本）→ 复查 → 建分区 → 复核布局。

    关键顺序：删除必须发生在建 EFI 之前，而且删完要再查一次。
    """
    image = _tiny_image(tmp_path)
    scripts: list[list[str]] = []
    queries: list[int] = []

    def fake_diskpart(lines, log, timeout=600.0, **kwargs):
        scripts.append(list(lines))
        return 0, "ok"

    def fake_query(disk_number, log):
        queries.append(1)
        return _msr_payload(1) if len(queries) == 1 else _empty_disk()

    runner = _msr_runner(
        _run_diskpart=fake_diskpart,
        _query_partitions=fake_query,
        _verify_partitions_after_create=lambda disk, parts, log: (
            True,
            "ok",
            {"efi_partition": 1},
        ),
        **_ok_pipeline(),
    )

    result = runner.run(_tiny_spec(image), threading.Event())

    assert result.success is True, result.error
    # 三段 diskpart，顺序固定
    assert scripts[0] == ["select disk 2", "clean", "convert gpt", "exit"]
    assert scripts[1] == ["select disk 2", "select partition 1", "delete partition override", "exit"]
    layout = scripts[2]
    assert "delete partition override" not in layout
    assert layout.index("create partition efi size=100") == 1
    # 找了 1 次（命中），删完复查用满 3 次重试才确认“没有 MSR 了”；
    # 收尾释放盘符复用复核结果里的 ESP 分区号，不会再多查一次
    assert len(queries) == 1 + 3


def test_partition_verification_failure_never_reaches_image(tmp_path):
    """分区阶段的验收不通过 → 立刻结束，绝不写入镜像（两个阶段是分开的）。"""
    image = _tiny_image(tmp_path)
    called: list[str] = []

    runner = _msr_runner(
        _run_diskpart=lambda lines, log, timeout=600.0, **kwargs: (0, "ok"),
        _query_partitions=lambda disk, log: _empty_disk(),
        _verify_partitions_after_create=lambda disk, parts, log: (
            False,
            "第 2 个分区（Windows）大小不符：计划 122880 MB，实际 102400 MB",
            {"problems": ["大小不符"]},
        ),
        _apply_image=lambda *a, **k: (called.append("image") or (0, "ok", 0.1)),
        _write_boot=lambda *a, **k: (called.append("boot") or (0, "ok", 0.1)),
    )

    result = runner.run(_tiny_spec(image), threading.Event())

    assert result.success is False
    assert "分区验证不通过" in (result.error or "")
    assert "大小不符" in (result.error or "")
    assert called == [], f"分区没验过就不该动镜像/引导，实际调用了 {called}"


def test_partition_step_details_carry_verification(tmp_path):
    """分区步骤要把验证结果（对照计划的结论）挂在 details 里，便于事后排查。"""
    image = _tiny_image(tmp_path)
    runner = _msr_runner(
        _run_diskpart=lambda lines, log, timeout=600.0, **kwargs: (0, "ok"),
        _query_partitions=lambda disk, log: _empty_disk(),
        _verify_partitions_after_create=lambda disk, parts, log: (
            True,
            "GPT、3 个分区与计划一致：#1(E:)=100.00 MB/EFI、#2(F:)=120.00 GB/Windows",
            {
                "efi_partition": 1,
                "problems": [],
                "warnings": ["第 1 个分区（EFI）没有挂上计划盘符 E"],
                "partitions": [],
            },
        ),
        **_ok_pipeline(),
    )

    result = runner.run(_tiny_spec(image), threading.Event())

    assert result.success is True, result.error
    data_step = next(step for step in result.steps if step.key == "data")
    verification = data_step.details["verification"]
    assert "与计划一致" in verification["message"]
    assert verification["warnings"]
    assert verification["problems"] == []


def test_run_steps_aborts_when_msr_survives_delete(tmp_path):
    """删 MSR 的脚本退出码 0，但复查发现它还在 → 立刻中止，绝不建分区。"""
    image = _tiny_image(tmp_path)
    scripts: list[list[str]] = []
    state = {"deleted": False}

    def fake_diskpart(lines, log, timeout=600.0, **kwargs):
        scripts.append(list(lines))
        if any("delete partition override" in line for line in lines):
            state["deleted"] = True  # 假装成功，但盘上其实没删掉
        return 0, "ok"

    def fake_query(disk_number, log):
        return _msr_payload(1)  # 永远查得到 → 说明删除没生效

    runner = _msr_runner(
        _run_diskpart=fake_diskpart,
        _query_partitions=fake_query,
        **_ok_pipeline(),
    )

    result = runner.run(_tiny_spec(image), threading.Event())

    assert result.success is False
    assert "MSR" in (result.error or "")
    assert "仍" in (result.error or "") or "失败" in (result.error or "")
    # 只跑了清盘 + 删 MSR 两段，没有建分区
    assert not any("create partition" in line for script in scripts for line in script)


def test_run_steps_aborts_when_layout_check_fails(tmp_path):
    """建完分区复核失败（比如多了一个分区）→ 在 dism 之前就停。"""
    image = _tiny_image(tmp_path)
    applied: list[str] = []

    runner = _msr_runner(
        _run_diskpart=lambda lines, log, timeout=600.0, **kwargs: (0, "ok"),
        _query_partitions=lambda disk, log: _empty_disk(),
        _verify_partitions_after_create=lambda disk, parts, log: (False, "分区数与计划不符（期望 3 个，实际 4 个）", {}),
        **_ok_pipeline(
            _apply_image=lambda image_path, index, target, log, **kwargs: (
                applied.append("image") or 0,
                "ok",
                0.1,
            ),
        ),
    )

    result = runner.run(_tiny_spec(image), threading.Event())

    assert result.success is False
    assert "分区数与计划不符" in (result.error or "")
    assert applied == []  # 没有进入 dism


def test_run_steps_aborts_when_msr_lookup_fails(tmp_path):
    """查不到 MSR 就整步失败：绝不在没确认的情况下继续建分区。"""
    image = _tiny_image(tmp_path)
    scripts: list[list[str]] = []

    def fake_diskpart(lines, log, timeout=600.0, **kwargs):
        scripts.append(list(lines))
        return 0, "ok"

    def boom(disk_number, log):
        raise RunnerError("查询 MSR 分区失败")

    runner = _msr_runner(_run_diskpart=fake_diskpart, _find_msr_partition=boom)

    result = runner.run(_tiny_spec(image), threading.Event())

    assert result.success is False
    assert "MSR" in (result.error or "")
    # 只跑了清盘那一段，没有继续建分区（另一条是收尾的释放盘符）
    assert scripts[0] == ["select disk 2", "clean", "convert gpt", "exit"]
    assert not any("create partition" in line for script in scripts for line in script)


def test_cancelled_job_reports_cancelled_even_if_executor_fails():
    def executor(spec, cancel_event):
        cancel_event.set()
        return WorkerResult.failure(spec.disk_number, "任务已取消（镜像未写入）")

    manager = JobManager(executor=executor)
    job = manager.submit(JobSpec(disk_number=1, dry_run=True), ExecutionPlan(disk_number=1))
    manager.confirm_batch([job.id])
    manager.wait_all(2)

    assert job.status == JobStatus.CANCELLED
    assert job.result is not None and "已取消" in (job.result.error or "")

# ------------------------------------------------------ 进度事件（job.step）
_STAGES = ("efi", "windows", "data", "image", "boot", "verify")


def _step_manager() -> tuple[JobManager, Job, list[tuple[str, str]]]:
    """构造一个会逐阶段上报的 JobManager（不碰磁盘）。"""
    seen: list[tuple[str, str]] = []

    def executor(spec: Any, cancel_event: threading.Event, on_step=None) -> WorkerResult:
        if on_step is not None:
            for key in _STAGES:
                on_step(key, "running", f"执行 {key}")
                on_step(key, "ok", f"{key} 完成")
                seen.append((key, "ok"))
            # 演练期的附加步骤：会发事件，但不该算进进度
            on_step("diskpart_preview", "skipped", "仅预览")
        return WorkerResult(disk_number=spec.disk_number, success=True, message="ok", dry_run=True)

    manager = JobManager(executor=executor)
    job = manager.submit(
        JobSpec(disk_number=1, dry_run=True),
        ExecutionPlan(disk_number=1, dry_run=True),
    )
    manager.confirm_batch([job.id])
    manager.wait_all(5)
    return manager, job, seen


def test_progress_counts_only_real_stages():
    """``diskpart_preview`` 这种演练步骤不能把进度顶到 7/6。"""
    _, job, seen = _step_manager()

    assert [key for key, _ in seen] == list(_STAGES)
    assert job.progress["total"] == len(_STAGES)
    assert job.progress["completed"] == list(_STAGES)
    assert job.progress["percent"] == 100
    assert job.status == JobStatus.OK


def test_step_events_are_broadcast_for_every_stage():
    """每个阶段都要广播 ``job.step``——进度页靠它点亮阶段条。"""
    import queue

    seen: queue.Queue = queue.Queue()
    manager = JobManager()

    def executor(spec: Any, cancel_event: threading.Event, on_step=None) -> WorkerResult:
        on_step("efi", "running", "开始")
        on_step("efi", "ok", "完成")
        on_step("diskpart_preview", "skipped", "仅预览")
        return WorkerResult(disk_number=spec.disk_number, success=True, message="ok", dry_run=True)

    manager.executor = executor  # type: ignore[assignment]
    manager.subscribe(seen.put)
    job = manager.submit(
        JobSpec(disk_number=1, dry_run=True),
        ExecutionPlan(disk_number=1, dry_run=True),
    )
    manager.confirm_batch([job.id])
    manager.wait_all(5)

    events = []
    while not seen.empty():
        events.append(seen.get())
    keyed = [
        (event.type, event.payload.get("key"))
        for event in events
        if event.type == "job.step"
    ]
    assert ("job.step", "efi") in keyed
    assert ("job.step", "diskpart_preview") in keyed
    assert job.to_dict()["progress"]["percent"] == 100


# ------------------------------------------------- 执行器失败分支（dism/bcdboot/verify/超时）
def _whole_disk_spec(tmp_path):
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    return SimpleNamespace(
        disk_number=1,
        config_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )


def _empty_disk(*_args, **_kwargs):
    """默认的分区查询结果：GPT、没有任何保留分区、也没有剩余空间。"""
    return {"partition_style": "GPT", "partitions": [], "largest_free": 0}


def _stub_runner(monkeypatch, runner, *, spawn=None, diskpart=None, partitions=None, layout_ok=True):
    released: list[bool] = []
    monkeypatch.setattr(runner, "_release_efi_letter", lambda *a, **k: released.append(True))
    monkeypatch.setattr(
        runner, "_run_diskpart", diskpart or (lambda lines, log, **kwargs: (0, "diskpart ok"))
    )
    # 分区查询与“建完复核”都走 PowerShell：这两个测试只关心 dism/bcdboot/盘符，
    # 所以默认给一个“正常 GPT 盘”，专项测试再单独覆盖它们的失败分支。
    monkeypatch.setattr(runner, "_query_partitions", partitions or _empty_disk)
    monkeypatch.setattr(
        runner,
        "_verify_partitions_after_create",
        lambda disk, parts, log: (layout_ok, "ok" if layout_ok else "分区数与计划不符", {}),
    )
    if spawn is not None:
        monkeypatch.setattr(runner, "_spawn", spawn)
    return released


def test_runner_reports_dism_failure(tmp_path, monkeypatch) -> None:
    runner = WindowsDiskRunner(diskpart="diskpart.exe", dism="dism.exe", bcdboot="bcdboot.exe")

    def fake_spawn(command, timeout, log, *, env=None):
        return (87, "dism 失败", 0.1) if str(command[0]).endswith("dism.exe") else (0, "ok", 0.1)

    _stub_runner(monkeypatch, runner, spawn=fake_spawn)

    result = runner.run(_whole_disk_spec(tmp_path), threading.Event())

    assert result.success is False
    image_step = next(step for step in result.steps if step.key == "image")
    assert image_step.failed is True
    assert "87" in image_step.message


def test_runner_reports_bcdboot_failure_and_releases_letter(tmp_path, monkeypatch) -> None:
    runner = WindowsDiskRunner(diskpart="diskpart.exe", dism="dism.exe", bcdboot="bcdboot.exe")

    def fake_spawn(command, timeout, log, *, env=None):
        return (2, "BFSVC Error: Could not open the BCD template store.", 0.1) if str(
            command[0]
        ).endswith("bcdboot.exe") else (0, "ok", 0.1)

    released = _stub_runner(monkeypatch, runner, spawn=fake_spawn)

    result = runner.run(_whole_disk_spec(tmp_path), threading.Event())

    assert result.success is False
    boot = next(step for step in result.steps if step.key == "boot")
    assert boot.failed is True
    assert "2" in boot.message
    assert released, "bcdboot 失败也必须释放 EFI 临时盘符"


def test_runner_reports_verify_rejection(tmp_path, monkeypatch) -> None:
    from app.models import StepResult

    runner = WindowsDiskRunner(diskpart="diskpart.exe", dism="dism.exe", bcdboot="bcdboot.exe")
    _stub_runner(monkeypatch, runner, spawn=lambda *a, **k: (0, "ok", 0.1))
    monkeypatch.setattr(
        runner,
        "_verify",
        lambda disk_number, log, letters=None, **_: StepResult(
            key="verify",
            title="校验分区结果",
            status="failed",
            message="校验不符合预期：系统分区里没有 Windows 系统文件",
        ),
    )

    result = runner.run(_whole_disk_spec(tmp_path), threading.Event())

    assert result.success is False
    assert "分区校验失败" in (result.error or "")
    verify = next(step for step in result.steps if step.key == "verify")
    assert verify.failed is True


def test_runner_reports_diskpart_timeout(tmp_path, monkeypatch) -> None:
    from app.application.real_runner import RunnerError

    runner = WindowsDiskRunner(diskpart="diskpart.exe", dism="dism.exe", bcdboot="bcdboot.exe")

    def boom(lines, log, **kwargs):
        raise RunnerError("命令超时（600 秒）：diskpart.exe")

    _stub_runner(monkeypatch, runner, diskpart=boom)

    result = runner.run(_whole_disk_spec(tmp_path), threading.Event())

    assert result.success is False
    efi = next(step for step in result.steps if step.key == "efi")
    assert efi.failed is True
    assert "超时" in efi.message


def test_runner_refuses_tiny_layout_before_touching_disk(tmp_path, monkeypatch) -> None:
    """下限拦截必须在 ``_wipe_script`` 之前：盘一根毛都不能动。"""
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    spec = SimpleNamespace(
        disk_number=2,
        config_payload={
            # 没有 allow_small_partitions：3 GB 的系统分区必须被拒
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 3, "data_size_gb": "auto"},
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    scripts: list[list[str]] = []

    def record(lines, log, **kwargs):
        scripts.append(list(lines))
        return 0, "diskpart ok"

    _stub_runner(monkeypatch, runner, diskpart=record)

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "分区过小" in (result.error or "")
    assert scripts == []  # 一段 diskpart 都没跑过
