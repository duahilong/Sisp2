"""盘符分配。

采用三分区布局（EFI + C + D），每块盘需要 3 个盘符，
角色键为 ``efi/windows/data``。当前只有盘符分配逻辑会被
``SispService.build_selection_plan()`` 调用；worker 进程时代的参数解析与
盘符复核函数已随重写删除。
"""

from __future__ import annotations

AVAILABLE_DRIVE_LETTERS = [
    "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
]
# 三分区（EFI / Windows / Data）
LETTERS_PER_DISK = 3
DRIVE_LETTER_ROLES = ("efi", "windows", "data")


def build_drive_letter_allocations(
    disk_numbers: list[int],
    forbidden_letters: list[str] | set[str] | None = None,
) -> dict[int, dict[str, str]]:
    forbidden = {str(letter).upper() for letter in (forbidden_letters or [])}
    pool = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter not in forbidden]
    allocations: dict[int, dict[str, str]] = {}
    index = 0
    for disk_number in disk_numbers:
        if index + LETTERS_PER_DISK > len(pool):
            occupied = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter in forbidden]
            raise RuntimeError(
                f"可用盘符不足，无法为硬盘 {disk_number} 分配 {LETTERS_PER_DISK} 个盘符，"
                f"被占用: {occupied}"
            )
        letters = pool[index:index + LETTERS_PER_DISK]
        allocations[disk_number] = dict(zip(DRIVE_LETTER_ROLES, letters, strict=False))
        index += LETTERS_PER_DISK
    return allocations
