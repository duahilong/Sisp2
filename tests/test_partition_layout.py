"""分区布局（EFI → C → D → 可选 E）的单元测试。

守护两件事：

1. **默认布局与旧行为逐字相同**——不加额外分区时生成的 diskpart 脚本一个字符
   都不能变，否则等于把 235 个已通过的真机路径重新赌一遍；
2. 加了 E 之后，布局 / 盘符 / 容量判据仍然自洽（4 个分区、4 个盘符、
   未分配空间可显示）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.application.contracts import DiskInfo
from app.application.partitions import (
    MAX_DATA_PARTITIONS,
    MIN_C_GB,
    MIN_DATA_GB,
    MIN_EXTRA_DATA_GB,
    MSR_GPT,
    PartitionLayoutError,
    allow_small_partitions,
    check_layout,
    check_minimums,
    create_partition_lines,
    data_partition_count,
    diskpart_lines,
    msr_delete_lines,
    partition_verdict,
    plan_partitions,
    strip_msr_lines,
    unallocated_bytes,
    wipe_lines,
)
from app.application.workflow import WorkflowController
from app.drive_letters import build_drive_letter_allocations

GIB = 1024 ** 3
MB = 1024 ** 2


def _legacy_runner_script(parts: list[Any], disk_number: int = 2) -> list[str]:
    """执行器现在分三段 diskpart：清盘 → 删 MSR（按真实分区号，单独跑）→ 建分区。

    这里把“清盘 + 建分区”两段拼起来（跳过删 MSR 那段，因为它依赖真实分区号），
    方便和重构前的单段脚本逐字对比。
    """
    wipe = wipe_lines(disk_number)
    layout = create_partition_lines(disk_number, parts)
    return [*wipe[:-1], *layout[1:-1], layout[-1]]


# ------------------------------------------------------------------ MSR 处理


@pytest.mark.parametrize("value", [None, "", "auto", 0, -1, "abc", True])
def test_strip_msr_lines_ignores_bad_numbers(value: Any) -> None:
    """查不到 MSR 分区号时不删任何东西（don't guess）。"""
    assert strip_msr_lines(value) == []


def test_strip_msr_lines_deletes_by_number() -> None:
    assert strip_msr_lines(1) == ["select partition 1", "delete partition override"]
    assert strip_msr_lines("3") == ["select partition 3", "delete partition override"]


def test_msr_delete_is_a_separate_step_before_layout() -> None:
    """``convert gpt`` 自动建的 MSR 单独一段删掉，建分区的脚本里不再出现删除。"""
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 100, "c_size_gb": 120}},
        letters={"efi": "F", "windows": "G", "data": "H"},
    )
    wipe = wipe_lines(1)
    assert wipe == ["select disk 1", "clean", "convert gpt", "exit"]

    delete = msr_delete_lines(1, 1)
    assert delete == ["select disk 1", "select partition 1", "delete partition override", "exit"]

    layout = create_partition_lines(1, parts)
    assert "delete partition override" not in layout
    assert layout[1] == "create partition efi size=100"


def _part_payload(
    *,
    msr=False,
    extra=False,
    efi_offset_mb=1,
    efi_size_mb=100,
    largest_free=0,
    c_size_gb=120,
    efi_letter="E",
    c_letter="F",
    data_letter="G",
    extra_letter="H",
    efi_fs="FAT32",
    c_fs="NTFS",
    data_fs="NTFS",
    extra_fs="NTFS",
    data_label="Data",
    extra_label="Data2",
    drop_efi_letter=False,
):
    """构造一份“完全符合计划”的真实分区表，再按测试需要逐项破坏。"""
    disk_mb = 477 * 1024
    efi_mb = efi_size_mb
    c_mb = int(c_size_gb * 1024)
    tail_mb = disk_mb - efi_mb - c_mb - (30 * 1024 if extra else 0) - 2
    items = [
        {
            "number": 1,
            "offset_bytes": efi_offset_mb * MB,
            "size_bytes": efi_mb * MB,
            "type": "System",
            "gpt_type": "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}",
            "drive_letter": None,
            # ESP 的盘符在 Get-Partition 里看不到，WMI 关联能看到
            "mounted_letter": None if drop_efi_letter else efi_letter,
            "file_system": efi_fs,
            "label": "EFI",
        },
        {
            "number": 2,
            "offset_bytes": (efi_offset_mb + efi_mb) * MB,
            "size_bytes": c_mb * MB,
            "type": "Basic",
            "gpt_type": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
            "drive_letter": c_letter,
            "mounted_letter": c_letter,
            "file_system": c_fs,
            "label": "Windows",
        },
    ]
    if extra:
        items.append(
            {
                "number": 3,
                "offset_bytes": (efi_offset_mb + efi_mb + c_mb) * MB,
                "size_bytes": 30 * 1024 * MB,
                "type": "Basic",
                "gpt_type": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
                "drive_letter": data_letter,
                "mounted_letter": data_letter,
                "file_system": data_fs,
                "label": "Data",
            }
        )
        items.append(
            {
                "number": 4,
                "offset_bytes": (efi_offset_mb + efi_mb + c_mb + 30 * 1024) * MB,
                "size_bytes": tail_mb * MB,
                "type": "Basic",
                "gpt_type": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
                "drive_letter": extra_letter,
                "mounted_letter": extra_letter,
                "file_system": extra_fs,
                "label": extra_label,
            }
        )
    else:
        items.append(
            {
                "number": 3,
                "offset_bytes": (efi_offset_mb + efi_mb + c_mb) * MB,
                "size_bytes": tail_mb * MB,
                "type": "Basic",
                "gpt_type": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
                "drive_letter": data_letter,
                "mounted_letter": data_letter,
                "file_system": data_fs,
                "label": data_label,
            }
        )
    if msr:
        items.insert(
            0,
            {
                "number": 1,
                "offset_bytes": 17 * 1024,
                "size_bytes": 16 * MB,
                "type": "Reserved",
                "gpt_type": f"{{{MSR_GPT}}}",
                "drive_letter": None,
            },
        )
    return {
        "disk_number": 1,
        "partition_style": "GPT",
        "disk_size_bytes": 477 * GIB,
        "largest_free": largest_free,
        "partitions": items,
    }


def _part_parts(extra: bool = False, c_size_gb: int = 120):
    payload = {"partition_info": {"efi_size_mb": 100, "c_size_gb": c_size_gb}}
    if extra:
        payload["partition_info"]["data_partitions"] = [
            {"size_gb": 30, "file_system": "NTFS"},
            {"size_gb": "auto", "file_system": "NTFS"},
        ]
    return plan_partitions(
        payload, letters={"efi": "E", "windows": "F", "data": "G", "data2": "H"}
    )


def test_partition_verdict_accepts_plan_exact_match() -> None:
    ok, message, details = partition_verdict(_part_payload(), _part_parts())
    assert ok is True, message
    assert "与计划一致" in message
    assert details["problems"] == []
    assert details["efi_partition"] == 1


def test_partition_verdict_accepts_four_partitions() -> None:
    ok, message, _ = partition_verdict(_part_payload(extra=True), _part_parts(extra=True))
    assert ok is True, message


def test_partition_verdict_rejects_leftover_msr() -> None:
    ok, message, _ = partition_verdict(_part_payload(msr=True), _part_parts())
    assert ok is False
    assert "保留分区" in message


def test_partition_verdict_rejects_wrong_efi_size() -> None:
    """EFI 建成 300 MB（计划 100 MB）→ 报大小不符，并定位到 EFI 那一行。"""
    payload = _part_payload(efi_size_mb=300)
    ok, message, _ = partition_verdict(payload, _part_parts())
    assert ok is False
    assert "第 1 个分区（EFI）大小不符" in message
    assert "计划 100 MB" in message


def test_partition_verdict_rejects_wrong_partition_count() -> None:
    ok, message, _ = partition_verdict(_part_payload(extra=True), _part_parts())
    assert ok is False
    assert "分区数与计划不符" in message


def test_partition_verdict_rejects_wrong_size() -> None:
    """C 实际只建了 100 GB（计划 120 GB）→ 必须报大小不符。"""
    payload = _part_payload(c_size_gb=100)
    ok, message, _ = partition_verdict(payload, _part_parts(c_size_gb=120))
    assert ok is False
    assert "大小不符" in message
    assert "计划 122880 MB" in message


def test_partition_verdict_rejects_wrong_drive_letter() -> None:
    """盘符被别的卷抢走 / assign 到错的分区 → 必须报盘符不符。"""
    payload = _part_payload(c_letter="G")
    ok, message, _ = partition_verdict(payload, _part_parts())
    assert ok is False
    assert "盘符不符" in message


def test_partition_verdict_rejects_wrong_label() -> None:
    payload = _part_payload(data_label="Data9")
    ok, message, _ = partition_verdict(payload, _part_parts())
    assert ok is False
    assert "卷标不符" in message


def test_partition_verdict_rejects_wrong_file_system() -> None:
    payload = _part_payload(data_fs="exFAT")
    ok, message, _ = partition_verdict(payload, _part_parts())
    assert ok is False
    assert "文件系统不符" in message


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("gpt_type", "GPT_TYPE_MISSING"),
        ("file_system", "FS_MISSING"),
        ("label", "LABEL_MISSING"),
    ],
)
def test_partition_verdict_rejects_missing_required_metadata(field, code) -> None:
    """查询缺字段是验收数据不完整，不能当成字段正确而放行。"""
    payload = _part_payload()
    payload["partitions"][1][field] = None
    ok, _message, details = partition_verdict(payload, _part_parts())
    assert ok is False
    assert code in {issue["code"] for issue in details["issues"]}


def test_partition_verdict_rejects_leftover_free_space() -> None:
    """最后一个分区没吃满（还剩 50 GB）→ 报未分配 + 容量不闭合。"""
    payload = _part_payload(largest_free=50 * GIB)
    ok, message, _ = partition_verdict(payload, _part_parts())
    assert ok is False
    assert "未分配" in message


def test_partition_verdict_allows_unallocated_space_for_explicit_plan() -> None:
    """所有分区都显式指定大小时，剩余空间是用户的选择，不应误判为失败。"""
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 100, "c_size_gb": 120, "data_size_gb": 5}}
    )
    payload = _part_payload(largest_free=50 * GIB)
    payload["partitions"][-1]["size_bytes"] = 5 * GIB

    ok, message, _ = partition_verdict(payload, parts)

    assert ok is True, message


def test_partition_verdict_warns_when_efi_letter_missing() -> None:
    """ESP 的临时字母查不到只告警：它是隐藏卷，任务收尾就释放。"""
    payload = _part_payload(drop_efi_letter=True)
    ok, message, details = partition_verdict(payload, _part_parts())
    assert ok is True, message
    assert details["warnings"], "应当有 ESP 盘符查不到的告警"
    assert "告警" not in "".join(details["problems"])


def test_partition_verdict_rejects_too_small_auto_partition() -> None:
    """auto 分区只剩 16 GB（下限 20 GB）→ 报低于下限。"""
    payload = _part_payload(c_size_gb=460)
    ok, message, _ = partition_verdict(payload, _part_parts(c_size_gb=460))
    assert ok is False
    assert "低于下限" in message


def test_partition_verdict_honours_explicit_small_partition_policy() -> None:
    """前置检查显式放宽后，最终验收必须使用同一下限策略。"""
    payload = _part_payload(c_size_gb=460)
    parts = _part_parts(c_size_gb=460)
    ok, message, details = partition_verdict(payload, parts, allow_small=True)
    assert ok is True, message
    assert details["allow_small"] is True


# --------------------------------------------------------------------- 默认布局


def test_default_layout_matches_legacy_diskpart_script() -> None:
    """EFI 300MB + C 30GB + D 剩余空间 → 与重构前的脚本逐字一致。"""
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 300, "c_size_gb": 30}},
        letters={"efi": "E", "windows": "F", "data": "G"},
    )

    assert [part.role for part in parts] == ["efi", "windows", "data"]
    assert _legacy_runner_script(parts) == [
        "select disk 2",
        "clean",
        "convert gpt",
        "create partition efi size=300",
        "format quick fs=fat32 label=EFI",
        "assign letter=E",
        "create partition primary size=30720",
        "format quick fs=ntfs label=Windows",
        "assign letter=F",
        "create partition primary",
        "format quick fs=ntfs label=Data",
        "assign letter=G",
        "exit",
    ]


def test_default_layout_uses_explicit_data_size() -> None:
    parts = plan_partitions(
        {"partition_info": {"efi_size_mb": 100, "c_size_gb": 10, "data_size_gb": 5}}
    )
    assert parts[-1].size_mb == 5120
    assert parts[-1].size_text == "5.00 GB"
    assert "create partition primary size=5120" in diskpart_lines(parts)


def test_default_layout_data_fs_is_always_ntfs() -> None:
    """数据分区固定 NTFS：连显式写 data_fs=exFAT 都要被拒。"""
    parts = plan_partitions({"partition_info": {"c_size_gb": 30, "data_size_gb": 10}})
    assert parts[-1].file_system == "NTFS"
    assert "format quick fs=ntfs label=Data" in diskpart_lines(parts)

    with pytest.raises(PartitionLayoutError) as info:
        plan_partitions(
            {"partition_info": {"c_size_gb": 30, "data_size_gb": 10, "data_fs": "exfat"}}
        )
    assert "文件系统不支持" in str(info.value)


def test_efi_size_is_clamped_to_minimum() -> None:
    parts = plan_partitions({"partition_info": {"efi_size_mb": 8, "c_size_gb": 30}})
    assert parts[0].size_mb == 64


def test_missing_c_size_is_rejected() -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions({"partition_info": {"efi_size_mb": 300}})
    assert "c_size_gb" in str(excinfo.value)


# --------------------------------------------------------------------- 额外分区


def test_extra_partition_appends_data2() -> None:
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 300,
                "c_size_gb": 120,
                "data_partitions": [
                    {"size_gb": 400, "file_system": "ntfs"},
                    {"size_gb": 500, "file_system": "ntfs"},
                ],
            }
        },
        letters={"efi": "E", "windows": "F", "data": "G", "data2": "H"},
    )

    assert [part.role for part in parts] == ["efi", "windows", "data", "data2"]
    assert [part.label for part in parts] == ["EFI", "Windows", "Data", "Data2"]
    assert [part.letter for part in parts] == ["E", "F", "G", "H"]
    assert parts[2].size_mb == 400 * 1024
    assert parts[3].size_mb == 500 * 1024
    assert parts[3].file_system == "NTFS"

    lines = diskpart_lines(parts)
    assert "create partition primary size=409600" in lines
    assert "create partition primary size=512000" in lines
    assert "format quick fs=ntfs label=Data2" in lines
    assert "assign letter=H" in lines


@pytest.mark.parametrize("size", [0, "abc", -5])
def test_data_partition_rejects_bad_size(size: Any) -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 100}, {"size_gb": size}],
                }
            }
        )
    assert "第 2 个数据分区" in str(excinfo.value)


@pytest.mark.parametrize("size", [None, "", "auto", "AUTO"])
def test_last_data_partition_may_eat_remaining_space(size: Any) -> None:
    """最后一个数据分区可以不写大小：diskpart 不写 ``size=`` 就会吃掉剩余空间。

    这比我们自己算“总容量 − C − EFI − 对齐余量”更准，也不会出现
    “算得刚刚好、diskpart 却报空间不足”。
    """
    parts = plan_partitions(
        {
            "partition_info": {
                "c_size_gb": 120,
                "data_partitions": [{"size_gb": 200}, {"size_gb": size}],
            }
        }
    )
    assert parts[2].size_mb == 200 * 1024
    assert parts[3].size_mb is None          # 吃掉剩余空间
    assert parts[3].size_text == "剩余空间"

    lines = diskpart_lines(parts)
    assert "create partition primary size=204800" in lines
    # 不带 size= 的那一行 = 用掉全部剩余空间
    assert "create partition primary" in lines
    assert lines.count("create partition primary") == 1


def test_only_last_data_partition_may_eat_remaining_space() -> None:
    """第一个数据分区不能 auto——否则它会把后面的分区的地盘也吃光。"""
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": "auto"}, {"size_gb": 100}],
                }
            }
        )
    assert "第 1 个数据分区" in str(excinfo.value)


def test_layout_with_auto_partition_only_checks_explicit_sizes() -> None:
    """有分区吃剩余空间时，仍要拦住“显式指定的那几块就超容量”的情况。"""
    payload = {
        "partition_info": {
            "c_size_gb": 900,
            "data_partitions": [{"size_gb": 200}, {"size_gb": "auto"}],
        }
    }
    parts = plan_partitions(payload)
    # EFI 100MB + C 900GB + D 200GB = 1100 GB，1 TB 盘放不下（auto 那块也得有地方）
    assert check_layout(parts, 1024 * 1024**3) != []
    # 同样的布局换成 2 TB 盘就合法
    assert check_layout(parts, 2048 * 1024**3) == []
    # 有 auto 分区时算不出未分配空间，返回 0（界面据此不提示“容量闲置”）
    assert unallocated_bytes(parts, 2048 * 1024**3) == 0


def test_extra_partition_rejects_fat32() -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 100}, {"size_gb": 100, "file_system": "FAT32"}],
                }
            }
        )
    assert "文件系统不支持" in str(excinfo.value)


def test_extra_partition_limit() -> None:
    with pytest.raises(PartitionLayoutError) as excinfo:
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 100}] * (MAX_DATA_PARTITIONS + 1),
                }
            }
        )
    assert "最多只能有" in str(excinfo.value)


def test_data_partition_count_reads_explicit_list() -> None:
    assert data_partition_count({"partition_info": {"c_size_gb": 120}}) == 1
    assert (
        data_partition_count(
            {"partition_info": {"c_size_gb": 120, "data_partitions": [{"size_gb": 1}]}}
        )
        == 1
    )
    assert (
        data_partition_count(
            {
                "partition_info": {
                    "c_size_gb": 120,
                    "data_partitions": [{"size_gb": 1}, {"size_gb": 2}],
                }
            }
        )
        == 2
    )


# --------------------------------------------------------------------- 容量与未分配


def test_layout_overflow_reports_sizes() -> None:
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 300,
                "c_size_gb": 30,
                "data_partitions": [{"size_gb": 50, "file_system": "NTFS"}],
            }
        }
    )
    assert check_layout(parts, 100 * GIB) == []
    problems = check_layout(parts, 80 * GIB)
    assert problems and "超过" in problems[0]
    assert "EFI 300MB" in problems[0]


def test_layout_with_rest_data_never_overflows() -> None:
    parts = plan_partitions({"partition_info": {"c_size_gb": 60, "data_size_gb": "auto"}})
    assert check_layout(parts, 100 * GIB) == []


def test_unallocated_bytes_counts_leftover() -> None:
    parts = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 300,
                "c_size_gb": 20,
                "data_partitions": [{"size_gb": 10, "file_system": "NTFS"}],
            }
        }
    )
    leftover = unallocated_bytes(parts, 100 * GIB)
    # 扣掉显式分区 + LAYOUT_SLACK_MB（GPT 首尾各 1 MB + 1 MB 冗余）
    assert leftover == 100 * GIB - (300 * 1024 * 1024 + 30 * GIB) - 3 * 1024 * 1024
    # 有分区吃剩余空间时没有“未分配”概念
    rest = plan_partitions({"partition_info": {"c_size_gb": 20}})
    assert unallocated_bytes(rest, 100 * GIB) == 0


# --------------------------------------------------------------------- 盘符


def test_drive_letters_allocate_four_for_extra_partition() -> None:
    allocations = build_drive_letter_allocations([1, 2], ["C", "D"], {1: 1})
    assert allocations[1] == {"efi": "E", "windows": "F", "data": "G", "data2": "H"}
    assert allocations[2] == {"efi": "I", "windows": "J", "data": "K"}


def test_drive_letters_pool_uses_a_and_b_as_fallback() -> None:
    """池是 E–Z（22 个）+ A/B 兜底（2 个）= 24。

    A/B 放最后：现代 Windows 没软驱时允许把 A:/B: 分给固定磁盘（dism 到 A: 根目录、
    bcdboot /s A: 实测可用），但老程序看到 A:/B: 会当软驱，所以只在字母紧张时用。
    """
    from app.drive_letters import AVAILABLE_DRIVE_LETTERS

    assert len(AVAILABLE_DRIVE_LETTERS) == 24
    assert AVAILABLE_DRIVE_LETTERS[:22] == list("EFGHIJKLMNOPQRSTUVWXYZ")
    assert AVAILABLE_DRIVE_LETTERS[-2:] == ["A", "B"]


def test_six_disks_with_extra_partition_fill_the_pool() -> None:
    """6 块盘 × 4 分区（EFI+C+D+E）= 24 个字母，正好用满。"""
    from app.drive_letters import AVAILABLE_DRIVE_LETTERS

    allocations = build_drive_letter_allocations(
        list(range(6)), ["C", "D"], {number: 1 for number in range(6)}
    )
    used = [letter for alloc in allocations.values() for letter in alloc.values()]
    assert len(used) == 24
    assert allocations[5] == {"efi": "Y", "windows": "Z", "data": "A", "data2": "B"}
    assert set(used) == set(AVAILABLE_DRIVE_LETTERS)


def test_seven_disks_with_extra_partition_do_not_fit() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        build_drive_letter_allocations(
            list(range(7)), ["C", "D"], {number: 1 for number in range(7)}
        )
    assert "可用盘符不足" in str(excinfo.value)
    assert "4 个盘符" in str(excinfo.value)


def test_eight_three_partition_disks_fill_the_pool() -> None:
    """三分区盘 8 块 × 3 = 24，最后一块落到 A/B。"""
    allocations = build_drive_letter_allocations(list(range(8)), ["C", "D"])
    assert allocations[7] == {"efi": "Z", "windows": "A", "data": "B"}


def test_drive_letters_report_short_pool() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        build_drive_letter_allocations([1], [letter for letter in "EFGHIJKLMNOPQRSTUVWXY"], {1: 1})
    assert "可用盘符不足" in str(excinfo.value)
    assert "4 个盘符" in str(excinfo.value)


# --------------------------------------------------------------------- 工作流接入


def _disk(number: int = 1, size_bytes: int = 100 * GIB) -> DiskInfo:
    return DiskInfo(
        disk_number=number,
        model="test disk",
        serial_number=None,
        unique_id=f"unique-{number}",
        size_bytes=size_bytes,
        size_display="100 GB",
        partition_style="RAW",
        bus_type="NVMe",
        drive_letters=(),
        is_boot=False,
        is_system=False,
        is_offline=False,
        is_read_only=False,
        is_selectable=True,
        protection_reasons=(),
    )


class _FakeService:
    def __init__(self, disks: list[DiskInfo]) -> None:
        self.disks = disks
        self.allowed_disk_numbers = frozenset()
        self.extra_counts: dict[int, int] | None = None

    def scan_protected_disks(self, excluded_names: Any = None) -> list[DiskInfo]:
        return list(self.disks)

    def scan_protected_snapshot(self) -> tuple[list[DiskInfo], tuple[str, ...]]:
        return list(self.disks), ()

    def build_selection_plan(self, payload, summaries, selected, **kwargs):
        self.extra_counts = dict(kwargs.get("extra_counts") or {})
        letters = build_drive_letter_allocations(
            list(selected), ["C", "D"], self.extra_counts
        )
        return SimpleNamespace(drive_letter_allocations=letters)

    def execute_spec(self, spec, cancel_event):
        from app.models import WorkerResult

        return WorkerResult(disk_number=int(getattr(spec, "disk_number", 0) or 0), success=True, message="ok")


def _controller(tmp_path, image_name: str = "win11.GHO") -> WorkflowController:
    """载荷来源：构造时的分区默认值快照 + 现读的设置（镜像路径）示例。"""
    image = tmp_path / image_name
    image.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))

    def preflight() -> dict[str, Any]:
        return {"results": [{"name": "运行前检查", "passed": True, "message": "ok"}]}

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        preflight_runner=preflight,
        partition_defaults={
            "efi_size_mb": 300,
            "c_size_gb": 20,
            # 单测用小尺寸：显式放宽后端下限（C ≥ 60 GB）
            "allow_small_partitions": True,
        },
        settings_provider=lambda: {"sysImageA": str(image)},
    )


def test_prepare_allocates_fourth_letter_for_extra_partition(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start()

    prepared = controller.prepare(
        {
            1: {
                "c_size": 20,
                "data_partitions": [
                    {"size_gb": 10, "file_system": "NTFS"},
                    {"size_gb": 15, "file_system": "NTFS"},
                ],
            }
        }
    )[0]

    assert prepared.plan.executable is True, prepared.plan.blockers
    assert prepared.plan.drive_letters == {
        "efi": "E",
        "windows": "F",
        "data": "G",
        "data2": "H",
    }
    assert [item["role"] for item in prepared.plan.partition_plan] == [
        "efi",
        "windows",
        "data",
        "data2",
    ]
    assert prepared.plan.partition_plan[-1]["file_system"] == "NTFS"
    assert prepared.plan.unallocated_bytes > 0
    assert prepared.spec.task_payload["partition_info"]["data_partitions"][1] == {
        "size_gb": 15.0,
        "file_system": "NTFS",
    }
    # 显式列表存在时老字段必须被清掉，避免两个来源打架
    assert "data_size_gb" not in prepared.spec.task_payload["partition_info"]


def test_prepare_rejects_invalid_extra_partition(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start()

    with pytest.raises(Exception) as excinfo:
        controller.prepare(
            {
                1: {
                    "data_partitions": [
                        {"size_gb": 10, "file_system": "NTFS"},
                        {"size_gb": 0, "file_system": "NTFS"},
                    ]
                }
            }
        )
    assert "分区配置无效" in str(excinfo.value)


def test_prepare_blocks_when_extra_partition_does_not_fit(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start()

    with pytest.raises(Exception) as excinfo:
        controller.prepare(
            {
                1: {
                    "c_size": 20,
                    "data_partitions": [
                        {"size_gb": 40, "file_system": "NTFS"},
                        {"size_gb": 60, "file_system": "NTFS"},
                    ],
                }
            }
        )
    assert "容量不足" in str(excinfo.value)


def test_prepare_without_extra_partition_keeps_three_letters(tmp_path) -> None:
    controller = _controller(tmp_path)
    controller.start()

    prepared = controller.prepare([1])[0]
    assert prepared.plan.drive_letters == {"efi": "E", "windows": "F", "data": "G"}
    assert [item["role"] for item in prepared.plan.partition_plan] == [
        "efi",
        "windows",
        "data",
    ]


# ------------------------------------------------------------------ 分区大小下限
def _no_flag_controller(tmp_path) -> WorkflowController:
    """不带 ``allow_small_partitions`` 的控制器：C 20 GB 必须在计划阶段被拒。"""
    image = tmp_path / "win11.GHO"
    image.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))

    def preflight() -> dict[str, Any]:
        return {"results": [{"name": "运行前检查", "passed": True, "message": "ok"}]}

    return WorkflowController(
        service=_FakeService([_disk(1)]),
        preflight_runner=preflight,
        partition_defaults={"efi_size_mb": 300, "c_size_gb": 20},
        settings_provider=lambda: {"sysImageA": str(image)},
    )


def test_check_minimums_blocks_tiny_partitions() -> None:
    tiny_c = plan_partitions({"partition_info": {"efi_size_mb": 100, "c_size_gb": 20}})
    assert any("系统分区至少 60 GB" in item for item in check_minimums(tiny_c, 500 * GIB))

    tiny_data = plan_partitions(
        {
            "partition_info": {
                "efi_size_mb": 100,
                "c_size_gb": 120,
                "data_partitions": [{"size_gb": 5}, {"size_gb": 0.5}],
            }
        }
    )
    problems = check_minimums(tiny_data, 1000 * GIB)
    assert any("Data 分区至少 20 GB" in item for item in problems)
    assert any("Data2 分区至少 1 GB" in item for item in problems)


def test_check_minimums_checks_auto_partition_remainder() -> None:
    """最后一个分区是 auto：剩下多少要够它自己的下限。"""
    parts = plan_partitions({"partition_info": {"efi_size_mb": 100, "c_size_gb": 120}})
    assert parts[-1].size_mb is None
    assert any("至少要 20 GB" in item for item in check_minimums(parts, 130 * GIB))
    assert check_minimums(parts, 500 * GIB) == []


def test_allow_small_partitions_flag_relaxes_minimums() -> None:
    payload = {
        "partition_info": {
            "efi_size_mb": 100,
            "c_size_gb": 20,
            "data_size_gb": 5,
            "allow_small_partitions": True,
        }
    }
    parts = plan_partitions(payload)
    assert check_minimums(parts, 100 * GIB)  # 没放宽时拦得住
    assert (
        check_minimums(parts, 100 * GIB, allow_small=allow_small_partitions(payload)) == []
    )


def test_prepare_rejects_tiny_layout_unless_flag(tmp_path) -> None:
    controller = _no_flag_controller(tmp_path)
    controller.start()

    with pytest.raises(Exception, match="分区过小"):
        controller.prepare([1])

    allowed = _controller(tmp_path)  # 同一个布局，但显式带了 allow_small_partitions
    allowed.start()
    assert allowed.prepare([1])[0].spec.disk_number == 1


def test_minimums_match_frontend_constants() -> None:
    """后端下限必须与 derive.js 一致，避免以后只有一端改了。"""
    from pathlib import Path

    derive = (
        Path(__file__).resolve().parents[1] / "app" / "gui_web" / "ui" / "js" / "derive.js"
    ).read_text(encoding="utf-8")
    assert f"export const MIN_SYS_GB = {MIN_C_GB};" in derive
    assert f"export const MIN_DATA_GB = {MIN_DATA_GB};" in derive
    assert f"export const MIN_EXTRA_GB = {MIN_EXTRA_DATA_GB};" in derive
