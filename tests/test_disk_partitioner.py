import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_partitioner.service import build_partition_disk_script, parse_partitioned_disks, partition_and_format_disks



def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"期望包含: {expected}\n实际内容:\n{text}")



def test_build_partition_disk_script() -> None:
    script = build_partition_disk_script([2], 100, 6)

    assert_contains(script, "Get-Disk -Number 2")
    assert_contains(script, "Wait-DiskReady")
    assert_contains(script, "LargestFreeExtent")
    assert_contains(script, "$newEfiSize = $efiSize")
    assert_contains(script, "New-Partition -DiskNumber 2 -Size $newEfiSize")
    assert_contains(script, "Format-Volume -FileSystem FAT32")
    assert_contains(script, "New-Partition -DiskNumber 2 -Size $cSize -AssignDriveLetter")
    assert_contains(script, "Format-Volume -FileSystem NTFS")
    assert_contains(script, "IsBoot")
    assert_contains(script, "IsSystem")
    assert_contains(script, "IsReadOnly")
    assert_contains(script, "IsOffline")
    assert_contains(script, "PartitionStyle -ne 'GPT'")
    assert_contains(script, "$freeSpace = $disk.LargestFreeExtent")
    assert_contains(script, "$halfSize = [UInt64]([Math]::Floor($freeSpace / 2))")
    assert_contains(script, "New-Partition -DiskNumber 2 -Size $halfSize -AssignDriveLetter")
    assert_contains(script, "NewFileSystemLabel 'Data1'")
    assert_contains(script, "New-Partition -DiskNumber 2 -UseMaximumSize -AssignDriveLetter")
    assert_contains(script, "NewFileSystemLabel 'Data2'")



def test_build_partition_disk_script_rejects_invalid_inputs() -> None:
    cases = [
        ([], 100, 6, "不能为空"),
        ([2], 0, 6, "EFI"),
        ([2], 100, 0, "C 分区"),
        (["2"], 100, 6, "整数"),
    ]

    for disk_numbers, efi_size_mb, c_size_gb, expected_message in cases:
        try:
            build_partition_disk_script(disk_numbers, efi_size_mb, c_size_gb)
        except ValueError as exc:
            if expected_message not in str(exc):
                raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {exc}") from exc
        else:
            raise AssertionError(f"期望参数失败: {disk_numbers}, {efi_size_mb}, {c_size_gb}")



def test_parse_partitioned_disks() -> None:
    single = parse_partitioned_disks(json.dumps({"disk_number": 2, "c_drive_letter": "F"}))
    if single != [{"disk_number": 2, "c_drive_letter": "F"}]:
        raise AssertionError(f"单个分区结果解析不正确: {single}")

    multiple = parse_partitioned_disks(json.dumps([{"disk_number": 2}, {"disk_number": 3}]))
    if multiple != [{"disk_number": 2}, {"disk_number": 3}]:
        raise AssertionError(f"多个分区结果解析不正确: {multiple}")



def test_partition_and_format_disks_success() -> None:
    captured_scripts: list[str] = []

    def fake_runner(script: str) -> CompletedProcess[str]:
        captured_scripts.append(script)
        return CompletedProcess(
            args=["powershell"],
            returncode=0,
            stdout=json.dumps(
                {
                    "disk_number": 2,
                    "efi_partition_number": 1,
                    "efi_file_system": "FAT32",
                    "c_partition_number": 2,
                    "c_drive_letter": "F",
                    "c_file_system": "NTFS",
                    "d1_partition_number": 3,
                    "d1_drive_letter": "G",
                    "d1_file_system": "NTFS",
                    "d2_partition_number": 4,
                    "d2_drive_letter": "H",
                    "d2_file_system": "NTFS",
                }
            ),
            stderr="",
        )

    results = partition_and_format_disks([2], {"efi_size_mb": 100, "c_size_gb": 6}, powershell_runner=fake_runner)
    if not captured_scripts:
        raise AssertionError("partition_and_format_disks 未执行注入的 PowerShell runner")
    if not results[0].get("passed"):
        raise AssertionError(f"分区格式化成功结果不正确: {results}")



def test_partition_and_format_disks_failure() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=["powershell"], returncode=1, stdout="", stderr="boom")

    results = partition_and_format_disks([2], {"efi_size_mb": 100, "c_size_gb": 6}, powershell_runner=fake_runner)
    if results[0].get("passed"):
        raise AssertionError(f"分区失败时不应通过: {results}")
    if "boom" not in results[0].get("message", ""):
        raise AssertionError(f"分区失败信息不正确: {results}")



def test_partition_and_format_disks_parse_failure() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=["powershell"], returncode=0, stdout="not json", stderr="")

    results = partition_and_format_disks([2], {"efi_size_mb": 100, "c_size_gb": 6}, powershell_runner=fake_runner)
    if results[0].get("passed"):
        raise AssertionError(f"解析失败时不应通过: {results}")
    if "结果解析失败" not in results[0].get("message", ""):
        raise AssertionError(f"解析失败信息不正确: {results}")



def test_partition_and_format_disks_abnormal_result() -> None:
    cases = [
        (
            {"disk_number": 2, "efi_file_system": "NTFS", "c_file_system": "NTFS", "c_drive_letter": "F",
             "d1_file_system": "NTFS", "d1_drive_letter": "G", "d2_file_system": "NTFS", "d2_drive_letter": "H"},
            "EFI 分区不是 FAT32",
        ),
        (
            {"disk_number": 2, "efi_file_system": "FAT32", "c_file_system": "FAT32", "c_drive_letter": "F",
             "d1_file_system": "NTFS", "d1_drive_letter": "G", "d2_file_system": "NTFS", "d2_drive_letter": "H"},
            "C 分区不是 NTFS",
        ),
        (
            {"disk_number": 2, "efi_file_system": "FAT32", "c_file_system": "NTFS", "c_drive_letter": "F",
             "d1_file_system": "FAT32", "d1_drive_letter": "G", "d2_file_system": "NTFS", "d2_drive_letter": "H"},
            "Data1 分区不是 NTFS",
        ),
        (
            {"disk_number": 2, "efi_file_system": "FAT32", "c_file_system": "NTFS", "c_drive_letter": "F",
             "d1_file_system": "NTFS", "d1_drive_letter": "G", "d2_file_system": "FAT32", "d2_drive_letter": "H"},
            "Data2 分区不是 NTFS",
        ),
    ]

    for mock_data, description in cases:
        def fake_runner(script: str, _data=mock_data) -> CompletedProcess[str]:
            return CompletedProcess(args=["powershell"], returncode=0, stdout=json.dumps(_data), stderr="")

        results = partition_and_format_disks([2], {"efi_size_mb": 100, "c_size_gb": 6}, powershell_runner=fake_runner)
        if results[0].get("passed"):
            raise AssertionError(f"异常分区结果不应通过: {description}")



def main() -> int:
    try:
        test_build_partition_disk_script()
        test_build_partition_disk_script_rejects_invalid_inputs()
        test_parse_partitioned_disks()
        test_partition_and_format_disks_success()
        test_partition_and_format_disks_failure()
        test_partition_and_format_disks_parse_failure()
        test_partition_and_format_disks_abnormal_result()
        print("模块5分区格式化测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
