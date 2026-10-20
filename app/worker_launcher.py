"""多硬盘 worker 进程启动。

使用独立的 Python 进程和控制台窗口，不再通过 PowerShell 包裹 Python 命令，
减少引号转义和环境差异；worker 结果写入文件，供主窗口统一汇总。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.paths import PROJECT_ROOT

Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class LaunchedWorker:
    disk_number: int
    result_file: Path
    process: Any = None


def normalize_worker_disk_identity(disk_identity: int | dict[str, Any]) -> dict[str, Any]:
    if isinstance(disk_identity, int):
        return {"disk_number": disk_identity}
    return dict(disk_identity)


def build_worker_command(
    disk_identity: dict[str, Any],
    config_path: str | None = None,
    result_file: str | Path | None = None,
    log_dir: str | Path | None = None,
    no_log: bool = False,
) -> list[str]:
    disk_number = disk_identity.get("disk_number")
    if not isinstance(disk_number, int):
        raise ValueError(f"worker 硬盘编号必须为整数: {disk_number}")

    if getattr(sys, "frozen", False):
        command_parts = [sys.executable, "--worker-disk", str(disk_number)]
    else:
        command_parts = [
            sys.executable,
            str(PROJECT_ROOT / "app" / "main.py"),
            "--worker-disk",
            str(disk_number),
        ]

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
        letters = disk_identity["drive_letters"]
        command_parts.extend(
            ["--worker-drive-letters", f"{letters['efi']},{letters['windows']},{letters['data1']},{letters['data2']}"]
        )
    if result_file:
        command_parts.extend(["--worker-result-file", str(result_file)])
    if log_dir:
        command_parts.extend(["--log-dir", str(log_dir)])
    if no_log:
        command_parts.append("--no-log")

    return command_parts


def create_result_dir(result_dir: str | Path | None = None) -> Path:
    if result_dir:
        path = Path(result_dir)
    else:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = Path(tempfile.gettempdir()) / "Sisp" / "results" / f"{timestamp}_{os.getpid()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_worker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def launch_worker_windows(
    disk_identities: list[int | dict[str, Any]],
    config_path: str | None = None,
    start_interval_seconds: float = 6.0,
    sleep_func: Sleeper | None = None,
    result_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    no_log: bool = False,
) -> list[LaunchedWorker]:
    """为每块硬盘启动独立 worker 窗口，并返回句柄与结果文件路径。"""
    sleeper = sleep_func or time.sleep
    run_dir = create_result_dir(result_dir)
    launched: list[LaunchedWorker] = []

    for index, raw_identity in enumerate(disk_identities):
        disk_identity = normalize_worker_disk_identity(raw_identity)
        disk_number = int(disk_identity["disk_number"])
        result_file = run_dir / f"disk_{disk_number}.json"
        try:
            result_file.unlink(missing_ok=True)
        except OSError:
            pass

        command_parts = build_worker_command(
            disk_identity,
            config_path,
            result_file,
            log_dir=log_dir,
            no_log=no_log,
        )
        process = subprocess.Popen(
            command_parts,
            cwd=PROJECT_ROOT,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            env=build_worker_env(),
        )
        launched.append(LaunchedWorker(disk_number=disk_number, result_file=result_file, process=process))
        print(f"硬盘 {disk_number}: 已启动独立执行窗口")
        print(f"硬盘 {disk_number}: 结果文件 {result_file}")

        if index < len(disk_identities) - 1 and start_interval_seconds > 0:
            print(f"等待 {start_interval_seconds:g} 秒后启动下一个 worker")
            sleeper(start_interval_seconds)

    return launched
