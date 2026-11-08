"""worker 进程侧的硬盘身份快照。

worker 是独立进程，拿不到主进程里的 ``DiskInfo`` 对象，只能收到一份纯字典。
这里把 ``DiskInfo.to_dict()`` 归一化成“能写进命令行、也能用来做事后校验”的身份：

- ``unique_id`` / ``serial_number`` 只要有一个非空，就认为身份足够强
  （与 ``workflow._is_strong_identity`` 的判定保持一致）；
- 两个都为空时不算强身份：写盘闸门会退回按“盘号 + 容量 + 型号”比对。
  这种弱比对比序列号粗，但仍然是执行前重扫一次的真比对，能拦住
  “确认时是这块盘、写时换了另一块”的大部分情况；
- 比较时忽略大小写和首尾空白。``UniqueId`` 在 Windows 上可能是
  ``\\?\\scsi#disk&ven_...`` 这种长串，硬编码大小写容易误判。

本模块只做数据归一化，不扫描、不写盘，也没有第三方依赖。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.application.contracts import format_size

IDENTITY_KEYS = (
    "disk_number",
    "unique_id",
    "serial_number",
    "model",
    "size_bytes",
    "size_display",
    "bus_type",
    "partition_style",
    "strong",
)


def _text(value: Any) -> str | None:
    """把任意值收敛成非空字符串；空白字符串视为缺失。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def is_strong_identity(identity: Mapping[str, Any] | None) -> bool:
    """有 UniqueId 或 SerialNumber 才算强身份。"""
    if not isinstance(identity, Mapping):
        return False
    return bool(_text(identity.get("unique_id")) or _text(identity.get("serial_number")))


def build_worker_disk_identity(disk: Mapping[str, Any] | None) -> dict[str, Any]:
    """把硬盘摘要（``DiskInfo.to_dict()`` 或扫描原始字典）转成身份字典。

    返回的键固定为 ``IDENTITY_KEYS``，缺值一律为 ``None``（``strong`` 为 bool），
    这样下游可以直接 JSON 序列化、拼命令行或做相等比较。
    """
    source: Mapping[str, Any] = disk if isinstance(disk, Mapping) else {}

    size_bytes = _int_or_none(source.get("size_bytes"))
    size_display = _text(source.get("size_display"))
    if size_display is None:
        size_display = format_size(size_bytes)

    # 扫描结果里模型名可能叫 model（summarize_disk 的输出）或 friendly_name
    model = _text(source.get("model")) or _text(source.get("friendly_name"))

    identity: dict[str, Any] = {
        "disk_number": _int_or_none(source.get("disk_number")),
        "unique_id": _text(source.get("unique_id")),
        "serial_number": _text(source.get("serial_number")),
        "model": model,
        "size_bytes": size_bytes,
        "size_display": size_display,
        "bus_type": _text(source.get("bus_type")),
        "partition_style": _text(source.get("partition_style")),
    }
    identity["strong"] = is_strong_identity(identity)
    return identity


def _same_text(left: Any, right: Any) -> bool:
    """忽略大小写/空白比较两个身份字段；都为空也算相同（由调用方决定是否允许）。"""
    return (_text(left) or "").casefold() == (_text(right) or "").casefold()


def identity_matches(
    expected: Mapping[str, Any] | None,
    actual: Mapping[str, Any] | None,
    *,
    require_strong: bool = True,
) -> bool:
    """确认“确认时看到的盘”和“worker 现在摸到的盘”是同一块。

    - ``require_strong=True`` 时，期望身份必须带 UniqueId 或 SerialNumber，
      且期望里出现的标识字段都要对得上（适合“有无标识都必须强校验”的调用方）；
    - ``require_strong=False`` 时允许只比对硬盘编号 + 容量 + 型号。
      写盘闸门用的就是这一档：有标识仍逐项比对，没有标识的盘（部分 USB
      桥接/虚拟盘）也不会被一律拒绝。
    """
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        return False
    if require_strong and not is_strong_identity(expected):
        return False

    expected_number = _int_or_none(expected.get("disk_number"))
    actual_number = _int_or_none(actual.get("disk_number"))
    if expected_number is not None and actual_number is not None and expected_number != actual_number:
        return False

    checked = False
    for key in ("unique_id", "serial_number"):
        left = _text(expected.get(key))
        if left is None:
            # 期望身份没这个字段：不比较，避免把“扫不到序列号”当成换盘。
            continue
        right = _text(actual.get(key))
        if right is None or not _same_text(left, right):
            return False
        checked = True

    if checked:
        return True

    # 没有唯一标识可比时退回弱比对：容量必须一致，型号一致或缺失。
    if _int_or_none(expected.get("size_bytes")) != _int_or_none(actual.get("size_bytes")):
        return False
    expected_model = _text(expected.get("model"))
    actual_model = _text(actual.get("model"))
    return not (expected_model and actual_model and not _same_text(expected_model, actual_model))
