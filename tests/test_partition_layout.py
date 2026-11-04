"""分区布局（EFI → C → D → 可选 E）的单元测试。

守护两件事：

1. **默认布局与旧行为逐字相同**——不加额外分区时生成的 diskpart 脚本一个字符
   都不能变，否则等于把 235 个已通过的真机路径重新赌一遍；
2. 加了 E 之后，布局 / 盘符 / 容量 / verify 判据仍然自洽（4 个分区、4 个盘符、
   未分配空间可显示）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.application.contracts import DiskInfo
from app.application.partitions import (
    MAX_DATA_PARTITIONS,
    PartitionLayoutError,
    check_layout,
    data_partition_count,
    diskpart_lines,
    plan_partitions,
    unallocated_bytes,
)
from app.application.real_runner import WindowsDiskRunner, _CommandLog, _verify_verdict
from app.application.workflow import WorkflowController
from app.drive_letters import build_drive_letter_allocations

GIB = 1024 ** 3


def _legacy_runner_script(parts: list[Any], disk_number: int = 2) -> list[str]:
    return WindowsDiskRunner()._diskpart_script(
        disk_number=disk_number, parts=parts, log=_CommandLog(None)
    )


# --------------------------------------------------------------------- 默认布局


def test_default_layout_matches_legacy_diskpart_script() -> None:
    """EFI 300MB + C 30GB + D 剩余空间 → 与重构前的脚本逐字一致。"""
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 300, "c_size_gb": 30}},
        letters={"efi": "E", "windows": "F", "data": "G"},
    )

    assert [part.role for part in parts] == ["efi", "windows", "data"]
    assert _legacy_runner_script(parts) == [
        "select disk 2",
        "clean",
        "convert gpt",
        "create partition efi size=300",
        "format quick fs=fat32 label=EFI",
        "assign letter=E",
        "create partition primary size=30720",
        "format quick fs=ntfs label=Windows",
        "assign letter=F",
        "create partition primary",
        "format quick fs=ntfs label=Data",
        "assign letter=G",
        "exit",
    ]


def test_default_layout_uses_explicit_data_size() -> None:
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 100, "c_size_gb": 10, "data_size_gb": 5}}
    )
    assert parts[-1].size_mb == 5120
    assert parts[-1].size_text == "5.00 GB"
    assert "create partition primary size=5120" in diskpart_lines(parts)


def test_default_layout_keeps_data_fs_from_settings() -> None:
    parts = plan_partitions(
        {"partition_info": {"c_size_gb": 30, "data_size_gb": 10, "data_fs": "exfat"}}
    )
    assert parts[-1].file_system == "exFAT"
    assert "format quick fs=exfat label=Data" in diskpart_lines(parts)


def test_efi_size_is_clamped_to_minimum() -> None:
    parts = plan_partitions({"partition_info": {"efi_size_mb": 8, "c_size_gb": 30}})
    assert parts[0].size_mb == 64


def test_missing_c_size_is_rejected() -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions({"partition_info": {"efi_size_mb": 300}})
    assert "c_size_gb" in str(excinfo.value)


# --------------------------------------------------------------------- 额外分区


def test_extra_partition_appends_data2() -> None:
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 300,
                "c_size_gb": 120,
                "data_partitions": [
                    {"size_gb": 400, "file_system": "ntfs"},
                    {"size_gb": 500, "file_system": "exfat"},
                ],
            }
        },
        letters={"efi": "E", "windows": "F", "data": "G", "data2": "H"},
    )

    assert [part.role for part in parts] == ["efi", "windows", "data", "data2"]
    assert [part.label for part in parts] == ["EFI", "Windows", "Data", "Data2"]
    assert [part.letter for part in parts] == ["E", "F", "G", "H"]
    assert parts[2].size_mb == 400 * 1024
    assert parts[3].size_mb == 500 * 1024
    assert parts[3].file_system == "exFAT"

    lines = diskpart_lines(parts)
    assert "create partition primary size=409600" in lines
    assert "create partition primary size=512000" in lines
    assert "format quick fs=exfat label=Data2" in lines
    assert "assign letter=H" in lines


@pytest.mark.parametrize("size", [None, 0, "auto", "abc", -5])
def test_extra_partition_requires_explicit_size(size: Any) -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 100}, {"size_gb": size}],
                }
            }
        )
    assert "第 2 个数据分区" in str(excinfo.value)


def test_extra_partition_rejects_fat32() -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 100}, {"size_gb": 100, "file_system": "FAT32"}],
                }
            }
        )
    assert "文件系统不支持" in str(excinfo.value)


def test_extra_partition_limit() -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 100}] * (MAX_DATA_PARTITIONS + 1),
                }
            }
        )
    assert "最多只能有" in str(excinfo.value)


def test_data_partition_count_reads_explicit_list() -> None:
    assert data_partition_count({"partition_info": {"c_size_gb": 120}}) == 1
    assert (
        data_partition_count(
            {"partition_info": {"c_size_gb": 120, "data_partitions": [{"size_gb": 1}]}}
        )
        == 1
    )
    assert (
        data_partition_count(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 1}, {"size_gb": 2}],
                }
            }
        )
        == 2
    )


# --------------------------------------------------------------------- 容量与未分配


def test_layout_overflow_reports_sizes() -> None:
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 300,
                "c_size_gb": 30,
                "data_partitions": [{"size_gb": 50, "file_system": "NTFS"}],
            }
        }
    )
    assert check_layout(parts, 100 * GIB) == []
    problems = check_layout(parts, 80 * GIB)
    assert problems and "超过" in problems[0]
    assert "EFI 300MB" in problems[0]


def test_layout_with_rest_data_never_overflows() -> None:
    parts = plan_partitions({"partition_info": {"c_size_gb": 60, "data_size_gb": "auto"}})
    assert check_layout(parts, 100 * GIB) == []


def test_unallocated_bytes_counts_leftover() -> None:
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 300,
                "c_size_gb": 20,
                "data_partitions": [{"size_gb": 10, "file_system": "NTFS"}],
            }
        }
    )
    leftover = unallocated_bytes(parts, 100 * GIB)
    assert leftover == 100 * GIB - (300 * 1024 * 1024 + 30 * GIB) - 1024 * 1024
    # 有分区吃剩余空间时没有“未分配”概念
    rest = plan_partitions({"partition_info": {"c_size_gb": 20}})
    assert unallocated_bytes(rest, 100 * GIB) == 0


# --------------------------------------------------------------------- 盘符


def test_drive_letters_allocate_four_for_extra_partition() -> None:
    allocations = build_drive_letter_allocations([1, 2], ["C", "D"], {1: 1})
    assert allocations[1] == {"efi": "E", "windows": "F", "data": "G", "data2": "H"}
    assert allocations[2] == {"efi": "I", "windows": "J", "data": "K"}


def test_drive_letters_report_short_pool() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        build_drive_letter_allocations([1], [letter for letter in "EFGHIJKLMNOPQRSTUVWXY"], {1: 1})
    assert "可用盘符不足" in str(excinfo.value)
    assert "4 个盘符" in str(excinfo.value)


# --------------------------------------------------------------------- verify


def _verify_payload(count: int) -> dict[str, Any]:
    return {
        "partition_style": "GPT",
        "partitions": [
            {"number": index + 1, "size_bytes": GIB, "drive_letter": None}
            for index in range(count)
        ],
        "boot_file": r"G:\EFI\Microsoft\Boot\bootmgfw.efi",
        "system_file": r"H:\Windows\System32\config\SYSTEM",
    }


def test_verify_requires_planned_partition_count() -> None:
    ok, message, _ = _verify_verdict(_verify_payload(3), min_partitions=4)
    assert ok is False
    assert "期望至少 4" in message


def test_verify_accepts_auto_msr_partition() -> None:
    """实际盘上有 diskpart 自动建的 16MB MSR，所以 5 个分区满足“至少 4 个”。"""
    ok, message, _ = _verify_verdict(_verify_payload(5), min_partitions=4)
    assert ok is True
    assert "GPT" in message


# --------------------------------------------------------------------- 工作流接入


def _disk(number: int = 1, size_bytes: int = 100 * GIB) -> DiskInfo:
    return DiskInfo(
        disk_number=number,
        model="test disk",
        serial_number=None,
        unique_id=f"unique-{number}",
        size_bytes=size_bytes,
        size_display="100 GB",
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
    def __init__(self, disks: list[DiskInfo]) -> None:
        self.disks = disks
        self.dry_run = True
        self.allowed_disk_numbers = frozenset()
        self.extra_counts: dict[int, int] | None = None

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        self.extra_counts = dict(kwargs.get("extra_counts") or {})
        letters = build_drive_letter_allocations(
            list(selected), ["C", "D"], self.extra_counts
        )
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        from app.models import WorkerResult

        return WorkerResult.dry_run_plan(int(getattr(spec, "disk_number", 0) or 0))

    def check_config(self, config_path=None):
        return SimpleNamespace(passed=True, message="配置可解析", error_code=0)


def _controller(tmp_path, image_name: str = "win11.wim") -> WorkflowController:
    image = tmp_path / image_name
    image.write_bytes(b"wim")

    def preflight(config_path=None, dry_run=False):
        return {
            "results": [{"name": "运行前检查", "passed": True, "message": "ok"}],
            "config_payload": {
                "partition_info": {"efi_size_mb": 300, "c_size_gb": 20},
                "image_info": {"image_path": str(image), "image_index": 1},
            },
        }

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        dry_run=True,
        preflight_runner=preflight,
        image_indexes=lambda path: [1],
    )


def test_prepare_allocates_fourth_letter_for_extra_partition(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start(None, dry_run=True)

    prepared = controller.prepare(
        {
            1: {
                "c_size": 20,
                "data_partitions": [
                    {"size_gb": 10, "file_system": "NTFS"},
                    {"size_gb": 15, "file_system": "exFAT"},
                ],
            }
        }
    )[0]

    assert prepared.plan.executable is True, prepared.plan.blockers
    assert prepared.plan.drive_letters == {
        "efi": "E",
        "windows": "F",
        "data": "G",
        "data2": "H",
    }
    assert [item["role"] for item in prepared.plan.partition_plan] == [
        "efi",
        "windows",
        "data",
        "data2",
    ]
    assert prepared.plan.partition_plan[-1]["file_system"] == "exFAT"
    assert prepared.plan.unallocated_bytes > 0
    assert prepared.spec.config_payload["partition_info"]["data_partitions"][1] == {
        "size_gb": 15.0,
        "file_system": "exFAT",
    }
    # 显式列表存在时老字段必须被清掉，避免两个来源打架
    assert "data_size_gb" not in prepared.spec.config_payload["partition_info"]


def test_prepare_rejects_invalid_extra_partition(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start(None, dry_run=True)

    with pytest.raises(Exception) as excinfo:
        controller.prepare(
            {
                1: {
                    "data_partitions": [
                        {"size_gb": 10, "file_system": "NTFS"},
                        {"size_gb": 0, "file_system": "NTFS"},
                    ]
                }
            }
        )
    assert "分区配置无效" in str(excinfo.value)


def test_prepare_blocks_when_extra_partition_does_not_fit(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start(None, dry_run=True)

    with pytest.raises(Exception) as excinfo:
        controller.prepare(
            {
                1: {
                    "c_size": 20,
                    "data_partitions": [
                        {"size_gb": 40, "file_system": "NTFS"},
                        {"size_gb": 60, "file_system": "NTFS"},
                    ],
                }
            }
        )
    assert "容量不足" in str(excinfo.value)


def test_prepare_without_extra_partition_keeps_three_letters(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start(None, dry_run=True)

    prepared = controller.prepare([1])[0]
    assert prepared.plan.drive_letters == {"efi": "E", "windows": "F", "data": "G"}
    assert [item["role"] for item in prepared.plan.partition_plan] == [
        "efi",
        "windows",
        "data",
    ]
