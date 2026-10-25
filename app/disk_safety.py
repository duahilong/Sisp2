"""目标盘身份与资源路径安全校验。

这里集中放置三类安全检查，供初始化和分区等破坏性步骤复用：

1. 目标盘必须至少具备 ``UniqueId`` 或 ``SerialNumber`` 之一，避免身份
   字段为空时“校验通过”。
2. 生成 PowerShell 身份断言代码，在真正执行 ``Clear-Disk`` /
   ``New-Partition`` 的脚本内部再次校验身份，缩小扫描与写盘之间的
   TOCTOU 窗口。
3. 将关键资源路径解析到物理磁盘，拒绝使用位于目标盘上的 GHO、工具、
   源目录、配置或日志目录。
4. 在 Ghost 写入、目录拷贝和 bcdboot 之前再次确认盘符仍属于目标盘。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from app.logging_setup import get_log_path
from app.modules.common.service import (
    POWERSHELL_PROBE_TIMEOUT,
    ConfigError,
    IdentityMismatchError,
    PartitionError,
    SispError,
    run_powershell,
)
from app.paths import get_app_dir

PowershellRunner = Callable[[str], subprocess.CompletedProcess[str]]
IDENTITY_FIELDS = ("disk_number", "unique_id", "serial_number", "model", "size_bytes")


def normalize_identity_value(value: Any) -> str:
    return str(value or "").strip()


def ensure_strong_identity(identity: dict[str, Any], context: str = "目标硬盘") -> None:
    """要求目标盘至少有一个强身份字段，否则拒绝写盘。"""
    unique_id = normalize_identity_value(identity.get("unique_id"))
    serial_number = normalize_identity_value(identity.get("serial_number"))
    if not unique_id and not serial_number:
        raise IdentityMismatchError(
            f"{context}缺少 UniqueId 和 SerialNumber，无法安全确认磁盘身份，拒绝写盘"
        )


def validate_disk_identity(
    disk: dict[str, Any],
    expected_identity: dict[str, Any] | None,
    require_strong: bool = True,
) -> None:
    """比较扫描结果与预期身份，失败时抛出 ``IdentityMismatchError``。"""
    if not expected_identity:
        if require_strong:
            raise IdentityMismatchError("缺少目标硬盘身份信息，拒绝写盘")
        return

    if require_strong:
        ensure_strong_identity(expected_identity)

    disk_number = disk.get("disk_number")
    expected_disk_number = expected_identity.get("disk_number")
    expected_unique_id = normalize_identity_value(expected_identity.get("unique_id"))
    expected_serial_number = normalize_identity_value(expected_identity.get("serial_number"))
    expected_model = normalize_identity_value(expected_identity.get("model"))
    expected_size_bytes = expected_identity.get("size_bytes")

    actual_unique_id = normalize_identity_value(disk.get("unique_id"))
    actual_serial_number = normalize_identity_value(disk.get("serial_number"))
    actual_model = normalize_identity_value(disk.get("model"))
    actual_size_bytes = disk.get("size_bytes")

    mismatches: list[str] = []
    if (
        isinstance(expected_disk_number, int)
        and not isinstance(expected_disk_number, bool)
        and isinstance(disk_number, int)
        and not isinstance(disk_number, bool)
        and expected_disk_number != disk_number
    ):
        mismatches.append("硬盘编号不一致")
    if expected_unique_id and actual_unique_id != expected_unique_id:
        mismatches.append("UniqueId 不一致")
    if expected_serial_number and actual_serial_number.upper() != expected_serial_number.upper():
        mismatches.append("SerialNumber 不一致")
    if expected_model and actual_model != expected_model:
        mismatches.append("硬盘型号不一致")
    if (
        isinstance(expected_size_bytes, int)
        and not isinstance(expected_size_bytes, bool)
        and actual_size_bytes != expected_size_bytes
    ):
        mismatches.append("硬盘容量不一致")

    if mismatches:
        raise IdentityMismatchError(
            f"目标硬盘身份校验失败: {disk_number}，{'，'.join(mismatches)}"
        )


def normalize_expected_identities(
    disk_numbers: list[int],
    expected_identities: dict[int, dict[str, Any]] | dict[str, Any] | list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[int, dict[str, Any]]:
    """将多种身份输入形式规整为 ``{disk_number: identity}``。

    支持：
    - 单个身份字典；
    - 身份字典列表；
    - ``{disk_number: identity}`` 映射。

    只要提供了身份，就必须覆盖所有目标盘，且每个身份都必须有强标识。
    """
    if expected_identities is None:
        return {}

    if isinstance(expected_identities, dict):
        if any(key in expected_identities for key in IDENTITY_FIELDS):
            raw_items: list[dict[str, Any]] = [expected_identities]
        else:
            raw_items = []
            for disk_number, value in expected_identities.items():
                if not isinstance(value, dict):
                    raise ValueError(f"硬盘 {disk_number} 的身份信息必须为对象")
                item = dict(value)
                item.setdefault("disk_number", disk_number)
                raw_items.append(item)
    elif isinstance(expected_identities, (list, tuple)):
        raw_items = list(expected_identities)
    else:
        raise ValueError("expected_identities 必须为字典、列表或 None")

    if not raw_items:
        raise IdentityMismatchError(
            "缺少目标硬盘身份信息，拒绝写盘: " + ", ".join(str(number) for number in disk_numbers)
        )

    normalized: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("目标硬盘身份信息必须为对象")
        disk_number = item.get("disk_number")
        if disk_number is None and len(disk_numbers) == 1:
            disk_number = disk_numbers[0]
        if not isinstance(disk_number, int) or isinstance(disk_number, bool):
            raise ValueError(f"目标硬盘身份缺少有效的 disk_number: {item}")
        if disk_number not in disk_numbers:
            raise ValueError(f"身份信息中的硬盘编号不在目标列表中: {disk_number}")
        ensure_strong_identity(item, context=f"硬盘 {disk_number} ")
        normalized[disk_number] = item

    missing = [str(number) for number in disk_numbers if number not in normalized]
    if missing:
        raise IdentityMismatchError(
            f"缺少目标硬盘身份信息，拒绝写盘: {', '.join(missing)}"
        )
    return normalized


def quote_powershell_string(value: str) -> str:
    """生成安全的 PowerShell 单引号字符串字面量。"""
    if "\r" in value or "\n" in value:
        raise ValueError("PowerShell 字符串参数不能包含换行")
    return "'" + value.replace("'", "''") + "'"


def build_identity_assertion_function_lines() -> list[str]:
    """返回 PowerShell 身份断言函数定义。"""
    return [
        "function Assert-DiskIdentity {",
        "    param([int]$DiskNumber, [string]$ExpectedIdentityJson)",
        "    $expected = $ExpectedIdentityJson | ConvertFrom-Json",
        "    $disk = Get-Disk -Number $DiskNumber -ErrorAction Stop",
        "    $expectedUniqueId = [string]$expected.unique_id",
        "    $expectedSerialNumber = [string]$expected.serial_number",
        "    $expectedModel = [string]$expected.model",
        "    if ($expectedUniqueId -and $disk.UniqueId -ne $expectedUniqueId) {",
        '        throw "目标硬盘 UniqueId 不匹配: $DiskNumber"',
        "    }",
        "    if ($expectedSerialNumber) {",
        "        $actualSerialNumber = [string]$disk.SerialNumber",
        "        if ($actualSerialNumber.Trim().ToUpper() -ne $expectedSerialNumber.Trim().ToUpper()) {",
        '            throw "目标硬盘 SerialNumber 不匹配: $DiskNumber"',
        "        }",
        "    }",
        "    if ($expectedModel) {",
        "        $actualModel = [string]$disk.FriendlyName",
        "        if ($actualModel.Trim() -ne $expectedModel) {",
        '            throw "目标硬盘型号不匹配: $DiskNumber"',
        "        }",
        "    }",
        "    if ($null -ne $expected.size_bytes -and [UInt64]$disk.Size -ne [UInt64]$expected.size_bytes) {",
        '        throw "目标硬盘容量不匹配: $DiskNumber"',
        "    }",
        "    return $disk",
        "}",
    ]


def build_identity_assertion_call(disk_number: int, identity: dict[str, Any]) -> str:
    """生成单个目标盘的 PowerShell 身份断言调用。"""
    payload = {key: identity.get(key) for key in IDENTITY_FIELDS}
    literal = quote_powershell_string(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return (
        f"$disk = Assert-DiskIdentity -DiskNumber {disk_number} "
        f"-ExpectedIdentityJson {literal}"
    )


def normalize_drive_letters(letters: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for letter in letters:
        value = str(letter or "").strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def build_drive_letter_ownership_script(disk_number: int, drive_letters: Iterable[str]) -> str:
    lines = ['$ErrorActionPreference = "Stop"']
    for letter in normalize_drive_letters(drive_letters):
        literal = quote_powershell_string(letter)
        lines.extend(
            [
                f"$volume = Get-Volume -DriveLetter {literal} -ErrorAction Stop",
                "$partition = Get-Partition -Volume $volume -ErrorAction Stop",
                f"if ($partition.DiskNumber -ne {disk_number}) {{",
                f'    throw "盘符 $($volume.DriveLetter) 属于磁盘 $($partition.DiskNumber)，不是目标硬盘 {disk_number}"',
                "}",
            ]
        )
    lines.append('"OK"')
    return "\n".join(lines)


def validate_drive_letter_ownership(
    disk_number: int,
    drive_letters: Iterable[str],
    powershell_runner: PowershellRunner | None = None,
    timeout: float = POWERSHELL_PROBE_TIMEOUT,
) -> None:
    """确认盘符当前确实属于目标盘，避免 Ghost/拷贝/bcdboot 写到别的盘。"""
    letters = normalize_drive_letters(drive_letters)
    if not letters:
        return

    runner = powershell_runner or (
        lambda script: run_powershell(script, timeout=timeout)
    )
    completed = runner(build_drive_letter_ownership_script(disk_number, letters))
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "未知错误"
        raise PartitionError(
            f"盘符归属校验失败（目标硬盘 {disk_number}，盘符 {', '.join(letters)}）: {stderr}"
        )


def is_unc_path(path: str | Path) -> bool:
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return True
    if text.startswith("\\\\?\\"):
        # 扩展长度本地路径（\\?\C:\...）仍然需要解析所在磁盘。
        return False
    return text.startswith("\\\\")


def find_existing_path(path: Path) -> Path | None:
    """向上查找最近的已存在路径，便于校验尚未创建的日志/结果目录。"""
    candidate = path
    while True:
        if candidate.exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def build_path_disk_script(path: str | Path) -> str:
    path_literal = quote_powershell_string(str(path))
    return "\n".join(
        [
            '$ErrorActionPreference = "Stop"',
            f"$path = {path_literal}",
            "if (-not (Test-Path -LiteralPath $path)) {",
            '    throw "路径不存在: $path"',
            "}",
            "$resolved = (Resolve-Path -LiteralPath $path -ErrorAction Stop).Path",
            "$volume = Get-Volume -FilePath $resolved -ErrorAction Stop",
            "$partition = Get-Partition -Volume $volume -ErrorAction Stop",
            "[PSCustomObject]@{",
            "    path = $resolved",
            "    disk_number = $partition.DiskNumber",
            "    partition_number = $partition.PartitionNumber",
            "    volume_unique_id = $volume.UniqueId",
            "} | ConvertTo-Json -Compress",
        ]
    )


def resolve_path_disk_number(
    path: str | Path,
    powershell_runner: PowershellRunner | None = None,
    timeout: float = POWERSHELL_PROBE_TIMEOUT,
) -> int | None:
    """解析本地路径所在磁盘号；UNC/网络路径返回 ``None``。

    对尚未创建的目录，使用最近的已存在父目录解析，避免日志/结果目录
    在写盘前还不存在时无法检查。
    """
    path_text = str(path)
    if is_unc_path(path_text):
        return None

    path_obj = Path(path_text).expanduser()
    try:
        resolved = path_obj.resolve(strict=False)
    except OSError:
        resolved = path_obj.absolute()

    existing = find_existing_path(resolved)
    if existing is None:
        raise ConfigError(f"无法确认路径所在磁盘（路径不存在）: {path_text}")

    runner = powershell_runner or (
        lambda script: run_powershell(script, timeout=timeout)
    )
    completed = runner(build_path_disk_script(existing))
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or "未知错误"
        raise ConfigError(
            f"无法确认路径所在磁盘: {path_text}（返回码 {completed.returncode}）；{detail}"
        )

    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise ConfigError(f"路径所在磁盘解析结果为空: {path_text}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"路径所在磁盘解析结果不是有效 JSON: {path_text}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"路径所在磁盘解析结果格式异常: {path_text}")

    disk_number = data.get("disk_number")
    if not isinstance(disk_number, int) or isinstance(disk_number, bool):
        raise ConfigError(f"路径所在磁盘编号无效: {path_text}")
    return disk_number


def build_resource_paths(
    config_payload: dict[str, Any],
    config_path: str | Path | None = None,
    log_dir: str | Path | None = None,
    result_dir: str | Path | None = None,
    include_app: bool = True,
) -> list[tuple[str, str]]:
    """收集所有不能位于目标盘上的关键资源路径。"""
    resources: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, value: Any) -> None:
        if isinstance(value, Path):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            return
        if not text.strip():
            return
        key = str(Path(text).absolute()).lower()
        if key in seen:
            return
        seen.add(key)
        resources.append((name, text))

    add("配置文件", config_payload.get("config_path") or config_path)
    software_paths = config_payload.get("software_paths") or {}
    add("Ghost 可执行文件 (gho_exe)", software_paths.get("ghost64_path"))
    add("bcdboot 可执行文件 (bcd_exe)", software_paths.get("bcdboot_path"))
    add("Ghost 镜像文件 (win_gho)", (config_payload.get("image_info") or {}).get("image_path"))
    add("目录拷贝源 (software_file)", (config_payload.get("copy_info") or {}).get("source_dir"))

    add("日志目录", log_dir)
    log_path = get_log_path()
    if log_path:
        add("日志目录(实际)", log_path.parent)
    add("worker 结果目录", result_dir)

    if include_app:
        add("程序所在目录", get_app_dir())
        add("Python 解释器", sys.executable)

    return resources


def validate_resources_not_on_target_disks(
    resources: Iterable[tuple[str, str]],
    target_disk_numbers: Iterable[int],
    resolver: Callable[[str | Path], int | None] | None = None,
) -> dict[str, int | None]:
    """拒绝位于任意目标盘上的资源路径。

    对本地路径解析失败采取 fail-closed：宁可中止，也不在无法确认资源
    位置时清盘。
    """
    targets = {int(number) for number in target_disk_numbers}
    if not targets:
        return {}

    resolve = resolver or resolve_path_disk_number
    resolved: dict[str, int | None] = {}
    conflicts: list[str] = []

    for name, path in resources:
        if not path:
            continue
        if is_unc_path(path):
            resolved[name] = None
            continue
        try:
            disk_number = resolve(path)
        except SispError:
            raise
        except Exception as exc:
            raise ConfigError(f"无法确认 {name} 所在磁盘: {path}；{exc}") from exc

        if disk_number is None:
            raise ConfigError(f"无法确认 {name} 所在磁盘，拒绝写盘: {path}")
        resolved[name] = disk_number
        if disk_number in targets:
            conflicts.append(f"{name}={path}（磁盘 {disk_number}）")

    if conflicts:
        raise ConfigError(
            "以下关键资源位于目标硬盘上，清盘会将其删除，已拒绝执行: "
            + "；".join(conflicts)
        )
    return resolved
