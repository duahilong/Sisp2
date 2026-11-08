"""应用服务层使用的稳定数据契约。

这些对象只包含可序列化数据，不包含 PowerShell 命令、文件句柄或 UI 控件，
方便 CLI、桌面 GUI 和未来的本地 HTTP 服务复用。

``DiskInfo`` 除了硬盘级摘要，还通过 ``PartitionInfo`` 携带分区明细
（每块盘有哪些分区、多大、什么文件系统），界面据此画出分区条。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 字节换算与显示只有一份实现（app/units.py），这里重导出是为了让
# ``from app.application.contracts import format_size`` 的旧调用点继续可用。
from app.units import format_size


@dataclass(frozen=True)
class PartitionInfo:
    """单个分区的摘要，供界面展示。"""

    partition_number: int
    drive_letter: str | None
    size_bytes: int | None
    size_display: str
    offset_bytes: int | None
    type: str | None
    gpt_type: str | None
    file_system: str | None
    label: str | None
    is_system: bool
    is_boot: bool
    is_hidden: bool
    used_bytes: int | None = None
    free_bytes: int | None = None

    @classmethod
    def from_raw(cls, part: dict[str, Any]) -> PartitionInfo:
        size_bytes = part.get("size_bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            size_bytes = None
        offset_bytes = part.get("offset_bytes")
        if not isinstance(offset_bytes, int) or isinstance(offset_bytes, bool):
            offset_bytes = None
        drive_letter = part.get("drive_letter")
        drive_letter = str(drive_letter).strip().upper()[:1] or None if drive_letter else None
        size_display = part.get("size_display")
        if not isinstance(size_display, str) or not size_display:
            size_display = format_size(size_bytes)
        return cls(
            partition_number=_as_int(part.get("partition_number")),
            drive_letter=drive_letter,
            size_bytes=size_bytes,
            size_display=size_display,
            offset_bytes=offset_bytes,
            type=_as_text(part.get("type")),
            gpt_type=_as_text(part.get("gpt_type")),
            file_system=_as_text(part.get("file_system")),
            label=_as_text(part.get("label")),
            is_system=bool(part.get("is_system")),
            is_boot=bool(part.get("is_boot")),
            is_hidden=bool(part.get("is_hidden")),
            used_bytes=_as_int_or_none(part.get("used_bytes")),
            free_bytes=_as_int_or_none(part.get("free_bytes")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_number": self.partition_number,
            "drive_letter": self.drive_letter,
            "size_bytes": self.size_bytes,
            "size_display": self.size_display,
            "offset_bytes": self.offset_bytes,
            "type": self.type,
            "gpt_type": self.gpt_type,
            "file_system": self.file_system,
            "label": self.label,
            "is_system": self.is_system,
            "is_boot": self.is_boot,
            "is_hidden": self.is_hidden,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
        }


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class DiskInfo:
    """给界面展示的硬盘摘要。"""

    disk_number: int
    model: str | None
    serial_number: str | None
    unique_id: str | None
    size_bytes: int | None
    size_display: str
    partition_style: str | None
    bus_type: str | None
    drive_letters: tuple[str, ...]
    is_boot: bool
    is_system: bool
    is_offline: bool
    is_read_only: bool
    is_selectable: bool
    protection_reasons: tuple[str, ...]
    partitions: tuple[PartitionInfo, ...] = ()

    @classmethod
    def from_summary(cls, disk: dict[str, Any]) -> DiskInfo:
        size_bytes = disk.get("size_bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            size_bytes = None

        raw_model = disk.get("model")
        raw_serial_number = disk.get("serial_number")
        raw_unique_id = disk.get("unique_id")
        raw_partition_style = disk.get("partition_style")
        raw_bus_type = disk.get("bus_type")

        partitions = tuple(
            PartitionInfo.from_raw(part)
            for part in (disk.get("partitions") or [])
            if isinstance(part, dict)
        )

        return cls(
            disk_number=int(disk.get("disk_number") or 0),
            model=str(raw_model) if raw_model is not None else None,
            serial_number=str(raw_serial_number) if raw_serial_number is not None else None,
            unique_id=str(raw_unique_id) if raw_unique_id is not None else None,
            size_bytes=size_bytes,
            size_display=str(disk.get("size_display") or "未知"),
            partition_style=str(raw_partition_style) if raw_partition_style is not None else None,
            bus_type=str(raw_bus_type) if raw_bus_type is not None else None,
            drive_letters=tuple(
                str(letter).strip().upper()
                for letter in (disk.get("drive_letters") or [])
                if str(letter).strip()
            ),
            is_boot=bool(disk.get("is_boot")),
            is_system=bool(disk.get("is_system")),
            is_offline=bool(disk.get("is_offline")),
            is_read_only=bool(disk.get("is_read_only")),
            is_selectable=bool(disk.get("is_selectable", True)),
            protection_reasons=tuple(
                str(reason) for reason in (disk.get("protection_reasons") or []) if str(reason).strip()
            ),
            partitions=partitions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "model": self.model,
            "serial_number": self.serial_number,
            "unique_id": self.unique_id,
            "size_bytes": self.size_bytes,
            "size_display": self.size_display,
            "partition_style": self.partition_style,
            "bus_type": self.bus_type,
            "drive_letters": list(self.drive_letters),
            "is_boot": self.is_boot,
            "is_system": self.is_system,
            "is_offline": self.is_offline,
            "is_read_only": self.is_read_only,
            "is_selectable": self.is_selectable,
            "protection_reasons": list(self.protection_reasons),
            "partitions": [part.to_dict() for part in self.partitions],
        }


@dataclass(frozen=True)
class CheckItem:
    """单项检查结果，供计划确认界面展示。"""

    key: str
    label: str
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "message": self.message,
            "details": dict(self.details),
            "optional": self.optional,
        }


@dataclass(frozen=True)
class SelectionPlan:
    """选中硬盘后的盘符与写前检查结果。

    当前阶段只覆盖选择后的全局检查；真正的执行计划会在后续阶段继续扩展。
    """

    selected_disk_numbers: tuple[int, ...]
    forbidden_drive_letters: tuple[str, ...]
    drive_letter_allocations: dict[int, dict[str, str]]
