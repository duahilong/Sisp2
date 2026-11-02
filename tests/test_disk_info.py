"""磁盘摘要归一化测试（纯内存，不调用 PowerShell）。

历史问题：摘要扫描脚本只采集盘符，导致界面上的分区大小/文件系统/类型全是“未知”。
这里锁定 ``summarize_disk`` 必须保留完整的 ``Get-Partition`` + ``Get-Volume`` 字段。
"""

from __future__ import annotations

from app.modules.disk_info.service import summarize_disk

_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024


def test_summarize_disk_keeps_partition_detail() -> None:
    summary = summarize_disk(
        {
            "disk_number": 1,
            "friendly_name": "Test Disk",
            "serial_number": "SN-1",
            "unique_id": "UID-1",
            "partition_style": "GPT",
            "size_bytes": 100 * _GB,
            "bus_type": "NVMe",
            "is_boot": False,
            "is_system": False,
            "is_offline": False,
            "is_read_only": False,
            "partitions": [
                {
                    "partition_number": 1,
                    "drive_letter": "E",
                    "size_bytes": 300 * _MB,
                    "offset_bytes": _MB,
                    "type": "System",
                    "gpt_type": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
                    "is_boot": True,
                    "is_hidden": False,
                    "is_system": True,
                    "volume": {
                        "drive_letter": "E",
                        "file_system": "FAT32",
                        "file_system_label": "EFI",
                        "size_bytes": 300 * _MB,
                        "size_remaining_bytes": 100 * _MB,
                    },
                }
            ],
        }
    )

    part = summary["partitions"][0]
    assert part["partition_number"] == 1
    assert part["drive_letter"] == "E"
    assert part["size_bytes"] == 300 * _MB
    assert part["size_display"] == "300.00 MB"
    assert part["file_system"] == "FAT32"
    assert part["label"] == "EFI"
    assert part["type"] == "System"
    assert part["free_bytes"] == 100 * _MB
    assert part["used_bytes"] == 200 * _MB
    assert part["is_boot"] is True
    assert summary["drive_letters"] == ["E"]
    assert summary["partition_count"] == 1


def test_summarize_disk_without_volume_still_keeps_size_and_type() -> None:
    summary = summarize_disk(
        {
            "disk_number": 2,
            "friendly_name": "RAW Disk",
            "size_bytes": 10 * _GB,
            "partitions": [
                {
                    "drive_letter": None,
                    "size_bytes": 16 * _MB,
                    "type": "Reserved",
                    "volume": None,
                }
            ],
        }
    )

    part = summary["partitions"][0]
    assert part["partition_number"] == 1  # 缺号时按位置补
    assert part["size_display"] == "16.00 MB"
    assert part["type"] == "Reserved"
    assert part["file_system"] is None
    assert part["free_bytes"] is None
    assert summary["drive_letters"] == []
    assert summary["model"] == "RAW Disk"
