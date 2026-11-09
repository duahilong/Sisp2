"""多盘并发：分区组串行、系统组并行、取消与失败放行、Ghost 工作目录隔离。

这些用例用替身阶段跑真实线程，时间窗由 ``spans`` 断言——不碰任何磁盘命令。
真实写盘路径的端到端验证在 VM 上做（见 ``Docs/多盘并发改造实施文档.md``）。
"""

from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path

from app.application.command_log import CommandLog
from app.application.coordination import PartitionSequencer
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.pipeline import JobPipeline
from app.application.real_runner import WindowsDiskRunner
from tests.fakes import FakeDiskpartShell, FakeProcess


class _Spans:
    """线程安全的时间窗记录器。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, float, float]] = []
        self._lock = threading.Lock()

    def add(self, group: str, disk: int, start: float, end: float) -> None:
        with self._lock:
            self.items.append((group, disk, start, end))

    def of(self, group: str) -> list[tuple[int, float, float]]:
        with self._lock:
            rows = [(disk, start, end) for name, disk, start, end in self.items if name == group]
        return sorted(rows, key=lambda row: row[1])


class _FakePhase:
    """按组产出结果的替身阶段，跑 ``delay`` 秒并记录时间窗。"""

    def __init__(
        self,
        spans: _Spans,
        group: str,
        *,
        delay: float = 0.06,
        ok: bool = True,
        failing_key: str = "efi",
    ) -> None:
        self.spans = spans
        self.group = group
        self.delay = delay
        self.ok = ok
        self.failing_key = failing_key

    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        start = time.monotonic()
        time.sleep(self.delay)
        end = time.monotonic()
        self.spans.add(self.group, ctx.disk_number, start, end)
        if self.ok:
            key = "efi" if self.group == "partition" else "image"
            return PhaseOutcome.succeeded(PhaseResult.ok(key, "ok"))
        return PhaseOutcome.failed(
            PhaseResult.failed(self.failing_key, "boom"), message="boom"
        )


def _ctx(disk: int, cancel: threading.Event | None = None) -> PhaseContext:
    return PhaseContext(
        disk_number=disk,
        parts=(),
        letters={},
        image_path=None,
        cancel=cancel or threading.Event(),
        log=CommandLog(None),
    )


def _run_all(pipelines: list[JobPipeline], disks: list[int]) -> list[object]:
    results: list[object] = [None] * len(pipelines)

    def worker(index: int) -> None:
        results[index] = pipelines[index].run(_ctx(disks[index]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(pipelines))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return results


# ---------------------------------------------------------------- 核心流水线
def test_partition_serial_but_system_overlaps() -> None:
    spans = _Spans()
    sequencer = PartitionSequencer([1, 2, 3])
    pipelines = [
        JobPipeline(
            partition=_FakePhase(spans, "partition"),
            system=_FakePhase(spans, "system", delay=0.12),
            sequencer=sequencer,
        )
        for _ in range(3)
    ]

    results = _run_all(pipelines, [1, 2, 3])
    assert all(getattr(result, "ok", False) for result in results)

    parts = spans.of("partition")
    assert [disk for disk, _s, _e in parts] == [1, 2, 3], "必须按盘号顺序分区"
    for previous, current in itertools.pairwise(parts):
        assert current[1] >= previous[2] - 1e-6, f"分区阶段出现重叠：{parts}"

    systems = spans.of("system")
    assert len(systems) == 3
    overlapped = any(
        later[1] < earlier[2] for earlier, later in itertools.pairwise(systems)
    )
    assert overlapped, "系统组（Ghost/引导）应该并行"

    # 盘 2 的系统组必须早于盘 3 的分区结束开始（流水线的意义）
    assert systems[0][1] < parts[-1][2], "第一块盘的系统组应该与后面的分区重叠"


def test_failed_partition_does_not_block_next_disk() -> None:
    """某块盘分区失败：后续盘继续分区（用户明确选择的语义）。"""
    spans = _Spans()
    sequencer = PartitionSequencer([1, 2])
    first = JobPipeline(
        partition=_FakePhase(spans, "partition", ok=False),
        system=_FakePhase(spans, "system"),
        sequencer=sequencer,
    )
    second = JobPipeline(
        partition=_FakePhase(spans, "partition"),
        system=_FakePhase(spans, "system"),
        sequencer=sequencer,
    )

    results = _run_all([first, second], [1, 2])

    assert getattr(results[0], "ok", True) is False
    assert [disk for disk, _s, _e in spans.of("partition")] == [1, 2]
    assert [disk for disk, _s, _e in spans.of("system")] == [2], "失败盘不写镜像"


def test_cancel_while_queued_skips_every_step() -> None:
    spans = _Spans()
    sequencer = PartitionSequencer([1, 2])
    assert sequencer.enter(1) is True  # 盘 1 占着闸门

    cancel = threading.Event()
    cancel.set()
    pipeline = JobPipeline(
        partition=_FakePhase(spans, "partition"),
        system=_FakePhase(spans, "system"),
        sequencer=sequencer,
    )
    result = pipeline.run(_ctx(2, cancel))

    assert getattr(result, "ok", True) is False
    assert "排队中" in (result.failure or "")
    assert spans.items == [], "排队中被取消的盘不应该执行任何阶段"
    skipped = [item for item in result.results if item.status == "skipped"]
    assert len(skipped) == 7, "七个步骤都应记为跳过（含收尾）"
    assert all("排队中" in item.message for item in skipped)
    assert sequencer.snapshot()["running"] == 2 or sequencer.snapshot()["finished"] == [2]


def test_pipeline_without_sequencer_keeps_old_behaviour() -> None:
    """单盘（sequencer=None）不应引入任何额外的排队/跳过逻辑。"""
    spans = _Spans()
    pipeline = JobPipeline(
        partition=_FakePhase(spans, "partition"),
        system=_FakePhase(spans, "system"),
    )
    result = pipeline.run(_ctx(5))
    assert getattr(result, "ok", False) is True
    assert [item.key for item in result.results] == ["efi", "image"]


# ---------------------------------------------------------------- runner 侧
def _runner(tmp_path: Path, **kwargs) -> WindowsDiskRunner:
    return WindowsDiskRunner(
        log_dir=tmp_path,
        diskpart="diskpart.exe",
        ghost="ghost64.exe",
        bcdboot="bcdboot.exe",
        allow_real_disk=True,
        shell=FakeDiskpartShell(),
        process=FakeProcess(),
        **kwargs,
    )


def test_build_pipeline_uses_per_job_workdir(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    ctx = _ctx(3)
    ctx.work_dir = str(tmp_path / "work-3")

    pipeline = runner._build_pipeline(ctx)

    assert pipeline.system.image.cwd == str(tmp_path / "work-3")
    assert pipeline.sequencer is None, "没注入 sequencer 时保持历史行为"


def test_build_pipeline_passes_sequencer_and_gate(tmp_path: Path) -> None:
    sequencer = PartitionSequencer([1, 2])
    gate = object()
    runner = _runner(tmp_path, sequencer=sequencer, ghost_gate=gate)
    ctx = _ctx(1)

    pipeline = runner._build_pipeline(ctx)

    assert pipeline.sequencer is sequencer
    assert pipeline.system.image.start_gate is gate


def test_archive_ghosterr_copies_reason_to_log_dir(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "ghosterr.txt").write_text("Error Number: (8027)", encoding="utf-8")

    runner._archive_ghosterr(str(work), 3, CommandLog(None))

    archived = list(tmp_path.glob("ghosterr-disk3-*.txt"))
    assert len(archived) == 1
    assert "8027" in archived[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------- 生产装配
def test_built_runner_receives_the_shared_coordinators(monkeypatch) -> None:
    """生产路径必须把 sequencer / ghost_gate 交给执行器。

    这是一条**只在生产装配里才会犯的错**：单测都是自己 new 一个 runner 再注入
    sequencer，所以即使 ``_build_service()`` 忘了传也照样全绿——而线上四块盘的分区
    脚本会交错执行（每个 diskpart 进程互斥，但「清盘 → 删 MSR → 建分区」三段会在盘
    之间穿插），Ghost 也可能同时起两个实例撞 8027。现场 0x80070017 就出在这种交错下。
    """
    import app.application.workflow as workflow_mod

    captured: dict[str, object] = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return "FAKE-RUNNER"

    monkeypatch.setattr(workflow_mod, "build_real_runner", fake_build)

    controller = workflow_mod.WorkflowController(
        preflight_runner=lambda: {"results": []},
        settings_provider=dict,
    )

    assert controller._service._real_runner == "FAKE-RUNNER"
    # 必须是同一个对象：set_order() / wait() 都作用在这两个实例上
    assert captured["sequencer"] is controller._sequencer
    assert captured["ghost_gate"] is controller._ghost_gate
    # 两个协调器要在装配 service 之前就存在（否则 build 时拿不到，会退化成 None）
    assert controller._sequencer is not None
    assert controller._ghost_gate is not None


def test_injected_runner_is_left_alone(monkeypatch) -> None:
    """显式注入执行器时不该再偷偷造一个（测试/自定义装配走这条）。"""
    import app.application.workflow as workflow_mod

    def boom(**kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("注入执行器时不该调用 build_real_runner")

    monkeypatch.setattr(workflow_mod, "build_real_runner", boom)
    sentinel = object()
    controller = workflow_mod.WorkflowController(
        real_runner=sentinel,
        preflight_runner=lambda: {"results": []},
        settings_provider=dict,
    )
    assert controller._service._real_runner is sentinel
