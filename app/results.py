"""worker 结果文件读写与主窗口汇总。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.models import WorkerResult
from app.modules.common.constants import ErrorCode

DEFAULT_WORKER_RESULT_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def write_worker_result(result_file: str | Path, result: WorkerResult) -> bool:
    """原子写入 worker 结果文件；失败时只记录日志，不影响主流程退出码。"""
    path = Path(result_file)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        get_logger("result").warning("写入 worker 结果失败: %s: %s", path, exc)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def read_worker_result(result_file: str | Path) -> WorkerResult | None:
    path = Path(result_file)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        get_logger("result").warning("读取 worker 结果失败: %s: %s", path, exc)
        return None

    if not isinstance(payload, dict):
        get_logger("result").warning("worker 结果根节点不是对象: %s", path)
        return None
    try:
        return WorkerResult.from_dict(payload)
    except (TypeError, ValueError) as exc:
        get_logger("result").warning("worker 结果字段无效: %s: %s", path, exc)
        return None


def wait_for_worker_results(
    launched_workers: Iterable[Any],
    timeout: float | None = DEFAULT_WORKER_RESULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep_func: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> list[WorkerResult]:
    """轮询 worker 结果文件，超时后为未完成的 worker 生成失败结果。"""
    sleeper = sleep_func or time.sleep
    now = clock or time.monotonic
    launched = list(launched_workers)
    deadline = None if timeout is None else now() + timeout

    results: dict[int, WorkerResult] = {}
    workers_by_number = {int(worker.disk_number): worker for worker in launched}
    pending: dict[int, Path] = {
        int(worker.disk_number): Path(worker.result_file)
        for worker in launched
    }

    while pending:
        for disk_number, result_file in list(pending.items()):
            result = read_worker_result(result_file)
            if result is not None:
                results[disk_number] = result
                del pending[disk_number]
                continue

            process = getattr(workers_by_number.get(disk_number), "process", None)
            if process is not None and process.poll() is not None:
                results[disk_number] = WorkerResult(
                    disk_number=disk_number,
                    success=False,
                    error_code=ErrorCode.WORKER_RESULT_MISSING,
                    message=f"worker 进程已退出但未生成结果（退出码 {process.returncode}）",
                )
                del pending[disk_number]

        if not pending:
            break
        if deadline is not None and now() >= deadline:
            break
        sleeper(poll_interval)

    for disk_number, result_file in pending.items():
        message = f"等待 worker 结果超时或结果文件不可用: {result_file}"
        results[disk_number] = WorkerResult(
            disk_number=disk_number,
            success=False,
            error_code=ErrorCode.WORKER_RESULT_MISSING,
            message=message,
        )

    return [results[number] for number in sorted(results)]


def build_worker_summary(results: list[WorkerResult]) -> str:
    lines = [
        "=" * 80,
        "Sisp 执行结果汇总",
        "=" * 80,
    ]
    for result in results:
        status = "成功" if result.success else f"失败 [错误码 {result.error_code}]"
        lines.append(f"硬盘 {result.disk_number}: {status} - {result.message}")
        if result.steps:
            step_text = "，".join(
                f"{step.name}={'通过' if step.passed else '失败'}" for step in result.steps
            )
            lines.append(f"  步骤: {step_text}")
        if result.log_path:
            lines.append(f"  日志: {result.log_path}")

    success_count = sum(1 for result in results if result.success)
    failed_count = len(results) - success_count
    lines.append("-" * 80)
    lines.append(f"合计: {len(results)} 块 | 成功 {success_count} | 失败 {failed_count}")
    return "\n".join(lines)


def print_worker_summary(results: list[WorkerResult]) -> None:
    print(build_worker_summary(results))
