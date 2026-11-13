"""清盘步骤的失败恢复：退出码翻译 + 幂等重试。

背景（真实现场）：批量装机 4 块盘，2 块在「硬盘分区」失败，日志只有一行

    DiskPart 遇到错误: 数据错误(循环冗余检查)。
    退出码 2147942423

``2147942423 = 0x80070017 = ERROR_CRC``：这是**存储层**读到了坏数据，不是分区逻辑
写错了。当时的失败点固定在 ``clean`` 之后的那条 ``convert gpt`` 上，而这一段命令
是幂等的——重扫一次再跑通常就过了。所以这里守护三件事：

1. 退出码要翻成人话（含下一步建议），别再只丢一个数字给用户；
2. 幂等的清盘步骤在**瞬时类**错误上重试，且重试前 ``rescan``；
3. 权限/参数这类重试也没用的错误，不浪费用户时间，一次就报出来。
"""

from __future__ import annotations

import threading

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import plan_partitions
from app.application.phases.base import PhaseContext
from app.application.phases.partition import PartitionPhase
from app.application.shell import (
    DISKPART_TRANSIENT_EXITS,
    describe_diskpart_exit,
    diskpart_exit_hint,
    is_transient_diskpart_exit,
    normalize_exit_code,
)
from tests.fakes import FakeDiskpartShell, build_snapshot

# 用户那张截图里的真实退出码
CRC = 0x80070017
CRC_DECIMAL = 2147942423
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


def _ctx(tmp_path, options=None) -> PhaseContext:
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
        identity={"model": "Test Disk 120GB", "size_bytes": None},
    )


def _phase(shell, **kwargs) -> PartitionPhase:
    kwargs.setdefault("retry_delay", 0)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return PartitionPhase(shell, **kwargs)


def _shell(codes, *, msr=None):
    """按顺序返回退出码；用完后一直返回最后一个。"""
    queue = list(codes)
    parts = _parts()

    def handler(_lines, _title, _timeout):
        code = queue.pop(0) if len(queue) > 1 else queue[0]
        return code, f"diskpart exit {code}"

    return FakeDiskpartShell(
        msr=msr, diskpart_handler=handler, query_handler=lambda disk: build_snapshot(parts)
    )


# ---------------------------------------------------------------- 退出码翻译
def test_crc_exit_code_is_explained_in_plain_words() -> None:
    text = describe_diskpart_exit(CRC_DECIMAL)
    assert "0x80070017" in text
    assert "循环冗余" in text
    # 数字本身也要留着，方便对照系统事件日志
    assert str(CRC_DECIMAL) in text


def test_signed_exit_code_normalizes_to_the_same_hresult() -> None:
    """有的路径上退出码是负数（32 位有符号），翻译必须一致。"""
    signed = CRC - 0x100000000
    assert normalize_exit_code(signed) == CRC
    assert describe_diskpart_exit(signed) == describe_diskpart_exit(CRC)
    assert is_transient_diskpart_exit(signed) is True


def test_unknown_exit_code_is_reported_verbatim() -> None:
    text = describe_diskpart_exit(42)
    assert "42" in text
    assert "0x0000002A" in text
    assert diskpart_exit_hint(42) == ""  # 认不出来就不编原因


def test_hints_are_actionable_for_known_codes() -> None:
    assert "数据线" in diskpart_exit_hint(CRC) or "接口" in diskpart_exit_hint(CRC)
    assert "管理员" in diskpart_exit_hint(ACCESS_DENIED)


def test_transient_set_matches_classification() -> None:
    assert CRC in DISKPART_TRANSIENT_EXITS
    for code in DISKPART_TRANSIENT_EXITS:
        assert is_transient_diskpart_exit(code) is True
    # 权限/参数类不该重试
    assert is_transient_diskpart_exit(ACCESS_DENIED) is False
    assert is_transient_diskpart_exit(0x80070057) is False
    assert is_transient_diskpart_exit(None) is False


# ---------------------------------------------------------------- 重试行为
def test_wipe_retries_after_crc_and_then_succeeds(tmp_path) -> None:
    """真实故障的形态：clean 成功、convert gpt 报 CRC；重扫后重试通过。"""
    shell = _shell([CRC, 0])
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is True
    assert shell.rescans == 1  # 重试前重扫过
    wipe_scripts = [script for script in shell.scripts if "convert gpt" in script]
    assert len(wipe_scripts) == 2  # 清盘脚本真的跑了两遍
    assert [result.key for result in outcome.results] == ["efi", "windows", "data", "layout"]


def test_wipe_does_not_retry_permanent_errors(tmp_path) -> None:
    shell = _shell([ACCESS_DENIED])
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is False
    assert shell.rescans == 0
    assert len([s for s in shell.scripts if "convert gpt" in s]) == 1
    message = outcome.message or ""
    assert "0x80070005" in message
    assert "拒绝访问" in message
    # 分区组其余步骤补 skipped，系统组一步都不许发
    assert [r.key for r in outcome.results] == ["efi", "windows", "data", "layout"]
    assert outcome.results[0].status == "failed"
    assert all(r.status == "skipped" for r in outcome.results[1:])


def test_wipe_gives_up_after_configured_attempts(tmp_path) -> None:
    shell = _shell([CRC])  # 一直失败
    outcome = _phase(shell).run(_ctx(tmp_path))

    assert outcome.ok is False
    assert shell.rescans == 2  # 3 次尝试 = 2 次重试
    assert len([s for s in shell.scripts if "convert gpt" in s]) == 3
    message = outcome.message or ""
    assert "已自动重试 2 次仍失败" in message
    assert "0x80070017" in message
    assert outcome.results[0].details.get("diskpart_attempts") == 3
    assert outcome.disk_touched is False  # 清盘没跑成，收尾不必释放盘符


def test_wipe_attempts_are_configurable(tmp_path) -> None:
    shell = _shell([CRC])
    outcome = _phase(shell, wipe_attempts=1).run(_ctx(tmp_path))

    assert outcome.ok is False
    assert shell.rescans == 0
    assert len([s for s in shell.scripts if "convert gpt" in s]) == 1
    assert "重试" not in (outcome.message or "")


def test_clean_failure_is_also_retried(tmp_path) -> None:
    """失败点在 clean 本身时同样重试：整段脚本都是幂等的。"""
    shell = _shell([0x80070015, 0])  # 设备未就绪 → 设备就绪
    outcome = _phase(shell).run(_ctx(tmp_path))
    assert outcome.ok is True
    assert shell.rescans == 1


def test_retry_backoff_is_exponential_and_injected(tmp_path) -> None:
    delays: list[float] = []
    shell = _shell([CRC])
    _phase(shell, retry_delay=1.5, sleep=delays.append).run(_ctx(tmp_path))
    assert delays == [1.5, 3.0]  # 递增等待，且没有真的睡


@pytest.mark.parametrize(
    "code",
    [0x80070017, 0x80070015, 0x8007001F, 0x8007045D, 0x80070020, 0x80070021],
)
def test_all_transient_codes_trigger_a_retry(tmp_path, code: int) -> None:
    shell = _shell([code, 0])
    outcome = _phase(shell).run(_ctx(tmp_path))
    assert outcome.ok is True
    assert shell.rescans == 1


# ---------------------------------------------------------------- VDS 错误码
VDS_IN_USE = 0x80042413


def test_vds_volume_in_use_code_is_explained() -> None:
    """现场第二次报错：0x80042413（VDS：设备使用中/所选卷或分区正在使用）。"""
    text = describe_diskpart_exit(VDS_IN_USE)
    assert "0x80042413" in text
    assert "正在使用" in text
    assert "2147755027" in text  # 十进制也要留着，方便对照日志
    assert is_transient_diskpart_exit(VDS_IN_USE) is True
    assert diskpart_exit_hint(VDS_IN_USE)  # 要有下一步建议


def test_vds_codes_are_not_confused_with_win32_codes() -> None:
    """0x800424xx 是 VDS 自己的码，别按 Win32 的 0x8007xxxx 去解释。"""
    assert describe_diskpart_exit(0x8004240C).count("0x8004240C") == 1
    assert "0x8007" not in describe_diskpart_exit(0x8004240C)


# ---------------------------------------------------------------- 续跑生成
def _actual(*rows):
    """构造 shell.query 返回的真实分区表（rows: (number, fs, letter)）。"""
    partitions = []
    for number, fs, letter in rows:
        partitions.append(
            {
                "number": number,
                "file_system": fs,
                "label": None,
                "drive_letter": letter,
                "mounted_letter": None,
            }
        )
    return {"partition_style": "GPT", "partitions": partitions, "largest_free": 0}


def test_repair_lines_create_what_is_missing() -> None:
    """一个分区都没建出来时，续跑应该把整份计划重新建一遍。"""
    from app.application.partitions import create_repair_lines

    lines = create_repair_lines(5, _parts(), _actual())
    assert lines[0] == "select disk 5"
    assert lines[-1] == "exit"
    assert lines.count("format quick fs=fat32 label=EFI") == 1
    assert any(line == "create partition primary size=3072" for line in lines)


def test_repair_lines_only_format_the_unformatted_partition() -> None:
    """EFI 建好且已格式化 → 只补没格式化的 C 与没分配盘符的数据分区。"""
    from app.application.partitions import create_repair_lines

    parts = _parts()
    actual = _actual((1, "FAT32", "E"), (2, None, None), (3, "NTFS", None))
    lines = create_repair_lines(1, parts, actual)

    assert "select partition 2" in lines
    assert "format quick fs=ntfs label=Windows" in lines
    assert 'assign letter=F' in lines
    # 数据分区已经有 NTFS，只差盘符
    assert "select partition 3" in lines
    assert 'assign letter=G' in lines
    # 已经好的 EFI 不再 format
    assert "format quick fs=fat32 label=EFI" not in lines


def test_repair_lines_empty_when_everything_is_done() -> None:
    from app.application.partitions import create_repair_lines

    actual = _actual((1, "FAT32", "E"), (2, "NTFS", "F"), (3, "NTFS", "G"))
    assert create_repair_lines(1, _parts(), actual) == []


def test_repair_lines_never_duplicate_an_existing_partition() -> None:
    """关键不变式：已经存在的分区绝不再 ``create``（否则 diskpart 必报已存在）。"""
    from app.application.partitions import create_repair_lines

    planned = len(_parts())
    cases = (
        (_actual(), 0, planned),
        (_actual((1, "FAT32", "E")), 1, planned - 1),
        (_actual((1, "FAT32", "E"), (2, "NTFS", "F")), 2, planned - 2),
        (_actual((1, "FAT32", "E"), (2, None, None)), 2, planned - 2),
    )
    for actual, existing, expected_creates in cases:
        lines = create_repair_lines(1, _parts(), actual)
        creates = [line for line in lines if line.startswith("create partition")]
        assert len(creates) == expected_creates, (existing, actual, lines)


# ---------------------------------------------------------------- 失败恢复
#
# 现场第二次报错（2026-XX，2 块盘里 1 块失败）：日志里成串的「百分比已完成 0」
# 只可能来自 ``format``，退出码 2147755027 = 0x80042413（VDS：设备使用中 /
# 所选卷或分区正在使用）。也就是说：**分区其实建出来了，只是某个 format 被占用**，
# 而整条建分区脚本是“跑到底”的，一条命令失败就整盘放弃。
#
# 现在的行为：重扫 + 按**真实分区表**续跑（缺的建、没格式化的格式化、没分配盘符
# 的分配），最多 ``create_repair_attempts`` 次；仍然不行才带着 VDS 码报错。
def _partial_snapshot(parts, keep: int):
    """只保留前 ``keep`` 个分区的“建了一半”的真实分区表。"""
    full = build_snapshot(parts)
    partial = dict(full)
    partial["partitions"] = list(full["partitions"])[:keep]
    return partial


def _scripted_shell(parts, *, create_fail_times, query_keep, repair_ok=True, partial_queries=1):
    """第 1 次含 create 的脚本报 VDS 占用；后续由 ``repair_ok`` 决定成败。"""
    state = {"creates": 0, "queries": 0}

    def diskpart(lines, _title, _timeout):
        if "create partition" not in " ".join(lines):
            return 0, "format ok"
        state["creates"] += 1
        if state["creates"] <= create_fail_times:
            return VDS_IN_USE, "虚拟磁盘服务错误: 设备使用中。"
        return (0, "repair ok") if repair_ok else (VDS_IN_USE, "还是被占用")

    def query(_disk):
        state["queries"] += 1
        if state["queries"] <= partial_queries:
            return _partial_snapshot(parts, query_keep)
        return build_snapshot(parts)

    return FakeDiskpartShell(
        msr=None, diskpart_handler=diskpart, query_handler=query
    ), state


def test_create_failure_resumes_from_the_real_partition_table(tmp_path) -> None:
    """EFI 建好了、C/D 还没建：续跑脚本只补 C/D，不再动 EFI。"""
    parts = _parts()
    shell, _ = _scripted_shell(parts, create_fail_times=1, query_keep=1, partial_queries=1)
    outcome = PartitionPhase(
        shell, sleep=lambda _s: None, create_repair_attempts=2
    ).run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert outcome.ok is True, outcome.message
    assert shell.rescans >= 1, "续跑前必须重扫磁盘"
    repair = shell.scripts[-1] if len(shell.scripts) > 1 else []
    # 最后一次脚本里不该再有 EFI 的 format（它已经好了）
    assert "format quick fs=fat32 label=EFI" not in " ".join(repair)
    assert shell.queries, "续跑前要先查真实分区表"


def test_create_failure_message_carries_vds_code_and_repair_count(tmp_path) -> None:
    """续跑也救不回来时，报错要带 VDS 码 + 续跑次数 + 下一步建议。"""
    parts = _parts()
    shell, _ = _scripted_shell(
        parts, create_fail_times=99, query_keep=1, repair_ok=False, partial_queries=99
    )
    outcome = PartitionPhase(
        shell,
        sleep=lambda _s: None,
        create_repair_attempts=2,
        retry_delay=0,
    ).run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert outcome.ok is False
    message = outcome.message or ""
    assert "0x80042413" in message
    assert "续跑 2 次仍未完成" in message
    assert "正在使用" in message  # 翻译成人话了
    details = outcome.results[0].details
    assert details["diskpart_repairs"] == 2
    assert outcome.disk_touched is True


def test_create_repair_can_be_disabled(tmp_path) -> None:
    parts = _parts()
    shell, state = _scripted_shell(parts, create_fail_times=99, query_keep=1, partial_queries=0)
    outcome = PartitionPhase(
        shell, sleep=lambda _s: None, create_repair_attempts=0
    ).run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert outcome.ok is False
    assert state["creates"] == 1, "关掉续跑就只能跑一次原脚本"
    assert shell.rescans == 0
    assert "已按真实分区表续跑" not in (outcome.message or "")


def test_repair_is_skipped_when_the_disk_already_matches_the_plan(tmp_path) -> None:
    """其实只是退出码假失败（脚本跑完了但返回非 0）时，不要白跑一遍 format。"""
    parts = _parts()
    state = {"creates": 0, "queries": 0}

    def diskpart(lines, _title, _timeout):
        if "create partition" in " ".join(lines):
            state["creates"] += 1
            return VDS_IN_USE, "设备使用中"
        return 0, "format ok"

    def query(_disk):
        state["queries"] += 1
        return build_snapshot(parts)  # 现场其实已经建好了

    shell = FakeDiskpartShell(msr=None, diskpart_handler=diskpart, query_handler=query)
    outcome = PartitionPhase(
        shell, sleep=lambda _s: None, create_repair_attempts=2
    ).run(_ctx(tmp_path, options={"partition_settle_ms": 0}))

    assert outcome.ok is True
    assert state["creates"] == 1, "分区已经对上了，不该再发建分区的命令"
    # scripts[0]=清盘、scripts[1]=建分区；之后是逐分区格式化（每个分区一次）
    assert len(shell.scripts) == 2 + len(_parts()), shell.scripts[2:]
    assert any("格式化 #2" in title for title in shell.titles)
