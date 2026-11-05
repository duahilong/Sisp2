"""真实磁盘护栏（``app.safety_guard``）的回归测试。

这些用例对应 2026-11-06 的清盘事故：分区实现搬家后，测试里对
``runner._run_diskpart`` 的"直接赋值 mock"静默失效，于是本机真的执行了
``diskpart`` → ``clean`` → ``format``。护栏必须在**任何**这类路径上先报错。
"""

from __future__ import annotations

import pytest

from app.application.real_runner import WindowsDiskRunner, _CommandLog, build_real_runner
from app.modules.boot.service import BootError, BootRepairer
from app.safety_guard import (
    BLOCK_ENV,
    RealDiskBlocked,
    block_reason,
    ensure_command_allowed,
    is_dangerous_command,
    program_name,
)

DISKPART = ["diskpart.exe", "/s", "script.txt"]
BCD = ["bcdboot.exe", r"F:\Windows", "/s", "E:", "/f", "UEFI"]
DISM = ["dism.exe", "/English", "/Apply-Image", "/ImageFile:x.wim", "/Index:1", "/ApplyDir:F:\\"]
FORMAT = ["format.com", "F:", "/Q", "/FS:NTFS", "/Y"]
READ_POWERSHELL = ["powershell.exe", "-NoProfile", "-Command", "Get-Partition -DiskNumber 1"]


def test_program_name_strips_path_and_case() -> None:
    assert program_name([r"C:\Windows\System32\DiskPart.EXE", "/s", "x"]) == "diskpart.exe"
    assert program_name(["diskpart"]) == "diskpart"
    assert program_name([]) == ""


@pytest.mark.parametrize("command", [DISKPART, BCD, DISM, FORMAT])
def test_write_commands_are_dangerous(command) -> None:
    assert is_dangerous_command(command) is True


def test_read_only_queries_are_allowed() -> None:
    assert is_dangerous_command(READ_POWERSHELL) is False
    assert is_dangerous_command(["powershell.exe", "-Command", "Get-Disk | ConvertTo-Json"]) is False


@pytest.mark.parametrize(
    "script",
    ["Format-Volume -DriveLetter F", "Clear-Disk -Number 1 -RemoveData", "New-Partition -DiskNumber 1"],
)
def test_powershell_write_cmdlets_are_dangerous(script: str) -> None:
    assert is_dangerous_command(["powershell.exe", "-Command", script]) is True


def test_block_reason_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv(BLOCK_ENV, "1")
    assert "SISP_BLOCK_REAL_DISK" in (block_reason() or "")

    monkeypatch.setenv(BLOCK_ENV, "0")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test_y")
    assert "pytest" in (block_reason() or "")


def test_unauthorized_caller_is_blocked(monkeypatch) -> None:
    """默认 ``allow_real_disk=False``：即使环境干净也不许写盘。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(BLOCK_ENV, raising=False)
    with pytest.raises(RealDiskBlocked) as excinfo:
        ensure_command_allowed(DISKPART, allow_real_disk=False, source="unit-test")
    assert "allow_real_disk=False" in str(excinfo.value)
    assert "diskpart.exe" in str(excinfo.value)


def test_authorized_caller_still_blocked_in_pytest(monkeypatch) -> None:
    """显式授权 + 测试环境：测试环境优先，仍然拦。"""
    monkeypatch.setenv(BLOCK_ENV, "1")
    with pytest.raises(RealDiskBlocked) as excinfo:
        ensure_command_allowed(DISKPART, allow_real_disk=True, source="unit-test")
    assert "SISP_BLOCK_REAL_DISK" in str(excinfo.value)


def test_authorized_caller_passes_in_production(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(BLOCK_ENV, raising=False)
    ensure_command_allowed(DISKPART, allow_real_disk=True, source="unit-test")  # 不抛异常
    ensure_command_allowed(READ_POWERSHELL, allow_real_disk=False)  # 只读永远放行


def test_runner_defaults_to_read_only() -> None:
    assert WindowsDiskRunner(diskpart="diskpart.exe").allow_real_disk is False
    assert build_real_runner().allow_real_disk is True


def test_runner_blocks_diskpart_without_authorization(monkeypatch) -> None:
    """事故现场复现：直接构造 runner（未授权）执行 diskpart → 被护栏拦下。

    这条用例取代了"mock ``runner._run_diskpart``"的写法：即便将来又有人把 mock
    搬到不存在的方法上，这里也只会在护栏处失败，不会真的 clean 任何盘。
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(BLOCK_ENV, raising=False)
    runner = WindowsDiskRunner(diskpart="diskpart.exe")
    with pytest.raises(RealDiskBlocked):
        runner._run_diskpart(["select disk 999", "clean"], _CommandLog(None))


def test_runner_blocks_diskpart_inside_pytest_even_if_authorized(monkeypatch) -> None:
    monkeypatch.setenv(BLOCK_ENV, "1")
    runner = WindowsDiskRunner(diskpart="diskpart.exe", allow_real_disk=True)
    with pytest.raises(RealDiskBlocked):
        runner._run_diskpart(["select disk 999", "clean"], _CommandLog(None))


def test_runner_blocks_bcdboot_and_dism_through_spawn(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(BLOCK_ENV, raising=False)
    runner = WindowsDiskRunner(diskpart="diskpart.exe", dism="dism.exe", bcdboot="bcdboot.exe")
    log = _CommandLog(None)
    with pytest.raises(RealDiskBlocked):
        runner._spawn(BCD, 30, log)
    with pytest.raises(RealDiskBlocked):
        runner._spawn(DISM, 30, log)
    # 只读查询仍然可以执行（这里用 python 代替 powershell 验证"非危险命令放行"）
    import sys

    result = runner._spawn([sys.executable, "-c", "print('ok')"], 30, log)
    assert result[0] == 0


def test_boot_repairer_defaults_to_read_only_and_blocks() -> None:
    repairer = BootRepairer(scanner=lambda: {"disks": []}, bcdboot="bcdboot.exe", diskpart="diskpart.exe")
    assert repairer.allow_real_disk is False
    with pytest.raises(RealDiskBlocked):
        repairer._run([r"bcdboot.exe", r"F:\Windows", "/s", "E:", "/f", "UEFI"])


def test_boot_repairer_block_error_type_is_boot_error_compatible() -> None:
    """``RealDiskBlocked`` 是 ``RuntimeError``，不会被误当成 ``BootError``（``ValueError``）。"""
    assert not issubclass(RealDiskBlocked, BootError)
    assert issubclass(RealDiskBlocked, RuntimeError)
