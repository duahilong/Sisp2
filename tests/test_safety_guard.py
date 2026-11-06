"""真实磁盘护栏（``app.safety_guard``）的回归测试。

这些用例对应 2026-11-06 的清盘事故：分区实现搬家后，测试里对
``runner._run_diskpart`` 的"直接赋值 mock"静默失效，于是本机真的执行了
``diskpart`` → ``clean`` → ``format``。护栏必须在**任何**这类路径上先报错。
"""

from __future__ import annotations

import pytest

from app.application.real_runner import WindowsDiskRunner, _CommandLog, build_real_runner
from app.application.shell import DiskpartShell, ProcessShell
from app.modules.boot.service import BootError, BootRepairer
from app.safety_guard import (
    BLOCK_ENV,
    RealDiskBlocked,
    block_reason,
    ensure_command_allowed,
    is_dangerous_command,
    program_name,
    strip_comments,
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


def test_shell_blocks_diskpart_without_authorization(monkeypatch) -> None:
    """事故现场复现：未授权的 ``DiskpartShell`` 执行 diskpart → 被护栏拦下。

    这条用例取代了"给 ``runner._run_diskpart`` 赋值"的写法：即便将来又有人把
    替身搬到不存在的方法上，这里也只会在护栏处失败，不会真的 clean 任何盘。
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(BLOCK_ENV, raising=False)
    shell = DiskpartShell(log=_CommandLog(None), diskpart="diskpart.exe")
    with pytest.raises(RealDiskBlocked):
        shell.run_diskpart(["select disk 999", "clean"])


def test_shell_blocks_diskpart_inside_pytest_even_if_authorized(monkeypatch) -> None:
    monkeypatch.setenv(BLOCK_ENV, "1")
    shell = DiskpartShell(
        log=_CommandLog(None), diskpart="diskpart.exe", allow_real_disk=True
    )
    with pytest.raises(RealDiskBlocked):
        shell.run_diskpart(["select disk 999", "clean"])


def test_process_shell_blocks_bcdboot_and_dism(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(BLOCK_ENV, raising=False)
    process = ProcessShell(log=_CommandLog(None), source="unit-test")
    with pytest.raises(RealDiskBlocked):
        process.run(BCD, 30)
    with pytest.raises(RealDiskBlocked):
        process.run(DISM, 30)
    # 只读查询仍然可以执行（这里用 python 代替 powershell 验证"非危险命令放行"）
    import sys

    result = process.run([sys.executable, "-c", "print('ok')"], 30)
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


# ------------------------------------------------- 注释剥离（引号感知）

def test_strip_comments_keeps_hash_inside_quotes() -> None:
    """引号里的 ``#`` 是路径/字符串内容，不是注释开头。"""
    assert strip_comments(r"Get-Disk # 注释") == r"Get-Disk "
    assert strip_comments(r"$p = 'D:\a#b\x.iso' # 尾部注释") == r"$p = 'D:\a#b\x.iso' "
    assert strip_comments('Write-Output "a # b"') == 'Write-Output "a # b"'
    # 转义引号不能提前结束字符串
    assert strip_comments(r"Write-Output 'it''s # not a comment'") == r"Write-Output 'it''s # not a comment'"


def test_hash_inside_quotes_cannot_hide_a_write_cmdlet() -> None:
    """回归：旧实现按 ``#`` 硬切，会把引号后面的 ``Clear-Disk`` 一起切掉 → 漏判。"""
    script = "$tag = 'a#b'; Clear-Disk -Number 1 -RemoveData"
    assert is_dangerous_command(["powershell.exe", "-Command", script]) is True


# ------------------------------------------------- 看不到内容的调用

@pytest.mark.parametrize("command", [
    ["powershell.exe", "-NoProfile", "-File", r"D:\scripts\wipe.ps1"],
    ["powershell.exe", "-NoProfile", "-f", r"D:\scripts\wipe.ps1"],
    ["pwsh.exe", "-fil", r"D:\scripts\wipe.ps1"],
    ["powershell.exe", "-EncodedCommand", "QwBsAGUAYQByAC0ARABpAHMAawAg" * 4],
])
def test_uninspectable_powershell_calls_are_dangerous(command) -> None:
    assert is_dangerous_command(command) is True


def test_powershell_parameters_that_are_not_scripts_stay_allowed() -> None:
    """``-NoProfile`` / ``-NonInteractive`` / ``-ExecutionPolicy`` 不是“看不到内容”的参数。"""
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "Get-Disk | Select-Object Number",
    ]
    assert is_dangerous_command(command) is False


@pytest.mark.parametrize("script", [
    "Invoke-Expression 'diskpart /s wipe.txt'",
    "iex $payload",
])
def test_dynamic_execution_is_dangerous(script: str) -> None:
    """拼出来的命令看不到内容，一律按危险处理。"""
    assert is_dangerous_command(["powershell.exe", "-Command", script]) is True


# ------------------------------------------------- 脚本里调用外部写盘程序

@pytest.mark.parametrize("script", [
    "Start-Process diskpart -ArgumentList '/s','wipe.txt'",
    "Start-Process -FilePath diskpart",
    r"& 'C:\Windows\System32\diskpart.exe' /s wipe.txt",
    "Get-Disk | ForEach-Object { bcdboot.exe C:\\Windows /s E: /f UEFI }",
    "dism.exe /Apply-Image /ImageFile:a.wim /Index:1 /ApplyDir:C:\\",
    "$x = 1; diskpart /s script.txt",
])
def test_script_program_invocations_are_dangerous(script: str) -> None:
    assert is_dangerous_command(["powershell.exe", "-Command", script]) is True


@pytest.mark.parametrize("script", [
    "Get-Disk | Format-Table Number,Size",
    "Get-Partition | Format-List",
    "Get-ChildItem D:\\diskpart-docs",
    "Test-Path D:\\diskpart",
    "$format = 'exFAT'; Get-Volume | Where-Object FileSystem -eq $format",
])
def test_read_only_scripts_are_not_mistaken_for_program_calls(script: str) -> None:
    assert is_dangerous_command(["powershell.exe", "-Command", script]) is False


# ------------------------------------------------- dism 按参数区分读写

def test_dism_read_verbs_are_allowed() -> None:
    assert is_dangerous_command(["dism.exe", "/English", "/Get-WimInfo", "/WimFile:D:\\a.wim"]) is False
    assert is_dangerous_command(["dism", "/Get-ImageInfo", "/ImageFile:a.wim"]) is False


def test_dism_write_verbs_stay_dangerous() -> None:
    assert is_dangerous_command(["dism.exe", "/Apply-Image", "/ImageFile:a.wim", "/ApplyDir:F:\\"]) is True
    assert is_dangerous_command(["dism.exe", "/Add-Driver", "/Image:C:\\", "/Driver:D:\\d"] ) is True
    assert is_dangerous_command(["dism.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"]) is True


# ------------------------------------------------- 新接入护栏的模块

GHOST_APPLY = [
    "ghost64.exe",
    "-clone,mode=pload,src=D:/a.GHO:1,dst=3:2",
    "-sure",
    "-batch",
    "-ntexact",
]


def test_ghost_apply_is_dangerous() -> None:
    assert is_dangerous_command(GHOST_APPLY) is True
    assert is_dangerous_command(["ghost64.exe", "-ver"]) is False


def test_ghost_apply_blocked_without_authorization() -> None:
    """未经授权的 Ghost 还原必须拦下（不 spawn 进程）。"""
    with pytest.raises(RealDiskBlocked) as excinfo:
        ensure_command_allowed(GHOST_APPLY, allow_real_disk=False, source="test")
    assert "未获得真实写盘授权" in str(excinfo.value)


def test_ghost_apply_blocked_even_when_authorized_in_tests() -> None:
    """授权了也还得看环境：测试进程里仍然拦（第一道闸是实例授权，第二道是环境）。"""
    with pytest.raises(RealDiskBlocked) as excinfo:
        ensure_command_allowed(GHOST_APPLY, allow_real_disk=True, source="test")
    assert BLOCK_ENV in str(excinfo.value)


def test_ghost_version_probe_is_allowed() -> None:
    ensure_command_allowed(["ghost64.exe", "-ver"], allow_real_disk=False, source="test")


def test_run_powershell_allows_read_only_scan(monkeypatch) -> None:
    import subprocess

    from app.modules.common import service as common_service

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")

    monkeypatch.setattr(common_service.subprocess, "run", fake_run)
    done = common_service.run_powershell("Get-Disk | ConvertTo-Json")
    assert done.returncode == 0


def test_run_powershell_blocks_write_script() -> None:
    """写盘 cmdlet 在 ``run_powershell`` 里也必须先被拦下（不 spawn 进程）。"""
    from app.modules.common import service as common_service

    with pytest.raises(RealDiskBlocked):
        common_service.run_powershell("Format-Volume -DriveLetter F -FileSystem NTFS")
