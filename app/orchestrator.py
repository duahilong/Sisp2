"""主流程编排。

该模块只负责“按步骤执行并记录结果”，具体的 PowerShell/文件操作仍由
``app.modules`` 下的模块完成。CLI 入口在 ``app.cli``。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.disk_safety import (
    build_resource_paths,
    ensure_strong_identity,
    normalize_drive_letters,
    resolve_path_disk_number,
    validate_disk_identity,
    validate_drive_letter_ownership,
    validate_resources_not_on_target_disks,
)
from app.drive_letters import (
    build_drive_letter_allocations,
    build_forbidden_drive_letters,
    validate_drive_letters_are_free,
)
from app.logging_setup import get_log_path, get_logger, log_step
from app.models import RunContext, WorkerResult
from app.modules.boot_creator.service import build_bcdboot_command, create_boot_record, print_boot_results
from app.modules.common.constants import ErrorCode
from app.modules.common.service import (
    BootError,
    ConfigError,
    CopyError,
    DiskProtectedError,
    GhostError,
    InitializationError,
    PartitionError,
    PreflightError,
    SispError,
    ValidationError,
)
from app.modules.directory_copier.service import copy_directory, print_copy_results
from app.modules.disk_info.service import scan_disk_summaries, scan_used_drive_letters
from app.modules.disk_initializer.service import (
    build_initialize_disk_script,
    initialize_disks,
    print_initialize_results,
)
from app.modules.disk_partitioner.service import (
    build_partition_disk_script,
    partition_and_format_disks,
    print_partition_results,
)
from app.modules.ghost_writer.service import build_ghost_command, print_ghost_results, write_ghost_image
from app.modules.initialization_validator.service import (
    print_initialization_validation_results,
    validate_initialized_disks,
)
from app.modules.partition_validator.service import print_partition_validation_results, validate_partitioned_disks
from app.modules.user_interaction.service import apply_disk_protection, print_disk_summaries, prompt_disk_selection
from app.preflight import print_preflight_report, run_preflight_checks
from app.results import DEFAULT_WORKER_RESULT_TIMEOUT_SECONDS, print_worker_summary, wait_for_worker_results
from app.worker_launcher import launch_worker_windows

PreflightRunner = Callable[[str | Path | None], dict[str, Any]]
WorkerLauncher = Callable[[list[dict[str, Any]], str | None], Any]

BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024
MIN_DATA_PARTITIONS_BYTES = 2 * BYTES_PER_MB
STEP_TOTAL = 7


def get_config_path_for_worker(config_payload: dict[str, Any]) -> str | None:
    config_path = config_payload.get("config_path")
    return str(config_path) if config_path else None


def find_disk_summary(disk_summaries: list[dict[str, Any]], disk_number: int) -> dict[str, Any] | None:
    for disk in disk_summaries:
        if disk.get("disk_number") == disk_number:
            return disk
    return None


def build_worker_disk_identity(disk: dict[str, Any]) -> dict[str, Any]:
    return {
        "disk_number": disk.get("disk_number"),
        "unique_id": disk.get("unique_id"),
        "serial_number": disk.get("serial_number"),
        "model": disk.get("model"),
        "size_bytes": disk.get("size_bytes"),
    }


def collect_partition_drive_letters(
    drive_letters: dict[str, str] | None,
    partition_results: list[dict[str, Any]],
) -> list[str]:
    letters: list[str] = []
    if drive_letters:
        letters.extend(str(value) for value in drive_letters.values() if value)
    if partition_results:
        partitions = partition_results[0].get("partitions") or {}
        for key in ("efi_drive_letter", "c_drive_letter", "d1_drive_letter", "d2_drive_letter"):
            letter = partitions.get(key)
            if letter:
                letters.append(str(letter))
    return normalize_drive_letters(letters)


def build_worker_disk_identities(disk_summaries: list[dict[str, Any]], disk_numbers: list[int]) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for disk_number in disk_numbers:
        disk = find_disk_summary(disk_summaries, disk_number)
        if not disk:
            raise RuntimeError(f"未找到目标硬盘: {disk_number}")
        identity = build_worker_disk_identity(disk)
        # 在启动 worker 前就 fail-closed，避免没有强身份的盘进入写流程。
        ensure_strong_identity(identity, context=f"硬盘 {disk_number} ")
        identities.append(identity)
    return identities


def validate_worker_disk_identity(
    disk: dict[str, Any],
    expected_identity: dict[str, Any] | None = None,
    require_strong: bool = True,
) -> None:
    validate_disk_identity(disk, expected_identity, require_strong=require_strong)


def build_failed_result_message(results: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for result in results:
        if result.get("passed"):
            continue
        disk_number = result.get("disk_number")
        message = result.get("message") or "未知错误"
        messages.append(f"硬盘 {disk_number}: {message}")
    return "；".join(messages) or "未知错误"


def build_target_disk_text(disk: dict[str, Any]) -> str:
    drive_letters = "，".join(disk.get("drive_letters") or []) or "无"
    return "\n".join(
        [
            "目标硬盘信息:",
            f"硬盘编号: {disk.get('disk_number')}",
            f"硬盘型号: {disk.get('model') or '未知'}",
            f"硬盘容量: {disk.get('size_display') or '未知'}",
            f"连接方式: {disk.get('bus_type') or '未知'}",
            f"当前盘符: {drive_letters}",
            f"分区表格式: {disk.get('partition_style') or '未知'}",
        ]
    )


def validate_required_paths(config_payload: dict[str, Any]) -> None:
    """校验 Ghost、镜像、目录拷贝源和 bcdboot 路径。

    对文件/目录类型也做检查，避免把目录误当成可执行文件，或把文件误当成
    目录拷贝源。该函数会在任何写盘动作前以及 Ghost 写入前各调用一次。
    """
    required_paths: dict[str, tuple[Any, str]] = {
        "Ghost 可执行文件路径 (gho_exe)": ((config_payload.get("software_paths") or {}).get("ghost64_path"), "file"),
        "Ghost 镜像文件路径 (win_gho)": ((config_payload.get("image_info") or {}).get("image_path"), "file"),
        "目录拷贝源路径 (software_file)": ((config_payload.get("copy_info") or {}).get("source_dir"), "dir"),
        "bcdboot 可执行文件路径 (bcd_exe)": ((config_payload.get("software_paths") or {}).get("bcdboot_path"), "file"),
    }
    for name, (value, expected_kind) in required_paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"配置中缺少 {name}")
        path = Path(value)
        if not path.exists():
            raise ConfigError(f"{name} 不存在: {value}")
        if expected_kind == "file" and not path.is_file():
            raise ConfigError(f"{name} 不是文件: {value}")
        if expected_kind == "dir" and not path.is_dir():
            raise ConfigError(f"{name} 不是目录: {value}")


def validate_target_resources(
    config_payload: dict[str, Any],
    target_disk_numbers: list[int],
    config_path: str | Path | None = None,
    log_dir: str | Path | None = None,
    result_dir: str | Path | None = None,
) -> None:
    """拒绝把 GHO、工具、源目录、配置或日志放在目标盘上。"""
    resources = build_resource_paths(
        config_payload,
        config_path=config_path,
        log_dir=log_dir,
        result_dir=result_dir,
    )
    validate_resources_not_on_target_disks(
        resources,
        target_disk_numbers,
        resolver=resolve_path_disk_number,
    )


def revalidate_target_disk(
    disk_number: int,
    expected_identity: dict[str, Any],
    excluded_disk_names: list[str] | None = None,
) -> dict[str, Any]:
    """在关键写入步骤前重新扫描并校验目标盘身份。"""
    disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names or [])
    disk = find_disk_summary(disk_summaries, disk_number)
    if not disk:
        raise DiskProtectedError(f"目标硬盘已不存在，拒绝继续: {disk_number}")
    if disk.get("is_selectable") is False:
        reasons = "，".join(disk.get("protection_reasons") or [])
        raise DiskProtectedError(f"目标硬盘不可操作: {disk_number}，原因: {reasons or '受保护'}")
    validate_worker_disk_identity(disk, expected_identity)
    return disk


def validate_partition_plan_before_initialize(disk: dict[str, Any], partition_info: dict[str, Any]) -> None:
    disk_number = disk.get("disk_number")
    size_bytes = disk.get("size_bytes")
    efi_size_mb = partition_info.get("efi_size_mb")
    c_size_gb = partition_info.get("c_size_gb")

    if not isinstance(size_bytes, int) or size_bytes <= 0:
        raise ConfigError(f"无法确认目标硬盘容量，拒绝初始化硬盘: {disk_number}")
    if (
        not isinstance(efi_size_mb, (int, float))
        or isinstance(efi_size_mb, bool)
        or not math.isfinite(efi_size_mb)
        or efi_size_mb <= 0
    ):
        raise ConfigError(f"EFI 分区大小无效，拒绝初始化硬盘: {efi_size_mb}")
    if (
        not isinstance(c_size_gb, (int, float))
        or isinstance(c_size_gb, bool)
        or not math.isfinite(c_size_gb)
        or c_size_gb <= 0
    ):
        raise ConfigError(f"C 分区大小无效，拒绝初始化硬盘: {c_size_gb}")

    required_bytes = int(efi_size_mb * BYTES_PER_MB) + int(c_size_gb * BYTES_PER_GB) + MIN_DATA_PARTITIONS_BYTES
    if size_bytes < required_bytes:
        required_gb = required_bytes / BYTES_PER_GB
        actual_gb = size_bytes / BYTES_PER_GB
        raise ConfigError(
            f"目标硬盘容量不足，拒绝初始化硬盘 {disk_number}: "
            f"至少需要 {required_gb:.2f} GB，实际 {actual_gb:.2f} GB"
        )


def _print_step(index: int, name: str) -> None:
    print(f"[{index}/{STEP_TOTAL}] {name}...")


def _record_step(context: RunContext, name: str, passed: bool, message: str) -> None:
    record = context.record_step(name, passed, message)
    log_step(context.disk_number, name, passed, message, record.duration_seconds)


def _print_dry_run_plan(
    disk_number: int,
    config_payload: dict[str, Any],
    drive_letters: dict[str, str] | None,
    partition_results: list[dict[str, Any]] | None = None,
    expected_identity: dict[str, Any] | None = None,
) -> None:
    partition_info = config_payload.get("partition_info") or {}
    gho_exe = (config_payload.get("software_paths") or {}).get("ghost64_path") or "<未配置>"
    win_gho = (config_payload.get("image_info") or {}).get("image_path") or "<未配置>"
    bcd_exe = (config_payload.get("software_paths") or {}).get("bcdboot_path") or "<未配置>"
    source_dir = (config_payload.get("copy_info") or {}).get("source_dir") or "<未配置>"

    windows_letter = (drive_letters or {}).get("windows")
    efi_letter = (drive_letters or {}).get("efi")
    data1_letter = (drive_letters or {}).get("data1")
    if partition_results:
        partitions = partition_results[0].get("partitions") or {}
        windows_letter = windows_letter or partitions.get("c_drive_letter")
        efi_letter = efi_letter or partitions.get("efi_drive_letter")
        data1_letter = data1_letter or partitions.get("d1_drive_letter")

    print("=" * 80)
    print(f"Dry-run 执行计划（硬盘 {disk_number}，不会执行任何写盘操作）")
    print("=" * 80)
    expected_identities = [expected_identity] if expected_identity else None
    print("步骤 1：初始化硬盘，将执行以下 PowerShell 脚本：")
    print(build_initialize_disk_script([disk_number], expected_identities=expected_identities))
    print("-" * 80)
    print("步骤 2：分区并格式化，将执行以下 PowerShell 脚本：")
    print(
        build_partition_disk_script(
            [disk_number],
            partition_info.get("efi_size_mb", 0),
            partition_info.get("c_size_gb", 0),
            drive_letters=drive_letters,
            expected_identities=expected_identities,
        )
    )
    print("-" * 80)
    print("步骤 3：Ghost 镜像写入命令：")
    print(build_ghost_command(str(gho_exe), str(win_gho), disk_number))
    print(f"目标 Windows 分区盘符: {windows_letter or '<未知>'}")
    print("-" * 80)
    print(f"步骤 4：目录拷贝源: {source_dir}")
    print(f"目标数据分区盘符: {data1_letter or '<未知>'}")
    print("-" * 80)
    print("步骤 5：引导记录创建命令：")
    if windows_letter and efi_letter:
        print(build_bcdboot_command(str(bcd_exe), str(windows_letter), str(efi_letter)))
    else:
        print(
            [
                str(bcd_exe),
                "<Windows盘符>:\\Windows",
                "/s",
                "<EFI盘符>:",
                "/f",
                "UEFI",
            ]
        )
    print("-" * 80)


def run_single_disk_flow(
    disk_number: int,
    config_payload: dict[str, Any],
    expected_identity: dict[str, Any] | None = None,
    drive_letters: dict[str, str] | None = None,
    run_context: RunContext | None = None,
    dry_run: bool = False,
) -> None:
    context = run_context or RunContext(disk_number=disk_number)
    context.disk_number = disk_number
    context.dry_run = dry_run
    logger = get_logger("flow")
    logger.info("开始处理硬盘 %s（dry_run=%s）", disk_number, dry_run)

    excluded_disk_names = config_payload.get("excluded_disk_names") or []
    disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names)
    disk = find_disk_summary(disk_summaries, disk_number)
    if not disk:
        raise DiskProtectedError(f"未找到目标硬盘: {disk_number}")
    if disk.get("is_selectable") is False:
        reasons = "，".join(disk.get("protection_reasons") or [])
        raise DiskProtectedError(f"目标硬盘不可操作: {disk_number}，原因: {reasons or '受保护'}")

    effective_identity = expected_identity or build_worker_disk_identity(disk)
    if expected_identity:
        validate_worker_disk_identity(disk, expected_identity, require_strong=not dry_run)
    elif not dry_run:
        ensure_strong_identity(effective_identity, context=f"硬盘 {disk_number} ")

    validate_required_paths(config_payload)
    validate_target_resources(config_payload, [disk_number])
    validate_partition_plan_before_initialize(disk, config_payload.get("partition_info") or {})
    if drive_letters and not dry_run:
        try:
            validate_drive_letters_are_free(drive_letters, scan_used_drive_letters())
        except ValueError as exc:
            raise PartitionError(f"盘符校验失败: {exc}") from exc

    print(build_target_disk_text(disk))
    print(f"开始处理硬盘 {disk_number}: {disk.get('model') or '未知'}")

    if dry_run:
        has_strong_identity = bool(
            str(effective_identity.get("unique_id") or "").strip()
            or str(effective_identity.get("serial_number") or "").strip()
        )
        plan_identity = effective_identity if has_strong_identity else None
        _print_dry_run_plan(disk_number, config_payload, drive_letters, expected_identity=plan_identity)
        _record_step(context, "dry-run", True, "已生成执行计划，未执行磁盘操作")
        print(f"硬盘 {disk_number} dry-run 完成，未执行任何磁盘操作")
        return

    _print_step(1, "初始化硬盘")
    context.start_step("初始化")
    initialize_results = initialize_disks([disk_number], expected_identities=[effective_identity])
    print_initialize_results(initialize_results)
    if not all(result.get("passed") for result in initialize_results):
        message = build_failed_result_message(initialize_results)
        _record_step(context, "初始化", False, message)
        raise InitializationError(f"硬盘初始化失败: {message}")
    _record_step(context, "初始化", True, "初始化完成")

    _print_step(2, "验证初始化结果")
    context.start_step("初始化验证")
    validation_results = validate_initialized_disks(initialize_results)
    print_initialization_validation_results(validation_results)
    if not all(result.get("passed") for result in validation_results):
        message = build_failed_result_message(validation_results)
        _record_step(context, "初始化验证", False, message)
        raise ValidationError(f"初始化结果验证失败: {message}")
    _record_step(context, "初始化验证", True, "初始化结果验证通过")

    _print_step(3, "分区并格式化")
    context.start_step("分区格式化")
    partition_results = partition_and_format_disks(
        [disk_number],
        config_payload.get("partition_info") or {},
        drive_letters=drive_letters,
        expected_identities=[effective_identity],
    )
    print_partition_results(partition_results)
    if not partition_results:
        _record_step(context, "分区格式化", False, "分区和格式化结果为空")
        raise PartitionError("分区和格式化结果为空，无法获取 Windows 分区盘符")
    if not all(result.get("passed") for result in partition_results):
        message = build_failed_result_message(partition_results)
        _record_step(context, "分区格式化", False, message)
        raise PartitionError(f"硬盘分区和格式化失败: {message}")
    _record_step(context, "分区格式化", True, "分区和格式化完成")

    _print_step(4, "验证分区结果")
    context.start_step("分区验证")
    partition_validation_results = validate_partitioned_disks(
        [disk_number],
        config_payload.get("partition_info") or {},
        drive_letters=drive_letters,
    )
    print_partition_validation_results(partition_validation_results)
    if not all(result.get("passed") for result in partition_validation_results):
        message = build_failed_result_message(partition_validation_results)
        _record_step(context, "分区验证", False, message)
        raise ValidationError(f"分区和格式化结果验证失败: {message}")
    _record_step(context, "分区验证", True, "分区和格式化结果验证通过")

    gho_exe = (config_payload.get("software_paths") or {}).get("ghost64_path")
    win_gho = (config_payload.get("image_info") or {}).get("image_path")
    if not gho_exe:
        _record_step(context, "Ghost 镜像写入", False, "配置中缺少 Ghost 可执行文件路径")
        raise ConfigError("配置中缺少 Ghost 可执行文件路径 (gho_exe)")
    if not win_gho:
        _record_step(context, "Ghost 镜像写入", False, "配置中缺少 Ghost 镜像文件路径")
        raise ConfigError("配置中缺少 Ghost 镜像文件路径 (win_gho)")

    windows_drive_letter = (drive_letters or {}).get("windows") or (partition_results[0].get("partitions") or {}).get("c_drive_letter")
    if not windows_drive_letter:
        _record_step(context, "Ghost 镜像写入", False, "无法确定 Windows 分区盘符")
        raise PartitionError("无法确定 Windows 分区盘符，Ghost 镜像写入中止")

    _print_step(5, "写入 Ghost 镜像")
    context.start_step("Ghost 镜像写入")
    try:
        # 写盘后再次确认外部资源仍然可用，避免中途被移动/删除；
        # 同时重新校验目标盘身份，防止扫描后磁盘编号发生变化。
        validate_required_paths(config_payload)
        validate_target_resources(config_payload, [disk_number])
        revalidate_target_disk(disk_number, effective_identity, excluded_disk_names)
        validate_drive_letter_ownership(
            disk_number,
            collect_partition_drive_letters(drive_letters, partition_results),
        )
    except SispError as exc:
        _record_step(context, "Ghost 镜像写入", False, str(exc))
        raise
    ghost_results = [write_ghost_image(str(gho_exe), str(win_gho), disk_number, windows_drive_letter)]
    print_ghost_results(ghost_results)
    if not all(result.get("passed") for result in ghost_results):
        message = build_failed_result_message(ghost_results)
        _record_step(context, "Ghost 镜像写入", False, message)
        raise GhostError(f"Ghost 镜像写入失败: {message}")
    _record_step(context, "Ghost 镜像写入", True, "Ghost 镜像写入完成")

    source_dir = (config_payload.get("copy_info") or {}).get("source_dir")
    data1_drive_letter = (drive_letters or {}).get("data1") or (partition_results[0].get("partitions") or {}).get("d1_drive_letter")
    _print_step(6, "拷贝目录")
    context.start_step("目录拷贝")
    try:
        revalidate_target_disk(disk_number, effective_identity, excluded_disk_names)
        validate_drive_letter_ownership(disk_number, [data1_drive_letter])
    except SispError as exc:
        _record_step(context, "目录拷贝", False, str(exc))
        raise
    copy_results = [copy_directory(str(source_dir), data1_drive_letter, disk_number)]
    print_copy_results(copy_results)
    if not all(result.get("passed") for result in copy_results):
        message = build_failed_result_message(copy_results)
        _record_step(context, "目录拷贝", False, message)
        raise CopyError(f"目录拷贝失败: {message}")
    _record_step(context, "目录拷贝", True, "目录拷贝完成")

    bcd_exe = (config_payload.get("software_paths") or {}).get("bcdboot_path")
    efi_drive_letter = (drive_letters or {}).get("efi") or (partition_results[0].get("partitions") or {}).get("efi_drive_letter")
    _print_step(7, "创建引导记录")
    context.start_step("引导创建")
    try:
        revalidate_target_disk(disk_number, effective_identity, excluded_disk_names)
        validate_drive_letter_ownership(
            disk_number,
            [windows_drive_letter, efi_drive_letter],
        )
    except SispError as exc:
        _record_step(context, "引导创建", False, str(exc))
        raise
    boot_results = [create_boot_record(str(bcd_exe), windows_drive_letter, efi_drive_letter, disk_number)]
    print_boot_results(boot_results)
    if not all(result.get("passed") for result in boot_results):
        message = build_failed_result_message(boot_results)
        _record_step(context, "引导创建", False, message)
        raise BootError(f"引导记录创建失败: {message}")
    _record_step(context, "引导创建", True, "引导记录创建完成")

    print(f"硬盘 {disk_number} 当前阶段处理完成")
    logger.info("硬盘 %s 全部步骤完成，总耗时 %.3fs", disk_number, sum(step.duration_seconds for step in context.steps))


def run_minimal_main_flow(
    input_func=input,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
    worker_launcher: WorkerLauncher | None = None,
    worker_interval: float = 6.0,
    result_dir: str | Path | None = None,
    worker_result_timeout: float | None = DEFAULT_WORKER_RESULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    log_dir: str | Path | None = None,
    no_log: bool = False,
) -> list[int]:
    preflight_report = (
        preflight_runner(config_path)
        if preflight_runner
        else run_preflight_checks(config_path, skip_powershell_probe=True)
    )
    print_preflight_report(preflight_report)
    if not preflight_report.get("all_passed"):
        raise PreflightError("运行前检查失败")

    config_payload = preflight_report.get("config_payload")
    if not isinstance(config_payload, dict):
        raise ConfigError("运行前检查未返回可用的配置数据")

    excluded_disk_names = config_payload.get("excluded_disk_names") or []
    try:
        disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names)
    except Exception as exc:
        raise PreflightError(f"硬盘信息扫描失败，请确认 PowerShell 和 Storage 模块可用: {exc}") from exc
    if not disk_summaries:
        raise DiskProtectedError("模块1未返回任何硬盘摘要信息")

    print("=" * 80)
    print("Sisp - 磁盘选择")
    print("=" * 80)
    print(f"配置文件: {config_payload.get('config_path')}")
    print_disk_summaries(disk_summaries)
    selected_disk_numbers = prompt_disk_selection(disk_summaries, input_func=input_func)
    if selected_disk_numbers:
        # 先做全局资源检查：多盘模式下，资源可能位于另一块稍后会被清空的目标盘。
        validate_required_paths(config_payload)
        validate_target_resources(
            config_payload,
            selected_disk_numbers,
            config_path=config_path,
            log_dir=log_dir,
            result_dir=result_dir,
        )
        used_letters = scan_used_drive_letters()
        forbidden = build_forbidden_drive_letters(disk_summaries, selected_disk_numbers, used_letters)
    else:
        forbidden = []

    if len(selected_disk_numbers) > 1 and not dry_run:
        launcher = worker_launcher or launch_worker_windows
        print("已选择多个硬盘，将为每个硬盘启动独立执行窗口")
        allocations = build_drive_letter_allocations(selected_disk_numbers, forbidden_letters=forbidden)
        identities = build_worker_disk_identities(disk_summaries, selected_disk_numbers)
        for identity in identities:
            identity["drive_letters"] = allocations.get(identity["disk_number"])
        if worker_launcher:
            # 兼容测试/外部注入的简化 launcher（只接收 identities 与 config_path）。
            launched = launcher(identities, get_config_path_for_worker(config_payload))
        else:
            launched = launcher(
                identities,
                get_config_path_for_worker(config_payload),
                start_interval_seconds=worker_interval,
                result_dir=result_dir,
                log_dir=log_dir,
                no_log=no_log,
            )
        print()
        print("所有 worker 窗口已启动，请在各窗口中查看执行结果")
        if launched:
            print("等待 worker 结果并汇总...")
            results = wait_for_worker_results(launched, timeout=worker_result_timeout)
            print_worker_summary(results)
        return selected_disk_numbers

    for disk_number in selected_disk_numbers:
        allocations = build_drive_letter_allocations(selected_disk_numbers, forbidden_letters=forbidden)
        disk = find_disk_summary(disk_summaries, disk_number)
        expected_identity = build_worker_disk_identity(disk) if disk else None
        run_single_disk_flow(
            disk_number,
            config_payload,
            expected_identity=expected_identity,
            drive_letters=allocations.get(disk_number),
            dry_run=dry_run,
        )

    return selected_disk_numbers


def run_worker_flow(
    disk_number: int,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
    expected_identity: dict[str, Any] | None = None,
    drive_letters: dict[str, str] | None = None,
    result_file: str | Path | None = None,
    dry_run: bool = False,
) -> None:
    started_at = datetime.now().isoformat(timespec="seconds")
    context = RunContext(disk_number=disk_number, dry_run=dry_run)
    result: WorkerResult | None = None
    logger = get_logger("worker")

    try:
        runner = preflight_runner or run_preflight_checks
        preflight_report = runner(config_path)
        print_preflight_report(preflight_report)
        if not preflight_report.get("all_passed"):
            raise PreflightError("运行前检查失败")

        config_payload = preflight_report.get("config_payload")
        if not isinstance(config_payload, dict):
            raise ConfigError("运行前检查未返回可用的配置数据")

        print("=" * 80)
        print(f"Sisp Worker 硬盘 {disk_number}")
        print("=" * 80)
        print(f"配置文件: {config_payload.get('config_path')}")
        validate_target_resources(
            config_payload,
            [disk_number],
            config_path=config_path,
            result_dir=Path(result_file).parent if result_file else None,
        )
        run_single_disk_flow(
            disk_number,
            config_payload,
            expected_identity,
            drive_letters=drive_letters,
            run_context=context,
            dry_run=dry_run,
        )
        result = WorkerResult(
            disk_number=disk_number,
            success=True,
            error_code=ErrorCode.SUCCESS,
            message="当前阶段处理完成" if not dry_run else "dry-run 完成",
            steps=tuple(context.steps),
            log_path=str(get_log_path()) if get_log_path() else None,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            dry_run=dry_run,
        )
    except SispError as exc:
        result = WorkerResult(
            disk_number=disk_number,
            success=False,
            error_code=exc.error_code,
            message=str(exc),
            steps=tuple(context.steps),
            log_path=str(get_log_path()) if get_log_path() else None,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            dry_run=dry_run,
        )
        raise
    except KeyboardInterrupt:
        result = WorkerResult(
            disk_number=disk_number,
            success=False,
            error_code=ErrorCode.CANCELLED,
            message="操作已取消",
            steps=tuple(context.steps),
            log_path=str(get_log_path()) if get_log_path() else None,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            dry_run=dry_run,
        )
        raise
    except Exception as exc:
        logger.exception("worker 硬盘 %s 未预期异常", disk_number)
        result = WorkerResult(
            disk_number=disk_number,
            success=False,
            error_code=ErrorCode.UNEXPECTED_ERROR,
            message=f"{type(exc).__name__}: {exc}",
            steps=tuple(context.steps),
            log_path=str(get_log_path()) if get_log_path() else None,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            dry_run=dry_run,
        )
        raise
    finally:
        if result_file:
            if result is None:
                result = WorkerResult(
                    disk_number=disk_number,
                    success=False,
                    error_code=ErrorCode.UNEXPECTED_ERROR,
                    message="worker 未生成结果",
                    steps=tuple(context.steps),
                    log_path=str(get_log_path()) if get_log_path() else None,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    dry_run=dry_run,
                )
            from app.results import write_worker_result

            if write_worker_result(result_file, result):
                print(f"结果文件: {result_file}")
