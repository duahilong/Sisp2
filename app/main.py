"""Sisp 入口。

实际流程在 ``app.orchestrator``，命令行解析在 ``app.cli``。这里保留一层
轻量 re-export，兼容旧的导入方式（例如 ``from app.main import ...``）。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cli import main, parse_command_line_args, wait_for_exit
from app.disk_safety import normalize_identity_value
from app.drive_letters import (
    AVAILABLE_DRIVE_LETTERS,
    build_drive_letter_allocations,
    build_forbidden_drive_letters,
    parse_worker_drive_letters,
)
from app.orchestrator import (
    build_failed_result_message,
    build_target_disk_text,
    build_worker_disk_identities,
    build_worker_disk_identity,
    find_disk_summary,
    get_config_path_for_worker,
    run_minimal_main_flow,
    run_single_disk_flow,
    run_worker_flow,
    validate_partition_plan_before_initialize,
    validate_required_paths,
    validate_worker_disk_identity,
)
from app.paths import get_app_dir
from app.version import __version__
from app.worker_launcher import launch_worker_windows

__all__ = [
    "AVAILABLE_DRIVE_LETTERS",
    "build_drive_letter_allocations",
    "build_failed_result_message",
    "build_forbidden_drive_letters",
    "build_target_disk_text",
    "build_worker_disk_identities",
    "build_worker_disk_identity",
    "find_disk_summary",
    "get_app_dir",
    "get_config_path_for_worker",
    "launch_worker_windows",
    "main",
    "normalize_identity_value",
    "parse_command_line_args",
    "parse_worker_drive_letters",
    "run_minimal_main_flow",
    "run_single_disk_flow",
    "run_worker_flow",
    "validate_partition_plan_before_initialize",
    "validate_required_paths",
    "validate_worker_disk_identity",
    "wait_for_exit",
    "__version__",
]


if __name__ == "__main__":
    # 修复管理员模式下控制台黑屏问题：刷新输出缓冲区
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    print(end="", flush=True)
    raise SystemExit(main())
