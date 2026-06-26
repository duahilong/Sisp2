import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.boot_creator.service import build_bcdboot_command, create_boot_record, verify_boot_result



def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"期望包含: {expected}\n实际内容:\n{text}")



def test_build_bcdboot_command() -> None:
    command = build_bcdboot_command("D:\\sw\\bcdboot.exe", "F", "E")

    if command[0] != "D:\\sw\\bcdboot.exe":
        raise AssertionError(f"bcdboot.exe 路径不正确: {command}")
    if command[1] != "F:\\Windows":
        raise AssertionError(f"Windows 路径不正确: {command}")
    if "/s" not in command:
        raise AssertionError(f"命令缺少 /s: {command}")
    if "E:" not in command:
        raise AssertionError(f"EFI 盘符不正确: {command}")
    if "/f" not in command:
        raise AssertionError(f"命令缺少 /f: {command}")
    if "UEFI" not in command:
        raise AssertionError(f"固件类型不正确: {command}")



def test_build_bcdboot_command_rejects_invalid_inputs() -> None:
    cases = [
        ("", "F", "E", "bcdboot.exe 路径无效"),
        ("D:\\sw\\bcdboot.exe", "", "E", "Windows 分区盘符无效"),
        ("D:\\sw\\bcdboot.exe", "F", "", "EFI 分区盘符无效"),
    ]

    for bcd_exe, windows_letter, efi_letter, expected_message in cases:
        try:
            build_bcdboot_command(bcd_exe, windows_letter, efi_letter)
        except (ValueError, TypeError) as exc:
            if expected_message not in str(exc):
                raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {exc}") from exc
        else:
            raise AssertionError(f"期望参数失败: {bcd_exe}, {windows_letter}, {efi_letter}")



def test_create_boot_record_success() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        efi_dir = Path(tmp_dir) / "EFI" / "Microsoft" / "Boot"
        efi_dir.mkdir(parents=True)
        boot_file = efi_dir / "bootmgfw.efi"
        boot_file.write_text("boot", encoding="utf-8")
        drive_letter = str(Path(tmp_dir))[0]

        def fake_runner(command: list[str]) -> CompletedProcess[str]:
            return CompletedProcess(args=command, returncode=0, stdout="BCDBoot completed", stderr="")

        def fake_verifier(efi_letter: str) -> tuple[bool, str]:
            return True, "引导文件存在"

        result = create_boot_record(
            "D:\\sw\\bcdboot.exe", "F", drive_letter, 2, bcdboot_runner=fake_runner, boot_verifier=fake_verifier,
        )
        if not result.get("passed"):
            raise AssertionError(f"bcdboot 成功时应通过: {result}")
        if "创建成功" not in result.get("message", ""):
            raise AssertionError(f"成功消息不正确: {result}")



def test_create_boot_record_failure() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        return CompletedProcess(args=command, returncode=1, stdout="", stderr="BCDBoot error")

    result = create_boot_record("D:\\sw\\bcdboot.exe", "F", "E", 2, bcdboot_runner=fake_runner)
    if result.get("passed"):
        raise AssertionError(f"bcdboot 失败时不应通过: {result}")
    if "返回码 1" not in result.get("message", ""):
        raise AssertionError(f"失败消息不正确: {result}")



def test_create_boot_record_verification_failure() -> None:
    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        return CompletedProcess(args=command, returncode=0, stdout="BCDBoot completed", stderr="")

    result = create_boot_record("D:\\sw\\bcdboot.exe", "F", "Z", 2, bcdboot_runner=fake_runner)
    if result.get("passed"):
        raise AssertionError(f"验证失败时不应通过: {result}")
    if "验证失败" not in result.get("message", ""):
        raise AssertionError(f"验证失败消息不正确: {result}")



def test_create_boot_record_empty_bcd_exe() -> None:
    result = create_boot_record("", "F", "E", 2)
    if result.get("passed"):
        raise AssertionError(f"bcd_exe 为空时不应通过: {result}")
    if "路径为空" not in result.get("message", ""):
        raise AssertionError(f"错误消息不正确: {result}")



def test_create_boot_record_empty_windows_letter() -> None:
    result = create_boot_record("D:\\sw\\bcdboot.exe", "", "E", 2)
    if result.get("passed"):
        raise AssertionError(f"Windows 盘符为空时不应通过: {result}")
    if "Windows 分区盘符为空" not in result.get("message", ""):
        raise AssertionError(f"错误消息不正确: {result}")



def test_create_boot_record_empty_efi_letter() -> None:
    result = create_boot_record("D:\\sw\\bcdboot.exe", "F", "", 2)
    if result.get("passed"):
        raise AssertionError(f"EFI 盘符为空时不应通过: {result}")
    if "EFI 分区盘符为空" not in result.get("message", ""):
        raise AssertionError(f"错误消息不正确: {result}")



def test_verify_boot_result_success() -> None:
    passed, message = verify_boot_result("C")
    if not passed:
        if "引导文件不存在" in message:
            print(f"跳过验证成功测试: C 盘引导文件不存在，需要在有 Windows 系统的机器上运行")
            return
        raise AssertionError(f"验证应通过: {message}")



def test_verify_boot_result_file_not_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        drive_letter = str(Path(tmp_dir))[0]

        passed, message = verify_boot_result(drive_letter)

        if passed:
            raise AssertionError(f"引导文件不存在时验证不应通过: {message}")
        if "引导文件不存在" not in message:
            raise AssertionError(f"错误消息不正确: {message}")



def test_verify_boot_result_empty_letter() -> None:
    passed, message = verify_boot_result("")

    if passed:
        raise AssertionError(f"盘符为空时验证不应通过: {message}")
    if "盘符为空" not in message:
        raise AssertionError(f"错误消息不正确: {message}")



def main() -> int:
    try:
        test_build_bcdboot_command()
        test_build_bcdboot_command_rejects_invalid_inputs()
        test_create_boot_record_success()
        test_create_boot_record_failure()
        test_create_boot_record_verification_failure()
        test_create_boot_record_empty_bcd_exe()
        test_create_boot_record_empty_windows_letter()
        test_create_boot_record_empty_efi_letter()
        test_verify_boot_result_success()
        test_verify_boot_result_file_not_exists()
        test_verify_boot_result_empty_letter()
        print("模块8引导记录创建测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
