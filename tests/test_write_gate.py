"""写盘闸门与“危险操作需输入确认”的回归测试。

覆盖两类“设置项终于接上了业务逻辑”的行为：

- ``protectSystemDisk`` / ``blockUnverified`` 决定哪些盘不可选，并且
  ``SispService._require_write_allowed`` 在执行层再判一次（纵深防御）；
- ``typeToConfirm`` 打开时，``WorkflowController.confirm`` 必须拿到匹配的
  确认文本，否则拒绝真实执行。
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from app.application.service import SispService
from app.application.workflow import WorkflowController, WorkflowError
from app.models import WorkerResult

_GIB = 1024**3


def _summary(
    number: int = 1,
    *,
    is_system: bool = False,
    unique_id: str | None = "unique-1",
    serial_number: str | None = "SN-1",
) -> dict[str, Any]:
    return {
        "disk_number": number,
        "model": "test disk",
        "serial_number": serial_number,
        "unique_id": unique_id,
        "size_bytes": 60 * _GIB,
        "size_display": "60 GB",
        "partition_style": "RAW",
        "bus_type": "NVMe",
        "drive_letters": [],
        "is_boot": is_system,
        "is_system": is_system,
        "is_offline": False,
        "is_read_only": False,
    }


def _service(disk: dict[str, Any], **kwargs: Any) -> SispService:
    runner = kwargs.pop("real_runner", None) or (lambda spec, event: WorkerResult.dry_run_plan(1))
    return SispService(disk_scanner=lambda: [disk], dry_run=False, real_runner=runner, **kwargs)


@pytest.fixture(autouse=True)
def _no_letter_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """盘符扫描会调用 PowerShell；测试里固定成空列表。"""
    import app.modules.disk_info.service as disk_info

    monkeypatch.setattr(disk_info, "scan_used_drive_letters", lambda: [])


# ------------------------------------------------------------------ 可选性
def test_system_disk_is_protected_by_default() -> None:
    service = _service(_summary(is_system=True))
    (disk,) = service.scan_protected_disks()
    assert disk.is_selectable is False
    assert "系统盘" in disk.protection_reasons


def test_protect_system_disk_off_makes_system_disk_selectable() -> None:
    service = _service(_summary(is_system=True), protect_system_disk=False)
    (disk,) = service.scan_protected_disks()
    assert disk.is_selectable is True
    assert disk.protection_reasons == ()


def test_offline_disk_stays_protected_even_when_system_protection_is_off() -> None:
    disk = dict(_summary(is_system=True), is_offline=True)
    service = _service(disk, protect_system_disk=False)
    (scanned,) = service.scan_protected_disks()
    assert scanned.is_selectable is False
    assert "硬盘离线" in scanned.protection_reasons


def test_block_unverified_rejects_disk_without_identity() -> None:
    disk = _summary(unique_id=None, serial_number=None)
    strict = _service(disk)
    loose = _service(disk, block_unverified=False)

    (strict_disk,) = strict.scan_protected_disks()
    (loose_disk,) = loose.scan_protected_disks()

    assert strict_disk.is_selectable is False
    assert any("唯一标识" in reason for reason in strict_disk.protection_reasons)
    assert loose_disk.is_selectable is True


# ------------------------------------------------------------------ 执行闸门
def _spec(number: int = 1, *, unique_id: str | None = "unique-1", serial: str | None = "SN-1"):
    return SimpleNamespace(
        disk_number=number,
        dry_run=False,
        config_payload={"excluded_disk_names": [], "partition_info": {"c_size_gb": 30}},
        expected_identity={
            "disk_number": number,
            "unique_id": unique_id,
            "serial_number": serial,
            "model": "test disk",
            "size_bytes": 60 * _GIB,
        },
    )


def test_execute_spec_refuses_protected_system_disk() -> None:
    """即便绕过 workflow.confirm()，服务层也必须拒绝系统盘。"""
    service = _service(_summary(is_system=True), allowed_disk_numbers=[1])
    with pytest.raises(PermissionError) as excinfo:
        service.execute_spec(_spec())
    assert "受保护" in str(excinfo.value)


def test_execute_spec_refuses_unverified_disk() -> None:
    disk = _summary(unique_id=None, serial_number=None)
    service = _service(disk, allowed_disk_numbers=[1])
    with pytest.raises(PermissionError) as excinfo:
        service.execute_spec(_spec(1, unique_id=None, serial=None))
    assert "UniqueId" in str(excinfo.value)


def test_execute_spec_runs_runner_for_clean_disk() -> None:
    calls: list[int] = []

    def runner(spec, event):
        calls.append(int(spec.disk_number))
        return WorkerResult.dry_run_plan(int(spec.disk_number))

    service = _service(_summary(), allowed_disk_numbers=[1], real_runner=runner)
    service.execute_spec(_spec())
    assert calls == [1]


# ------------------------------------------------------------------ 输入确认
# 计划里会按 payload 重算“镜像文件是否存在”，所以这里的路径必须真实存在；
# 用本测试文件当占位——检查只看存在性，不解析内容。
_FAKE_IMAGE = str(Path(__file__).resolve())


def _preflight(config_path=None, dry_run=False) -> dict[str, Any]:
    return {
        "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
        "config_payload": {
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 10, "data_size_gb": "auto"},
            "image_info": {"image_path": _FAKE_IMAGE, "image_index": 1},
            "excluded_disk_names": [],
        },
    }


def _controller(type_to_confirm: bool) -> tuple[WorkflowController, list[str]]:
    service = _service(_summary(), allowed_disk_numbers=[1])
    wf = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
        type_to_confirm=type_to_confirm,
        worker_interval=0.0,
    )
    wf.start(None, dry_run=False)
    prepared = wf.prepare([1], dry_run=False)
    jobs = wf.submit(prepared, require_confirmation=True)
    return wf, [job.id for job in jobs]


def test_confirm_requires_text_when_type_to_confirm_on() -> None:
    wf, job_ids = _controller(type_to_confirm=True)
    with pytest.raises(WorkflowError) as excinfo:
        wf.confirm(job_ids)
    assert "输入" in str(excinfo.value)


def test_confirm_rejects_mismatched_text() -> None:
    wf, job_ids = _controller(type_to_confirm=True)
    with pytest.raises(WorkflowError) as excinfo:
        wf.confirm(job_ids, confirm_text="9")
    assert "不匹配" in str(excinfo.value)


@pytest.mark.parametrize("text", ["1", " disk 1 ", "硬盘1", "TEST DISK"])
def test_confirm_accepts_disk_number_or_model(text: str) -> None:
    wf, job_ids = _controller(type_to_confirm=True)
    wf.confirm(job_ids, confirm_text=text)
    jobs = wf.wait_all(timeout=5)
    assert jobs
    assert all(job.status.value != "waiting_confirmation" for job in jobs)


def test_confirm_without_text_when_gate_disabled() -> None:
    wf, job_ids = _controller(type_to_confirm=False)
    wf.confirm(job_ids)
    jobs = wf.wait_all(timeout=5)
    assert jobs
    assert all(job.status.value != "waiting_confirmation" for job in jobs)


# ------------------------------------------------------------------ 设置热更新
def test_settings_provider_updates_protection_live() -> None:
    """设置页关掉系统盘保护后，无需重启即可重新扫描出可选盘。"""
    settings: dict[str, Any] = {
        "protectSystemDisk": True,
        "blockUnverified": True,
        "typeToConfirm": True,
    }
    service = _service(_summary(0, is_system=True), allowed_disk_numbers=[0])
    wf = WorkflowController(
        service=service,
        dry_run=False,
        settings_provider=lambda: dict(settings),
    )

    assert service.protect_system_disk is True
    (disk,) = service.scan_protected_disks()
    assert disk.is_selectable is False

    settings["protectSystemDisk"] = False
    settings["blockUnverified"] = False
    wf._apply_live_safety_flags()

    assert service.protect_system_disk is False
    assert service.block_unverified is False
    (disk,) = service.scan_protected_disks()
    assert disk.is_selectable is True


def test_confirm_follows_live_type_to_confirm_setting() -> None:
    settings: dict[str, Any] = {"typeToConfirm": True}
    service = _service(_summary(), allowed_disk_numbers=[1])
    wf = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
        type_to_confirm=False,
        worker_interval=0.0,
        settings_provider=lambda: dict(settings),
    )
    wf.start(None, dry_run=False)
    jobs = wf.submit(wf.prepare([1], dry_run=False), require_confirmation=True)
    job_ids = [job.id for job in jobs]

    with pytest.raises(WorkflowError):
        wf.confirm(job_ids)

    settings["typeToConfirm"] = False
    wf.confirm(job_ids)
    assert all(job.status.value != "waiting_confirmation" for job in wf.wait_all(timeout=5))


def test_refresh_disks_applies_live_protection_setting() -> None:
    """点「重新扫描」时也要读最新设置，不能等重启。"""
    settings: dict[str, Any] = {"protectSystemDisk": True}
    service = _service(_summary(0, is_system=True), allowed_disk_numbers=[0])
    wf = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
        settings_provider=lambda: dict(settings),
    )
    session = wf.start(None, dry_run=True)
    assert session.disks[0].is_selectable is False

    settings["protectSystemDisk"] = False
    disks = wf.refresh_disks()
    assert disks[0].is_selectable is True


# ------------------------------------------------------------------ 省略选择
def _controller_multi(disks: list[dict[str, Any]], **kwargs: Any) -> WorkflowController:
    """多盘控制器：验证“不传 selection”时按可选性自动挑盘。"""
    service = SispService(disk_scanner=lambda: list(disks), dry_run=False, **kwargs)
    wf = WorkflowController(
        service=service,
        dry_run=False,
        preflight_runner=_preflight,
        worker_interval=0.0,
    )
    wf.start(None, dry_run=False)
    return wf


def test_prepare_without_selection_uses_selectable_disks() -> None:
    wf = _controller_multi([_summary(1), _summary(2, is_system=True)])
    prepared = wf.prepare(dry_run=True)
    assert [item.disk.disk_number for item in prepared] == [1]


def test_prepare_empty_selection_is_still_rejected() -> None:
    wf = _controller_multi([_summary(1)])
    with pytest.raises(WorkflowError) as excinfo:
        wf.prepare([], dry_run=True)
    assert "尚未选择" in str(excinfo.value)


def test_prepare_without_selection_fails_when_all_protected() -> None:
    wf = _controller_multi([_summary(1, is_system=True)])
    with pytest.raises(WorkflowError) as excinfo:
        wf.prepare(dry_run=True)
    assert "没有可操作" in str(excinfo.value)
