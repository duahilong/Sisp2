"""分区阶段「建分区 / 格式化」拆开之后的护栏与兜底。

## 现场（2026-11-10，4 块盘只成功 1 块）

    12:47:12  create partition primary size=307200   成功
    12:47:12  format quick fs=ntfs label=Windows     卡在 0% 17 秒
    12:47:29  虚拟磁盘服务错误: 设备使用中  (0x80042413)
    续跑两次同样的 format 也全失败（第二次起 6~12 秒就失败）

结论：分区几何没问题，坏在「建完分区立刻格式化」——新卷还在被系统异步挂载/
枚举，`format` 拿不到独占锁；而且一旦失败，卷会一直处于被占用状态，再发同一条
`diskpart format` 一百次都一样。

所以这里守四件事：

1. 建分区那一段**只 create**（不 format、不 assign），格式化单独一段；
2. 格式化前等「分区数与计划一致」，逐分区执行；
3. 卷被占用时先由 diskpart 自己 ``override`` 强制卸载，不行再换 ``format.com /X``；
4. 失败要归因到真正的那个分区（以前一律记在「创建 EFI 分区」上）。

外加一条安全闸：走 ``format.com`` 之前必须确认盘符真的挂在自己的目标分区上，
否则宁可失败，绝不格式化别人的卷。安全闸“拒绝”与工具“失败”分开处理，
不允许把两者的错都算成永久失败（那会把后面能用的途径全拖掉）。

> 2026-11-11 现场（三块盘全卡在新建 EFI 分区）：diskpart 打出
> `0x80042413 设备使用中` + 「若要继续操作，请使用 OVERRIDE 参数」，
> 而 1.4.0 只会用 ``format.com``，又因为 ESP 的 ``drive_letter`` 永远是 null
> 被安全闸拒掉——两下夹死。1.4.1 就是补这两块。
"""

from __future__ import annotations

import threading

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import (
    create_geometry_lines,
    format_lines,
    plan_partitions,
)
from app.application.phases.base import PhaseContext
from app.application.phases.partition import PartitionPhase
from tests.fakes import FakeDiskpartShell, build_snapshot

VDS_IN_USE = 0x80042413
ACCESS_DENIED = 0x80070005

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


def _parts():
    return plan_partitions(PAYLOAD, letters=LETTERS)


def _ctx(tmp_path, *, options=None):
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
        options=dict({"partition_settle_ms": 0, **(options or {})}),
        label="Test Disk（磁盘 1）",
        identity={},
    )


def _shell(**kwargs):
    """默认返回"分区都已建好、都格式化好的真实分区表"（§ 需要覆盖时自己传）。"""
    kwargs.setdefault("query_handler", lambda _disk: build_snapshot(_parts()))
    msr = kwargs.pop("msr", None)
    return FakeDiskpartShell(msr=msr, **kwargs)


def _phase(shell, **kwargs) -> PartitionPhase:
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("retry_delay", 0)
    # 单测里"卷就绪"不该真等 12 秒：默认关掉轮询（专门测它的用例自己设）。
    kwargs.setdefault("ready_timeout", 0)
    return PartitionPhase(shell, **kwargs)


def _fails_partition(position: int):
    """只在「格式化第 position 个分区」的脚本上失败。"""

    def handler(lines, _title, _timeout):
        joined = " ".join(lines)
        if f"select partition {position}" in joined and "format" in joined:
            return VDS_IN_USE, "虚拟磁盘服务错误: 设备使用中。"
        return 0, "ok"

    return handler


# ---------------------------------------------------------------- 脚本拆分
def test_create_script_only_creates_and_format_script_only_formats() -> None:
    parts = _parts()

    create = create_geometry_lines(1, parts)
    assert not any(line.startswith("format") for line in create)
    assert not any(line.startswith("assign") for line in create)

    one = format_lines(1, 2, parts[1])
    assert one == [
        "select disk 1",
        "select partition 2",
        "format quick fs=ntfs label=Windows override",
        "assign letter=F",
        "exit",
    ]
    assert not any(line.startswith("create") for line in one)
    # ``force=False`` 是 1.4.0 的老行为（保留为可回退的开关）
    assert format_lines(1, 2, parts[1], force=False)[2] == "format quick fs=ntfs label=Windows"


# ---------------------------------------------------------------- 等卷就绪
def test_waits_until_all_partitions_are_visible(tmp_path) -> None:
    """刚建完分区时可能只看到一部分：等分区数达标再格式化。"""
    parts = _parts()
    full = build_snapshot(parts)
    state = {"queries": 0}

    def query(_disk):
        state["queries"] += 1
        if state["queries"] <= 2:
            payload = dict(full)
            payload["partitions"] = list(full["partitions"])[:1]
            return payload
        return full

    ctx = _ctx(tmp_path)
    shell = FakeDiskpartShell(msr=None, query_handler=query)
    outcome = _phase(shell, ready_interval=0.0, ready_timeout=5.0).run(ctx)

    assert outcome.ok is True, outcome.message
    assert state["queries"] >= 3
    assert "卷就绪" in ctx.log.summary()


# ---------------------------------------------------------------- 兜底格式化
def test_in_use_format_falls_back_to_format_com(tmp_path) -> None:
    """第 2 个分区 diskpart 报「设备使用中」→ 退避后用 format.com /X 成功。"""
    delays: list[float] = []
    ctx = _ctx(tmp_path)
    shell = _shell(diskpart_handler=_fails_partition(2))
    outcome = _phase(shell, sleep=delays.append).run(ctx)

    assert outcome.ok is True, outcome.message
    assert shell.formats == [("F", "NTFS", "Windows")], "只对第 2 个分区走兜底"
    assert delays == [1.0], "第一次重试前等 1 秒"
    assert "强制卸载" in ctx.log.summary()


def test_format_com_failure_escalates_backoff(tmp_path) -> None:
    """兜底也失败时按 1s → 5s → 10s 退避，并在 diskpart / format.com 之间交替。"""
    delays: list[float] = []
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(2),
        format_handler=lambda _l, _f, _v: (VDS_IN_USE, "还是被占用"),
    )
    outcome = _phase(shell, sleep=delays.append).run(ctx)

    assert outcome.ok is False
    assert delays == [1.0, 5.0, 10.0]
    assert len(shell.formats) == 2, "第 2 次与第 4 次尝试用 format.com"
    formats = [
        script for script in shell.scripts if any(line.startswith("format") for line in script)
    ]
    # 第 2 个分区：diskpart 1 次 + format.com 2 次 + diskpart 1 次 = 4 次尝试
    p2_diskpart = [s for s in formats if "select partition 2" in " ".join(s)]
    assert len(p2_diskpart) == 2


def test_permanent_format_error_does_not_retry(tmp_path) -> None:
    """权限不足这类错误重试也没用：只尝试一次，不浪费用户时间。"""
    delays: list[float] = []
    ctx = _ctx(tmp_path)

    def handler(lines, _title, _timeout):
        if "select partition 1" in " ".join(lines):
            return ACCESS_DENIED, "拒绝访问"
        return 0, "ok"

    shell = _shell(diskpart_handler=handler)
    outcome = _phase(shell, sleep=delays.append).run(ctx)

    assert outcome.ok is False
    assert delays == []
    assert shell.formats == []


def test_format_com_is_skipped_when_no_letter_can_be_assigned(tmp_path) -> None:
    """安全闸：拿不到盘符（盘符被别人占了）就绝不调用 format.com。

    ``format.com`` 只认盘符——如果为了兜底随便选一个字母去格式化，很可能格式化
    的是**别的卷**（那台机器上 L/M/N、S/T/U/V 都挂着别的盘的卷）。所以宁可失败。
    """
    parts = _parts()
    payload = build_snapshot(parts)
    for part in payload["partitions"]:
        if part.get("number") == 2:
            part["drive_letter"] = None

    def diskpart(lines, _title, _timeout):
        joined = " ".join(lines)
        if "assign letter" in joined:
            return 0x80070005, "指定的驱动器号不是空闲的"
        if "select partition 2" in joined and "format" in joined:
            return VDS_IN_USE, "设备使用中"
        return 0, "ok"

    ctx = _ctx(tmp_path)
    shell = _shell(diskpart_handler=diskpart, query_handler=lambda _disk: payload)
    outcome = _phase(shell, format_attempts=2).run(ctx)

    assert outcome.ok is False
    assert shell.formats == [], "拿不到盘符就绝对不能调用 format.com"


def test_format_com_uses_the_letter_that_really_belongs_to_the_partition(tmp_path) -> None:
    """卷上已有一个（非计划的）真实盘符时，兜底就用它——但**必须**是这块分区的。

    这条是安全闸的正面断言：盘符来自"现场查询到的本分区盘符"，不是"计划里那个"，
    更不是随便挑一个。
    """
    parts = _parts()
    payload = build_snapshot(parts)
    for part in payload["partitions"]:
        if part.get("number") == 2:
            part["drive_letter"] = "G"  # 现场挂的是 G，不是计划里的 F

    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(2),
        query_handler=lambda _disk: payload,
    )
    _phase(shell, format_attempts=2).run(ctx)

    assert shell.formats == [("G", "NTFS", "Windows")]


# ---------------------------------------------------------------- 归因
def test_failure_is_attributed_to_the_failed_partition(tmp_path) -> None:
    """第 2 个分区挂掉时，✗ 必须落在「Windows 分区」上，而不是 EFI。"""
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(2),
        format_handler=lambda _l, _f, _v: (VDS_IN_USE, "还是被占用"),
    )
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is False
    by_key = {result.key: result for result in outcome.results}
    assert by_key["efi"].status == "ok", "EFI 明明成功了"
    assert by_key["windows"].status == "failed"
    assert by_key["data"].status == "skipped"
    assert by_key["layout"].status == "skipped"
    assert "分区 #2（Windows）格式化失败" in (outcome.message or "")
    assert outcome.details["failed_partition"] == 2
    assert outcome.details["format_attempts"] == 4


def test_first_partition_failure_marks_efi(tmp_path) -> None:
    """第 1 个分区挂掉时仍然记在 EFI 上（这条老行为要保留）。"""
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(1),
        format_handler=lambda _l, _f, _v: (VDS_IN_USE, "还是被占用"),
    )
    outcome = _phase(shell).run(ctx)

    by_key = {result.key: result for result in outcome.results}
    assert by_key["efi"].status == "failed"
    assert by_key["windows"].status == "skipped"
    assert outcome.details["failed_partition"] == 1


def test_data_partition_failure_is_attributed_to_data(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(3),
        format_handler=lambda _l, _f, _v: (VDS_IN_USE, "还是被占用"),
    )
    outcome = _phase(shell).run(ctx)

    by_key = {result.key: result for result in outcome.results}
    assert by_key["efi"].status == "ok"
    assert by_key["windows"].status == "ok"
    assert by_key["data"].status == "failed"
    assert outcome.details["failed_partition"] == 3


# ---------------------------------------------------------------- 参数
@pytest.mark.parametrize("attempts", [1, 2, 4])
def test_attempt_count_is_configurable(tmp_path, attempts: int) -> None:
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(2),
        format_handler=lambda _l, _f, _v: (VDS_IN_USE, "还是被占用"),
    )
    outcome = _phase(shell, format_attempts=attempts).run(ctx)

    assert outcome.ok is False
    assert "format_attempts" in outcome.details
    assert outcome.details["format_attempts"] == attempts


def test_tool_startup_failure_has_a_human_message(tmp_path) -> None:
    """兜底工具压根没跑起来（缺文件/被拒）时，不能只丢一个 0xFFFFFFFF。"""
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(2),
        format_handler=lambda _l, _f, _v: (-1, "无法启动 format.com：[WinError 193] 不是有效的应用程序"),
    )
    outcome = _phase(shell, format_attempts=2).run(ctx)

    assert outcome.ok is False
    message = outcome.message or ""
    assert "分区 #2（Windows）格式化失败" in message
    assert "无法启动 format.com" in message
    assert "0xFFFFFFFF" not in message
    assert "前面 1 个分区已完成" in message


# ---------------------------------------------------------------- 1.4.1：override 与 ESP
def test_format_script_carries_override_by_default(tmp_path) -> None:
    """2026-11-11 现场：diskpart 自己让用 OVERRIDE，所以 format 一律带 override。

    卷被判「设备使用中」时，``override`` 先在卷上强制卸载再格式化；对刚在清过盘的
    磁盘上建出来的 RAW 分区没有任何数据可丢。
    """
    shell = _shell()
    ctx = _ctx(tmp_path)
    _phase(shell).run(ctx)

    format_scripts = [
        script for script in shell.scripts if any(line.startswith("format") for line in script)
    ]
    assert format_scripts, "应该跑过格式化脚本"
    for script in format_scripts:
        line = next(item for item in script if item.startswith("format"))
        assert line.endswith(" override"), line
    assert any("override" in title for title in shell.titles)


def test_format_force_can_be_switched_off(tmp_path) -> None:
    """``format_force=False`` 回到 1.4.0 的老行为（留一条可回退的路）。"""
    shell = _shell()
    ctx = _ctx(tmp_path)
    outcome = _phase(shell, format_force=False).run(ctx)

    assert outcome.ok is True
    format_scripts = [
        script for script in shell.scripts if any(line.startswith("format") for line in script)
    ]
    for script in format_scripts:
        line = next(item for item in script if item.startswith("format"))
        assert not line.endswith("override")
    assert not any("override" in title for title in shell.titles)


def test_esp_fallback_passes_the_ownership_gate_via_mounted_letter(tmp_path) -> None:
    """**2026-11-11 的回归**：ESP 只有 ``mounted_letter``，兜底不能再被误拒。

    现场日志：``盘符 E: 与分区 #1（EFI）对不上，放弃 format.com 兜底``——ESP 的
    ``drive_letter`` 永远是 null，只看它必然"对不上"，而 ESP 恰恰是最需要兜底的。
    """
    ctx = _ctx(tmp_path)
    shell = _shell(diskpart_handler=_fails_partition(1))
    outcome = _phase(shell, format_attempts=2).run(ctx)

    assert outcome.ok is True, outcome.message
    assert shell.formats == [("E", "FAT32", "EFI")], "ESP 也要能用兜底"
    assert "对不上" not in ctx.log.summary()


def test_data_partition_without_drive_letter_is_still_refused(tmp_path) -> None:
    """严格性不降：数据分区（非隐藏类型）只有 mounted_letter 时仍然拒绝兜底。"""
    parts = _parts()
    payload = build_snapshot(parts)
    for part in payload["partitions"]:
        if part.get("number") == 2:
            part["drive_letter"] = None
            part["mounted_letter"] = "F"  # 看着像有盘符，但它不是隐藏分区

    ctx = _ctx(tmp_path)
    shell = _shell(diskpart_handler=_fails_partition(2), query_handler=lambda _d: payload)
    outcome = _phase(shell, format_attempts=2).run(ctx)

    assert outcome.ok is False
    assert shell.formats == [], "数据分区不许靠 mounted_letter 蒙混过关"


def test_mounted_letter_size_mismatch_is_refused(tmp_path) -> None:
    """容量对不上就不能格式化（防止盘号漂移把别人的卷格掉）。"""
    parts = _parts()
    payload = build_snapshot(parts)
    for part in payload["partitions"]:
        if part.get("number") == 1:
            part["size_bytes"] = int(part["size_bytes"]) + 8 * 1024 * 1024  # +8 MB

    ctx = _ctx(tmp_path)
    shell = _shell(diskpart_handler=_fails_partition(1), query_handler=lambda _d: payload)
    outcome = _phase(shell, format_attempts=2).run(ctx)

    assert outcome.ok is False
    assert shell.formats == []


def test_refused_fallback_keeps_going_through_the_plan(tmp_path) -> None:
    """安全闸拒绝 ≠ 失败：还要继续试后面的途径（1.4.0 就是在这里 break 掉的）。"""
    parts = _parts()
    payload = build_snapshot(parts)
    for part in payload["partitions"]:
        if part.get("number") == 2:
            part["drive_letter"] = None  # 数据分区拿不到盘符 → 兜底必被拒

    def diskpart(lines, _title, _timeout):
        joined = " ".join(lines)
        is_format = any(line.startswith("format") for line in lines)
        if "select partition 2" in joined and is_format:
            return VDS_IN_USE, "虚拟磁盘服务错误: 设备使用中。"
        if not is_format and "assign letter" in joined:
            return ACCESS_DENIED, "指定的驱动器号不是空闲的"
        return 0, "ok"

    ctx = _ctx(tmp_path)
    shell = _shell(diskpart_handler=diskpart, query_handler=lambda _d: payload)
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is False
    # 4 次尝试：diskpart、format.com(拒)、diskpart、format.com(拒)
    p2 = [
        script
        for script in shell.scripts
        if "select partition 2" in " ".join(script)
        and any(item.startswith("format") for item in script)
    ]
    assert len(p2) == 2, "被拒绝之后必须再走一次 diskpart"
    assert shell.formats == []
    text = ctx.log.summary()
    assert "继续下一种途径" in text
    assert "0xFFFFFFFF" not in text


def test_tool_not_started_and_refused_report_every_way(tmp_path) -> None:
    """失败信息要把每条途径都列出来，而不是只留最后一个假错误码。"""
    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=_fails_partition(2),
        format_handler=lambda _l, _f, _v: (-1, "[WinError 193] 不是有效的 Win32 应用程序"),
    )
    outcome = _phase(shell).run(ctx)

    assert outcome.ok is False
    message = outcome.message or ""
    assert "4 次尝试均未成功" in message
    assert "diskpart（含 override）" in message
    assert "format.com /X" in message
    assert "0x80042413" in message
    assert "0xFFFFFFFF" not in message
    assert "不是有效的 Win32 应用程序" in message
    assert len(outcome.details["format_history"]) == 4
    assert outcome.details["format_force"] is True


def test_tool_not_started_does_not_stop_the_next_diskpart_try(tmp_path) -> None:
    """format.com 没跑起来（哨兵 -1）只是这条路不通，后面还能成功。"""
    calls = {"diskpart": 0}

    def diskpart(lines, _title, _timeout):
        joined = " ".join(lines)
        if "select partition 2" in joined and "format" in joined:
            calls["diskpart"] += 1
            if calls["diskpart"] == 1:
                return VDS_IN_USE, "设备使用中"
            return 0, "ok"
        return 0, "ok"

    ctx = _ctx(tmp_path)
    shell = _shell(
        diskpart_handler=diskpart,
        format_handler=lambda _l, _f, _v: (-1, "找不到 format.com"),
    )
    outcome = _phase(shell, format_attempts=3).run(ctx)

    assert outcome.ok is True, outcome.message
    assert calls["diskpart"] == 2, "第 3 次又回到 diskpart，并且这次成功了"
    assert "不可用" in ctx.log.summary()
