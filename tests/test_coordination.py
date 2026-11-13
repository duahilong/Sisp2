"""多盘并发的协调件：分区轮次闸门、Ghost 启动间隔、diskpart 全局锁、日志文件名。

这些用例守护四条不变量（详见 ``app/application/coordination.py``）：

1. 分区阶段任意时刻最多一块盘，且必须按 ``set_order()`` 的顺序；
2. 任何退出路径（成功/失败/取消/异常）都要 ``done()``，否则后面的盘永久卡住；
3. 排队期间的取消要能被及时感知（不必等前一块盘跑完）；
4. 两个 Ghost 的启动间隔不小于 ``GhostStartGate.min_gap``。
"""

from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path

import pytest

from app.application.command_log import CommandLog
from app.application.coordination import GhostStartGate, PartitionSequencer
from app.application.shell import DiskpartShell


# ---------------------------------------------------------------- 顺序与互斥
def test_sequencer_follows_order_even_when_threads_start_reversed() -> None:
    seq = PartitionSequencer([1, 2, 3])
    entered: list[int] = []

    def worker(number: int) -> None:
        assert seq.enter(number, poll=0.01) is True
        entered.append(number)
        time.sleep(0.02)
        seq.done(number)

    threads = [threading.Thread(target=worker, args=(n,)) for n in (3, 2, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert entered == [1, 2, 3]


def test_sequencer_only_lets_head_enter() -> None:
    seq = PartitionSequencer([1, 2])
    assert seq.enter(1) is True

    result: dict[str, bool] = {}

    def wait_second() -> None:
        result["ok"] = seq.enter(2, poll=0.01)

    thread = threading.Thread(target=wait_second)
    thread.start()
    time.sleep(0.15)
    assert "ok" not in result, "第一块盘还没 done()，第二块不能进入"
    assert seq.snapshot()["running"] == 1

    seq.done(1)
    thread.join(3)
    assert result["ok"] is True
    assert seq.snapshot()["head"] == 2


def test_done_is_idempotent_and_releases_head() -> None:
    seq = PartitionSequencer([1, 2])
    seq.enter(1)
    seq.done(1)
    seq.done(1)  # 重复调用不能把顺序表搞坏
    assert seq.snapshot() == {"order": [1, 2], "head": 2, "running": None, "finished": [1]}
    assert seq.enter(2) is True
    seq.done(2)
    assert seq.snapshot()["head"] is None
    assert seq.snapshot()["finished"] == [1, 2]


def test_unknown_disk_passes_through() -> None:
    """没被 set_order() 收录的盘直接放行：单盘任务不该被闸门卡住。"""
    seq = PartitionSequencer([1])
    assert seq.enter(1) is True
    assert seq.enter(7) is True


def test_enter_returns_false_when_cancelled_while_queued() -> None:
    seq = PartitionSequencer([1, 2])
    assert seq.enter(1) is True
    cancel = threading.Event()
    result: dict[str, bool] = {}

    def wait_second() -> None:
        result["ok"] = seq.enter(2, cancel.is_set, poll=0.01)

    thread = threading.Thread(target=wait_second)
    thread.start()
    time.sleep(0.05)
    started = time.monotonic()
    cancel.set()
    thread.join(3)
    elapsed = time.monotonic() - started

    assert result["ok"] is False
    assert elapsed < 0.3, f"取消要在一个轮询周期内生效，实际 {elapsed:.2f}s"
    seq.done(1)


def test_running_disk_is_never_released_by_a_wait_timeout() -> None:
    """无论等多久都不能把仍在分区的盘当作完成；只能由 done() 释放。"""
    seq = PartitionSequencer([1, 2])
    assert seq.enter(1) is True
    result: dict[str, bool] = {}

    def wait_second() -> None:
        result["ok"] = seq.enter(2, poll=0.01)

    thread = threading.Thread(target=wait_second)
    thread.start()
    time.sleep(0.15)
    assert thread.is_alive()
    assert "ok" not in result
    assert seq.snapshot()["running"] == 1
    assert seq.snapshot()["finished"] == []

    seq.done(1)
    thread.join(3)
    assert result["ok"] is True
    assert seq.snapshot()["finished"] == [1]


# ---------------------------------------------------------------- Ghost 间隔
def test_ghost_gate_waits_remaining_gap() -> None:
    now = [0.0]
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(round(seconds, 3))
        now[0] += seconds

    gate = GhostStartGate(2.5, clock=lambda: now[0], sleep=fake_sleep)
    assert gate.wait() == 0.0, "第一次启动不需要等"
    now[0] += 1.0
    assert gate.wait() == pytest.approx(1.5)
    assert slept == [1.5]
    # 两次启动挨着发生：间隔必须重新算满 2.5 s。
    assert gate.wait() == pytest.approx(2.5)
    now[0] += 3.0
    assert gate.wait() == 0.0, "距上次启动已超过间隔，直接放行"


def test_ghost_gate_disabled() -> None:
    gate = GhostStartGate(0)
    assert gate.wait() == 0.0
    assert gate.wait() == 0.0


# ---------------------------------------------------------------- diskpart 锁
class _SlowProcess:
    """替身 diskpart 进程：每次调用占用 60 ms，记录起止时间。"""

    def __init__(self) -> None:
        self.spans: list[tuple[float, float]] = []
        self.commands: list[list[str]] = []

    def run(self, command, timeout, *, env=None, cwd=None, quiet=""):
        self.commands.append(list(command))
        start = time.monotonic()
        time.sleep(0.06)
        end = time.monotonic()
        self.spans.append((start, end))
        return 0, "ok", end - start


def test_diskpart_calls_are_serialized(tmp_path: Path) -> None:
    process = _SlowProcess()
    shell = DiskpartShell(
        log=CommandLog(str(tmp_path)),
        diskpart="diskpart.exe",
        process=process,  # type: ignore[arg-type]
    )

    threads = [
        threading.Thread(target=shell.run_diskpart, args=(["select disk 1", "exit"],))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(process.spans) == 3
    spans = sorted(process.spans)
    for previous, current in itertools.pairwise(spans):
        assert current[0] >= previous[1] - 1e-6, "diskpart 调用有重叠"


# ---------------------------------------------------------------- 日志文件名
def test_command_log_names_are_unique_per_task(tmp_path: Path) -> None:
    first = CommandLog(str(tmp_path))
    second = CommandLog(str(tmp_path))
    assert first.path is not None and second.path is not None
    assert first.path != second.path
    assert first.path.name.startswith("disk-runner-")
