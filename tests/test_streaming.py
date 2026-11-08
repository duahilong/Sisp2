"""流式子进程：日志实时落盘、百分比解析、取消与超时。"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import plan_partitions
from app.application.phases.base import PhaseContext
from app.application.phases.ghost import GhostImagePhase
from app.application.runner_utils import RunnerError
from app.application.streaming import StreamOutcome, parse_percent, stream_process
from app.safety_guard import RealDiskBlocked

PAYLOAD = {
    "partition_info": {
        "efi_size_mb": 100,
        "c_size_gb": 3,
        "data_size_gb": "auto",
        "allow_small_partitions": True,
    },
    "image_info": {"image_path": "D:/images/test.GHO"},
}
LETTERS = {"efi": "E", "windows": "F", "data": "G"}


def test_parse_percent_takes_largest_and_clamps():
    assert parse_percent("  42% complete") == 42
    assert parse_percent("100%") == 100
    assert parse_percent("no progress here") is None
    assert parse_percent("150%") == 100


def test_stream_process_writes_lines_as_they_arrive(tmp_path):
    log = CommandLog(tmp_path, disk_number=2)
    lines: list[str] = []
    script = "import sys; print('  42% complete'); print('done'); sys.stdout.flush()"

    outcome = stream_process(
        [sys.executable, "-u", "-c", script],
        60,
        log=log,
        allow_real_disk=False,
        on_line=lines.append,
    )

    assert outcome.returncode == 0
    assert outcome.cancelled is False
    assert "42% complete" in outcome.output
    assert any("42%" in line for line in lines)
    # 日志在进程结束前就已经落盘（不是等命令跑完才写）。
    assert "42% complete" in log.summary()


def test_stream_process_cancels_quickly(tmp_path):
    log = CommandLog(None)
    cancel = threading.Event()
    script = "import time; time.sleep(30)"
    threading.Timer(0.4, cancel.set).start()
    started = time.monotonic()

    outcome = stream_process(
        [sys.executable, "-u", "-c", script],
        60,
        log=log,
        allow_real_disk=False,
        cancel=cancel,
    )
    elapsed = time.monotonic() - started

    assert outcome.cancelled is True
    assert elapsed < 15  # 远小于 30 s 的 sleep，说明进程真的被终止了


def test_stream_process_timeout_keeps_error_wording(tmp_path):
    log = CommandLog(None)
    with pytest.raises(RunnerError, match="命令超时"):
        stream_process(
            [sys.executable, "-u", "-c", "import time; time.sleep(10)"],
            0.6,
            log=log,
            allow_real_disk=False,
        )


def test_stream_process_still_passes_safety_guard(tmp_path):
    log = CommandLog(None)
    with pytest.raises(RealDiskBlocked):
        stream_process(
            ["diskpart.exe", "/s", "x.txt"],
            10,
            log=log,
            allow_real_disk=False,
        )


class _StreamingShell:
    """带流式能力的 Ghost 壳：按预设行回放，并回报 cancelled。"""

    def __init__(self, lines=(), code=0, cancelled=False):
        self.lines = list(lines)
        self.code = code
        self.cancelled = cancelled
        self.calls: list[dict] = []

    def run_streaming(self, command, timeout, *, env=None, cwd=None, cancel=None, on_line=None):
        self.calls.append(
            {"command": list(command), "timeout": timeout, "cwd": cwd, "cancel": cancel}
        )
        for line in self.lines:
            if on_line is not None:
                on_line(line)
        return StreamOutcome(
            returncode=None if self.cancelled else self.code,
            output="\n".join(self.lines),
            elapsed=0.1,
            cancelled=self.cancelled,
        )

    def run(self, command, timeout, *, env=None, cwd=None):  # pragma: no cover - 不应被调用
        raise AssertionError("有 run_streaming 时不应退回阻塞 run")


def _ctx(tmp_path, notified, *, cancel=None, on_step=None):
    gho = tmp_path / "test.GHO"
    gho.write_bytes(b"\xfe\xef" + b"\x00" * 8)
    parts = plan_partitions(PAYLOAD, letters=LETTERS)
    return PhaseContext(
        disk_number=2,
        parts=parts,
        letters=LETTERS,
        image_path=str(gho),
        cancel=cancel or threading.Event(),
        log=CommandLog(None),
        options={},
        on_step=on_step
        or (lambda key, status, message="", fraction=None: notified.append(
            (key, status, message, fraction)
        )),
    )


def test_ghost_phase_reports_fraction_from_output(tmp_path):
    notified: list[tuple] = []
    shell = _StreamingShell(["Ghost 15.0", "  42% complete", "  57% complete", "done"])
    phase = GhostImagePhase(shell, ghost="ghost64.exe")
    ctx = _ctx(tmp_path, notified)

    outcome = phase.run(ctx)

    assert outcome.ok is True
    fractions = [item[3] for item in notified if item[0] == "image" and item[3] is not None]
    assert 0.42 in fractions
    assert max(fractions) >= 0.57
    # 只前进不后退：57% 之后不会再报 42%。
    assert fractions == sorted(fractions)
    assert shell.calls and shell.calls[0]["cancel"] is ctx.cancel


def test_ghost_phase_cancelled_reports_touched_disk(tmp_path):
    shell = _StreamingShell(["  10% complete"], cancelled=True)
    phase = GhostImagePhase(shell, ghost="ghost64.exe")

    outcome = phase.run(_ctx(tmp_path, []))

    assert outcome.cancelled is True
    assert outcome.ok is False
    assert outcome.disk_touched is True
    assert "镜像写入被中止" in outcome.message


def test_notify_keeps_three_argument_callbacks(tmp_path):
    """老的三参 on_step 不能因为 fraction 参数而报错。"""
    seen: list[tuple] = []
    ctx = _ctx(
        tmp_path,
        [],
        on_step=lambda key, status, message="": seen.append((key, status, message)),
    )
    ctx.notify("image", "running", "应用镜像", fraction=0.5)
    assert seen == [("image", "running", "应用镜像")]
