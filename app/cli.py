"""命令行入口与轻量诊断命令。"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from app.application.service import SispService
from app.drive_letters import parse_worker_drive_letters
from app.logging_setup import configure_logging, get_logger, reset_logging
from app.modules.common.constants import ErrorCode
from app.modules.common.service import ConfigError, SispError
from app.modules.config_loader.service import load_config
from app.modules.disk_info.service import scan_disk_summaries
from app.modules.user_interaction.service import print_disk_summaries
from app.orchestrator import (
    run_minimal_main_flow,
    run_worker_flow,
)
from app.results import DEFAULT_WORKER_RESULT_TIMEOUT_SECONDS
from app.version import __version__


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sisp 磁盘处理工具")
    parser.add_argument("-j", dest="config_path", help="JSON 配置文件路径")
    parser.add_argument("--version", action="version", version=f"Sisp {__version__}")
    parser.add_argument("--list-disks", action="store_true", help="只列出硬盘信息，不执行任何写盘操作")
    parser.add_argument("--check-config", action="store_true", help="检查配置文件及关键路径，不执行写盘操作")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的步骤/命令，不执行写盘操作")
    parser.add_argument("--log-dir", dest="log_dir", help="日志目录（默认 %%LOCALAPPDATA%%\\Sisp\\logs）")
    parser.add_argument("--no-log", action="store_true", help="不写文件日志")
    parser.add_argument("--worker-interval", dest="worker_interval", type=float, default=6.0, help="多盘 worker 启动间隔秒数")
    parser.add_argument("--result-dir", dest="result_dir", help="worker 结果文件目录")
    parser.add_argument(
        "--worker-result-timeout",
        dest="worker_result_timeout",
        type=float,
        default=DEFAULT_WORKER_RESULT_TIMEOUT_SECONDS,
        help="等待 worker 结果的最长秒数",
    )
    parser.add_argument("--worker-disk", dest="worker_disk", type=int, help="worker 模式：处理指定硬盘编号，可由主程序自动启动，也可用于单盘调试")
    parser.add_argument("--worker-unique-id", dest="worker_unique_id", help="worker 模式：目标硬盘 UniqueId，用于二次确认硬盘身份")
    parser.add_argument("--worker-serial-number", dest="worker_serial_number", help="worker 模式：目标硬盘 SerialNumber，用于二次确认硬盘身份")
    parser.add_argument("--worker-model", dest="worker_model", help="worker 模式：目标硬盘型号，用于二次确认硬盘身份")
    parser.add_argument("--worker-size-bytes", dest="worker_size_bytes", type=int, help="worker 模式：目标硬盘容量字节数，用于二次确认硬盘身份")
    parser.add_argument("--worker-drive-letters", dest="worker_drive_letters", help="worker 模式：分配的盘符列表（逗号分隔，如 E,F,G,H），分别对应 EFI、Windows、Data1、Data2")
    parser.add_argument("--worker-result-file", dest="worker_result_file", help="worker 模式：执行结果 JSON 文件路径")
    return parser


def parse_command_line_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def wait_for_exit() -> None:
    """等待用户按 Enter 键退出，仅在交互式终端中生效。"""
    if sys.stdin.isatty():
        try:
            input("按 Enter 键退出...")
        except (EOFError, KeyboardInterrupt):
            pass


def _build_service() -> SispService:
    """构造 CLI 使用的服务，保留模块级 monkeypatch 的兼容性。"""
    return SispService(
        config_loader=lambda path: load_config(path),
        disk_scanner=lambda: scan_disk_summaries(),
    )


def check_config(config_path: str | Path | None = None) -> int:
    result = _build_service().check_config(config_path)
    if not result.passed:
        print(f"配置检查失败: {result.message}", file=sys.stderr)
        return result.error_code

    payload = result.payload or {}
    partition_info = payload.get("partition_info") or {}
    print("配置检查通过")
    print(f"配置文件: {payload.get('config_path')}")
    print(f"EFI 分区: {partition_info.get('efi_size_mb')} MB")
    print(f"C 分区: {partition_info.get('c_size_gb')} GB")
    print(f"Ghost: {(payload.get('software_paths') or {}).get('ghost64_path')}")
    print(f"镜像: {(payload.get('image_info') or {}).get('image_path')}")
    print(f"拷贝源: {(payload.get('copy_info') or {}).get('source_dir')}")
    print(f"bcdboot: {(payload.get('software_paths') or {}).get('bcdboot_path')}")
    return ErrorCode.SUCCESS


def list_disks(config_path: str | Path | None = None) -> int:
    try:
        result = _build_service().list_disks(config_path, ignore_config_errors=True)
    except Exception as exc:
        print(f"硬盘信息扫描失败: {exc}", file=sys.stderr)
        return ErrorCode.PREFLIGHT_FAILED

    if result.warning:
        print(result.warning, file=sys.stderr)

    print("=" * 80)
    print("Sisp - 硬盘列表（只读）")
    print("=" * 80)
    print_disk_summaries([disk.to_dict() for disk in result.disks])
    return ErrorCode.SUCCESS


def _configure_file_logging(args: argparse.Namespace) -> None:
    if args.no_log:
        reset_logging()
        return
    log_path = configure_logging(args.log_dir)
    if log_path:
        print(f"日志文件: {log_path}")


def _run_worker(args: argparse.Namespace) -> int:
    expected_identity = {
        "unique_id": args.worker_unique_id,
        "serial_number": args.worker_serial_number,
        "model": args.worker_model,
        "size_bytes": args.worker_size_bytes,
    }
    try:
        drive_letters = parse_worker_drive_letters(args.worker_drive_letters) if args.worker_drive_letters else None
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    run_worker_flow(
        args.worker_disk,
        config_path=args.config_path,
        expected_identity=expected_identity,
        drive_letters=drive_letters,
        result_file=args.worker_result_file,
        dry_run=args.dry_run,
    )
    print()
    wait_for_exit()
    return ErrorCode.SUCCESS


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_command_line_args(argv)
        _configure_file_logging(args)

        if args.check_config:
            exit_code = check_config(args.config_path)
            wait_for_exit()
            return exit_code
        if args.list_disks:
            exit_code = list_disks(args.config_path)
            wait_for_exit()
            return exit_code

        if args.worker_disk is not None:
            return _run_worker(args)

        run_minimal_main_flow(
            config_path=args.config_path,
            worker_interval=args.worker_interval,
            result_dir=args.result_dir,
            worker_result_timeout=args.worker_result_timeout,
            dry_run=args.dry_run,
            log_dir=args.log_dir,
            no_log=args.no_log,
        )
        wait_for_exit()
        return ErrorCode.SUCCESS
    except KeyboardInterrupt:
        print("\n操作已取消", file=sys.stderr)
        return ErrorCode.CANCELLED
    except SispError as exc:
        get_logger("cli").warning("运行失败: %s", exc)
        print(f"运行失败: {exc}", file=sys.stderr)
        wait_for_exit()
        return exc.error_code
    except Exception as exc:
        get_logger("cli").exception("未预期异常")
        traceback.print_exc()
        print(f"运行失败: {exc}", file=sys.stderr)
        wait_for_exit()
        return ErrorCode.UNEXPECTED_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
