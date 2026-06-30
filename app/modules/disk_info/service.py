import json
import math
from typing import Any

from app.modules.common.service import run_powershell


POWERSHELL_SCRIPT = r"""
$disks = Get-Disk | Sort-Object Number
$result = @()

foreach ($disk in $disks) {
    $partitions = @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue)
    $partitionList = @()

    foreach ($partition in $partitions) {
        $volume = $null
        if ($partition.DriveLetter) {
            $volume = Get-Volume -DriveLetter $partition.DriveLetter -ErrorAction SilentlyContinue
        }

        $partitionList += [PSCustomObject]@{
            partition_number = $partition.PartitionNumber
            drive_letter = $partition.DriveLetter
            size_bytes = $partition.Size
            offset_bytes = $partition.Offset
            type = $partition.Type
            gpt_type = $partition.GptType
            mbr_type = $partition.MbrType
            is_active = $partition.IsActive
            is_boot = $partition.IsBoot
            is_hidden = $partition.IsHidden
            is_system = $partition.IsSystem
            access_paths = @($partition.AccessPaths)
            volume = if ($volume) {
                [PSCustomObject]@{
                    drive_letter = $volume.DriveLetter
                    file_system = $volume.FileSystem
                    file_system_label = $volume.FileSystemLabel
                    size_bytes = $volume.Size
                    size_remaining_bytes = $volume.SizeRemaining
                    health_status = $volume.HealthStatus
                    operational_status = @($volume.OperationalStatus)
                }
            } else {
                $null
            }
        }
    }

    $result += [PSCustomObject]@{
        disk_number = $disk.Number
        friendly_name = $disk.FriendlyName
        serial_number = $disk.SerialNumber
        unique_id = $disk.UniqueId
        partition_style = $disk.PartitionStyle
        size_bytes = $disk.Size
        allocated_size_bytes = $disk.AllocatedSize
        logical_sector_size = $disk.LogicalSectorSize
        physical_sector_size = $disk.PhysicalSectorSize
        bus_type = $disk.BusType
        media_type = $disk.MediaType
        health_status = $disk.HealthStatus
        operational_status = @($disk.OperationalStatus)
        is_boot = $disk.IsBoot
        is_system = $disk.IsSystem
        is_offline = $disk.IsOffline
        is_read_only = $disk.IsReadOnly
        location = $disk.Location
        path = $disk.Path
        partition_count = $partitions.Count
        partitions = $partitionList
    }
}

$result | ConvertTo-Json -Depth 6
""".strip()

USED_DRIVE_LETTERS_SCRIPT = r"""
$letters = @()
$letters += Get-Volume | Where-Object { $_.DriveLetter } | ForEach-Object { $_.DriveLetter.ToString().ToUpper() }
$letters += Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -match '^[A-Z]$' } | ForEach-Object { $_.Name.ToUpper() }
$letters | Select-Object -Unique | Sort-Object
""".strip()

INVALID_SIZE_DISPLAY = "大小异常"
UNKNOWN_SIZE_DISPLAY = "未知"


def format_size(size_bytes: Any) -> str:
    if size_bytes is None:
        return UNKNOWN_SIZE_DISPLAY
    if isinstance(size_bytes, bool):
        return INVALID_SIZE_DISPLAY
    if not isinstance(size_bytes, (int, float)):
        return INVALID_SIZE_DISPLAY

    value = float(size_bytes)
    if math.isnan(value) or math.isinf(value) or value < 0:
        return INVALID_SIZE_DISPLAY

    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size_bytes} B"



def run_powershell_json(script: str) -> list[dict[str, Any]]:
    completed = run_powershell(script)

    if completed.returncode != 0:
        raise RuntimeError(
            "PowerShell 执行失败\n"
            f"退出码: {completed.returncode}\n"
            f"标准输出:\n{completed.stdout}\n"
            f"标准错误:\n{completed.stderr}"
        )

    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise RuntimeError("PowerShell 没有返回任何内容")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "PowerShell 返回了无法解析的 JSON\n"
            f"原始输出:\n{stdout[:500]}"
        ) from exc
    if isinstance(data, dict):
        return [data]
    return data



def collect_drive_letters(disk: dict[str, Any]) -> list[str]:
    drive_letters: list[str] = []
    partitions = disk.get("partitions") or []
    for partition in partitions:
        volume = partition.get("volume")
        drive_letter = None

        if volume:
            drive_letter = volume.get("drive_letter") or partition.get("drive_letter")
        else:
            drive_letter = partition.get("drive_letter")

        if drive_letter:
            drive_letter_str = str(drive_letter)
            if drive_letter_str not in drive_letters:
                drive_letters.append(drive_letter_str)

    return drive_letters



def summarize_disk(disk: dict[str, Any]) -> dict[str, Any]:
    return {
        "disk_number": disk.get("disk_number"),
        "model": disk.get("friendly_name"),
        "serial_number": disk.get("serial_number"),
        "unique_id": disk.get("unique_id"),
        "size_bytes": disk.get("size_bytes"),
        "size_display": format_size(disk.get("size_bytes")),
        "partition_style": disk.get("partition_style"),
        "bus_type": disk.get("bus_type"),
        "drive_letters": collect_drive_letters(disk),
        "is_boot": bool(disk.get("is_boot")),
        "is_system": bool(disk.get("is_system")),
        "is_offline": bool(disk.get("is_offline")),
        "is_read_only": bool(disk.get("is_read_only")),
    }



def scan_disks() -> list[dict[str, Any]]:
    return run_powershell_json(POWERSHELL_SCRIPT)



def scan_disk_summaries() -> list[dict[str, Any]]:
    return [summarize_disk(disk) for disk in scan_disks()]



def scan_used_drive_letters() -> list[str]:
    completed = run_powershell(USED_DRIVE_LETTERS_SCRIPT)
    if completed.returncode != 0:
        raise RuntimeError(
            "扫描已用盘符失败\n"
            f"退出码: {completed.returncode}\n"
            f"标准输出:\n{completed.stdout}\n"
            f"标准错误:\n{completed.stderr}"
        )
    stdout = (completed.stdout or "").strip()
    if not stdout:
        return []
    letters: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line and line not in letters:
            letters.append(line.upper())
    return letters
