"""A2/A5 改造的回归：diskpart 间隔等待 + 「其它盘在干什么」诊断。

背景：现场反馈 4 盘批量装机，2 块在「硬盘分区」报 ``0x80070017``（数据错误/CRC）。
分区虽然全局串行，但流水线允许 A 盘写镜像时 B 盘分区——两条 I/O 抢同一条链路时，
链路差的盘就可能读回坏数据。所以这一版做两件事：

- **A2**：两次 diskpart 之间等 ``partitionSettleMs``（设置页可调，默认 1000ms，0=不等）；
- **A5**：把「失败那一刻别的盘在干什么」记进日志与步骤详情，下次复现能直接定位。

这些用例守住：等待真的发生在两次 diskpart 之间、0 能关掉、设置值能下发到执行器、
诊断内容随登记表变化且不会因为登记表出错而影响执行。
"""

from __future__ import annotations

import threading

import pytest

from app.application import activity
from app.application.command_log import CommandLog
from app.application.partitions import plan_partitions
from app.application.phases.base import PhaseContext
from app.application.phases.partition import DEFAULT_SETTLE_MS, PartitionPhase
from app.application.runner_utils import option_int
from app.modules.settings.service import DEFAULTS, SettingsError, coerce_value
from tests.fakes import FakeDiskpartShell, build_snapshot

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


@pytest.fixture(autouse=True)
def _clean_activity():
    activity.reset()
    yield
    activity.reset()


def _parts():
    return plan_partitions(PAYLOAD, letters=LETTERS)


def _ctx(tmp_path, *, options=None) -> PhaseContext:
    image = tmp_path / "tiny.GHO"
    if not image.exists():
        image.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))
    return PhaseContext(
        disk_number=1,
        parts=_parts(),
        letters=LETTERS,
        image_path=str(image),
        cancel=threading.Event(),
        log=CommandLog(None),
        options=options or {},
        identity={"model": "Test Disk 120GB", "size_bytes": None},
    )


def _shell(*, msr=1, **kwargs):
    """``msr=1`` = 「convert gpt 建出了 MSR」；删完复查时必须查不到，
    否则分区阶段会按“MSR 没删掉”中止（真实行为）。"""
    if msr == 1:
        seen: list[int] = []

        def msr_reader(_disk: int):
            seen.append(1)
            return 1 if len(seen) == 1 else None  # 第一次查到、复查查不到

        return FakeDiskpartShell(
            msr=msr_reader, query_handler=lambda disk: build_snapshot(_parts()), **kwargs
        )
    return FakeDiskpartShell(
        msr=msr, query_handler=lambda disk: build_snapshot(_parts()), **kwargs
    )


# ---------------------------------------------------------------- 设置项本身
def test_setting_defaults_to_one_second() -> None:
    assert DEFAULTS["partitionSettleMs"] == 1000


@pytest.mark.parametrize("value,expected", [(0, 0), (1, 1), (1000, 1000), (5000, 5000), ("800", 800)])
def test_setting_accepts_zero_to_five_seconds(value, expected: int) -> None:
    assert coerce_value("partitionSettleMs", value) == expected


@pytest.mark.parametrize("value", [-1, 5001, 100000, "abc", None])
def test_setting_rejects_out_of_range(value) -> None:
    with pytest.raises(SettingsError):
        coerce_value("partitionSettleMs", value)


def test_option_int_is_defensive() -> None:
    assert option_int({"partition_settle_ms": 1500}, "partition_settle_ms") == 1500
    assert option_int({"partition_settle_ms": "1500"}, "partition_settle_ms") == 1500
    assert option_int({"partition_settle_ms": None}, "partition_settle_ms", default=700) == 700
    assert option_int(None, "partition_settle_ms", default=700) == 700
    # 越界夹紧，不让界面里的脏值把任务等成十分钟
    assert option_int({"x": 99999}, "x", low=0, high=5000) == 5000
    assert option_int({"x": -5}, "x", low=0, high=5000) == 0


# ---------------------------------------------------------------- 等待时机
def test_settle_happens_between_diskpart_calls(tmp_path) -> None:
    """每次 diskpart 之间都要等：清盘 → 删 MSR → 建分区 → 逐分区格式化。

    1.4.0 起「建分区」与「格式化」拆成两段，所以等待次数比以前多
    （多出来的正是现场缺的那个空档）。这里守两件事：**清盘本身不加等待**、
    每次等待都用设置里的值（默认 1000 ms）。
    """
    delays: list[float] = []
    shell = _shell(msr=1)
    phase = PartitionPhase(shell, sleep=delays.append)
    ctx = _ctx(tmp_path)
    outcome = phase.run(ctx)

    assert outcome.ok is True
    assert len([s for s in shell.scripts if "convert gpt" in s]) == 1
    # 清盘脚本之前没有等待（第一条 diskpart 不该被拖慢）
    log_text = ctx.log.summary()
    assert "删除 MSR 前：等待 1000 ms" in log_text
    assert "建分区前：等待 1000 ms" in log_text
    # 1.4.1 起只等一次（卷就绪后不再重复等，见 _wait_volumes）
    assert "格式化分区 #1（EFI，第 1 次）前：等待 1000 ms" in log_text
    assert "格式化前：等待 1000 ms" not in log_text, "卷就绪后不再重复等待"
    assert delays
    assert all(delay == pytest.approx(1.0, abs=0.05) for delay in delays)


def test_settle_can_be_disabled(tmp_path) -> None:
    delays: list[float] = []
    shell = _shell(msr=1)
    phase = PartitionPhase(shell, sleep=delays.append)
    outcome = phase.run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert outcome.ok is True
    assert delays == []


def test_settle_comes_from_runner_options(tmp_path) -> None:
    delays: list[float] = []
    shell = _shell(msr=1)
    phase = PartitionPhase(shell, sleep=delays.append)
    phase.run(_ctx(tmp_path, options={"partition_settle_ms": 250}))
    assert delays
    assert all(delay == pytest.approx(0.25, abs=0.02) for delay in delays)


def test_settle_is_clamped_for_absurd_values(tmp_path) -> None:
    delays: list[float] = []
    phase = PartitionPhase(_shell(msr=1), sleep=delays.append)
    phase.run(_ctx(tmp_path, options={"partition_settle_ms": 10 * 60 * 1000}))
    assert delays
    assert all(delay == pytest.approx(5.0, abs=0.05) for delay in delays)  # 上限 5 秒


def test_identity_guard_time_counts_toward_settle(tmp_path) -> None:
    now = [0.0]
    delays: list[float] = []
    base = FakeDiskpartShell()

    def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds

    def slow_guard(disk):
        state = base.disk_state(disk)
        now[0] += 0.4
        return state

    phase = PartitionPhase(
        _shell(msr=None, disk_state_handler=slow_guard),
        sleep=sleep,
        clock=lambda: now[0],
    )
    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is True
    assert delays
    assert all(delay == pytest.approx(0.6) for delay in delays)


def test_guard_longer_than_settle_adds_no_extra_sleep(tmp_path) -> None:
    now = [0.0]
    delays: list[float] = []
    base = FakeDiskpartShell()

    def very_slow_guard(disk):
        state = base.disk_state(disk)
        now[0] += 1.2
        return state

    phase = PartitionPhase(
        _shell(msr=None, disk_state_handler=very_slow_guard),
        sleep=delays.append,
        clock=lambda: now[0],
    )
    outcome = phase.run(_ctx(tmp_path))

    assert outcome.ok is True
    assert delays == []


def test_no_settle_needed_when_there_is_no_msr(tmp_path) -> None:
    """没有 MSR 时少一次 diskpart，但「建分区前 / 逐分区格式化前」的等待仍然要有。"""
    delays: list[float] = []
    phase = PartitionPhase(_shell(msr=None), sleep=delays.append)
    outcome = phase.run(_ctx(tmp_path))
    assert outcome.ok is True
    assert delays
    assert all(delay == pytest.approx(1.0, abs=0.05) for delay in delays)


# ---------------------------------------------------------------- A5 诊断
def test_activity_registry_describes_other_disks() -> None:
    activity.begin(3, "image")
    activity.begin(5, "partition")
    assert activity.snapshot() == {3: "image", 5: "partition"}
    text = activity.describe(exclude=5)
    assert "磁盘 3" in text
    assert "Ghost" in text
    assert "磁盘 5" not in text
    activity.end(3)
    assert activity.snapshot() == {5: "partition"}
    assert activity.describe(exclude=5) == ""


def test_activity_never_raises_on_bad_input() -> None:
    activity.begin("not-a-number", "image")  # 类型不对也不许抛
    activity.reset()
    activity.end(123456)
    assert activity.describe() == ""


def test_partition_logs_what_other_disks_are_doing(tmp_path) -> None:
    log = CommandLog(str(tmp_path))
    activity.begin(3, "image")  # 模拟“另一块盘正在写镜像”
    ctx = PhaseContext(
        disk_number=1,
        parts=_parts(),
        letters=LETTERS,
        image_path=str(tmp_path / "tiny.GHO"),
        cancel=threading.Event(),
        log=log,
        options={"partition_settle_ms": 0},
        identity={"model": "Test Disk 120GB", "size_bytes": None},
    )
    (tmp_path / "tiny.GHO").write_bytes(bytes([0xFE, 0xEF]) + bytes(8))

    PartitionPhase(_shell(msr=1), sleep=lambda _s: None).run(ctx)
    assert log.path is not None
    text = log.path.read_text(encoding="utf-8")
    assert "[诊断] 开始分区" in text
    assert "磁盘 3" in text and "Ghost" in text


def test_wipe_failure_details_capture_parallel_activity(tmp_path) -> None:
    """失败详情里要能看出当时别的盘在干什么——这是 A5 的核心目的。"""
    activity.begin(3, "image")

    def handler(_lines, _title, _timeout):
        return 0x80070017, "DiskPart 遇到错误: 数据错误(循环冗余检查)。"

    shell = FakeDiskpartShell(
        diskpart_handler=handler,
        msr=1,
        query_handler=lambda disk: build_snapshot(_parts()),
    )
    outcome = PartitionPhase(
        shell, retry_delay=0, sleep=lambda _s: None, wipe_attempts=1
    ).run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert outcome.ok is False
    details = outcome.results[0].details
    assert details["settle_ms"] == 0
    assert "3=写镜像（Ghost）" in details["parallel_activity"]
    assert "0x80070017" in (outcome.message or "")


def test_default_settle_constant_matches_setting_default() -> None:
    """代码里的兜底默认值必须和设置项默认值一致，否则“没配过”的机器行为会漂。"""
    assert DEFAULTS["partitionSettleMs"] == DEFAULT_SETTLE_MS


# ---------------------------------------------------------------- 编排层登记
def test_pipeline_registers_and_clears_activity(tmp_path) -> None:
    """分区/系统/收尾三组都要登记；任务结束（含失败）必须清掉，否则诊断信息会串台。"""
    seen: list[dict[int, str]] = []

    class _Probe:
        def __init__(self, group, ok=True):
            self.group = group
            self._ok = ok

        def run(self, ctx):
            seen.append(activity.snapshot())
            from app.application.phases.base import PhaseOutcome, PhaseResult

            if not self._ok:
                return PhaseOutcome.failed(
                    PhaseResult.failed("efi", "boom"), message="boom"
                )
            keys = {
                "partition": ("efi", "windows", "data", "layout"),
                "system": ("image", "boot"),
                "extras": ("copy",),
            }[self.group]
            return PhaseOutcome.succeeded(*[PhaseResult.ok(key) for key in keys])

    from app.application.pipeline import JobPipeline

    pipeline = JobPipeline(
        partition=_Probe("partition"), system=_Probe("system"), extras=_Probe("extras")
    )
    pipeline.run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert seen[0] == {1: "partition"}
    assert seen[1] == {1: "system"}
    assert seen[2] == {1: "extras"}
    assert activity.snapshot() == {}  # 收尾后清空


def test_pipeline_clears_activity_even_when_partition_fails(tmp_path) -> None:
    class _Failing:
        def run(self, ctx):
            from app.application.phases.base import PhaseOutcome, PhaseResult

            return PhaseOutcome.failed(PhaseResult.failed("efi", "boom"), message="boom")

    class _Never:
        def run(self, ctx):  # pragma: no cover - 不该被调用
            raise AssertionError("分区失败后系统组不该执行")

    from app.application.pipeline import JobPipeline

    JobPipeline(partition=_Failing(), system=_Never()).run(_ctx(tmp_path))
    assert activity.snapshot() == {}
