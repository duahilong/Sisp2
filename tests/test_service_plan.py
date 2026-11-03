"""盘符规划回归测试：必须避开 OS 层已占用/遗留的“幽灵盘符”。

背景：EFI 分区被 ``clean`` 后，盘符可能残留在 ``DriveInfo`` / Win32_LogicalDisk
里（``IsReady=False``），而 ``Get-Partition`` / ``Get-Volume`` 看不到它。
如果规划阶段仍选中该盘符，diskpart ``assign letter`` 会直接失败。
"""

from __future__ import annotations

from app.application.service import SispService


def _summary(number: int = 1, letters: tuple[str, ...] = ()) -> dict:
    return {
        "disk_number": number,
        "model": "test disk",
        "size_bytes": 100 * 1024**3,
        "drive_letters": list(letters),
    }


def test_plan_avoids_os_level_drive_letters(monkeypatch) -> None:
    import app.modules.disk_info.service as disk_info

    monkeypatch.setattr(disk_info, "scan_used_drive_letters", lambda: ["E", "F"])

    service = SispService(disk_scanner=lambda: [_summary()])
    plan = service.build_selection_plan({}, [_summary()], [1])

    assert plan.drive_letter_allocations[1] == {"efi": "G", "windows": "H", "data": "I"}


def test_plan_keeps_summary_letters_forbidden(monkeypatch) -> None:
    import app.modules.disk_info.service as disk_info

    monkeypatch.setattr(disk_info, "scan_used_drive_letters", list)

    service = SispService(disk_scanner=lambda: [_summary(letters=("E",))])
    plan = service.build_selection_plan({}, [_summary(letters=("E",))], [1])

    assert plan.drive_letter_allocations[1] == {"efi": "F", "windows": "G", "data": "H"}


def test_plan_falls_back_when_used_letter_scan_fails(monkeypatch) -> None:
    import app.modules.disk_info.service as disk_info

    def boom() -> list[str]:
        raise RuntimeError("powershell 不可用")

    monkeypatch.setattr(disk_info, "scan_used_drive_letters", boom)

    service = SispService(disk_scanner=lambda: [_summary()])
    plan = service.build_selection_plan({}, [_summary()], [1])

    assert plan.drive_letter_allocations[1] == {"efi": "E", "windows": "F", "data": "G"}
