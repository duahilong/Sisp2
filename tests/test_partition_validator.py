import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.partition_validator.service import validate_partitioned_disks


PARTITION_INFO = {"efi_size_mb": 100, "c_size_gb": 6}



def build_partitioned_disk(overrides: dict | None = None, partitions: list[dict] | None = None) -> dict:
    disk = {
        "disk_number": 2,
        "friendly_name": "Realtek USB 3.2 Device",
        "partition_style": "GPT",
        "is_boot": False,
        "is_system": False,
        "partitions": partitions
        if partitions is not None
        else [
            {"partition_number": 1, "type": "Reserved", "size_bytes": 16 * 1024 * 1024, "drive_letter": None, "volume": None},
            {
                "partition_number": 2,
                "type": "System",
                "gpt_type": "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}",
                "size_bytes": 100 * 1024 * 1024,
                "drive_letter": None,
                "volume": None,
            },
            {
                "partition_number": 3,
                "type": "Basic",
                "size_bytes": 6 * 1024 * 1024 * 1024,
                "drive_letter": "F",
                "volume": {"file_system": "NTFS", "file_system_label": "Windows"},
            },
            {
                "partition_number": 4,
                "type": "Basic",
                "size_bytes": 3 * 1024 * 1024 * 1024,
                "drive_letter": "G",
                "volume": {"file_system": "NTFS", "file_system_label": "Data1"},
            },
            {
                "partition_number": 5,
                "type": "Basic",
                "size_bytes": 3 * 1024 * 1024 * 1024,
                "drive_letter": "H",
                "volume": {"file_system": "NTFS", "file_system_label": "Data2"},
            },
        ],
    }
    if overrides:
        disk.update(overrides)
    return disk



def assert_failed(result: dict, expected_message: str) -> None:
    if result.get("passed"):
        raise AssertionError(f"期望验证失败，实际通过: {result}")
    if expected_message not in result.get("message", ""):
        raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {result}")



def test_validate_partitioned_disks_success() -> None:
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk()])
    if results != [{"disk_number": 2, "passed": True, "message": "分区和格式化结果验证通过"}]:
        raise AssertionError(f"分区验证成功结果不正确: {results}")



def test_validate_partitioned_disks_missing_disk() -> None:
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [])
    assert_failed(results[0], "未找到目标硬盘")



def test_validate_partitioned_disks_rejects_non_gpt() -> None:
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk({"partition_style": "MBR"})])
    assert_failed(results[0], "分区表格式不是 GPT")



def test_validate_partitioned_disks_rejects_boot_or_system_disk() -> None:
    boot_results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk({"is_boot": True})])
    assert_failed(boot_results[0], "启动盘")

    system_results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk({"is_system": True})])
    assert_failed(system_results[0], "系统盘")



def test_validate_partitioned_disks_missing_efi_partition() -> None:
    partitions = [partition for partition in build_partitioned_disk()["partitions"] if partition.get("type") != "System"]
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk(partitions=partitions)])
    assert_failed(results[0], "未检测到 EFI 分区")



def test_validate_partitioned_disks_wrong_efi_size() -> None:
    disk = build_partitioned_disk()
    disk["partitions"][1]["size_bytes"] = 200 * 1024 * 1024
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [disk])
    assert_failed(results[0], "EFI 分区大小")



def test_validate_partitioned_disks_missing_windows_partition() -> None:
    partitions = [partition for partition in build_partitioned_disk()["partitions"] if partition.get("type") != "Basic"]
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk(partitions=partitions)])
    assert_failed(results[0], "未检测到可用的 Windows 分区")



def test_validate_partitioned_disks_wrong_windows_size() -> None:
    disk = build_partitioned_disk()
    disk["partitions"][2]["size_bytes"] = 8 * 1024 * 1024 * 1024
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [disk])
    assert_failed(results[0], "Windows 分区大小")



def test_validate_partitioned_disks_wrong_windows_label() -> None:
    disk = build_partitioned_disk()
    disk["partitions"][2]["volume"]["file_system_label"] = "Wrong"
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [disk])
    assert_failed(results[0], "卷标")



def test_validate_partitioned_disks_missing_data_partition() -> None:
    partitions = [partition for partition in build_partitioned_disk()["partitions"] if (partition.get("volume") or {}).get("file_system_label") != "Data1"]
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk(partitions=partitions)])
    assert_failed(results[0], "未检测到 Data1 分区")



def test_validate_partitioned_disks_wrong_data_file_system() -> None:
    disk = build_partitioned_disk()
    disk["partitions"][4]["volume"]["file_system"] = "FAT32"
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [disk])
    assert_failed(results[0], "Data2 分区文件系统不是 NTFS")



def test_validate_partitioned_disks_missing_data_drive_letter() -> None:
    disk = build_partitioned_disk()
    disk["partitions"][3]["drive_letter"] = None
    results = validate_partitioned_disks([2], PARTITION_INFO, disk_scanner=lambda: [disk])
    assert_failed(results[0], "Data1 分区未分配盘符")



def test_validate_partitioned_disks_rejects_empty_numbers() -> None:
    try:
        validate_partitioned_disks([], PARTITION_INFO, disk_scanner=lambda: [build_partitioned_disk()])
    except ValueError as exc:
        if "不能为空" not in str(exc):
            raise AssertionError(f"错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("空硬盘编号列表应失败")



def main() -> int:
    try:
        test_validate_partitioned_disks_success()
        test_validate_partitioned_disks_missing_disk()
        test_validate_partitioned_disks_rejects_non_gpt()
        test_validate_partitioned_disks_rejects_boot_or_system_disk()
        test_validate_partitioned_disks_missing_efi_partition()
        test_validate_partitioned_disks_wrong_efi_size()
        test_validate_partitioned_disks_missing_windows_partition()
        test_validate_partitioned_disks_wrong_windows_size()
        test_validate_partitioned_disks_wrong_windows_label()
        test_validate_partitioned_disks_missing_data_partition()
        test_validate_partitioned_disks_wrong_data_file_system()
        test_validate_partitioned_disks_missing_data_drive_letter()
        test_validate_partitioned_disks_rejects_empty_numbers()
        print("分区和格式化结果验证测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
