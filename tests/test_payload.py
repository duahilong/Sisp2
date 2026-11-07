"""``app.application.payload`` 与分区入口断言的行为契约。

这一层的存在理由：任务参数是一个**纯字典**，键名只以 ``.get()`` 的形式散落在
执行链各处（``plan_partitions``、``real_runner``、``phases/ghost``）。键名写错不
会报错，只会在 Ghost 那一步变成一个 ``None`` 拼进命令行。所以：

- ``TaskPayload`` 把形状写下来（TypedDict，运行时零开销）；
- ``plan_partitions`` 入口用 ``require_layout_keys`` 断言，最早在计划阶段炸开；
- ``build_task_payload`` 是唯一的构造口，空值统一丢弃（空串 ≠ 有效值）。
"""

from __future__ import annotations

import pytest

from app.application.partitions import (
    EFI_DEFAULT_MB,
    PartitionLayoutError,
    plan_partitions,
)
from app.application.payload import (
    REQUIRED_PARTITION_KEYS,
    build_task_payload,
    require_layout_keys,
)

# --------------------------------------------------------------- build_task_payload


def test_build_from_nothing_is_empty() -> None:
    assert build_task_payload() == {}


def test_build_drops_blank_values() -> None:
    payload = build_task_payload(
        partition_info={
            "c_size_gb": 60,
            "efi_size_mb": None,
            "data_size_gb": "",
            "data_fs": "   ",
            "data_partitions": [],
        },
        image_info={"image_path": "  "},
    )
    assert payload == {"partition_info": {"c_size_gb": 60}}


def test_build_keeps_zero_and_auto() -> None:
    """``0`` 与 ``"auto"`` 都是有效值：分区块序号固定 1，``"auto"`` 是“吃掉剩余空间”。"""
    payload = build_task_payload(
        partition_info={"c_size_gb": 60, "data_size_gb": "auto"},
        image_info={"image_path": "D:/images/test.GHO"},
    )
    assert payload["partition_info"]["data_size_gb"] == "auto"
    assert payload["image_info"]["image_path"] == "D:/images/test.GHO"


def test_build_replaces_whole_section_over_base() -> None:
    """分区段是**整段替换**，不是逐键合并：设置里的默认值要能压掉旧值。"""
    base = build_task_payload(
        partition_info={"c_size_gb": 120, "efi_size_mb": 300},
        image_info={"image_path": "D:/a.gho"},
    )
    merged = build_task_payload(base=base, partition_info={"c_size_gb": 60})
    assert merged["partition_info"] == {"c_size_gb": 60}
    # 没被覆盖的段原样保留
    assert merged["image_info"] == {"image_path": "D:/a.gho"}


def test_build_does_not_mutate_base() -> None:
    base = build_task_payload(partition_info={"c_size_gb": 60})
    snapshot = str(base)
    build_task_payload(base=base, partition_info={"c_size_gb": 60, "data_fs": "NTFS"})
    assert str(base) == snapshot


def test_build_rejects_non_mapping_section() -> None:
    with pytest.raises(PartitionLayoutError):
        build_task_payload(partition_info="c_size_gb=60")  # type: ignore[arg-type]


# ------------------------------------------------------------ require_layout_keys


def test_require_layout_keys_passes_when_present() -> None:
    payload = {"partition_info": {"c_size_gb": 60}}
    assert require_layout_keys(payload) is payload


@pytest.mark.parametrize("missing", [{}, {"partition_info": {}}, {"partition_info": None}])
def test_require_layout_keys_raises_on_missing(missing: dict[str, object]) -> None:
    with pytest.raises(PartitionLayoutError) as info:
        require_layout_keys(missing)
    # 文案与历史版本一字不差：界面和文档都在引用它。
    assert "partition_info.c_size_gb 必须是正整数" in str(info.value)


def test_required_keys_are_documented() -> None:
    assert REQUIRED_PARTITION_KEYS == ("c_size_gb",)


def test_plan_partitions_asserts_entry() -> None:
    """断言必须发生在算容量之前：缺 c_size_gb 时不能先算出半个计划。"""
    with pytest.raises(PartitionLayoutError) as info:
        plan_partitions({"partition_info": {"efi_size_mb": 300}})
    assert "partition_info.c_size_gb 必须是正整数" in str(info.value)


def test_plan_partitions_defaults_efi_when_absent() -> None:
    parts = plan_partitions({"partition_info": {"c_size_gb": 60}})
    efi = parts[0]
    assert efi.size_mb == EFI_DEFAULT_MB


# --------------------------------------------------------------- 数据分区文件系统


def test_data_fs_is_ntfs_by_default() -> None:
    parts = plan_partitions({"partition_info": {"c_size_gb": 60}})
    assert parts[-1].file_system == "NTFS"


def test_exfat_is_rejected() -> None:
    """exFAT 已从产品移除；旧任务文件里的 data_fs=exFAT 必须被明确拒绝。"""
    with pytest.raises(PartitionLayoutError) as info:
        plan_partitions({"partition_info": {"c_size_gb": 60, "data_fs": "exFAT"}})
    assert "文件系统不支持" in str(info.value)


def test_exfat_in_extra_data_partition_is_rejected() -> None:
    with pytest.raises(PartitionLayoutError):
        plan_partitions(
            {
                "partition_info": {
                    "c_size_gb": 60,
                    "data_partitions": [
                        {"size_gb": 20, "file_system": "exfat"},
                        {"size_gb": "auto"},
                    ],
                }
            }
        )
