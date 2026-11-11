"""回归测试：真实执行路径必须真的可达。

覆盖三处曾经的断点（历史 bug）：

1. ``WorkflowController`` 装配的 ``SispService`` 必须带上真实执行器，不能是空壳；
2. ``JobManager`` 的默认执行器不能另造一个服务把控制器上挂的执行器绕开；
3. 真实执行要求注入 ``real_runner``，没有执行器时必须在执行层拒绝。

这些测试全部用替身执行器，不碰 diskpart/ghost。
"""

from __future__ import annotations

import json
import pathlib
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from app.application.contracts import DiskInfo
from app.application.jobs import ExecutionPlan, Job, JobManager, JobSpec, JobStatus
from app.application.partitions import (
    create_geometry_lines,
    format_lines,
    msr_delete_lines,
    plan_partitions,
    wipe_lines,
)
from app.application.real_runner import (
    RunnerError,
    WindowsDiskRunner,
    _CommandLog,
    build_real_runner,
    prune_runner_logs,
)
from app.application.workflow import WorkflowController
from app.models import WorkerResult
from tests.fakes import FakeDiskpartShell as FakeShell

_GIB = 1024**3
# 单测用小尺寸（C 30 GB）测算术：显式放宽后端下限（真实布局 C ≥ 60 GB）。
_PAYLOAD: dict[str, Any] = {
    "partition_info": {
        "efi_size_mb": 300,
        "c_size_gb": 30,
        "data_size_gb": "auto",
        "allow_small_partitions": True,
    },
    "image_info": {"image_path": r"D:\images	est.GHO"},
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

    def __init__(self, disks: list[DiskInfo]) -> None:
        self.disks = disks
        self.allowed_disk_numbers = frozenset()
        self.executed: list[Any] = []

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def scan_protected_snapshot(self) -> tuple[list[DiskInfo], tuple[str, ...]]:
        return list(self.disks), ()

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        letters = {number: {"efi": "E", "windows": "F", "data": "G"} for number in selected}
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        self.executed.append(spec)
        return "executed"


# 任务载荷只有两个来源：构造时的分区默认值快照 + 每次现读的设置。
# （曾经还有第三个：``-j`` 指定的业务 JSON，显式传时压过设置。那一层已移除。）
_PARTITION_DEFAULTS: dict[str, Any] = dict(_PAYLOAD["partition_info"])
_SETTINGS: dict[str, Any] = {
    "sysImageA": _PAYLOAD["image_info"]["image_path"],
}


def _preflight() -> dict[str, Any]:
    return {"results": [{"name": "运行前检查", "passed": True, "message": "ok"}]}


def _wire(**kwargs: Any) -> WorkflowController:
    """按“设置 + 默认值”装配控制器，省掉每个用例重复写两个注入点。"""
    kwargs.setdefault("partition_defaults", dict(_PARTITION_DEFAULTS))
    kwargs.setdefault("settings_provider", lambda: dict(_SETTINGS))
    return WorkflowController(**kwargs)


# ---------------------------------------------------------------- 装配（P0-1）
def test_real_mode_builds_service_with_runner_and_allowlist():
    controller = _wire(allowed_disk_numbers=[1, 2])

    assert controller.service.allowed_disk_numbers == frozenset({1, 2})
    # 默认执行器必须是真实实现，不能再是 None（旧实现从不注入）。
    assert isinstance(controller.service._real_runner, WindowsDiskRunner)


def test_default_executor_delegates_to_session_service():
    controller = _wire()
    fake = _FakeService([])
    controller._service = fake

    spec = SimpleNamespace(disk_number=1)
    result = controller.job_manager._executor(spec, threading.Event())

    assert result == "executed"
    assert fake.executed == [spec]


def test_custom_job_manager_is_respected():
    manager = JobManager()
    controller = _wire(job_manager=manager)

    assert controller.job_manager is manager


# ---------------------------------------------------------------- 计划（白名单）
def test_prepare_grants_only_selected_disks_when_no_explicit_allowlist():
    service = _FakeService([_disk(1)])
    controller = _wire(
        service=service,
        preflight_runner=_preflight,
    )
    controller.start()

    prepared = controller.prepare([1])

    assert service.allowed_disk_numbers == frozenset({1})
    checks = {check.key: check for check in prepared[0].plan.checks}
    assert checks["write_allowlist"].passed is True
    assert "选中" in checks["write_allowlist"].message


def test_per_disk_image_override_recomputes_image_problems(tmp_path):
    """第 3 步换了镜像之后，计划里的“镜像文件不存在”必须跟着重算。

    会话级预检只在 ``start()`` 里按配置文件算一次；如果计划直接沿用那份阻断项，
    用户在第 3 步选了一个真实存在的镜像也永远开始不了——第 4 步会一直挂着阻断。
    """
    good = tmp_path / "ok.GHO"
    good.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))

    missing = r"D:\images\missing.GHO"

    def preflight() -> dict[str, Any]:
        return {
            "results": [
                {
                    "name": "镜像文件存在性检查",
                    "passed": False,
                    "message": r"镜像文件不存在: D:\images\missing.GHO",
                }
            ]
        }

    controller = _wire(
        service=_FakeService([_disk(1)]),
        preflight_runner=preflight,
        partition_defaults={"c_size_gb": 30, "allow_small_partitions": True},
        settings_provider=lambda: {"sysImageA": missing},
    )
    controller.start()

    stale = controller.prepare({1: {}})[0]
    assert any("镜像文件不存在" in item for item in stale.plan.blockers)

    fixed = controller.prepare({1: {"win_gho": str(good)}})[0]
    assert not any("镜像文件不存在" in item for item in fixed.plan.blockers)
    assert fixed.spec.task_payload["image_info"]["image_path"] == str(good)
    assert tuple(fixed.plan.blockers) == ()
    assert fixed.plan.executable is True


def test_authorize_disks_grants_single_disk_operations():
    """分区/修复引导不经过 prepare，必须自己授权，否则真实执行被白名单挡掉。"""
    service = _FakeService([_disk(1), _disk(2)])
    controller = _wire(
        service=service,
        preflight_runner=_preflight,
    )
    controller.start()

    assert service.allowed_disk_numbers == frozenset()
    controller.authorize_disks([1])
    assert service.allowed_disk_numbers == frozenset({1})
    # 再授权另一块盘是追加，不是覆盖
    controller.authorize_disks([2])
    assert service.allowed_disk_numbers == frozenset({1, 2})


def test_prepare_keeps_explicit_allowlist_untouched():
    service = _FakeService([_disk(1), _disk(2)])
    controller = _wire(
        service=service,
        allowed_disk_numbers=[1],
        preflight_runner=_preflight,
    )
    controller.start()

    prepared = controller.prepare([1])

    assert service.allowed_disk_numbers == frozenset({1})
    checks = {check.key: check for check in prepared[0].plan.checks}
    assert checks["write_allowlist"].message.startswith("白名单")


def test_real_plan_blocks_disk_outside_allowlist():
    service = _FakeService([_disk(1), _disk(2)])
    controller = _wire(
        service=service,
        allowed_disk_numbers=[2],
        preflight_runner=_preflight,
    )
    controller.start()

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
    """分区脚本分四段：清盘+转 GPT → 删 MSR → 建分区（几何）→ 逐分区格式化。"""
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 300, "c_size_gb": 30}},
        letters={"efi": "E", "windows": "F", "data": "G"},
    )

    wipe = wipe_lines(2)
    assert wipe == ["select disk 2", "clean", "convert gpt", "exit"]

    delete = msr_delete_lines(2, 1)
    assert delete == ["select disk 2", "select partition 1", "delete partition override", "exit"]

    layout = create_geometry_lines(2, parts)
    assert layout[0] == "select disk 2"
    assert "delete partition override" not in layout  # MSR 在上一段已删
    assert "create partition efi size=300" in layout
    assert "create partition primary size=30720" in layout  # 30 GB * 1024 MB
    assert "create partition primary" in layout  # 最后一项吃剩余空间
    assert layout[-1] == "exit"
    # 建分区这一段**不格式化、不分配盘符**（1.4.0 拆分的核心）
    assert not any(line.startswith("format") for line in layout)
    assert not any(line.startswith("assign") for line in layout)

    # 格式化单独一段，且一次只处理一个分区（1.4.1 起 format 带 override：
    # 卷被判「设备使用中」时由 diskpart 自己强制卸载，见 2026-11-11 现场日志）
    windows = format_lines(2, 2, parts[1])
    assert windows == [
        "select disk 2",
        "select partition 2",
        "format quick fs=ntfs label=Windows override",
        "assign letter=F",
        "exit",
    ]


def test_diskpart_script_lets_last_data_partition_eat_remaining_space():
    """最后一个数据分区可以不带 size：diskpart 用掉全部剩余空间。

    这比“总容量 − C − EFI − 对齐余量”更准，也不会出现“界面上刚好、
    diskpart 报空间不足”——所以界面的准备页面（selectionPayload）在把盘填满时
    就发 ``"auto"``。
    """
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
    lines = create_geometry_lines(1, parts)

    assert "create partition primary size=122880" in lines          # C 120 GB
    assert "create partition primary size=204800" in lines          # D 200 GB
    # 只有最后一个数据分区不带 size，且它是最后一个 create 行
    creates = [line for line in lines if line.startswith("create partition primary")]
    assert creates == ["create partition primary size=122880",
                       "create partition primary size=204800",
                       "create partition primary"]
    # 盘符在格式化那一段分配（几何段不 assign）
    assert "assign letter=H" in format_lines(1, 4, parts[3])


def test_diskpart_script_uses_explicit_data_size():
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 100, "c_size_gb": 10, "data_size_gb": 5}},
        letters={"efi": "E", "windows": "F", "data": "G"},
    )
    lines = create_geometry_lines(1, parts)

    assert "create partition primary size=5120" in lines


def test_runner_reports_missing_image_without_touching_disks(tmp_path):
    runner = WindowsDiskRunner(
        diskpart=r"C:\nope\diskpart.exe",
        ghost=r"C:\nope\ghost64.exe",
        bcdboot=r"C:\nope\bcdboot.exe",
    )
    spec = SimpleNamespace(
        disk_number=1,
        task_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(tmp_path / "missing.GHO")},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "镜像文件不存在" in (result.error or "")
    assert result.steps == ()


def test_runner_fails_cleanly_when_diskpart_cannot_start(tmp_path):
    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"not a real wim")
    runner = WindowsDiskRunner(
        diskpart=r"C:\nope\diskpart.exe",
        ghost=r"C:\nope\ghost64.exe",
        bcdboot=r"C:\nope\bcdboot.exe",
    )
    spec = SimpleNamespace(
        disk_number=1,
        task_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image)},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "无法执行 diskpart" in (result.error or "")
    efi = next(step for step in result.steps if step.key == "efi")
    assert efi.failed is True


def test_runner_rejects_incomplete_letters(tmp_path):
    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner()
    spec = SimpleNamespace(
        disk_number=1,
        task_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image)},
        },
        drive_letters={"efi": "E"},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "缺少盘符分配" in (result.error or "")


def test_runner_honours_pre_cancelled_event(tmp_path):
    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner()
    spec = SimpleNamespace(
        disk_number=1,
        task_payload={
            "partition_info": _PAYLOAD["partition_info"],
            "image_info": {"image_path": str(image)},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )
    cancel = threading.Event()
    cancel.set()

    result = runner.run(spec, cancel)

    assert result.success is False
    assert "已取消" in (result.error or "")


# ---------------------------------------------------------------- P0 回归
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
        )

    manager = JobManager(executor=executor)
    job = manager.submit(
        JobSpec(disk_number=1),
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
    spec = JobSpec(disk_number=1)
    plan = ExecutionPlan(disk_number=1)
    manager.submit(spec, plan)

    with pytest.raises(ValueError, match="已有未结束任务"):
        manager.submit(spec, plan)


# ---------------------------------------------------------------- 界面设置接线
def test_partition_defaults_come_from_settings():
    """设置页的分区默认值就是计划的默认值——没有更高的优先级层了。"""
    settings = {"efiDefaultMb": 300, "sysDefaultGb": 500, "dataDefaultGb": "auto"}
    controller = _wire(
        service=_FakeService([_disk(1, size_gb=1000)]),
        preflight_runner=_preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 120, "data_size_gb": 30},
        settings_provider=lambda: dict(settings),
    )
    controller.start()

    info = controller.prepare([1])[0].spec.task_payload["partition_info"]

    assert info["efi_size_mb"] == 300      # 设置里的最新值压过构造时快照
    assert info["c_size_gb"] == 500
    # "auto" = 吃掉剩余空间，载荷里干脆不写 data_size_gb
    assert "data_size_gb" not in info


def test_partition_defaults_snapshot_used_without_settings():
    """没有设置提供者时退回构造时的快照（CLI 场景）。"""
    controller = _wire(
        service=_FakeService([_disk(1, size_gb=1000)]),
        preflight_runner=_preflight,
        partition_defaults={"efi_size_mb": 100, "c_size_gb": 120, "data_size_gb": None},
        settings_provider=None,
    )
    controller.start()

    info = controller.prepare([1])[0].spec.task_payload["partition_info"]

    assert info == {"efi_size_mb": 100, "c_size_gb": 120}


def test_tool_paths_reach_default_real_runner():
    controller = _wire(
        tool_paths={
            "ghost": r"C:\tools\ghost64.exe",
            "bcdboot": r"C:\tools\bcdboot.exe",
            "": "",
        },
    )

    runner = controller.service._real_runner

    assert runner.ghost == r"C:\tools\ghost64.exe"
    assert runner.bcdboot == r"C:\tools\bcdboot.exe"
    # diskpart 不再由设置提供：永远是系统探测到的那个
    assert runner.diskpart != r"C:\tools\diskpart.exe"


def test_build_real_runner_accepts_tool_paths():
    runner = build_real_runner(ghost=r"C:\d\ghost64.exe", bcdboot=r"C:\d\bcdboot.exe")

    assert (runner.ghost, runner.bcdboot) == (r"C:\d\ghost64.exe", r"C:\d\bcdboot.exe")


def test_prepare_does_not_accumulate_implicit_allowlist():
    service = _FakeService([_disk(1), _disk(2)])
    controller = _wire(
        service=service,
        preflight_runner=_preflight,
    )
    controller.start()

    controller.prepare([1])
    assert service.allowed_disk_numbers == frozenset({1})

    # 第二次只选 2：上一次隐式加入的 1 必须撤出，不能累积。
    controller.prepare([2])
    assert service.allowed_disk_numbers == frozenset({2})

    # 开新会话同样清空隐式授权。
    controller.start()
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
    from app.application.runner_utils import option_flag as _option_flag

    assert _option_flag({}, "release_efi_letter") is True
    assert _option_flag({"release_efi_letter": True}, "release_efi_letter") is True
    assert _option_flag({"release_efi_letter": False}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "false"}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "0"}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "off"}, "release_efi_letter") is False
    assert _option_flag({"release_efi_letter": "1"}, "release_efi_letter") is True
    assert _option_flag({"release_efi_letter": "也许"}, "release_efi_letter") is True
    assert _option_flag(None, "release_efi_letter") is True


def _releaser(shell):
    from app.application.phases.partition import EfiLetterReleaser

    return EfiLetterReleaser(shell)


def _touched(efi_partition=None, *, disk_touched=True):
    from app.application.phases.base import PhaseOutcome

    return PhaseOutcome(ok=True, disk_touched=disk_touched, efi_partition=efi_partition)


def test_release_efi_letter_switch_off_keeps_letter():
    """关掉 releaseEfiLetter → 一条 remove 都不发，只在日志里说明。"""
    log = _CommandLog(None)
    shell = FakeShell()

    _releaser(shell).release(
        disk_number=2,
        letter="E",
        outcome=_touched(1),
        options={"release_efi_letter": False},
        log=log,
    )

    assert shell.released == []
    assert "已关闭 EFI 盘符释放" in log.summary()


def test_release_efi_letter_switch_on_removes_letter():
    log = _CommandLog(None)
    shell = FakeShell()

    _releaser(shell).release(
        disk_number=2,
        letter="E",
        outcome=_touched(1),
        options={"release_efi_letter": True},
        log=log,
    )

    assert shell.released == [(2, 1, "E")]


def test_release_efi_letter_switch_off_does_not_even_query():
    """关掉开关时连 ESP 分区号都不查（不碰盘，性能与安全都更好）。"""
    log = _CommandLog(None)

    class BoomShell(FakeShell):
        def find_esp(self, disk_number):
            raise AssertionError("关掉开关时不该查 ESP")

        def release_letter(self, disk_number, partition, letter):
            raise AssertionError("关掉开关时不该执行 diskpart")

    _releaser(BoomShell()).release(
        disk_number=2,
        letter="E",
        outcome=_touched(None),
        options={"release_efi_letter": False},
        log=log,
    )
    assert "已关闭 EFI 盘符释放" in log.summary()


def test_release_efi_letter_switch_skips_when_disk_untouched():
    """任务根本没动过盘（前置失败）→ 开关开着也不去释放。"""
    shell = FakeShell()

    _releaser(shell).release(
        disk_number=2,
        letter="E",
        outcome=_touched(1, disk_touched=False),
        options={"release_efi_letter": True},
        log=_CommandLog(None),
    )
    assert shell.released == []


def test_release_efi_letter_uses_disk_and_partition_not_letter():
    """按“磁盘号 + 分区号”释放，不再 ``select volume <字母>``。

    实测（Win10 19045）ESP 的临时字母在 ``Get-Partition -DriveLetter`` /
    ``Get-Volume`` / ``Win32_Volume`` 里都查不到（只有 ``Win32_LogicalDisk`` 看得到），
    所以“先按字母查归属”必然查空 → 跳过释放 → 幽灵盘符残留。改成定位 ESP 分区后，
    字母被谁占用都不影响：操作对象是确定的分区。
    """
    shell = FakeShell()

    _releaser(shell).release(
        disk_number=2,
        letter="E",
        outcome=_touched(1),
        options={},
        log=_CommandLog(None),
    )

    assert shell.released == [(2, 1, "E")]


def test_diskpart_shell_release_script_uses_disk_and_partition():
    """释放脚本本身：``select disk N`` + ``select partition k`` + ``remove letter=X``。"""
    from app.application.command_log import CommandLog
    from app.application.shell import DiskpartShell, ProcessShell

    written: list[str] = []

    def fake_run(command, timeout, *, env=None, cwd=None):
        written.extend(pathlib.Path(command[2]).read_text(encoding="ascii").splitlines())
        return 0, "ok", 0.1

    shell = DiskpartShell(
        log=CommandLog(None),
        diskpart="diskpart.exe",
        process=ProcessShell(log=CommandLog(None), runner=fake_run),
    )
    assert shell.release_letter(2, 1, "E") is True

    assert [line for line in written if line] == [
        "select disk 2",
        "select partition 1",
        "remove letter=E",
        "exit",
    ]


def test_release_efi_letter_reuses_partition_number_from_layout_check():
    """布局复核时已经知道 ESP 是第几个分区，收尾直接用，不再查一次。"""

    class BoomShell(FakeShell):
        def find_esp(self, disk_number):
            raise AssertionError("不该再查分区表")

    boom = BoomShell()
    _releaser(boom).release(
        disk_number=3,
        letter="F",
        outcome=_touched(4),
        options={},
        log=_CommandLog(None),
    )

    assert boom.released == [(3, 4, "F")]


def test_release_efi_letter_skips_when_no_esp_found():
    """盘上没有 ESP（还没分区 / 分区表被改过）→ 不动手。"""
    log = _CommandLog(None)
    shell = FakeShell(esp=None)

    _releaser(shell).release(
        disk_number=2,
        letter="E",
        outcome=_touched(None),
        options={},
        log=log,
    )

    assert shell.released == []
    assert "找不到 ESP 分区" in log.summary()


def test_release_efi_letter_skips_when_query_fails():
    """查不到 ESP 也不动手：宁可留幽灵盘符，也不误操作。"""
    log = _CommandLog(None)

    def boom(disk_number):
        raise RunnerError("powershell 不可用")

    shell = FakeShell(esp=boom)

    _releaser(shell).release(
        disk_number=2,
        letter="E",
        outcome=_touched(None),
        options={},
        log=log,
    )

    assert shell.released == []
    assert "已跳过" in log.summary()


def test_diskpart_shell_finds_esp_by_gpt_type(fake_process):
    """按 GPT 类型找 ESP，不依赖盘符（ESP 是隐藏卷，可能没有可读字母）。"""
    from app.application.command_log import CommandLog
    from app.application.shell import DiskpartShell

    def handler(command, timeout, env):
        return 0, json.dumps(_esp_payload(4)), 0.1

    shell = DiskpartShell(
        log=CommandLog(None),
        powershell="powershell.exe",
        process=fake_process(handler=handler),
    )
    assert shell.find_esp(2) == 4


def test_diskpart_shell_returns_none_without_esp(fake_process):
    from app.application.command_log import CommandLog
    from app.application.shell import DiskpartShell

    payload = {
        "partition_style": "GPT",
        "partitions": [
            {
                "number": 1,
                "type": "Basic",
                "gpt_type": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
            }
        ],
    }

    def handler(command, timeout, env):
        return 0, json.dumps(payload), 0.1

    shell = DiskpartShell(
        log=CommandLog(None),
        powershell="powershell.exe",
        process=fake_process(handler=handler),
    )
    assert shell.find_esp(2) is None


def test_release_efi_letter_skips_without_letter():
    shell = FakeShell()

    _releaser(shell).release(
        disk_number=2,
        letter=None,
        outcome=_touched(1),
        options={},
        log=_CommandLog(None),
    )

    assert shell.released == []


def test_runner_keeps_efi_letter_when_switch_off(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
) -> None:
    """同一个任务，release_efi_letter=False → 全流程一条 remove letter 都没有。"""
    from app.application.partitions import plan_partitions

    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    payload = {
        "partition_info": _PAYLOAD["partition_info"],
        "image_info": {"image_path": str(image)},
    }
    letters = {"efi": "E", "windows": "F", "data": "G"}
    parts = plan_partitions(payload, letters=letters)
    shell = fake_shell(query_handler=lambda disk: make_snapshot(parts), msr=None)
    process = fake_process(handler=lambda command, timeout, env: (87, "bad image", 0.1))
    runner = make_runner(shell=shell, process=process)

    spec = SimpleNamespace(
        disk_number=2,
        task_payload=payload,
        drive_letters=letters,
        runner_options={"release_efi_letter": False},
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert shell.released == []
    assert not any("remove letter" in line for script in shell.scripts for line in script)


def test_runner_releases_efi_letter_even_when_image_fails(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
) -> None:
    from app.application.partitions import plan_partitions

    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    payload = {
        "partition_info": _PAYLOAD["partition_info"],
        "image_info": {"image_path": str(image)},
    }
    letters = {"efi": "E", "windows": "F", "data": "G"}
    parts = plan_partitions(payload, letters=letters)
    shell = fake_shell(query_handler=lambda disk: make_snapshot(parts), msr=None)
    process = fake_process(handler=lambda command, timeout, env: (87, "bad image", 0.1))
    runner = make_runner(shell=shell, process=process)

    spec = SimpleNamespace(
        disk_number=2,
        task_payload=payload,
        drive_letters=letters,
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "应用镜像失败" in (result.error or "")
    assert shell.scripts[0] == ["select disk 2", "clean", "convert gpt", "exit"]
    # 收尾按“磁盘 + 分区号”释放，不按字母选卷
    assert shell.released == [(2, 1, "E")]


# ----------------------------------------------------------- MSR 删除（两段式）


def _tiny_image(tmp_path):
    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    return image


def _tiny_spec(image, *, payload=None):
    return SimpleNamespace(
        disk_number=2,
        task_payload=payload or _payload(image),
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )


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


def _sequence_process(fake_process, *payloads):
    """按顺序吐出分区表 JSON 的 ProcessShell 替身（用于 find_partition 重试）。"""
    queue = list(payloads)

    def handler(command, timeout, env):
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return 0, json.dumps(payload), 0.1

    return fake_process(handler=handler)


def _msr_shell(fake_process, *payloads):
    from app.application.command_log import CommandLog
    from app.application.shell import DiskpartShell

    return DiskpartShell(
        log=CommandLog(None),
        powershell="powershell.exe",
        process=_sequence_process(fake_process, *payloads),
    )


def test_find_msr_partition_reads_partition_number(fake_process) -> None:
    shell = _msr_shell(fake_process, _msr_payload(3))
    assert shell.find_msr(2) == 3


def test_find_msr_partition_returns_none_without_msr(fake_process) -> None:
    """有些盘 ``convert gpt`` 之后没有 MSR：不能瞎删，直接返回 None。"""
    payload = {"partition_style": "GPT", "partitions": [], "largest_free": 0}
    shell = _msr_shell(fake_process, payload)
    assert shell.find_msr(2) is None


def test_find_msr_partition_retries_before_giving_up(fake_process) -> None:
    """枚举慢：头两次查不到、第三次才出现 → 仍然要认出来（不能就此跳过删除）。"""
    empty = {"partition_style": "GPT", "partitions": [], "largest_free": 0}
    shell = _msr_shell(fake_process, empty, empty, _msr_payload(1))
    assert shell.find_msr(2) == 1


def test_find_msr_partition_raises_when_query_fails(fake_process) -> None:
    """查询失败要中止装机，而不是猜一个分区号去删。"""
    from app.application.command_log import CommandLog
    from app.application.shell import DiskpartShell

    def handler(command, timeout, env):
        raise RunnerError("Get-Partition : 拒绝访问")

    shell = DiskpartShell(
        log=CommandLog(None),
        powershell="powershell.exe",
        process=fake_process(handler=handler),
    )
    with pytest.raises(RunnerError):
        shell.find_msr(2)


def _payload(image, **partition_overrides):
    info = {"efi_size_mb": 100, "c_size_gb": 3, "data_size_gb": "auto",
            "allow_small_partitions": True}
    info.update(partition_overrides)
    return {
        "partition_info": info,
        "image_info": {"image_path": str(image)},
    }


def _parts(payload):
    from app.application.partitions import plan_partitions

    return plan_partitions(payload, letters={"efi": "E", "windows": "F", "data": "G"})


def test_run_steps_deletes_msr_then_verifies_before_creating_partitions(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
):
    """清盘 → 删 MSR（独立脚本）→ 复查 → 建分区 → 分区验收。

    关键顺序：删除必须发生在建 EFI 之前，而且删完要再查一次。
    """
    image = _tiny_image(tmp_path)
    payload = _payload(image)
    parts = _parts(payload)
    msr_calls: list[int] = []

    def msr(disk_number):
        msr_calls.append(disk_number)
        return 1 if len(msr_calls) == 1 else None

    shell = fake_shell(msr=msr, query_handler=lambda disk: make_snapshot(parts))
    runner = make_runner(shell=shell, process=fake_process())

    result = runner.run(_tiny_spec(image, payload=payload), threading.Event())

    assert result.success is True, result.error
    # 前三段 diskpart 顺序固定（标题也钉住，便于日志排错）
    assert shell.titles[:3] == [
        "diskpart 脚本（清盘 + 转 GPT）",
        "diskpart 脚本（删除 MSR）",
        "diskpart 脚本（建分区）",
    ]
    assert shell.scripts[0] == ["select disk 2", "clean", "convert gpt", "exit"]
    assert shell.scripts[1] == [
        "select disk 2",
        "select partition 1",
        "delete partition override",
        "exit",
    ]
    # 1.4.0：建分区只建几何（不 format / 不 assign），格式化逐分区单独一次
    assert shell.scripts[2] == create_geometry_lines(2, parts)
    assert "delete partition override" not in shell.scripts[2]
    expected_formats = [
        format_lines(2, position, part) for position, part in enumerate(parts, start=1)
    ]
    assert shell.scripts[3:] == expected_formats
    # 一次查 MSR + 一次复查；验收前还要查一次"卷是否就绪"
    assert msr_calls == [2, 2]
    assert len(shell.queries) >= 1


def test_partition_verification_failure_never_reaches_image(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
):
    """分区验收不通过 → 立刻结束，绝不写入镜像（两个阶段是分开的）。"""
    image = _tiny_image(tmp_path)
    payload = _payload(image)
    parts = _parts(payload)

    def broken(disk):
        snapshot = make_snapshot(parts)
        snapshot["partitions"][1]["size_bytes"] -= 10 * 1024 ** 3
        return snapshot

    shell = fake_shell(msr=None, query_handler=broken)
    process = fake_process()
    runner = make_runner(shell=shell, process=process)

    result = runner.run(_tiny_spec(image, payload=payload), threading.Event())

    assert result.success is False
    assert "分区验收不通过" in (result.error or "")
    assert "大小不符" in (result.error or "")
    assert process.calls == [], f"分区没验过就不该动镜像/引导，实际调用了 {process.calls}"
    # 分区三个步骤仍是 ok，只有验收失败——"建好了但没验过"要说清楚
    assert [step.status for step in result.steps if step.key in ("efi", "windows", "data")] == [
        "ok",
        "ok",
        "ok",
    ]
    assert next(step for step in result.steps if step.key == "layout").status == "failed"


def test_partition_step_details_carry_verification(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
):
    """「分区验收」是独立一步，细节（对照计划的结论）挂在它的 details 里。"""
    image = _tiny_image(tmp_path)
    payload = _payload(image)
    parts = _parts(payload)
    shell = fake_shell(msr=None, query_handler=lambda disk: make_snapshot(parts))
    runner = make_runner(shell=shell, process=fake_process())

    result = runner.run(_tiny_spec(image, payload=payload), threading.Event())

    layout = next(step for step in result.steps if step.key == "layout")
    assert layout.status == "ok"
    assert "与计划一致" in layout.message
    assert layout.details["problems"] == []
    assert layout.details["efi_partition"] == 1
    assert layout.details["partitions"]
    # 进度键固定为 7 步（含 layout 与收尾拷贝）
    assert [step.key for step in result.steps] == [
        "efi",
        "windows",
        "data",
        "layout",
        "image",
        "boot",
        "copy",
    ]


def test_run_steps_aborts_when_msr_survives_delete(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
):
    """删完复查还查得到 MSR → 中止，绝不继续建分区。"""
    image = _tiny_image(tmp_path)
    payload = _payload(image)
    parts = _parts(payload)
    shell = fake_shell(msr=1, query_handler=lambda disk: make_snapshot(parts))
    runner = make_runner(shell=shell, process=fake_process())

    result = runner.run(_tiny_spec(image, payload=payload), threading.Event())

    assert result.success is False
    assert "删除后仍然存在" in (result.error or "")
    assert len(shell.scripts) == 2  # 清盘 + 删 MSR，没有建分区


def test_run_steps_aborts_when_layout_check_fails(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
):
    """分区数不对 → 分区验收失败，不进镜像。"""
    image = _tiny_image(tmp_path)
    payload = _payload(image)
    parts = _parts(payload)

    def one_partition(disk):
        snapshot = make_snapshot(parts)
        snapshot["partitions"] = snapshot["partitions"][:1]
        return snapshot

    shell = fake_shell(msr=None, query_handler=one_partition)
    process = fake_process()
    runner = make_runner(shell=shell, process=process)

    result = runner.run(_tiny_spec(image, payload=payload), threading.Event())

    assert result.success is False
    assert "分区数与计划不符" in (result.error or "")
    assert process.calls == []


def test_run_steps_aborts_when_msr_lookup_fails(
    tmp_path, make_runner, fake_shell, fake_process
):
    """查不到 MSR 就整步失败：绝不在没确认的情况下继续建分区。"""
    image = _tiny_image(tmp_path)
    payload = _payload(image)

    def boom(disk_number):
        raise RunnerError("查询 MSR 分区失败")

    shell = fake_shell(msr=boom)
    runner = make_runner(shell=shell, process=fake_process())

    result = runner.run(_tiny_spec(image, payload=payload), threading.Event())

    assert result.success is False
    assert "无法确认 MSR 分区" in (result.error or "")
    assert len(shell.scripts) == 1  # 只跑了清盘


def test_cancelled_job_reports_cancelled_even_if_executor_fails():
    def executor(spec, cancel_event):
        cancel_event.set()
        return WorkerResult.failure(spec.disk_number, "任务已取消（镜像未写入）")

    manager = JobManager(executor=executor)
    job = manager.submit(JobSpec(disk_number=1), ExecutionPlan(disk_number=1))
    manager.confirm_batch([job.id])
    manager.wait_all(2)

    assert job.status == JobStatus.CANCELLED
    assert job.result is not None and "已取消" in (job.result.error or "")

# ------------------------------------------------------ 进度事件（job.step）
# 真实写盘的 6 个步骤：分区组（含独立的「分区验收」）+ 系统组（镜像 + 引导）
_STAGES = ("efi", "windows", "data", "layout", "image", "boot", "copy")


def _step_manager() -> tuple[JobManager, Job, list[tuple[str, str]]]:
    """构造一个会逐阶段上报的 JobManager（不碰磁盘）。"""
    seen: list[tuple[str, str]] = []

    def executor(spec: Any, cancel_event: threading.Event, on_step=None) -> WorkerResult:
        if on_step is not None:
            for key in _STAGES:
                on_step(key, "running", f"执行 {key}")
                on_step(key, "ok", f"{key} 完成")
                seen.append((key, "ok"))
            # 未知/历史附加步骤：会发事件，但不该算进进度
            on_step("diskpart_preview", "skipped", "仅预览")
        return WorkerResult(disk_number=spec.disk_number, success=True, message="ok")

    manager = JobManager(executor=executor)
    job = manager.submit(
        JobSpec(disk_number=1),
        ExecutionPlan(disk_number=1),
    )
    manager.confirm_batch([job.id])
    manager.wait_all(5)
    return manager, job, seen


def test_progress_counts_only_real_stages():
    """历史遗留的 ``diskpart_preview`` 步骤不能把进度顶到 8/7。"""
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
        return WorkerResult(disk_number=spec.disk_number, success=True, message="ok")

    # JobManager 内部读的是 _executor；写成 manager.executor 只会多一个新属性，
    # 真正的执行器仍会落到默认 SispService，事件一条都不会发。
    manager._executor = executor
    manager.subscribe(seen.put)
    job = manager.submit(
        JobSpec(disk_number=1),
        ExecutionPlan(disk_number=1),
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


# ------------------------------------------------- 执行器失败分支（Ghost/bcdboot/超时）
def _whole_disk_spec(tmp_path):
    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    return _tiny_spec(image)


def _ok_shell(fake_shell, make_snapshot, payload):
    parts = _parts(payload)
    return fake_shell(msr=None, query_handler=lambda disk: make_snapshot(parts))


def test_runner_reports_ghost_failure(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
) -> None:
    spec = _whole_disk_spec(tmp_path)
    shell = _ok_shell(fake_shell, make_snapshot, spec.task_payload)

    def handler(command, timeout, env):
        if str(command[0]).endswith("ghost64.exe"):
            return 87, "Ghost 失败", 0.1
        return 0, "ok", 0.1

    runner = make_runner(shell=shell, process=fake_process(handler=handler))

    result = runner.run(spec, threading.Event())

    assert result.success is False
    image_step = next(step for step in result.steps if step.key == "image")
    assert image_step.failed is True
    assert "87" in image_step.message
    # 引导记为"没执行"，分区步骤仍是通过的
    statuses = {step.key: step.status for step in result.steps}
    assert statuses["efi"] == "ok" and statuses["layout"] == "ok"
    assert statuses["boot"] == "skipped"
    assert "跳过引导" in next(step for step in result.steps if step.key == "boot").message


def test_runner_reports_bcdboot_failure_and_releases_letter(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
) -> None:
    spec = _whole_disk_spec(tmp_path)
    shell = _ok_shell(fake_shell, make_snapshot, spec.task_payload)

    def handler(command, timeout, env):
        if str(command[0]).endswith("bcdboot.exe"):
            return 2, "BFSVC Error: Could not open the BCD template store.", 0.1
        return 0, "ok", 0.1

    from app.application.uefi import UefiBootPhase

    process = fake_process(handler=handler)
    runner = make_runner(
        shell=shell, process=process, uefi=UefiBootPhase(process, bcdboot="bcdboot.exe")
    )

    result = runner.run(spec, threading.Event())

    assert result.success is False
    boot = next(step for step in result.steps if step.key == "boot")
    assert boot.failed is True
    assert "2" in boot.message
    # bcdboot 失败也必须释放 EFI 临时盘符
    assert shell.released == [(2, 1, "E")]
    # 引导没写成，就不该再假装校验过
    assert [step.key for step in result.steps].count("boot") == 1


def test_runner_reports_boot_verification_rejection(
    tmp_path, make_runner, fake_shell, fake_process, fake_uefi, make_snapshot
) -> None:
    """引导写成功、但盘上查不到 bootmgfw.efi → 仍然判失败（不能六步全绿）。"""
    spec = _whole_disk_spec(tmp_path)
    shell = _ok_shell(fake_shell, make_snapshot, spec.task_payload)
    uefi = fake_uefi(ok=False, message="EFI 分区里没有 bootmgfw.efi（bcdboot 没写进去）")
    runner = make_runner(shell=shell, process=fake_process(), uefi=uefi)

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "bootmgfw.efi" in (result.error or "")
    assert next(step for step in result.steps if step.key == "boot").failed is True


def test_runner_reports_diskpart_timeout(
    tmp_path, make_runner, fake_shell, fake_process
) -> None:
    spec = _whole_disk_spec(tmp_path)

    def timeout_handler(lines, title, timeout):
        raise RunnerError("命令超时（600 秒）：diskpart.exe")

    shell = fake_shell(diskpart_handler=timeout_handler)
    runner = make_runner(shell=shell, process=fake_process())

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "超时" in (result.error or "")
    assert result.steps[0].failed is True


def test_runner_refuses_tiny_layout_before_touching_disk(
    tmp_path, make_runner, fake_shell, fake_process
) -> None:
    """下限拦截必须在第一次 diskpart 之前：盘一根毛都不能动。"""
    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    spec = SimpleNamespace(
        disk_number=2,
        task_payload={
            # 没有 allow_small_partitions：3 GB 的系统分区必须被拒
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 3, "data_size_gb": "auto"},
            "image_info": {"image_path": str(image)},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
    )
    shell = fake_shell()
    runner = make_runner(shell=shell, process=fake_process())

    result = runner.run(spec, threading.Event())

    assert result.success is False
    assert "分区过小" in (result.error or "")
    assert shell.scripts == []


