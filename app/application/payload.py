"""任务载荷：一次装机要用到的全部参数，以及它们的唯一形状定义。

为什么需要这个模块
------------------

``payload`` 本来是一个纯字典，字段名只存在于各个消费方的 ``.get("...")`` 里：
``partitions`` 读 ``c_size_gb``、``real_runner`` 读 ``image_info``、``pipeline``
整包透传……改一个键名要靠全局搜索，写错一个键名则静默变成 ``None``，一路
排到 Ghost 还原才失败——而那时候盘已经被清空了。

这里把形状写成 :class:`TaskPayload`，并提供两个入口：

- :func:`build_task_payload` —— **唯一**的构造入口（设置 → 载荷）；
- :func:`require_layout_keys` —— 规划分区前的显式门禁。

载荷来源（重构后）
------------------

界面设置（分区默认值、镜像位置）+ 向导里每块盘的逐盘覆盖。
不再有任何“配置文件优先 / 设置优先”的歧义。

约定
----

- 载荷必须能被 ``json.dumps`` 序列化：它会原样写进 ``JobSpec`` 随任务一起落盘。
- 空值不写：``build_task_payload`` 会丢掉空字符串 / ``None`` / 空列表，
  这样 ``payload == {}`` 与「什么都没配」等价，下游的 ``or {}`` 判断不会失效。
- 键名一律用长名（``c_size_gb``）。短名（``c_size`` / ``efi_size`` / ``win_gho``）
  只在向导的逐盘覆盖里出现，由 ``workflow._merge_payload`` 负责映射，不进载荷。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

__all__ = [
    "REQUIRED_PARTITION_KEYS",
    "ImageInfo",
    "PartitionInfo",
    "PartitionLayoutError",
    "TaskPayload",
    "build_task_payload",
    "require_layout_keys",
]


class PartitionLayoutError(ValueError):
    """布局不合法：缺大小、分区太多、文件系统不支持……消息直接给界面用。

    定义在这里（而不是 ``partitions``）是为了让 :func:`require_layout_keys`
    与 ``partitions`` 都能抛同一个类型，同时避免两个模块循环导入。
    ``partitions`` 仍然重新导出这个名字，老的 ``from app.application.partitions
    import PartitionLayoutError`` 继续有效。
    """


# 规划分区时**必须**存在的键；缺了直接报错，不要拖到写盘阶段。
REQUIRED_PARTITION_KEYS: tuple[str, ...] = ("c_size_gb",)


class PartitionInfo(TypedDict, total=False):
    """``payload["partition_info"]`` 的形状。

    ``total=False`` 是有意的：只有 :data:`REQUIRED_PARTITION_KEYS` 里的键
    必须出现，其余都给下游一个合理缺省（EFI 100 MB、数据分区吃剩余空间）。
    """

    efi_size_mb: float
    c_size_gb: float
    data_size_gb: float | None
    data_partitions: list[dict[str, Any]]
    data_fs: str
    allow_small_partitions: bool


class ImageInfo(TypedDict, total=False):
    """``payload["image_info"]`` 的形状。"""

    image_path: str


class TaskPayload(TypedDict, total=False):
    """交给 ``JobSpec`` 的完整载荷。"""

    partition_info: PartitionInfo
    image_info: ImageInfo


def _clean(value: Any) -> bool:
    """这个值是否算“没填”（空字符串 / None / 空列表）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _merge_section(
    target: dict[str, Any],
    key: str,
    values: Mapping[str, Any] | None,
) -> None:
    if not values:
        return
    if not isinstance(values, Mapping):
        raise PartitionLayoutError(
            f"{key} 必须是一段配置（字典），收到的是 {type(values).__name__}"
        )
    section = {
        str(name): value
        for name, value in dict(values).items()
        if not _clean(value)
    }
    if section:
        target[key] = section


def build_task_payload(
    *,
    partition_info: Mapping[str, Any] | None = None,
    image_info: Mapping[str, Any] | None = None,
    base: Mapping[str, Any] | None = None,
) -> TaskPayload:
    """构造一份任务载荷：``base`` 打底，其余两节覆盖其上。

    ``base`` 用于把已有载荷（例如会话里那份）再叠一层，其余参数优先。
    空值一律丢弃，保证 ``payload == {}`` 表示「什么都没配」。
    """
    payload: dict[str, Any] = dict(base or {})
    _merge_section(payload, "partition_info", partition_info)
    _merge_section(payload, "image_info", image_info)
    return payload  # type: ignore[return-value]


def require_layout_keys(payload: Mapping[str, Any] | None) -> TaskPayload:
    """规划分区前检查必需键；缺了就抛 :class:`PartitionLayoutError`。

    消息文案与 ``partitions.plan_partitions`` 里原有的那句完全一致——它会被
    界面直接显示，也会被测试断言，所以这里刻意不改措辞。
    """
    info: Mapping[str, Any] = {}
    if isinstance(payload, Mapping):
        raw = payload.get("partition_info")
        if isinstance(raw, Mapping):
            info = raw
    for key in REQUIRED_PARTITION_KEYS:
        if _clean(info.get(key)):
            raise PartitionLayoutError(f"配置里 partition_info.{key} 必须是正整数")
    return payload  # type: ignore[return-value]
