import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


def get_app_dir() -> Path:
    """获取应用根目录，兼容 PyInstaller 打包"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = get_app_dir()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_info.service import scan_disk_summaries, scan_used_drive_letters
from app.modules.disk_initializer.service import initialize_disks, print_initialize_results
from app.modules.disk_partitioner.service import partition_and_format_disks, print_partition_results
from app.modules.ghost_writer.service import print_ghost_results, write_ghost_image
from app.modules.initialization_validator.service import print_initialization_validation_results, validate_initialized_disks
from app.modules.partition_validator.service import print_partition_validation_results, validate_partitioned_disks
from app.modules.user_interaction.service import apply_disk_protection, print_disk_summaries, prompt_disk_selection
from app.modules.directory_copier.service import copy_directory, print_copy_results
from app.modules.boot_creator.service import create_boot_record, print_boot_results
from app.modules.common.constants import ErrorCode
from app.modules.common.service import (
    SispError, PreflightError, InitializationError, PartitionError,
    GhostError, CopyError, BootError, ValidationError, ConfigError,
    DiskProtectedError, IdentityMismatchError, PathTraversalError
)
from app.preflight import print_preflight_report, run_preflight_checks


PreflightRunner = Callable[[str | Path | None], dict[str, Any]]
WorkerLauncher = Callable[[list[dict[str, Any]], str | None], None]
Sleeper = Callable[[float], None]

AVAILABLE_DRIVE_LETTERS = [
    "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
    "A", "B",
]



def parse_command_line_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("-j", dest="config_path", help="JSON 配置文件路径")
    parser.add_argument("--worker-disk", dest="worker_disk", type=int, help="worker 模式：处理指定硬盘编号，可由主程序自动启动，也可用于单盘调试")
    parser.add_argument("--worker-unique-id", dest="worker_unique_id", help="worker 模式：目标硬盘 UniqueId，用于二次确认硬盘身份")
    parser.add_argument("--worker-serial-number", dest="worker_serial_number", help="worker 模式：目标硬盘 SerialNumber，用于二次确认硬盘身份")
    parser.add_argument("--worker-model", dest="worker_model", help="worker 模式：目标硬盘型号，用于二次确认硬盘身份")
    parser.add_argument("--worker-size-bytes", dest="worker_size_bytes", type=int, help="worker 模式：目标硬盘容量字节数，用于二次确认硬盘身份")
    parser.add_argument("--worker-drive-letters", dest="worker_drive_letters", help="worker 模式：分配的盘符列表（逗号分隔，如 E,F,G,H），分别对应 EFI、Windows、Data1、Data2")
    return parser.parse_args(argv)



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



def build_worker_disk_identities(disk_summaries: list[dict[str, Any]], disk_numbers: list[int]) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for disk_number in disk_numbers:
        disk = find_disk_summary(disk_summaries, disk_number)
        if not disk:
            raise RuntimeError(f"未找到目标硬盘: {disk_number}")
        identities.append(build_worker_disk_identity(disk))
    return identities



def normalize_identity_value(value: Any) -> str:
    return str(value or "").strip()



def validate_worker_disk_identity(disk: dict[str, Any], expected_identity: dict[str, Any] | None = None) -> None:
    if not expected_identity:
        return

    disk_number = disk.get("disk_number")
    expected_unique_id = normalize_identity_value(expected_identity.get("unique_id"))
    expected_serial_number = normalize_identity_value(expected_identity.get("serial_number"))
    expected_model = normalize_identity_value(expected_identity.get("model"))
    expected_size_bytes = expected_identity.get("size_bytes")

    actual_unique_id = normalize_identity_value(disk.get("unique_id"))
    actual_serial_number = normalize_identity_value(disk.get("serial_number"))
    actual_model = normalize_identity_value(disk.get("model"))
    actual_size_bytes = disk.get("size_bytes")

    mismatches: list[str] = []
    if expected_unique_id and actual_unique_id != expected_unique_id:
        mismatches.append("UniqueId 不一致")
    if expected_serial_number and actual_serial_number != expected_serial_number:
        mismatches.append("SerialNumber 不一致")
    if expected_model and actual_model != expected_model:
        mismatches.append("硬盘型号不一致")
    if isinstance(expected_size_bytes, int) and actual_size_bytes != expected_size_bytes:
        mismatches.append("硬盘容量不一致")

    if mismatches:
        raise IdentityMismatchError(f"目标硬盘身份校验失败: {disk_number}，{'，'.join(mismatches)}")



def build_drive_letter_allocations(
    disk_numbers: list[int],
    forbidden_letters: list[str] | set[str] | None = None,
) -> dict[int, dict[str, str]]:
    forbidden = {str(letter).upper() for letter in (forbidden_letters or [])}
    pool = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter not in forbidden]
    allocations: dict[int, dict[str, str]] = {}
    index = 0
    for disk_number in disk_numbers:
        if index + 4 > len(pool):
            occupied = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter in forbidden]
            raise RuntimeError(
                f"可用盘符不足，无法为硬盘 {disk_number} 分配 4 个盘符，"
                f"被占用: {occupied}"
            )
        letters = pool[index:index + 4]
        allocations[disk_number] = {
            "efi": letters[0],
            "windows": letters[1],
            "data1": letters[2],
            "data2": letters[3],
        }
        index += 4
    return allocations



def parse_worker_drive_letters(text: str) -> dict[str, str]:
    letters = [part.strip().upper() for part in text.split(",")]
    if len(letters) != 4:
        raise ValueError(f"盘符参数必须为 4 个字母（EFI,Windows,Data1,Data2），实际为: {text}")
    for letter in letters:
        if len(letter) != 1 or not letter.isalpha():
            raise ValueError(f"盘符必须为单个字母: {letter}")
    return {
        "efi": letters[0],
        "windows": letters[1],
        "data1": letters[2],
        "data2": letters[3],
    }



def build_forbidden_drive_letters(
    disk_summaries: list[dict[str, Any]],
    selected_disk_numbers: list[int],
    used_letters: list[str],
) -> list[str]:
    selected_own: set[str] = set()
    selected_set = set(selected_disk_numbers)
    for disk in disk_summaries:
        if disk.get("disk_number") in selected_set:
            for letter in disk.get("drive_letters") or []:
                selected_own.add(str(letter).upper())
    return [str(letter).upper() for letter in used_letters if str(letter).upper() not in selected_own]



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
    required_paths = {
        "Ghost 可执行文件路径 (gho_exe)": (config_payload.get("software_paths") or {}).get("ghost64_path"),
        "Ghost 镜像文件路径 (win_gho)": (config_payload.get("image_info") or {}).get("image_path"),
        "目录拷贝源路径 (software_file)": (config_payload.get("copy_info") or {}).get("source_dir"),
        "bcdboot 可执行文件路径 (bcd_exe)": (config_payload.get("software_paths") or {}).get("bcdboot_path"),
    }
    for name, value in required_paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"配置中缺少 {name}")
        if not Path(value).exists():
            raise ConfigError(f"{name} 不存在: {value}")



def quote_powershell_argument(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"



def run_single_disk_flow(
    disk_number: int,
    config_payload: dict[str, Any],
    expected_identity: dict[str, Any] | None = None,
    drive_letters: dict[str, str] | None = None,
) -> None:
    excluded_disk_names = config_payload.get("excluded_disk_names") or []
    disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names)
    disk = find_disk_summary(disk_summaries, disk_number)
    if not disk:
        raise DiskProtectedError(f"未找到目标硬盘: {disk_number}")
    if disk.get("is_selectable") is False:
        reasons = "，".join(disk.get("protection_reasons") or [])
        raise DiskProtectedError(f"目标硬盘不可操作: {disk_number}，原因: {reasons or '受保护'}")
    validate_worker_disk_identity(disk, expected_identity)
    validate_required_paths(config_payload)

    print(build_target_disk_text(disk))
    print(f"开始处理硬盘 {disk_number}: {disk.get('model') or '未知'}")
    initialize_results = initialize_disks([disk_number])
    print_initialize_results(initialize_results)
    if not all(result.get("passed") for result in initialize_results):
        raise InitializationError(f"硬盘初始化失败: {build_failed_result_message(initialize_results)}")

    validation_results = validate_initialized_disks(initialize_results)
    print_initialization_validation_results(validation_results)
    if not all(result.get("passed") for result in validation_results):
        raise ValidationError(f"初始化结果验证失败: {build_failed_result_message(validation_results)}")

    partition_results = partition_and_format_disks([disk_number], config_payload.get("partition_info") or {}, drive_letters=drive_letters)
    print_partition_results(partition_results)
    if not all(result.get("passed") for result in partition_results):
        raise PartitionError(f"硬盘分区和格式化失败: {build_failed_result_message(partition_results)}")

    partition_validation_results = validate_partitioned_disks([disk_number], config_payload.get("partition_info") or {}, drive_letters=drive_letters)
    print_partition_validation_results(partition_validation_results)
    if not all(result.get("passed") for result in partition_validation_results):
        raise ValidationError(f"分区和格式化结果验证失败: {build_failed_result_message(partition_validation_results)}")

    gho_exe = (config_payload.get("software_paths") or {}).get("ghost64_path")
    win_gho = (config_payload.get("image_info") or {}).get("image_path")
    if not gho_exe:
        raise ConfigError("配置中缺少 Ghost 可执行文件路径 (gho_exe)")
    if not win_gho:
        raise ConfigError("配置中缺少 Ghost 镜像文件路径 (win_gho)")

    windows_drive_letter = (drive_letters or {}).get("windows") or (partition_results[0].get("partitions") or {}).get("c_drive_letter")
    if not windows_drive_letter:
        raise PartitionError("无法确定 Windows 分区盘符，Ghost 镜像写入中止")

    ghost_results = [write_ghost_image(gho_exe, win_gho, disk_number, windows_drive_letter)]
    print_ghost_results(ghost_results)
    if not all(result.get("passed") for result in ghost_results):
        raise GhostError(f"Ghost 镜像写入失败: {build_failed_result_message(ghost_results)}")

    source_dir = (config_payload.get("copy_info") or {}).get("source_dir")
    data1_drive_letter = (drive_letters or {}).get("data1") or (partition_results[0].get("partitions") or {}).get("d1_drive_letter")
    copy_results = [copy_directory(source_dir, data1_drive_letter, disk_number)]
    print_copy_results(copy_results)
    if not all(result.get("passed") for result in copy_results):
        raise CopyError(f"目录拷贝失败: {build_failed_result_message(copy_results)}")

    bcd_exe = (config_payload.get("software_paths") or {}).get("bcdboot_path")
    efi_drive_letter = (drive_letters or {}).get("efi") or (partition_results[0].get("partitions") or {}).get("efi_drive_letter")
    boot_results = [create_boot_record(bcd_exe, windows_drive_letter, efi_drive_letter, disk_number)]
    print_boot_results(boot_results)
    if not all(result.get("passed") for result in boot_results):
        raise BootError(f"引导记录创建失败: {build_failed_result_message(boot_results)}")

    print(f"硬盘 {disk_number} 当前阶段处理完成")



def normalize_worker_disk_identity(disk_identity: int | dict[str, Any]) -> dict[str, Any]:
    if isinstance(disk_identity, int):
        return {"disk_number": disk_identity}
    return dict(disk_identity)



def launch_worker_windows(
    disk_identities: list[int | dict[str, Any]],
    config_path: str | None = None,
    start_interval_seconds: float = 6.0,
    sleep_func: Sleeper | None = None,
) -> None:
    sleeper = sleep_func or time.sleep

    for index, raw_identity in enumerate(disk_identities):
        disk_identity = normalize_worker_disk_identity(raw_identity)
        disk_number = disk_identity.get("disk_number")
        if not isinstance(disk_number, int):
            raise ValueError(f"worker 硬盘编号必须为整数: {disk_number}")

        if getattr(sys, 'frozen', False):
            command_parts = [sys.executable, "--worker-disk", str(disk_number)]
        else:
            command_parts = ["py", str(PROJECT_ROOT / "app" / "main.py"), "--worker-disk", str(disk_number)]

        if config_path:
            command_parts.extend(["-j", config_path])
        if disk_identity.get("unique_id"):
            command_parts.extend(["--worker-unique-id", str(disk_identity.get("unique_id"))])
        if disk_identity.get("serial_number"):
            command_parts.extend(["--worker-serial-number", str(disk_identity.get("serial_number"))])
        if disk_identity.get("model"):
            command_parts.extend(["--worker-model", str(disk_identity.get("model"))])
        if isinstance(disk_identity.get("size_bytes"), int):
            command_parts.extend(["--worker-size-bytes", str(disk_identity.get("size_bytes"))])
        if disk_identity.get("drive_letters"):
            dl = disk_identity["drive_letters"]
            command_parts.extend(["--worker-drive-letters", f"{dl['efi']},{dl['windows']},{dl['data1']},{dl['data2']}"])

        env = os.environ.copy()

        quoted_command = (
            "$env:PYTHONUTF8 = '1'; "
            "$env:PYTHONIOENCODING = 'utf-8'; "
            "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "& "
            + " ".join(quote_powershell_argument(part) for part in command_parts)
        )
        subprocess.Popen(
            ["powershell", "-NoExit", "-NoProfile", "-Command", quoted_command],
            cwd=PROJECT_ROOT,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            env=env,
        )
        print(f"硬盘 {disk_number}: 已启动独立执行窗口")
        if index < len(disk_identities) - 1 and start_interval_seconds > 0:
            print(f"等待 {start_interval_seconds:g} 秒后启动下一个 worker")
            sleeper(start_interval_seconds)



def run_minimal_main_flow(
    input_func=input,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
    worker_launcher: WorkerLauncher | None = None,
) -> list[int]:
    runner = preflight_runner or run_preflight_checks
    preflight_report = runner(config_path)
    print_preflight_report(preflight_report)
    if not preflight_report.get("all_passed"):
        raise PreflightError("运行前检查失败")

    config_payload = preflight_report.get("config_payload")
    if not isinstance(config_payload, dict):
        raise ConfigError("运行前检查未返回可用的配置数据")

    excluded_disk_names = config_payload.get("excluded_disk_names") or []
    disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names)
    if not disk_summaries:
        raise DiskProtectedError("模块1未返回任何硬盘摘要信息")

    print("=" * 80)
    print("Sisp")
    print("=" * 80)
    print(f"配置文件: {config_payload.get('config_path')}")
    print_disk_summaries(disk_summaries)
    selected_disk_numbers = prompt_disk_selection(disk_summaries, input_func=input_func)
    if selected_disk_numbers:
        used_letters = scan_used_drive_letters()
        forbidden = build_forbidden_drive_letters(disk_summaries, selected_disk_numbers, used_letters)
    else:
        forbidden = []

    if len(selected_disk_numbers) > 1:
        launcher = worker_launcher or launch_worker_windows
        print("已选择多个硬盘，将为每个硬盘启动独立执行窗口")
        allocations = build_drive_letter_allocations(selected_disk_numbers, forbidden_letters=forbidden)
        identities = build_worker_disk_identities(disk_summaries, selected_disk_numbers)
        for identity in identities:
            identity["drive_letters"] = allocations.get(identity["disk_number"])
        launcher(identities, get_config_path_for_worker(config_payload))
        print()
        print("所有 worker 窗口已启动，请在各窗口中查看执行结果")
        input_func("按 Enter 键退出主程序...")
        return selected_disk_numbers

    if selected_disk_numbers:
        allocations = build_drive_letter_allocations(selected_disk_numbers, forbidden_letters=forbidden)
        run_single_disk_flow(selected_disk_numbers[0], config_payload, drive_letters=allocations.get(selected_disk_numbers[0]))

    return selected_disk_numbers



def run_worker_flow(
    disk_number: int,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
    expected_identity: dict[str, Any] | None = None,
    drive_letters: dict[str, str] | None = None,
) -> None:
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
    run_single_disk_flow(disk_number, config_payload, expected_identity, drive_letters=drive_letters)



def wait_for_exit() -> None:
    """等待用户按 Enter 键退出，仅在交互式终端中生效"""
    if sys.stdin.isatty():
        try:
            input("按 Enter 键退出...")
        except (EOFError, KeyboardInterrupt):
            pass



def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_command_line_args(argv)
        if args.worker_disk is not None:
            expected_identity = {
                "unique_id": args.worker_unique_id,
                "serial_number": args.worker_serial_number,
                "model": args.worker_model,
                "size_bytes": args.worker_size_bytes,
            }
            drive_letters = parse_worker_drive_letters(args.worker_drive_letters) if args.worker_drive_letters else None
            run_worker_flow(args.worker_disk, config_path=args.config_path, expected_identity=expected_identity, drive_letters=drive_letters)
            print()
            wait_for_exit()
            return ErrorCode.SUCCESS
        else:
            run_minimal_main_flow(config_path=args.config_path)
        return ErrorCode.SUCCESS
    except SispError as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        wait_for_exit()
        return exc.error_code
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        wait_for_exit()
        return ErrorCode.PREFLIGHT_FAILED


if __name__ == "__main__":
    # 修复管理员模式下控制台黑屏问题：刷新输出缓冲区
    os.environ['PYTHONUNBUFFERED'] = '1'
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    print(end="", flush=True)
    raise SystemExit(main())
