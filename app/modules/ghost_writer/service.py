import os
import subprocess
from pathlib import Path
from typing import Any, Callable


Ghostrunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
GhostVerifier = Callable[[str], tuple[bool, str]]
GHOST_TIMEOUT_SECONDS = 1800


def get_system_drive_letter() -> str:
    return os.environ.get("SystemDrive", "C:")[0].upper()



def build_ghost_command(gho_exe: str, win_gho: str, disk_number: int) -> list[str]:
    if not gho_exe or not isinstance(gho_exe, str):
        raise ValueError("ghost64.exe 路径无效")
    if not win_gho or not isinstance(win_gho, str):
        raise ValueError("镜像文件路径无效")
    if not isinstance(disk_number, int):
        raise ValueError(f"磁盘编号必须为整数: {disk_number}")

    ghost_disk_number = disk_number + 1
    return [
        gho_exe,
        f"-clone,mode=pload,src={win_gho}:1,dst={ghost_disk_number}:2",
        "-sure",
        "-batch",
    ]



def run_ghost(command: list[str], timeout: float = GHOST_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )



def verify_ghost_result(windows_drive_letter: str) -> tuple[bool, str]:
    if not windows_drive_letter:
        return False, "Windows 分区盘符为空，无法验证"

    windows_dir = Path(f"{windows_drive_letter}:\\Windows")
    if not windows_dir.exists():
        return False, f"验证失败: {windows_dir} 不存在"

    return True, f"验证通过: {windows_dir} 存在"



def write_ghost_image(
    gho_exe: str,
    win_gho: str,
    disk_number: int,
    windows_drive_letter: str,
    ghost_runner: Ghostrunner | None = None,
    ghost_verifier: GhostVerifier | None = None,
) -> dict[str, Any]:
    if windows_drive_letter and windows_drive_letter.upper() == get_system_drive_letter():
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"拒绝向系统盘 {windows_drive_letter.upper()}: 写入 Ghost 镜像",
        }

    command = build_ghost_command(gho_exe, win_gho, disk_number)
    runner = ghost_runner or run_ghost

    try:
        completed = runner(command)
    except FileNotFoundError:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"Ghost 可执行文件不存在: {gho_exe}",
        }
    except subprocess.TimeoutExpired:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"Ghost 镜像写入超时（超过 {GHOST_TIMEOUT_SECONDS} 秒）",
        }

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "未知错误"
        stdout = (completed.stdout or "").strip()
        detail = stderr if stderr != "未知错误" else stdout
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"Ghost 镜像写入失败（返回码 {completed.returncode}）: {detail}",
        }

    verifier = ghost_verifier or verify_ghost_result
    verified, verify_message = verifier(windows_drive_letter)
    if not verified:
        return {
            "disk_number": disk_number,
            "passed": False,
            "message": f"Ghost 镜像写入后验证失败: {verify_message}",
        }

    return {
        "disk_number": disk_number,
        "passed": True,
        "message": f"Ghost 镜像写入成功: {verify_message}",
    }



def print_ghost_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "通过" if result.get("passed") else "失败"
        print(f"硬盘 {result.get('disk_number')} 镜像写入{status}: {result.get('message')}")
