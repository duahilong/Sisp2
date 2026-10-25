"""应用服务层（app.application）的稳定接口测试。

这些测试不触碰 PowerShell、真实磁盘或交互输入，只验证 CLI/GUI 共用的
DTO、配置检查、保护规则和盘符计划。
"""

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application.contracts import (
    ConfigCheckResult,
    DiskInfo,
    DiskListResult,
    SelectionPlan,
)
from app.application.reporting import ConsoleReporter, MemoryReporter
from app.application.service import SispService
from app.modules.common.constants import ErrorCode
from app.modules.common.service import ConfigError

GB = 1024 ** 3


def make_disk(
    disk_number: int,
    *,
    model: str = "Target Disk",
    size_bytes: int = 500 * GB,
    drive_letters: list[str] | None = None,
    is_boot: bool = False,
    is_system: bool = False,
) -> dict:
    return {
        "disk_number": disk_number,
        "model": model,
        "serial_number": f"SN-{disk_number}",
        "unique_id": f"UID-{disk_number}",
        "size_bytes": size_bytes,
        "size_display": f"{size_bytes / GB:.2f} GB",
        "partition_style": "GPT",
        "bus_type": "SATA",
        "drive_letters": drive_letters or [],
        "is_boot": is_boot,
        "is_system": is_system,
        "is_offline": False,
        "is_read_only": False,
    }


def make_config(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir()
    image = tmp_path / "win.gho"
    ghost = tmp_path / "ghost64.exe"
    bcd = tmp_path / "bcdboot.exe"
    for path in (image, ghost, bcd):
        path.write_bytes(b"x")

    return {
        "config_path": str(tmp_path / "config.json"),
        "partition_info": {"efi_size_mb": 100, "c_size_gb": 100},
        "software_paths": {"ghost64_path": str(ghost), "bcdboot_path": str(bcd)},
        "image_info": {"image_path": str(image)},
        "copy_info": {"source_dir": str(source)},
        "excluded_disk_names": [],
    }


def build_service(**overrides) -> SispService:
    defaults = {
        "config_loader": lambda _: {"excluded_disk_names": []},
        "disk_scanner": lambda: [],
        "used_letter_scanner": lambda: [],
        "protection_applier": lambda disks, excluded: disks,
    }
    defaults.update(overrides)
    return SispService(**defaults)


def test_disk_info_from_summary_normalizes_fields() -> None:
    disk = DiskInfo.from_summary(
        {
            "disk_number": 2,
            "model": "Demo",
            "serial_number": "sn-2",
            "unique_id": None,
            "size_bytes": 1024,
            "size_display": "",
            "partition_style": "GPT",
            "bus_type": "NVMe",
            "drive_letters": ["e", " f "],
            "is_boot": False,
            "is_system": True,
            "is_offline": False,
            "is_read_only": True,
            "is_selectable": False,
            "protection_reasons": ["系统盘"],
        }
    )

    assert disk.disk_number == 2
    assert disk.size_bytes == 1024
    assert disk.size_display == "未知"
    assert disk.drive_letters == ("E", "F")
    assert disk.unique_id is None
    assert disk.is_selectable is False
    assert disk.to_dict()["protection_reasons"] == ["系统盘"]


def test_disk_info_ignores_invalid_size_bytes() -> None:
    assert DiskInfo.from_summary({"disk_number": 0, "size_bytes": True}).size_bytes is None
    assert DiskInfo.from_summary({"disk_number": 0, "size_bytes": "1024"}).size_bytes is None


def test_contracts_are_frozen_dataclasses() -> None:
    result = ConfigCheckResult(passed=True, payload={"a": 1})
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]

    plan = SelectionPlan(
        selected_disk_numbers=(1,),
        forbidden_drive_letters=("E",),
        drive_letter_allocations={1: {"efi": "F"}},
    )
    assert plan.drive_letter_allocations[1]["efi"] == "F"
    assert DiskListResult(disks=()).disks == ()


def test_list_disks_applies_protection_and_exclusions() -> None:
    disks = [
        make_disk(0, model="System Disk", is_system=True),
        make_disk(1, model="Target 1"),
        make_disk(2, model="Excluded Disk"),
    ]
    service = SispService(
        config_loader=lambda _: {"excluded_disk_names": ["Excluded Disk"]},
        disk_scanner=lambda: disks,
    )

    result = service.list_disks()

    assert isinstance(result, DiskListResult)
    assert result.warning is None
    assert [disk.disk_number for disk in result.disks] == [0, 1, 2]
    assert result.disks[0].is_selectable is False
    assert "系统盘" in result.disks[0].protection_reasons
    assert result.disks[1].is_selectable is True
    assert result.disks[2].is_selectable is False
    assert "配置排除盘" in result.disks[2].protection_reasons


def test_list_disks_tolerates_config_errors_with_warning() -> None:
    def broken_loader(_):
        raise OSError("配置损坏")

    service = SispService(
        config_loader=broken_loader,
        disk_scanner=lambda: [make_disk(1)],
    )

    result = service.list_disks(ignore_config_errors=True)

    assert result.warning is not None
    assert "配置损坏" in result.warning
    assert len(result.disks) == 1
    assert result.disks[0].is_selectable is True


def test_list_disks_raises_when_config_errors_not_ignored() -> None:
    def broken_loader(_):
        raise OSError("配置损坏")

    service = SispService(config_loader=broken_loader, disk_scanner=lambda: [])

    with pytest.raises(OSError):
        service.list_disks()


def test_check_config_passes_for_valid_files(tmp_path: Path) -> None:
    payload = make_config(tmp_path)
    service = build_service(config_loader=lambda _: payload)

    result = service.check_config(tmp_path / "config.json")

    assert isinstance(result, ConfigCheckResult)
    assert result.passed is True
    assert result.payload is payload
    assert result.error_code == 0


def test_check_config_returns_structured_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["image_info"] = {"image_path": str(tmp_path / "source")}
    service = build_service(config_loader=lambda _: config)

    result = service.check_config()

    assert result.passed is False
    assert result.error_code == ErrorCode.CONFIG_ERROR
    assert "不是文件" in result.message


def test_build_selection_plan_allocates_drive_letters(tmp_path: Path) -> None:
    payload = make_config(tmp_path)
    disks = [make_disk(0, is_system=True), make_disk(1)]
    service = SispService(
        config_loader=lambda _: payload,
        disk_scanner=lambda: disks,
        used_letter_scanner=lambda: ["E", "F", "f"],
    )

    plan = service.build_selection_plan(
        payload,
        disks,
        [1],
        resource_resolver=lambda _: 0,
    )

    assert isinstance(plan, SelectionPlan)
    assert plan.selected_disk_numbers == (1,)
    assert plan.forbidden_drive_letters == ("E", "F")
    assert plan.drive_letter_allocations == {
        1: {"efi": "G", "windows": "H", "data1": "I", "data2": "J"}
    }


def test_build_selection_plan_uses_explicit_used_letters(tmp_path: Path) -> None:
    payload = make_config(tmp_path)

    def unexpected_scan():
        raise AssertionError("提供 used_letters 时不应再次扫描盘符")

    service = SispService(
        config_loader=lambda _: payload,
        disk_scanner=lambda: [],
        used_letter_scanner=unexpected_scan,
    )
    plan = service.build_selection_plan(
        payload,
        [make_disk(1)],
        [1],
        used_letters=["E"],
        resource_resolver=lambda _: 0,
    )

    assert plan.forbidden_drive_letters == ("E",)
    assert plan.drive_letter_allocations[1]["efi"] == "F"


def test_build_selection_plan_empty_selection_does_no_work(tmp_path: Path) -> None:
    payload = make_config(tmp_path)
    service = SispService(
        config_loader=lambda _: payload,
        disk_scanner=lambda: [],
        used_letter_scanner=lambda: (_ for _ in ()).throw(AssertionError("不应扫描盘符")),
    )

    plan = service.build_selection_plan(payload, [], [])

    assert plan.selected_disk_numbers == ()
    assert plan.forbidden_drive_letters == ()
    assert plan.drive_letter_allocations == {}


def test_build_selection_plan_rejects_protected_disk(tmp_path: Path) -> None:
    payload = make_config(tmp_path)
    disks = [
        dict(
            make_disk(0, is_system=True),
            is_selectable=False,
            protection_reasons=["系统盘"],
        )
    ]
    service = build_service(config_loader=lambda _: payload)

    with pytest.raises(ValueError, match="不可选择"):
        service.build_selection_plan(payload, disks, [0], used_letters=[])


def test_build_selection_plan_rejects_resource_on_target(tmp_path: Path) -> None:
    payload = make_config(tmp_path)
    service = build_service(
        config_loader=lambda _: payload,
        used_letter_scanner=lambda: [],
    )

    with pytest.raises(ConfigError, match="位于目标硬盘上"):
        service.build_selection_plan(
            payload,
            [make_disk(1)],
            [1],
            resource_resolver=lambda _: 1,
        )


def test_reporters_capture_step_output(capsys) -> None:
    memory = MemoryReporter()
    memory.line("hello")
    memory.step_started(1, 7, "初始化硬盘")
    assert memory.lines == ["hello", "[1/7] 初始化硬盘..."]

    console = ConsoleReporter()
    console.line("console line")
    console.step_started(2, 7, "验证")
    output = capsys.readouterr().out
    assert "console line" in output
    assert "[2/7] 验证..." in output
