"""盘符分配。

基础布局是三分区（EFI + C + D），每块盘需要 3 个盘符，角色键为
``efi/windows/data``。向导里给某块盘加了额外数据分区（E）时，``extra_counts``
告诉本模块这块盘要多一个盘符，额外的角色依次是 ``data2`` / ``data3``……，
键名与 ``app.application.partitions`` 的 role 保持一致。

“``data`` 保持字符串、额外分区另开键”是刻意的：旧调用方读 ``letters['data']``
不受影响，属于纯增量。

当前只有盘符分配逻辑会被 ``SispService.build_selection_plan()`` 调用；worker
进程时代的参数解析与盘符复核函数已随重写删除。
"""

from __future__ import annotations

from collections.abc import Mapping

# E–Z 优先，A/B 放最后兜底：现代 Windows（Vista 之后）没有软驱时允许把 A:/B:
# 分给固定磁盘，dism /ApplyDir=A:\、bcdboot /s A: 都实测可用；但少数老程序看到
# A:/B: 会当成软驱，所以只在字母紧张时才用它。
# 合计 24 个：正好够“6 块盘 × 4 分区（EFI+C+D+E）”，或“8 块盘 × 3 分区”。
AVAILABLE_DRIVE_LETTERS = [
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "Q",
    "R",
    "S",
    "T",
    "U",
    "V",
    "W",
    "X",
    "Y",
    "Z",
    "A",
    "B",
]
# 三分区（EFI / Windows / Data）
LETTERS_PER_DISK = 3
DRIVE_LETTER_ROLES = ("efi", "windows", "data")
# 最多再加一个数据分区（E），与 partitions.MAX_DATA_PARTITIONS 对应。
MAX_EXTRA_PARTITIONS = 1


def letter_roles(extra_partitions: int = 0) -> list[str]:
    """角色顺序：``efi``、``windows``、``data``，之后是 ``data2``、``data3``……"""
    count = max(0, min(int(extra_partitions), MAX_EXTRA_PARTITIONS))
    return list(DRIVE_LETTER_ROLES) + [f"data{index}" for index in range(2, 2 + count)]


def build_drive_letter_allocations(
    disk_numbers: list[int],
    forbidden_letters: list[str] | set[str] | None = None,
    extra_counts: Mapping[int, int] | None = None,
) -> dict[int, dict[str, str]]:
    """为每块盘分配 ``3 + 额外数据分区数`` 个盘符。

    ``extra_counts`` 是 ``{硬盘编号: 额外数据分区数}``；缺省的盘按 0 算，
    所以不传时行为与过去完全一致（每盘 3 个）。
    """
    forbidden = {str(letter).upper() for letter in (forbidden_letters or [])}
    pool = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter not in forbidden]
    extras = {
        int(number): max(0, min(int(count), MAX_EXTRA_PARTITIONS))
        for number, count in (extra_counts or {}).items()
    }
    allocations: dict[int, dict[str, str]] = {}
    index = 0
    for disk_number in disk_numbers:
        extra = extras.get(int(disk_number), 0)
        need = LETTERS_PER_DISK + extra
        if index + need > len(pool):
            occupied = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter in forbidden]
            raise RuntimeError(
                f"可用盘符不足，无法为硬盘 {disk_number} 分配 {need} 个盘符"
                f"（剩余 {len(pool) - index} 个可用），被占用: {occupied}"
            )
        letters = pool[index:index + need]
        allocations[int(disk_number)] = dict(zip(letter_roles(extra), letters, strict=False))
        index += need
    return allocations
