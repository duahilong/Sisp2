"""应用服务层使用的稳定数据契约。

这些对象只包含可序列化数据，不包含 PowerShell 命令、文件句柄或 UI 控件，
方便 CLI、桌面 GUI 和未来的本地 HTTP 服务复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
        }


@dataclass(frozen=True)
class DiskListResult:
    """硬盘列表及非致命配置警告。"""

    disks: tuple[DiskInfo, ...]
    warning: str | None = None


@dataclass(frozen=True)
class ConfigCheckResult:
    """配置检查结果，供 CLI/GUI 决定如何展示错误。"""

    passed: bool
    payload: dict[str, Any] | None = None
    error_code: int = 0
    message: str = ""


@dataclass(frozen=True)
class CheckItem:
    """单项检查结果，供计划确认界面展示。"""

    key: str
    label: str
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class SelectionPlan:
    """选中硬盘后的盘符与写前检查结果。

    当前阶段只覆盖选择后的全局检查；真正的执行计划会在后续阶段继续扩展。
    """

    selected_disk_numbers: tuple[int, ...]
    forbidden_drive_letters: tuple[str, ...]
    drive_letter_allocations: dict[int, dict[str, str]]
