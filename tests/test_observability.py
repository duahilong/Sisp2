import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.modules.common.service as common_service
from app.logging_setup import configure_logging, log_powershell, log_step, reset_logging
from app.models import StepRecord, WorkerResult
from app.results import (
    build_worker_summary,
    read_worker_result,
    wait_for_worker_results,
    write_worker_result,
)


def test_configure_logging_writes_powershell_details(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path, run_id="pytest")
    assert log_path is not None

    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="stdout-data", stderr="stderr-data")
    log_powershell("Get-Disk -Number 2", completed, 0.25)
    log_step(2, "初始化", False, "初始化失败", 0.1)
    reset_logging()

    text = log_path.read_text(encoding="utf-8")
    assert "Get-Disk -Number 2" in text
    assert "returncode=1" in text
    assert "stderr-data" in text
    assert "硬盘 2 初始化失败" in text


def test_run_powershell_uses_timeout_and_handles_os_error(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(common_service.subprocess, "run", fake_run)
    completed = common_service.run_powershell("Write-Output ok", timeout=7)
    assert completed.returncode == 0
    assert calls[0]["timeout"] == 7

    def broken_run(args, **kwargs):
        raise OSError("powershell missing")

    monkeypatch.setattr(common_service.subprocess, "run", broken_run)
    failed = common_service.run_powershell("Write-Output ok", timeout=7)
    assert failed.returncode == 1
    assert "powershell missing" in failed.stderr


def test_worker_result_roundtrip(tmp_path: Path) -> None:
    result = WorkerResult(
        disk_number=2,
        success=False,
        error_code=3,
        message="分区失败",
        steps=(StepRecord(name="初始化", passed=True, message="完成", duration_seconds=1.25),),
        log_path="C:\\logs\\sisp.log",
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:00:02",
        dry_run=True,
    )
    path = tmp_path / "disk_2.json"
    assert write_worker_result(path, result)

    loaded = read_worker_result(path)
    assert loaded == result


def test_worker_result_summary() -> None:
    results = [
        WorkerResult(disk_number=2, success=True, error_code=0, message="完成", log_path="a.log"),
        WorkerResult(
            disk_number=3,
            success=False,
            error_code=3,
            message="分区失败",
            steps=(StepRecord(name="分区格式化", passed=False, message="失败"),),
        ),
    ]
    summary = build_worker_summary(results)
    assert "硬盘 2: 成功" in summary
    assert "硬盘 3: 失败 [错误码 3]" in summary
    assert "分区格式化=失败" in summary
    assert "日志: a.log" in summary
    assert "成功 1 | 失败 1" in summary


class FakeLaunchedWorker:
    def __init__(self, disk_number: int, result_file: Path):
        self.disk_number = disk_number
        self.result_file = result_file


def test_wait_for_worker_results_reads_files(tmp_path: Path) -> None:
    workers = []
    for disk_number in (2, 3):
        path = tmp_path / f"disk_{disk_number}.json"
        write_worker_result(
            path,
            WorkerResult(disk_number=disk_number, success=True, error_code=0, message="完成"),
        )
        workers.append(FakeLaunchedWorker(disk_number, path))

    results = wait_for_worker_results(workers, timeout=1, sleep_func=lambda _: None)
    assert [result.disk_number for result in results] == [2, 3]
    assert all(result.success for result in results)


def test_wait_for_worker_results_timeout(tmp_path: Path) -> None:
    workers = [FakeLaunchedWorker(9, tmp_path / "disk_9.json")]
    results = wait_for_worker_results(workers, timeout=0, sleep_func=lambda _: None)
    assert len(results) == 1
    assert results[0].disk_number == 9
    assert results[0].success is False
    assert results[0].error_code == 14


class FakeProcess:
    returncode = 1

    def poll(self):
        return self.returncode


def test_wait_for_worker_results_detects_exited_process_without_result(tmp_path: Path) -> None:
    worker = FakeLaunchedWorker(9, tmp_path / "disk_9.json")
    worker.process = FakeProcess()
    results = wait_for_worker_results([worker], timeout=10, sleep_func=lambda _: None)
    assert len(results) == 1
    assert results[0].error_code == 14
    assert "worker 进程已退出但未生成结果" in results[0].message
