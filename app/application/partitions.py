"""分区布局：把 ``partition_info`` 变成一份明确的“要建哪些分区”的列表。

整盘装机的分区结构以前散在三个地方（真实执行器拼 diskpart 脚本、容量预检算
算术、dry-run 生成脚本预览），加一个数据分区要同时改三处，很容易漂移。这里把
布局收敛成一个纯函数，执行器 / 预检 / 预览 / 界面摘要都消费同一份结果：

    EFI（FAT32） → Windows（NTFS） → Data（NTFS/exFAT） → Data2（可选，NTFS/exFAT）

约定：

- ``role`` 同时是盘符字典的键（``efi`` / ``windows`` / ``data`` / ``data2``），
  与 ``drive_letters`` 保持一一对应；
- ``size_mb=None`` 只可能出现在**唯一**的数据分区上（业务配置写了
  ``data_size_gb: auto``），表示“吃掉剩余空间”。显式给出 ``data_partitions``
  时每项都必须是正数，剩余空间保持未分配——界面上“加一个 E 分区”走的就是这条；
- 文件系统统一成规范写法（``NTFS`` / ``exFAT`` / ``FAT32``），拼脚本时再转成
  diskpart 的小写 token；
- 容量算术留 1 MB 给 GPT 表与分区对齐，和 ``_check_partition_fit`` 的老口径一致。

全部是纯函数，不碰磁盘、不读设置，方便单测。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.application.contracts import format_size
from app.drive_letters import letter_roles

BYTES_PER_MB = 1024 * 1024

# EFI 分区：太小放不下引导文件，太大浪费；与设置页的范围保持一致。
EFI_MIN_MB = 64
EFI_DEFAULT_MB = 100

# C 之后最多再加一个数据分区（E）。盘符池、verify 判据都按这个上限设计。
MAX_DATA_PARTITIONS = 2

# 额外数据分区只给这两个：diskpart 对 >32 GB 的 FAT32 会直接失败。
DATA_FS_CHOICES = ("NTFS", "exFAT")

_FS_CANON = {"ntfs": "NTFS", "exfat": "exFAT", "fat32": "FAT32"}


class PartitionLayoutError(ValueError):
    """布局不合法：缺大小、分区太多、文件系统不支持……消息直接给界面用。"""


@dataclass(frozen=True)
class PlannedPartition:
    """一个将要被创建的分区。``size_mb=None`` 表示吃掉剩余空间。"""

    role: str
    label: str
    file_system: str
    size_mb: int | None = None
    letter: str | None = None

    @property
    def size_text(self) -> str:
        if self.size_mb is None:
            return "剩余空间"
        return format_size(self.size_mb * BYTES_PER_MB)

    @property
    def is_data(self) -> bool:
        return self.role.startswith("data")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "label": self.label,
            "file_system": self.file_system,
            "size_mb": self.size_mb,
            "size_display": self.size_text,
            "letter": self.letter,
        }


def role_names(data_partitions: int) -> tuple[str, ...]:
    """按数据分区个数给出角色顺序（与盘符字典的键同源）。"""
    return tuple(letter_roles(max(0, int(data_partitions) - 1)))


def partition_info(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """取出 ``partition_info``；不是对象时当作空。"""
    info = (payload or {}).get("partition_info") if isinstance(payload, Mapping) else None
    return info if isinstance(info, Mapping) else {}


def data_partition_count(payload: Mapping[str, Any] | None) -> int:
    """这块盘要建几个数据分区（1 或 2）；盘符分配在解析布局之前就要用到。"""
    raw = partition_info(payload).get("data_partitions")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and raw:
        return min(len(raw), MAX_DATA_PARTITIONS)
    return 1


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN / 非正数
        return None
    return number


def _gb_to_mb(value: Any) -> int | None:
    number = _positive_number(value)
    return None if number is None else round(number * 1024)


def _canonical_fs(value: Any, *, fallback: str = "NTFS") -> str:
    text = str(value or "").strip()
    return _FS_CANON.get(text.lower(), fallback)


def _letter_of(letters: Mapping[str, str] | None, role: str) -> str | None:
    if not letters:
        return None
    text = str(letters.get(role) or "").strip().upper()[:1]
    return text or None


def normalise_data_partitions(raw: Any) -> list[dict[str, Any]]:
    """校验并归一化界面的 ``data_partitions``（保留 ``size_gb`` + 规范文件系统）。

    返回的列表可以直接存回 payload，保证“校验通过”与“存进去的形状”一致。
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PartitionLayoutError("partition_info.data_partitions 必须是数组")
    if not raw:
        raise PartitionLayoutError("partition_info.data_partitions 至少要有一个数据分区")
    if len(raw) > MAX_DATA_PARTITIONS:
        raise PartitionLayoutError(
            f"最多只能有 {MAX_DATA_PARTITIONS} 个数据分区"
            f"（C 之后最多再加 {MAX_DATA_PARTITIONS - 1} 个）"
        )
    normalised: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise PartitionLayoutError(f"第 {index} 个数据分区的配置不是对象")
        raw_gb = item.get("size_gb")
        if _positive_number(raw_gb) is None and _positive_number(item.get("size_mb")) is not None:
            # 也接受直接给 MB 的调用方（手写 REST payload 时更省事）。
            size_gb: float = round(float(item["size_mb"]) / 1024, 3)
        else:
            size_gb = float(raw_gb) if _positive_number(raw_gb) is not None else 0.0
        if size_gb <= 0:
            raise PartitionLayoutError(f"第 {index} 个数据分区必须填写大小（GB，正整数）")
        file_system = _canonical_fs(item.get("file_system"))
        if file_system not in DATA_FS_CHOICES:
            raise PartitionLayoutError(
                f"第 {index} 个数据分区的文件系统不支持：{file_system}"
                f"（可选 {' / '.join(DATA_FS_CHOICES)}）"
            )
        normalised.append({"size_gb": size_gb, "file_system": file_system})
    return normalised


def _explicit_data_specs(raw: Any) -> list[dict[str, Any]]:
    """把归一化后的数据分区列表换成“MB + 文件系统”，供布局使用。"""
    return [
        {"size_mb": _gb_to_mb(item["size_gb"]), "file_system": item["file_system"]}
        for item in normalise_data_partitions(raw)
    ]


def plan_partitions(
    payload: Mapping[str, Any] | None,
    *,
    letters: Mapping[str, str] | None = None,
) -> list[PlannedPartition]:
    """把配置解析成明确的分区列表（EFI → Windows → 数据分区）。

    ``letters`` 给出时顺带把盘符写进结果，方便执行器和界面共用同一份计划。
    """
    info = partition_info(payload)

    raw_efi = _positive_number(info.get("efi_size_mb"))
    efi_mb = max(round(raw_efi), EFI_MIN_MB) if raw_efi is not None else EFI_DEFAULT_MB

    c_gb = _positive_number(info.get("c_size_gb"))
    if c_gb is None:
        raise PartitionLayoutError("配置里 partition_info.c_size_gb 必须是正整数")
    c_mb = round(c_gb * 1024)

    parts: list[PlannedPartition] = [
        PlannedPartition(role="efi", label="EFI", file_system="FAT32", size_mb=efi_mb),
        PlannedPartition(role="windows", label="Windows", file_system="NTFS", size_mb=c_mb),
    ]

    raw_list = info.get("data_partitions")
    if raw_list is None:
        # 老路径：单个数据分区；data_size_gb 缺省/auto 表示吃掉剩余空间。
        raw_size = info.get("data_size_gb")
        if raw_size in (None, "", "auto"):
            data_mb: int | None = None
        else:
            data_mb = _gb_to_mb(raw_size)
            if data_mb is None:
                raise PartitionLayoutError("partition_info.data_size_gb 必须是正数或 auto")
        parts.append(
            PlannedPartition(
                role="data",
                label="Data",
                file_system=_canonical_fs(info.get("data_fs")),
                size_mb=data_mb,
            )
        )
    else:
        for index, spec in enumerate(_explicit_data_specs(raw_list), start=1):
            parts.append(
                PlannedPartition(
                    role="data" if index == 1 else f"data{index}",
                    label="Data" if index == 1 else f"Data{index}",
                    file_system=str(spec["file_system"]),
                    size_mb=int(spec["size_mb"]),
                )
            )

    if letters:
        parts = [replace(part, letter=_letter_of(letters, part.role)) for part in parts]
    return parts


def layout_summary(parts: Sequence[PlannedPartition]) -> str:
    """``EFI 100MB + C 120GB + D 300GB`` 这种简短描述（报错与界面共用）。"""
    pieces: list[str] = []
    for part in parts:
        if part.role == "efi":
            label = "EFI"
            size = f"{part.size_mb}MB" if part.size_mb else "?"
        elif part.role == "windows":
            label = "C"
            size = f"{part.size_mb / 1024:.0f}GB" if part.size_mb else "?"
        else:
            label = part.label
            size = f"{part.size_mb / 1024:.0f}GB" if part.size_mb else "剩余空间"
        pieces.append(f"{label} {size}")
    return " + ".join(pieces)


def check_layout(parts: Sequence[PlannedPartition], disk_size_bytes: Any) -> list[str]:
    """容量检查：返回阻断项（空列表 = 放得下），消息不含硬盘编号。"""
    if not isinstance(disk_size_bytes, int) or isinstance(disk_size_bytes, bool):
        return []
    if disk_size_bytes <= 0:
        return []
    if any(part.size_mb is None for part in parts):
        # 有分区吃剩余空间，不可能超容量。
        return []
    required = sum(int(part.size_mb or 0) for part in parts) * BYTES_PER_MB
    # 留 1 MB 给 GPT 表与分区对齐。
    if required + BYTES_PER_MB <= disk_size_bytes:
        return []
    return [f"{layout_summary(parts)} 超过 {format_size(disk_size_bytes)}"]


def unallocated_bytes(parts: Sequence[PlannedPartition], disk_size_bytes: Any) -> int:
    """按计划建完分区后还剩多少未分配空间（界面用它提示“容量会闲置”）。"""
    if not isinstance(disk_size_bytes, int) or isinstance(disk_size_bytes, bool):
        return 0
    if disk_size_bytes <= 0 or any(part.size_mb is None for part in parts):
        return 0
    used = sum(int(part.size_mb or 0) for part in parts) * BYTES_PER_MB + BYTES_PER_MB
    return max(disk_size_bytes - used, 0)


def diskpart_lines(parts: Sequence[PlannedPartition]) -> list[str]:
    """生成 ``create/format/assign`` 行；调用方负责前面加 select/clean/convert。"""
    lines: list[str] = []
    for part in parts:
        if part.role == "efi":
            lines.append(f"create partition efi size={int(part.size_mb or EFI_DEFAULT_MB)}")
        elif part.size_mb is None:
            lines.append("create partition primary")
        else:
            lines.append(f"create partition primary size={int(part.size_mb)}")
        lines.append(f"format quick fs={part.file_system.lower()} label={part.label}")
        if part.letter:
            lines.append(f"assign letter={part.letter}")
    return lines
