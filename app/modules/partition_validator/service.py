from typing import Any, Callable

from app.modules.disk_info.service import scan_disks


DiskScanner = Callable[[], list[dict[str, Any]]]
SIZE_TOLERANCE_BYTES = 64 * 1024 * 1024



def is_size_close(actual_size: Any, expected_size: int, tolerance: int = SIZE_TOLERANCE_BYTES) -> bool:
    if not isinstance(actual_size, (int, float)) or isinstance(actual_size, bool):
        return False
    return abs(int(actual_size) - expected_size) <= tolerance



def find_disk(disks: list[dict[str, Any]], disk_number: int) -> dict[str, Any] | None:
    for disk in disks:
        if disk.get("disk_number") == disk_number:
            return disk
    return None



def find_efi_partition(partitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for partition in partitions:
        if partition.get("type") == "System" or str(partition.get("gpt_type") or "").lower() == "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}":
            return partition
    return None



def find_windows_partition(partitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for partition in partitions:
        volume = partition.get("volume") or {}
        if partition.get("type") == "Basic" and volume.get("file_system") == "NTFS" and partition.get("drive_letter"):
            return partition
    return None



def find_data_partition(partitions: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    for partition in partitions:
        volume = partition.get("volume") or {}
        if partition.get("type") == "Basic" and volume.get("file_system_label") == label:
            return partition
    return None



def validate_data_partition(partitions: list[dict[str, Any]], disk_number: Any, label: str) -> dict[str, Any] | None:
    partition = find_data_partition(partitions, label)
    if not partition:
        return {"disk_number": disk_number, "passed": False, "message": f"分区验证失败: 未检测到 {label} 分区"}

    volume = partition.get("volume") or {}
    if volume.get("file_system") != "NTFS":
        return {"disk_number": disk_number, "passed": False, "message": f"分区验证失败: {label} 分区文件系统不是 NTFS"}
    if not partition.get("drive_letter"):
        return {"disk_number": disk_number, "passed": False, "message": f"分区验证失败: {label} 分区未分配盘符"}

    return None



def validate_partitioned_disk(disk: dict[str, Any], partition_info: dict[str, Any]) -> dict[str, Any]:
    disk_number = disk.get("disk_number")

    if disk.get("partition_style") != "GPT":
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: 硬盘分区表格式不是 GPT"}
    if disk.get("is_boot"):
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: 硬盘被标记为启动盘"}
    if disk.get("is_system"):
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: 硬盘被标记为系统盘"}

    partitions = disk.get("partitions") or []
    if not isinstance(partitions, list) or not partitions:
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: 未检测到分区"}

    efi_partition = find_efi_partition(partitions)
    if not efi_partition:
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: 未检测到 EFI 分区"}

    efi_size_mb = partition_info.get("efi_size_mb")
    if not isinstance(efi_size_mb, (int, float)) or isinstance(efi_size_mb, bool):
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: EFI 分区配置无效"}
    expected_efi_size = int(efi_size_mb * 1024 * 1024)
    if not is_size_close(efi_partition.get("size_bytes"), expected_efi_size):
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: EFI 分区大小不符合配置"}

    windows_partition = find_windows_partition(partitions)
    if not windows_partition:
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: 未检测到可用的 Windows 分区"}

    c_size_gb = partition_info.get("c_size_gb")
    if not isinstance(c_size_gb, (int, float)) or isinstance(c_size_gb, bool):
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: C 分区配置无效"}
    expected_c_size = int(c_size_gb * 1024 * 1024 * 1024)
    if not is_size_close(windows_partition.get("size_bytes"), expected_c_size):
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: Windows 分区大小不符合配置"}

    volume = windows_partition.get("volume") or {}
    if volume.get("file_system") != "NTFS":
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: Windows 分区文件系统不是 NTFS"}
    if volume.get("file_system_label") != "Windows":
        return {"disk_number": disk_number, "passed": False, "message": "分区验证失败: Windows 分区卷标不正确"}

    for label in ["Data1", "Data2"]:
        failed_result = validate_data_partition(partitions, disk_number, label)
        if failed_result:
            return failed_result

    return {"disk_number": disk_number, "passed": True, "message": "分区和格式化结果验证通过"}



def validate_partitioned_disks(
    disk_numbers: list[int],
    partition_info: dict[str, Any],
    disk_scanner: DiskScanner | None = None,
) -> list[dict[str, Any]]:
    if not disk_numbers:
        raise ValueError("分区验证硬盘编号不能为空")

    scanner = disk_scanner or scan_disks
    disks = scanner()
    results: list[dict[str, Any]] = []

    for disk_number in disk_numbers:
        disk = find_disk(disks, disk_number)
        if not disk:
            results.append({"disk_number": disk_number, "passed": False, "message": "分区验证失败: 未找到目标硬盘"})
            continue
        results.append(validate_partitioned_disk(disk, partition_info))

    return results



def print_partition_validation_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "通过" if result.get("passed") else "失败"
        print(f"硬盘 {result.get('disk_number')} 分区格式化验证{status}: {result.get('message')}")
