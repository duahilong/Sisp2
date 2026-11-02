"""盘符分配与 worker 盘符参数解析。

采用三分区布局（EFI + C + D），每块盘需要 3 个盘符，
角色键为 ``efi/windows/data``。
"""

from __future__ import annotations

from typing import Any

from app.modules.common.service import get_system_drive_letter

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
        allocations[disk_number] = dict(zip(DRIVE_LETTER_ROLES, letters))
        index += LETTERS_PER_DISK
    return allocations


def parse_worker_drive_letters(
    text: str,
    used_letters: list[str] | set[str] | None = None,
) -> dict[str, str]:
    letters = [part.strip().upper() for part in text.split(",")]
    if len(letters) != LETTERS_PER_DISK:
        raise ValueError(f"盘符参数必须为 3 个字母（EFI,Windows,Data），实际为: {text}")
    seen: set[str] = set()
    used = {str(letter).upper() for letter in (used_letters or [])}
    forbidden = {"A", "B", "C", get_system_drive_letter()} | used
    for letter in letters:
        if len(letter) != 1 or not letter.isalpha():
            raise ValueError(f"盘符必须为单个字母: {letter}")
        if letter in seen:
            raise ValueError(f"盘符不能重复: {letter}")
        if letter in used:
            raise ValueError(f"盘符 {letter} 已被占用，禁止用于 worker 分区")
        if letter in forbidden:
            raise ValueError(f"盘符 {letter} 为保留盘符或系统盘符，禁止用于 worker 分区")
        seen.add(letter)
    return dict(zip(DRIVE_LETTER_ROLES, letters))


def validate_drive_letters_are_free(
    drive_letters: dict[str, str] | None,
    used_letters: list[str] | set[str] | None = None,
) -> None:
    """确认本次分配的盘符都未被任何卷/分区占用。

    主流程只从空闲盘符池分配；worker 启动后仍会再次校验，避免分配后
    被其他进程或并发 worker 抢占。
    """
    if not drive_letters:
        return

    required_keys = DRIVE_LETTER_ROLES
    letters: list[str] = []
    for key in required_keys:
        letter = str(drive_letters.get(key) or "").strip().upper()
        if len(letter) != 1 or not letter.isalpha():
            raise ValueError(f"{key} 盘符必须为单个字母: {drive_letters.get(key)}")
        letters.append(letter)

    duplicates = sorted({letter for letter in letters if letters.count(letter) > 1})
    if duplicates:
        raise ValueError(f"盘符不能重复: {', '.join(duplicates)}")

    forbidden = {"A", "B", "C", get_system_drive_letter()}
    blocked = [letter for letter in letters if letter in forbidden]
    if blocked:
        raise ValueError(f"盘符为保留盘符或系统盘符，禁止使用: {', '.join(blocked)}")

    used = {str(letter).upper() for letter in (used_letters or [])}
    conflicts = [letter for letter in letters if letter in used]
    if conflicts:
        raise ValueError(f"盘符已被占用，拒绝继续: {', '.join(conflicts)}")


def build_forbidden_drive_letters(
    disk_summaries: list[dict[str, Any]],
    selected_disk_numbers: list[int],
    used_letters: list[str],
) -> list[str]:
    """返回本次运行中禁止复用的全部盘符。

    选中盘自身当前使用的盘符也必须禁止复用：多盘 worker 并发启动时，
    复用另一块目标盘正在使用的盘符会导致 worker 冲突，甚至解挂错误
    的卷。清盘后旧盘符自然释放，但本次运行继续使用新的空闲盘符。
    """
    # 保留参数以兼容现有调用方；盘符分配不再区分“选中盘自己的盘符”。
    _ = (disk_summaries, selected_disk_numbers)
    forbidden: list[str] = []
    seen: set[str] = set()
    for letter in used_letters:
        normalized = str(letter).upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            forbidden.append(normalized)
    return forbidden
