"""CLI 结果展示的回归测试（纯内存，不触碰磁盘）。

覆盖两处历史缺陷：
1. 步骤状态读错属性（``StepResult`` 没有 ``passed``），失败步骤被显示成“完成”；
2. 执行日志路径实际挂在最后一个步骤的 ``details`` 里，却从 ``result.log_path`` 读取。
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from app.cli import (
    Reporter,
    _print_jobs,
    _print_step_records,
    _result_log_path,
    create_controller,
    main,
)
from app.models import STEP_FAILED, STEP_OK, STEP_SKIPPED, StepResult, WorkerResult


def _result() -> WorkerResult:
    return WorkerResult(
        disk_number=1,
        success=False,
        error="diskpart 失败",
        steps=(
            StepResult(key="efi", title="创建 EFI 分区", status=STEP_OK, message="已创建"),
            StepResult(
                key="windows",
                title="创建 Windows 分区",
                status=STEP_FAILED,
                message="容量不足",
            ),
            StepResult(
                key="boot",
                title="写入 UEFI 引导",
                status=STEP_SKIPPED,
                message="前置步骤失败",
            ),
            StepResult(
                key="verify",
                title="校验分区结果",
                status=STEP_FAILED,
                message="未通过",
                details={"log_path": r"C:\logs\disk-runner.log"},
            ),
        ),
    )


def test_print_step_records_uses_step_status(capsys) -> None:
    _print_step_records(Reporter(), _result())

    out = capsys.readouterr().out
    assert "[完成] 创建 EFI 分区" in out
    assert "[失败] 创建 Windows 分区" in out
    assert "[跳过] 写入 UEFI 引导" in out
    assert "容量不足" in out


def test_result_log_path_reads_last_step_details() -> None:
    assert _result_log_path(_result()) == r"C:\logs\disk-runner.log"


def test_print_jobs_reports_log_path_and_error(capsys) -> None:
    job = SimpleNamespace(disk_number=1, result=_result(), status="failed", succeeded=False)

    _print_jobs(Reporter(), [job])

    out = capsys.readouterr().out
    assert "日志：C:\\logs\\disk-runner.log" in out
    assert "原因：diskpart 失败" in out


def test_create_controller_passes_log_dir(tmp_path) -> None:
    controller = create_controller(
        dry_run=True,
        test_mode=False,
        allow_disks=None,
        log_dir=str(tmp_path),
    )

    assert controller._log_dir == str(tmp_path)


def test_create_controller_falls_back_to_settings_log_dir(tmp_path) -> None:
    """设置里配了 logDir，CLI 没给 --log-dir 时也要写日志到那里。"""
    controller = create_controller(
        dry_run=True,
        test_mode=False,
        allow_disks=None,
        settings={"logDir": str(tmp_path), "logRetentionDays": 30},
    )

    assert controller._log_dir == str(tmp_path)


def test_create_controller_prefers_explicit_log_dir(tmp_path) -> None:
    explicit = tmp_path / "explicit"
    controller = create_controller(
        dry_run=True,
        test_mode=False,
        allow_disks=None,
        log_dir=str(explicit),
        settings={"logDir": str(tmp_path / "from-settings")},
    )

    assert controller._log_dir == str(explicit)


def test_create_controller_applies_security_settings() -> None:
    controller = create_controller(
        dry_run=True,
        test_mode=False,
        allow_disks=None,
        settings={"protectSystemDisk": False, "blockUnverified": False},
    )

    assert controller._protect_system_disk is False
    assert controller._block_unverified is False


@pytest.mark.skipif(os.name != "nt", reason="--list 会调用 PowerShell 扫描硬盘")
def test_cli_list_json_uses_settings_log_dir(tmp_path, capsys) -> None:
    log_dir = tmp_path / "logs"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"version": 1, "settings": {"logDir": str(log_dir)}}),
        encoding="utf-8",
    )

    code = main(["--list", "--json", "--settings", str(settings_file)])

    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["log_dir"] == str(log_dir)
    # --list 只列硬盘，不该声称会写盘
    assert payload["would_execute"] is False
