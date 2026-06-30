import json
import subprocess
from typing import Any, Callable

from app.modules.common.service import run_powershell as _run_powershell


PowershellRunner = Callable[[str], subprocess.CompletedProcess[str]]



def build_partition_disk_script(disk_numbers: list[int], efi_size_mb: int | float, c_size_gb: int | float, drive_letters: dict[str, str] | None = None) -> str:
    if not disk_numbers:
        raise ValueError("分区硬盘编号不能为空")
    if not isinstance(efi_size_mb, (int, float)) or isinstance(efi_size_mb, bool) or efi_size_mb <= 0:
        raise ValueError("EFI 分区大小必须为大于 0 的数字")
    if not isinstance(c_size_gb, (int, float)) or isinstance(c_size_gb, bool) or c_size_gb <= 0:
        raise ValueError("C 分区大小必须为大于 0 的数字")
    if drive_letters:
        for key in ["efi", "windows", "data1", "data2"]:
            if key not in drive_letters:
                raise ValueError(f"盘符配置缺少: {key}")

    efi_letter = (drive_letters or {}).get("efi")
    windows_letter = (drive_letters or {}).get("windows")
    data1_letter = (drive_letters or {}).get("data1")
    data2_letter = (drive_letters or {}).get("data2")

    lines = [
        '$ErrorActionPreference = "Stop"',
        "$results = @()",
        f"$efiSize = [UInt64]({efi_size_mb} * 1MB)",
        f"$cSize = [UInt64]({c_size_gb} * 1GB)",
        "function Wait-DiskReady {",
        "    param([UInt32]$DiskNumber, [UInt64]$RequiredBytes, [String]$StepName)",
        "    for ($i = 0; $i -lt 10; $i++) {",
        "        Update-Disk -Number $DiskNumber -ErrorAction SilentlyContinue",
        "        $currentDisk = Get-Disk -Number $DiskNumber",
        "        if ($currentDisk.PartitionStyle -eq 'GPT' -and $currentDisk.LargestFreeExtent -ge $RequiredBytes) {",
        "            return $currentDisk",
        "        }",
        "        Start-Sleep -Seconds 1",
        "    }",
        "    $currentDisk = Get-Disk -Number $DiskNumber",
        "    throw \"目标硬盘没有足够可用空间执行 $StepName。最大连续可用空间: $($currentDisk.LargestFreeExtent)，需要: $RequiredBytes\"",
        "}",
    ]

    for disk_number in disk_numbers:
        if not isinstance(disk_number, int):
            raise ValueError(f"硬盘编号必须为整数: {disk_number}")

        lines.extend(
            [
                f"$disk = Get-Disk -Number {disk_number}",
                "if ($disk.IsBoot -or $disk.IsSystem) {",
                f'    throw "拒绝对系统盘或启动盘分区: {disk_number}"',
                "}",
                "if ($disk.IsReadOnly) {",
                f'    throw "拒绝对只读硬盘分区: {disk_number}"',
                "}",
                "if ($disk.IsOffline) {",
                f'    throw "拒绝对离线硬盘分区: {disk_number}"',
                "}",
                "if ($disk.PartitionStyle -ne 'GPT') {",
                f'    throw "硬盘分区表格式不是 GPT，无法继续分区: {disk_number}"',
                "}",
                f"$msrPartition = Get-Partition -DiskNumber {disk_number} -ErrorAction SilentlyContinue | Where-Object {{ $_.GptType -eq '{{e3c9e316-0b5c-4db8-817d-f92df00215ae}}' }}",
                "if ($msrPartition) {",
                f"    Remove-Partition -DiskNumber {disk_number} -PartitionNumber $msrPartition.PartitionNumber -Confirm:$false",
                "    Start-Sleep -Seconds 1",
                f"    Update-Disk -Number {disk_number} -ErrorAction SilentlyContinue",
                "    Start-Sleep -Seconds 1",
                "}",
            ]
        )

        if efi_letter or windows_letter or data1_letter or data2_letter:
            assigned = [l for l in [efi_letter, windows_letter, data1_letter, data2_letter] if l]
            lines.extend(
                [
                    f"$assignedLetters = @({', '.join(repr(l) for l in assigned)})",
                    "# 清理可能的幽灵盘符（EFI盘符在Clear-Disk后注册表残留）",
                    "foreach ($letter in $assignedLetters) {",
                    "    $mountPoint = \"$letter`:\\\"",
                    "    mountvol $mountPoint /d *>$null",
                    "    $existingVolume = Get-Volume -DriveLetter $letter -ErrorAction SilentlyContinue",
                    "    if ($existingVolume) {",
                    f"        throw \"盘符 $letter 已被占用，无法继续分区: {disk_number}\"",
                    "    }",
                    "}",
                ]
            )

        lines.extend(
            [
                f"$disk = Wait-DiskReady -DiskNumber {disk_number} -RequiredBytes $efiSize -StepName '创建 EFI 分区'",
                f"$efiPartition = New-Partition -DiskNumber {disk_number} -Size $efiSize -GptType '{{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}}'{" -DriveLetter '" + efi_letter + "'" if efi_letter else ""}",
                "Start-Sleep -Seconds 1",
                "$efiVolume = $efiPartition | Format-Volume -FileSystem FAT32 -NewFileSystemLabel 'EFI' -Confirm:$false",
                "Start-Sleep -Seconds 1",
                f"$disk = Wait-DiskReady -DiskNumber {disk_number} -RequiredBytes $cSize -StepName '创建 Windows 分区'",
                f"$cPartition = New-Partition -DiskNumber {disk_number} -Size $cSize{" -DriveLetter '" + windows_letter + "'" if windows_letter else " -AssignDriveLetter"}",
                "Start-Sleep -Seconds 1",
                "$cVolume = $cPartition | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'Windows' -Confirm:$false",
                "Start-Sleep -Seconds 1",
                f"$disk = Wait-DiskReady -DiskNumber {disk_number} -RequiredBytes 1 -StepName '创建 Data1 分区'",
                "$freeSpace = $disk.LargestFreeExtent",
                "$halfSize = [UInt64]([Math]::Floor($freeSpace / 2))",
                "if ($halfSize -lt 1MB) {",
                f"    throw \"目标硬盘剩余空间不足，无法创建 Data1 分区: {disk_number}\"",
                "}",
                f"$d1Partition = New-Partition -DiskNumber {disk_number} -Size $halfSize{" -DriveLetter '" + data1_letter + "'" if data1_letter else " -AssignDriveLetter"}",
                "Start-Sleep -Seconds 1",
                "$d1Volume = $d1Partition | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'Data1' -Confirm:$false",
                "Start-Sleep -Seconds 1",
                f"$disk = Wait-DiskReady -DiskNumber {disk_number} -RequiredBytes 1 -StepName '创建 Data2 分区'",
                f"$d2Partition = New-Partition -DiskNumber {disk_number} -UseMaximumSize{" -DriveLetter '" + data2_letter + "'" if data2_letter else " -AssignDriveLetter"}",
                "Start-Sleep -Seconds 1",
                "$d2Volume = $d2Partition | Format-Volume -FileSystem NTFS -NewFileSystemLabel 'Data2' -Confirm:$false",
                "$results += [PSCustomObject]@{",
                f"    disk_number = {disk_number}",
                "    efi_partition_number = $efiPartition.PartitionNumber",
                "    efi_size_bytes = $efiPartition.Size",
                "    efi_file_system = $efiVolume.FileSystem",
                "    efi_drive_letter = $efiPartition.DriveLetter",
                "    c_partition_number = $cPartition.PartitionNumber",
                "    c_drive_letter = $cPartition.DriveLetter",
                "    c_size_bytes = $cPartition.Size",
                "    c_file_system = $cVolume.FileSystem",
                "    d1_partition_number = $d1Partition.PartitionNumber",
                "    d1_drive_letter = $d1Partition.DriveLetter",
                "    d1_size_bytes = $d1Partition.Size",
                "    d1_file_system = $d1Volume.FileSystem",
                "    d2_partition_number = $d2Partition.PartitionNumber",
                "    d2_drive_letter = $d2Partition.DriveLetter",
                "    d2_size_bytes = $d2Partition.Size",
                "    d2_file_system = $d2Volume.FileSystem",
                "}",
            ]
        )

    lines.append("$results | ConvertTo-Json -Depth 4")
    return "\n".join(lines)



def parse_partitioned_disks(stdout: str) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return []

    data = json.loads(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data

    raise ValueError("分区结果 JSON 根节点必须为对象或列表")



def build_success_results(partitioned_disks: list[dict[str, Any]], drive_letters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for disk in partitioned_disks:
        disk_number = disk.get("disk_number")
        efi_ok = disk.get("efi_file_system") == "FAT32"
        c_ok = disk.get("c_file_system") == "NTFS" and bool(disk.get("c_drive_letter"))
        d1_ok = disk.get("d1_file_system") == "NTFS" and bool(disk.get("d1_drive_letter"))
        d2_ok = disk.get("d2_file_system") == "NTFS" and bool(disk.get("d2_drive_letter"))
        passed = efi_ok and c_ok and d1_ok and d2_ok
        if passed and drive_letters:
            if drive_letters.get("efi") and str(disk.get("efi_drive_letter") or "").upper() != drive_letters["efi"].upper():
                passed = False
            if drive_letters.get("windows") and str(disk.get("c_drive_letter") or "").upper() != drive_letters["windows"].upper():
                passed = False
            if drive_letters.get("data1") and str(disk.get("d1_drive_letter") or "").upper() != drive_letters["data1"].upper():
                passed = False
            if drive_letters.get("data2") and str(disk.get("d2_drive_letter") or "").upper() != drive_letters["data2"].upper():
                passed = False
        message = "硬盘分区和格式化完成" if passed else "硬盘分区或格式化结果异常"
        results.append(
            {
                "disk_number": disk_number,
                "passed": passed,
                "message": message,
                "partitions": disk,
            }
        )
    return results



def partition_and_format_disks(
    disk_numbers: list[int],
    partition_info: dict[str, Any],
    powershell_runner: PowershellRunner | None = None,
    drive_letters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    efi_size_mb = partition_info.get("efi_size_mb")
    c_size_gb = partition_info.get("c_size_gb")
    script = build_partition_disk_script(disk_numbers, efi_size_mb, c_size_gb, drive_letters=drive_letters)
    runner = powershell_runner or _run_powershell
    completed = runner(script)

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "未知错误"
        return [
            {
                "disk_number": disk_number,
                "passed": False,
                "message": f"硬盘分区和格式化失败: {stderr}",
                "partitions": None,
            }
            for disk_number in disk_numbers
        ]

    try:
        partitioned_disks = parse_partitioned_disks(completed.stdout or "")
    except Exception as exc:
        return [
            {
                "disk_number": disk_number,
                "passed": False,
                "message": f"硬盘分区和格式化结果解析失败: {exc}",
                "partitions": None,
            }
            for disk_number in disk_numbers
        ]

    return build_success_results(partitioned_disks, drive_letters=drive_letters)



def print_partition_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "通过" if result.get("passed") else "失败"
        print(f"硬盘 {result.get('disk_number')} 分区格式化{status}: {result.get('message')}")
