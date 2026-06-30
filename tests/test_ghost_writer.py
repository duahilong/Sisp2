import subprocess
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.ghost_writer.service import build_ghost_command, write_ghost_image, GhostVerifier



def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"期望包含: {expected}\n实际内容:\n{text}")



def test_build_ghost_command() -> None:
    command = build_ghost_command("D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", 2)

    if command[0] != "D:\\sw\\ghost64.exe":
        raise AssertionError(f"ghost64.exe 路径不正确: {command}")
    clone_arg = command[1]
    if "-clone,mode=pload" not in clone_arg:
        raise AssertionError(f"命令缺少 -clone,mode=pload: {command}")
    if "src=D:\\img\\111.GHO:1" not in clone_arg:
        raise AssertionError(f"src 参数不正确: {clone_arg}")
    if "dst=3:2" not in clone_arg:
        raise AssertionError(f"dst 参数不正确，磁盘号应为 2+1=3: {clone_arg}")
    if "-sure" not in command:
        raise AssertionError(f"命令缺少 -sure: {command}")
    if "-batch" not in command:
        raise AssertionError(f"命令缺少 -batch: {command}")



def test_build_ghost_command_rejects_invalid_inputs() -> None:
    cases = [
        ("", "D:\\img\\111.GHO", 2, "ghost64.exe 路径无效"),
        ("D:\\sw\\ghost64.exe", "", 2, "镜像文件路径无效"),
        ("D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", "2", "整数"),
    ]

    for gho_exe, win_gho, disk_number, expected_message in cases:
        try:
            build_ghost_command(gho_exe, win_gho, disk_number)
        except (ValueError, TypeError) as exc:
            if expected_message not in str(exc):
                raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {exc}") from exc
        else:
            raise AssertionError(f"期望参数失败: {gho_exe}, {win_gho}, {disk_number}")



def test_write_ghost_image_success() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        mock_windows_dir = Path(tmp_dir) / "Windows"
        mock_windows_dir.mkdir()

        def fake_runner(command: list[str]) -> CompletedProcess[str]:
            return CompletedProcess(args=command, returncode=0, stdout="Ghost completed", stderr="")

        def fake_verifier(drive_letter: str) -> tuple[bool, str]:
            if mock_windows_dir.exists():
                return True, f"验证通过: {mock_windows_dir} 存在"
            return False, f"验证失败: {drive_letter}:\\Windows 不存在"

        result = write_ghost_image(
            "D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", 2, "X", ghost_runner=fake_runner, ghost_verifier=fake_verifier,
        )
        if not result.get("passed"):
            raise AssertionError(f"Ghost 成功时应通过: {result}")
        if "写入成功" not in result.get("message", ""):
            raise AssertionError(f"成功消息不正确: {result}")



def test_write_ghost_image_rejects_system_drive() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        raise AssertionError("系统盘防护失败时不应执行 Ghost")

    result = write_ghost_image("D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", 2, "C", ghost_runner=fake_runner)
    if result.get("passed"):
        raise AssertionError(f"系统盘写入不应通过: {result}")
    if "系统盘" not in result.get("message", ""):
        raise AssertionError(f"系统盘错误消息不正确: {result}")



def test_write_ghost_image_failure() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        return CompletedProcess(args=command, returncode=1, stdout="", stderr="Ghost error")

    def fake_verifier(drive_letter: str) -> tuple[bool, str]:
        raise AssertionError("不应调用 verifier（returncode 非 0 时应提前返回）")

    result = write_ghost_image("D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", 2, "F", ghost_runner=fake_runner, ghost_verifier=fake_verifier)
    if result.get("passed"):
        raise AssertionError(f"Ghost 失败时不应通过: {result}")
    if "返回码 1" not in result.get("message", ""):
        raise AssertionError(f"失败消息不正确: {result}")



def test_write_ghost_image_verification_failure() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        return CompletedProcess(args=command, returncode=0, stdout="Ghost completed", stderr="")

    def fake_verifier(drive_letter: str) -> tuple[bool, str]:
        return False, f"验证失败: {drive_letter}:\\Windows 不存在"

    result = write_ghost_image(
        "D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", 2, "Z", ghost_runner=fake_runner, ghost_verifier=fake_verifier,
    )
    if result.get("passed"):
        raise AssertionError(f"验证失败时不应通过: {result}")
    if "验证失败" not in result.get("message", ""):
        raise AssertionError(f"验证失败消息不正确: {result}")



def test_write_ghost_image_file_not_found() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        raise FileNotFoundError(f"[Errno 2] No such file or directory: '{command[0]}'")

    result = write_ghost_image(
        "D:\\nonexistent\\ghost64.exe", "D:\\img\\111.GHO", 2, "F", ghost_runner=fake_runner,
    )
    if result.get("passed"):
        raise AssertionError(f"文件不存在时不应通过: {result}")
    if "不存在" not in result.get("message", ""):
        raise AssertionError(f"文件不存在错误消息不正确: {result}")
    if result.get("disk_number") != 2:
        raise AssertionError(f"disk_number 不正确: {result}")



def test_write_ghost_image_timeout() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=command, timeout=1800)

    result = write_ghost_image(
        "D:\\sw\\ghost64.exe", "D:\\img\\111.GHO", 2, "F", ghost_runner=fake_runner,
    )
    if result.get("passed"):
        raise AssertionError(f"超时时不应通过: {result}")
    if "超时" not in result.get("message", ""):
        raise AssertionError(f"超时错误消息不正确: {result}")
    if result.get("disk_number") != 2:
        raise AssertionError(f"disk_number 不正确: {result}")



def main() -> int:
    try:
        test_build_ghost_command()
        test_build_ghost_command_rejects_invalid_inputs()
        test_write_ghost_image_success()
        test_write_ghost_image_rejects_system_drive()
        test_write_ghost_image_failure()
        test_write_ghost_image_verification_failure()
        test_write_ghost_image_file_not_found()
        test_write_ghost_image_timeout()
        print("模块6镜像写入测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
