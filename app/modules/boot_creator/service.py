import subprocess
from pathlib import Path
from typing import Any, Callable

from app.modules.common.service import get_system_drive_letter


BcdbootRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
BootVerifier = Callable[[str], tuple[bool, str]]
BCDBOOT_TIMEOUT_SECONDS = 300



def build_bcdboot_command(bcd_exe: str, windows_drive_letter: str, efi_drive_letter: str) -> list[str]:
    if not bcd_exe or not isinstance(bcd_exe, str):
        raise ValueError("bcdboot.exe 路径无效")
    if not windows_drive_letter or not isinstance(windows_drive_letter, str):
        raise ValueError("Windows 分区盘符无效")
    if not efi_drive_letter or not isinstance(efi_drive_letter, str):
        raise ValueError("EFI 分区盘符无效")

    return [
        bcd_exe,
        f"{windows_drive_letter}:\\Windows",
        "/s", f"{efi_drive_letter}:",
        "/f", "UEFI",
    ]



def run_bcdboot(command: list[str], timeout: float = BCDBOOT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )



def verify_boot_result(efi_drive_letter: str) -> tuple[bool, str]:
    if not efi_drive_letter:
        return False, "EFI 分区盘符为空，无法验证"

    boot_file = Path(f"{efi_drive_letter}:\\EFI\\Microsoft\\Boot\\bootmgfw.efi")
    if not boot_file.exists():
        return False, f"引导文件不存在: {boot_file}"

    return True, f"引导文件存在: {boot_file}"



def create_boot_record(
    bcd_exe: str,
    windows_drive_letter: str,
    efi_drive_letter: str,
    disk_number: int,
    bcdboot_runner: BcdbootRunner | None = None,
    boot_verifier: BootVerifier | None = None,
) -> dict[str, Any]:
    if not bcd_exe:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "bcdboot.exe 路径为空",
        }

    if not windows_drive_letter:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "Windows 分区盘符为空",
        }

    if not efi_drive_letter:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": "EFI 分区盘符为空",
        }

    system_drive = get_system_drive_letter()
    if windows_drive_letter.upper() == system_drive or efi_drive_letter.upper() == system_drive:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"拒绝在系统盘 {system_drive}: 创建引导记录",
        }

    command = build_bcdboot_command(bcd_exe, windows_drive_letter, efi_drive_letter)
    runner = bcdboot_runner or run_bcdboot

    try:
        completed = runner(command)
    except FileNotFoundError:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"bcdboot 可执行文件不存在: {bcd_exe}",
        }
    except subprocess.TimeoutExpired:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"bcdboot 执行超时（超过 {BCDBOOT_TIMEOUT_SECONDS} 秒）",
        }

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "未知错误"
        stdout = (completed.stdout or "").strip()
        detail = stderr if stderr != "未知错误" else stdout
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"bcdboot 执行失败（返回码 {completed.returncode}）: {detail}",
        }

    verifier = boot_verifier or verify_boot_result
    verified, verify_message = verifier(efi_drive_letter)
    if not verified:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"引导记录创建后验证失败: {verify_message}",
        }

    return {
        "disk_number": disk_number,
        "passed": True,
        "message": f"引导记录创建成功: {verify_message}",
    }



def print_boot_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "通过" if result.get("passed") else "失败"
        print(f"硬盘 {result.get('disk_number')} 引导创建{status}: {result.get('message')}")
