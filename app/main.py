import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_info.service import scan_disk_summaries
from app.modules.disk_initializer.service import initialize_disks, print_initialize_results
from app.modules.disk_partitioner.service import partition_and_format_disks, print_partition_results
from app.modules.initialization_validator.service import print_initialization_validation_results, validate_initialized_disks
from app.modules.partition_validator.service import print_partition_validation_results, validate_partitioned_disks
from app.modules.user_interaction.service import apply_disk_protection, print_disk_summaries, prompt_disk_selection
from app.preflight import print_preflight_report, run_preflight_checks


PreflightRunner = Callable[[str | Path | None], dict[str, Any]]
WorkerLauncher = Callable[[list[int], str | None], None]



def parse_command_line_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("-j", dest="config_path", help="JSON 配置文件路径")
    parser.add_argument("--worker-disk", dest="worker_disk", type=int, help="worker 模式：处理指定硬盘编号，可由主程序自动启动，也可用于单盘调试")
    return parser.parse_args(argv)



def get_config_path_for_worker(config_payload: dict[str, Any]) -> str | None:
    config_path = config_payload.get("config_path")
    return str(config_path) if config_path else None



def find_disk_summary(disk_summaries: list[dict[str, Any]], disk_number: int) -> dict[str, Any] | None:
    for disk in disk_summaries:
        if disk.get("disk_number") == disk_number:
            return disk
    return None



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



def quote_powershell_argument(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"



def run_single_disk_flow(disk_number: int, config_payload: dict[str, Any]) -> None:
    excluded_disk_names = config_payload.get("excluded_disk_names") or []
    disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names)
    disk = find_disk_summary(disk_summaries, disk_number)
    if not disk:
        raise RuntimeError(f"未找到目标硬盘: {disk_number}")
    if disk.get("is_selectable") is False:
        reasons = "，".join(disk.get("protection_reasons") or [])
        raise RuntimeError(f"目标硬盘不可操作: {disk_number}，原因: {reasons or '受保护'}")

    print(build_target_disk_text(disk))
    print(f"开始处理硬盘 {disk_number}: {disk.get('model') or '未知'}")
    initialize_results = initialize_disks([disk_number])
    print_initialize_results(initialize_results)
    if not all(result.get("passed") for result in initialize_results):
        raise RuntimeError("硬盘初始化失败")

    validation_results = validate_initialized_disks(initialize_results)
    print_initialization_validation_results(validation_results)
    if not all(result.get("passed") for result in validation_results):
        raise RuntimeError("初始化结果验证失败")

    partition_results = partition_and_format_disks([disk_number], config_payload.get("partition_info") or {})
    print_partition_results(partition_results)
    if not all(result.get("passed") for result in partition_results):
        raise RuntimeError("硬盘分区和格式化失败")

    partition_validation_results = validate_partitioned_disks([disk_number], config_payload.get("partition_info") or {})
    print_partition_validation_results(partition_validation_results)
    if not all(result.get("passed") for result in partition_validation_results):
        raise RuntimeError("分区和格式化结果验证失败")

    print(f"硬盘 {disk_number} 当前阶段处理完成")



def launch_worker_windows(disk_numbers: list[int], config_path: str | None = None) -> None:
    for disk_number in disk_numbers:
        command_parts = [
            "py",
            str(PROJECT_ROOT / "app" / "main.py"),
            "--worker-disk",
            str(disk_number),
        ]
        if config_path:
            command_parts.extend(["-j", config_path])

        quoted_command = "& " + " ".join(quote_powershell_argument(part) for part in command_parts)
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", quoted_command],
            cwd=PROJECT_ROOT,
        )
        print(f"硬盘 {disk_number}: 已启动独立执行窗口")



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
        raise RuntimeError("运行前检查失败")

    config_payload = preflight_report.get("config_payload")
    if not isinstance(config_payload, dict):
        raise RuntimeError("运行前检查未返回可用的配置数据")

    excluded_disk_names = config_payload.get("excluded_disk_names") or []
    disk_summaries = apply_disk_protection(scan_disk_summaries(), excluded_disk_names)
    if not disk_summaries:
        raise RuntimeError("模块1未返回任何硬盘摘要信息")

    print("=" * 80)
    print("Sisp 当前演示入口")
    print("=" * 80)
    print(f"配置文件: {config_payload.get('config_path')}")
    print_disk_summaries(disk_summaries)
    selected_disk_numbers = prompt_disk_selection(disk_summaries, input_func=input_func)
    if len(selected_disk_numbers) > 1:
        launcher = worker_launcher or launch_worker_windows
        print("已选择多个硬盘，将为每个硬盘启动独立执行窗口")
        launcher(selected_disk_numbers, get_config_path_for_worker(config_payload))
        return selected_disk_numbers

    if selected_disk_numbers:
        run_single_disk_flow(selected_disk_numbers[0], config_payload)

    return selected_disk_numbers



def run_worker_flow(
    disk_number: int,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
) -> None:
    runner = preflight_runner or run_preflight_checks
    preflight_report = runner(config_path)
    print_preflight_report(preflight_report)
    if not preflight_report.get("all_passed"):
        raise RuntimeError("运行前检查失败")

    config_payload = preflight_report.get("config_payload")
    if not isinstance(config_payload, dict):
        raise RuntimeError("运行前检查未返回可用的配置数据")

    print("=" * 80)
    print(f"Sisp Worker 硬盘 {disk_number}")
    print("=" * 80)
    print(f"配置文件: {config_payload.get('config_path')}")
    run_single_disk_flow(disk_number, config_payload)



def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_command_line_args(argv)
        if args.worker_disk is not None:
            run_worker_flow(args.worker_disk, config_path=args.config_path)
        else:
            run_minimal_main_flow(config_path=args.config_path)
        return 0
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
