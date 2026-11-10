"""分区阶段的三道新护栏：

1. **动盘前复核**：每段 diskpart 之前比一次身份（盘号漂移/换盘/脱机/只读 → 停手），
   查询本身失败只记日志、不改变结论；
2. **删 MSR 可重试**：瞬时错误重扫重试，且重试前先复查——上一条"删掉了但报错"
   时不能再删（会撞分区不存在）；
3. **验收不通过后补做**：按真实分区表补 format/assign 再验一次，大小/类型不符
   不硬来（交给验收报错）。

这三条的共同前提：**绝不因为"补救"把盘改得更糟**。所以每个用例都同时检查
"该停的时候真的停了"，而不只是"能过的能过"。
"""

from __future__ import annotations

import threading

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import plan_partitions
from app.application.phases.base import PhaseContext
from app.application.phases.partition import PartitionPhase
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
VDS_IN_USE = 0x80042413
CRC = 0x80070017


def _parts():
    return plan_partitions(PAYLOAD, letters=LETTERS)


def _identity(**overrides):
    identity = {
        "disk_number": 1,
        "model": "Test Disk 120GB",
        "serial_number": "SN-TEST",
        "unique_id": None,
        "size_bytes": 120 * 1024**3,
        "size_display": "120 GB",
        "bus_type": "NVMe",
        "partition_style": "GPT",
        "strong": True,
    }
    identity.update(overrides)
    return identity


def _ctx(tmp_path, *, identity=None, options=None):
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
        options=dict(options or {}),
        label="Test Disk 120GB（磁盘 1）",
        identity=dict(_identity() if identity is None else identity),
    )


def _state(**overrides):
    state = {
        "disk_number": 1,
        "model": "Test Disk 120GB",
        "friendly_name": "Test Disk 120GB",
        "serial_number": "SN-TEST",
        "unique_id": None,
        "size_bytes": 120 * 1024**3,
        "bus_type": "NVMe",
        "partition_style": "GPT",
        "is_offline": False,
        "is_read_only": False,
        "health_status": "Healthy",
        "operational_status": "Online",
    }
    state.update(overrides)
    return state


def _shell(**kwargs):
    """建一个替身 shell：默认复查结果与 ``_identity()`` 一致（健康、可写）。"""
    kwargs.setdefault("disk_state_handler", lambda _n: _state())
    return FakeDiskpartShell(**kwargs)


def _phase(shell, **kwargs) -> PartitionPhase:
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("retry_delay", 0)
    return PartitionPhase(shell, **kwargs)


# ---------------------------------------------------------------- 动盘前复核
def test_identity_mismatch_aborts_before_wipe(tmp_path) -> None:
    """盘号还在，但已经换成另一块盘（序列号不同）→ 连清盘都不许做。"""
    shell = _shell(
        msr=None,
        disk_state_handler=lambda _n: _state(serial_number="SN-OTHER"),
        query_handler=lambda _n: build_snapshot(_parts()),
    )
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is False
    assert "复核未通过" in (outcome.message or "")
    assert "身份与确认时不一致" in (outcome.message or "")
    assert shell.scripts == [], "身份不符时一条 diskpart 都不该发"
    details = outcome.results[0].details
    assert details["guard_stage"] == "清盘"
    assert outcome.disk_touched is False


def test_size_mismatch_aborts_when_no_serial_numbers(tmp_path) -> None:
    """没有序列号的盘按「盘号 + 容量 + 型号」比对：容量变了同样要停。"""
    shell = _shell(
        msr=None,
        disk_state_handler=lambda _n: _state(serial_number=None, size_bytes=60 * 1024**3),
        query_handler=lambda _n: build_snapshot(_parts()),
    )
    outcome = _phase(shell).run(_ctx(tmp_path, identity=_identity(serial_number=None)))

    assert outcome.ok is False
    assert "复核未通过" in (outcome.message or "")
    assert shell.scripts == []


def test_offline_or_readonly_disk_aborts(tmp_path) -> None:
    for overrides in ({"is_offline": True}, {"is_read_only": True}, {"health_status": "Unhealthy"}):
        shell = _shell(
            msr=None,
            disk_state_handler=lambda _n, _o=overrides: _state(**_o),
            query_handler=lambda _n: build_snapshot(_parts()),
        )
        outcome = _phase(shell).run(_ctx(tmp_path))
        assert outcome.ok is False, overrides
        assert shell.scripts == [], overrides


def test_guard_query_failure_does_not_change_the_result(tmp_path) -> None:
    """复核查询本身失败（PowerShell 抖动/盘刚掉线）→ 只记日志，继续走。"""

    def boom(_n):
        raise RuntimeError("powershell 挂了")

    shell = _shell(
        msr=None,
        disk_state_handler=boom,
        query_handler=lambda _n: build_snapshot(_parts()),
    )
    log = CommandLog(None)
    ctx = _ctx(tmp_path)
    ctx.log = log
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is True, outcome.message
    assert "查不到磁盘状态，继续执行" in log.summary()


def test_guard_transient_query_error_is_retried(tmp_path) -> None:
    """第一次查询抛异常、第二次成功：不该为了抖动白跑一次失败。"""
    calls = {"n": 0}

    def flaky(_n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("暂时查不到")
        return _state()

    shell = _shell(
        msr=None, disk_state_handler=flaky, query_handler=lambda _n: build_snapshot(_parts())
    )
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is True
    assert calls["n"] >= 2
    assert shell.disk_states  # 复核真的被调用了


def test_guard_without_identity_skips_silently(tmp_path) -> None:
    """老调用方（没有确认时的身份）不该被新护栏挡住。"""
    shell = _shell(msr=None, query_handler=lambda _n: build_snapshot(_parts()))
    ctx = _ctx(tmp_path, identity={})
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is True
    assert shell.disk_states == [], "没有身份时不该白花一次 PowerShell"
    assert "跳过身份复核" in ctx.log.summary()


def test_guard_runs_again_before_create(tmp_path) -> None:
    """清盘后、建分区前要再看一眼（此时盘已经动过了）。"""
    shell = _shell(msr=None, query_handler=lambda _n: build_snapshot(_parts()))
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is True
    assert len(shell.disk_states) == 2  # 清盘前 + 建分区前


# ---------------------------------------------------------------- 删 MSR 重试
def _msr_shell(codes, *, gone_after_first_failure=False):
    """``codes``：删除 MSR 每次尝试的退出码；MSR 是否存在由删除结果决定。

    ``gone_after_first_failure=True`` 模拟"其实删掉了但报了个错"：第一次尝试
    返回失败码，但盘上的 MSR 已经没了——重试前复查必须发现这一点，不能删第二次。
    """
    queue = list(codes)
    state = {"deletes": 0, "gone": False}

    def diskpart(lines, _title, _timeout):
        joined = " ".join(lines)
        if "delete partition override" in joined:
            state["deletes"] += 1
            code = queue.pop(0) if len(queue) > 1 else queue[0]
            if code == 0 or (gone_after_first_failure and state["deletes"] == 1):
                state["gone"] = True
            return code, f"exit {code}"
        return 0, "ok"

    def find(_n):
        return None if state["gone"] else 1

    return (
        _shell(
            msr=find,
            diskpart_handler=diskpart,
            query_handler=lambda _n: build_snapshot(_parts()),
        ),
        state,
    )


def test_msr_delete_retries_on_transient_error(tmp_path) -> None:
    shell, state = _msr_shell([CRC, 0])
    ctx = _ctx(tmp_path)
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is True, outcome.message
    assert state["deletes"] == 2
    assert shell.rescans >= 1
    assert "删除 MSR 第 1/3 次失败" in ctx.log.summary()


def test_msr_delete_does_not_retry_permanent_errors(tmp_path) -> None:
    shell, state = _msr_shell([0x80070005])  # 权限不足：重试没用
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is False
    assert state["deletes"] == 1
    assert "删除 MSR 分区失败" in (outcome.message or "")


def test_msr_delete_treats_already_gone_as_success(tmp_path) -> None:
    """上一条"其实删掉了但报错"：复查发现没了 → 继续，绝不能删第二次。"""
    shell, state = _msr_shell([CRC, CRC], gone_after_first_failure=True)
    ctx = _ctx(tmp_path)
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is True, outcome.message
    assert state["deletes"] == 1, "复查发现已经删掉了就不该再发一次 delete"
    assert "已经不存在" in ctx.log.summary()


# ---------------------------------------------------------------- 验收后补做
def _acceptance_repair_shell(*, repair_succeeds=True):
    """验收第一次不通过（Windows 分区没挂盘符），补做之后通过。"""
    parts = _parts()
    partial = build_snapshot(parts)
    full = build_snapshot(parts)
    for part in partial["partitions"]:
        if part.get("label") == "Windows":
            part["drive_letter"] = None
    state = {"repaired": False}

    def diskpart(_lines, title, _timeout):
        if "补做" in title:
            state["repaired"] = repair_succeeds
            return (0, "ok") if repair_succeeds else (1, "nope")
        return 0, "ok"

    def query(_n):
        return full if state["repaired"] else partial

    return _shell(msr=None, diskpart_handler=diskpart, query_handler=query), state


def test_acceptance_failure_is_repaired_then_passes(tmp_path) -> None:
    shell, state = _acceptance_repair_shell()
    ctx = _ctx(tmp_path)
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is True, outcome.message
    assert state["repaired"] is True
    text = ctx.log.summary()
    assert "验收不通过：按真实分区表补做" in text
    repair_scripts = [script for script in shell.scripts if "assign letter=F" in " ".join(script)]
    assert repair_scripts, "补做脚本里应当有 assign letter=F"
    assert any("验收后补做" in title for title in shell.titles)


def test_acceptance_failure_with_nothing_to_repair_still_fails(tmp_path) -> None:
    """大小/类型不符这类"补不动"的问题：不许瞎补，直接按验收失败报出来。"""
    parts = _parts()
    broken = build_snapshot(parts)
    for part in broken["partitions"]:
        if part.get("label") == "Data":
            part["size_bytes"] = 1 * 1024**3  # 容量不符，补做也改不了
    shell = _shell(msr=None, query_handler=lambda _n: broken)
    ctx = _ctx(tmp_path)
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is False
    assert "分区验收不通过" in (outcome.message or "")
    assert "没有可补做的项" in ctx.log.summary()


def test_acceptance_repair_disabled_by_zero_attempts(tmp_path) -> None:
    shell, state = _acceptance_repair_shell()
    outcome = _phase(shell, create_repair_attempts=0).run(_ctx(tmp_path))

    assert outcome.ok is False
    assert state["repaired"] is False
    assert "分区验收不通过" in (outcome.message or "")


# ---------------------------------------------------------------- 日志可读性
class _NullSystemPhase:
    """流水线跑完分区组就够了；系统组只是占位（不写镜像、不写引导）。"""

    group = "system"

    def run(self, ctx):
        from app.application.phases.base import PhaseOutcome

        return PhaseOutcome.succeeded()


def test_guard_and_steps_are_visible_in_the_log(tmp_path) -> None:
    """日志要能"扫读"：抬头有型号、每步有 ✓/✗、结论在最后。"""
    from app.application.pipeline import JobPipeline

    shell = _shell(msr=None, query_handler=lambda _n: build_snapshot(_parts()))
    ctx = _ctx(tmp_path)
    pipeline = JobPipeline(partition=_phase(shell), system=_NullSystemPhase())
    result = pipeline.run(ctx)

    assert result.ok is True
    text = ctx.log.summary()
    assert "① 硬盘分区" in text
    assert "[复核] 清盘前磁盘正常" in text
    assert "步骤 ✓" in text


def test_effective_label_appears_in_diagnostics(tmp_path) -> None:
    from app.application import activity

    activity.reset()
    activity.begin(9, "image", "GeIL P3A 2TB（磁盘 9）")
    try:
        assert activity.describe() == "GeIL P3A 2TB（磁盘 9）=写镜像（Ghost）"
        assert activity.describe(exclude=9) == ""
        assert activity.label_of(9) == "GeIL P3A 2TB（磁盘 9）"
    finally:
        activity.end(9)
    assert activity.label_of(9) == ""


@pytest.mark.parametrize("phase_text", ["partition", "image", "extras"])
def test_activity_phase_text_kept(phase_text: str) -> None:
    from app.application import activity

    activity.reset()
    activity.begin(3, phase_text, "Model（磁盘 3）")
    try:
        described = activity.describe()
        assert described.startswith("Model（磁盘 3）=")
    finally:
        activity.reset()


# ---------------------------------------------------------------- 日志噪声
def test_read_only_queries_are_logged_as_one_line() -> None:
    """只读查询（PowerShell 脚本几十行）不该整段刷进日志：只留一行说明。

    现场反馈"日志太长，看不懂"，而日志里最大的噪声就是每次查询都把整段
    PowerShell 脚本原样打一遍（一块盘要查 4–6 次）。
    """
    from app.application.shell import DiskpartShell
    from tests.fakes import FakeProcess

    payload = '{"disk_number": 1, "partition_style": "GPT", "disk_size_bytes": 1, "partitions": []}'
    process = FakeProcess(lambda _cmd, _timeout, _env: (0, payload, 0.1))
    log = CommandLog(None)
    shell = DiskpartShell(log=log, powershell="powershell.exe", process=process)

    shell.query(1)
    shell.disk_state(1)

    assert process.quiet[0].startswith("查询磁盘 1 的分区表")
    assert process.quiet[1].startswith("复核磁盘 1 的身份与健康状态")
    assert all("shell.py" in item for item in process.quiet), "要指明脚本在哪能看到"


def test_format_command_line_hides_the_script_but_keeps_the_reason() -> None:
    from app.application.shell import format_command_line

    noisy = ["powershell.exe", "-Command", " ".join(["Get-Partition -DiskNumber 1"] * 20)]
    plain = format_command_line(noisy)
    assert "Get-Partition" in plain and plain.startswith("$ ")

    quiet = format_command_line(noisy, "查询磁盘 1 的分区表（脚本见 shell.py）")
    assert quiet == "$ <查询磁盘 1 的分区表（脚本见 shell.py）>"
    assert "Get-Partition" not in quiet
