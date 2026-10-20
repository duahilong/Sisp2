import json
import subprocess
from collections.abc import Callable
from typing import Any

from app.disk_safety import (
    build_identity_assertion_call,
    build_identity_assertion_function_lines,
    normalize_expected_identities,
)
from app.modules.common.service import POWERSHELL_STORAGE_TIMEOUT, print_step_results
from app.modules.common.service import run_powershell as _run_powershell

PowershellRunner = Callable[[str], subprocess.CompletedProcess[str]]



def build_initialize_disk_script(
    disk_numbers: list[int],
    expected_identities: dict[int, dict[str, Any]] | dict[str, Any] | list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> str:
    if not disk_numbers:
        raise ValueError("初始化硬盘编号不能为空")

    expected_map = normalize_expected_identities(disk_numbers, expected_identities)
    lines = [
        '$ErrorActionPreference = "Stop"',
        "$results = @()",
    ]
    if expected_map:
        lines.extend(build_identity_assertion_function_lines())

    for disk_number in disk_numbers:
        if not isinstance(disk_number, int):
            raise ValueError(f"硬盘编号必须为整数: {disk_number}")

        identity = expected_map.get(disk_number)
        lines.extend(
            [
                build_identity_assertion_call(disk_number, identity)
                if identity
                else f"$disk = Get-Disk -Number {disk_number}",
                "if ($disk.IsBoot -or $disk.IsSystem) {",
                f'    throw "拒绝初始化系统盘或启动盘: {disk_number}"',
                "}",
                "if ($disk.IsReadOnly) {",
                f'    throw "拒绝初始化只读硬盘: {disk_number}"',
                "}",
                "if ($disk.IsOffline) {",
                f'    throw "拒绝初始化离线硬盘: {disk_number}"',
                "}",
                "if ($disk.PartitionStyle -eq 'RAW') {",
            ]
        )
        if identity:
            lines.append(build_identity_assertion_call(disk_number, identity))
        lines.extend(
            [
                f"    Initialize-Disk -Number {disk_number} -PartitionStyle GPT",
                "} else {",
            ]
        )
        if identity:
            lines.append("    " + build_identity_assertion_call(disk_number, identity))
        lines.extend(
            [
                f"    Clear-Disk -Number {disk_number} -RemoveData -RemoveOEM -Confirm:$false",
                "    Start-Sleep -Seconds 2",
                f"    Update-Disk -Number {disk_number} -ErrorAction SilentlyContinue",
                "    Start-Sleep -Seconds 1",
                f"    $diskAfterClear = Get-Disk -Number {disk_number}",
                "    if ($diskAfterClear.PartitionStyle -eq 'RAW') {",
            ]
        )
        if identity:
            lines.append("        " + build_identity_assertion_call(disk_number, identity))
        lines.extend(
            [
                f"        Initialize-Disk -Number {disk_number} -PartitionStyle GPT",
                "    } elseif ($diskAfterClear.PartitionStyle -ne 'GPT') {",
                f"        throw \"清盘后硬盘分区表格式异常，无法初始化为 GPT: {disk_number}，当前格式: $($diskAfterClear.PartitionStyle)\"",
                "    }",
                "}",
                "Start-Sleep -Seconds 2",
                f"Update-Disk -Number {disk_number} -ErrorAction SilentlyContinue",
                "Start-Sleep -Seconds 1",
                f"$initializedDisk = Get-Disk -Number {disk_number}",
                "$results += [PSCustomObject]@{",
                "    disk_number = $initializedDisk.Number",
                "    friendly_name = $initializedDisk.FriendlyName",
                "    partition_style = $initializedDisk.PartitionStyle",
                "    size_bytes = $initializedDisk.Size",
                "    is_boot = $initializedDisk.IsBoot",
                "    is_system = $initializedDisk.IsSystem",
                "    is_offline = $initializedDisk.IsOffline",
                "    is_read_only = $initializedDisk.IsReadOnly",
                "}",
            ]
        )

    lines.append("$results | ConvertTo-Json -Depth 4")
    return "\n".join(lines)



def parse_initialized_disks(stdout: str) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return []

    data = json.loads(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data

    raise ValueError("初始化结果 JSON 根节点必须为对象或列表")



def build_success_results(initialized_disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for disk in initialized_disks:
        disk_number = disk.get("disk_number")
        partition_style = disk.get("partition_style")
        passed = partition_style == "GPT"
        message = "硬盘初始化完成" if passed else f"硬盘初始化后分区表格式异常: {partition_style}"
        results.append(
            {
                "disk_number": disk_number,
                "passed": passed,
                "message": message,
                "disk": disk,
            }
        )
    return results



def initialize_disks(
    disk_numbers: list[int],
    powershell_runner: PowershellRunner | None = None,
    expected_identities: dict[int, dict[str, Any]] | dict[str, Any] | list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> list[dict[str, Any]]:
    script = build_initialize_disk_script(disk_numbers, expected_identities=expected_identities)
    runner = powershell_runner or (lambda script: _run_powershell(script, timeout=POWERSHELL_STORAGE_TIMEOUT))
    completed = runner(script)

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "未知错误"
        return [
            {
                "disk_number": disk_number,
                "passed": False,
                "message": f"硬盘初始化失败: {stderr}",
                "disk": None,
            }
            for disk_number in disk_numbers
        ]

    try:
        initialized_disks = parse_initialized_disks(completed.stdout or "")
    except Exception as exc:
        return [
            {
                "disk_number": disk_number,
                "passed": False,
                "message": f"硬盘初始化结果解析失败: {exc}",
                "disk": None,
            }
            for disk_number in disk_numbers
        ]

    return build_success_results(initialized_disks)



def print_initialize_results(results: list[dict[str, Any]]) -> None:
    print_step_results("初始化", results)
