"""硬盘标签：一块盘在**界面、日志、导出统计**里怎么称呼自己。

## 为什么需要单独一层

现场排查时最容易卡住的地方是「哪块盘？」。旧文案只有盘号：

    [13:40:23] 磁盘 2 执行失败：建分区失败（退出码 …）
    其它盘活动：磁盘 4=写镜像（Ghost）

盘号是**会变**的（热插拔、换口、少插一块盘），而且用户手上四块盘长得一模一样
时，`磁盘 2` 毫无辨识度。所以统一改成「型号（磁盘 N）」：

    Msft Virtual Disk（磁盘 2）执行失败：建分区失败（退出码 …）
    其它盘活动：GeIL P3A 2TB（磁盘 4）=写镜像（Ghost）

盘号仍然保留（型号相同的两块盘靠它区分，日志文件名也靠它定位），但**型号在前**，
统计与扫读都以型号为准。

## 三种形态

- ``style="full"``（默认）：``Msft Virtual Disk（磁盘 2）`` —— 句子里用；
- ``style="short"``：``磁盘 2 · Msft Virtual Disk`` —— 界面卡片标题，盘号在前；
- ``style="name"``：``Msft Virtual Disk`` —— 只有型号（导出表格的「硬盘」列）。

没有型号（扫描失败、虚拟盘没填、驱动的 ``None``）时一律退化成 ``磁盘 N``，
绝不出现 ``（磁盘 2）`` 前面空一截这种残句。纯函数，不碰磁盘。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.application.runner_utils import to_int, to_text

__all__ = [
    "clean_model",
    "disk_label",
    "disk_slug",
    "label_from_identity",
    "model_of",
]

# 驱动/扫描层可能给出的“等于没有型号”的占位值。
_BLANK_MODELS = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "未知",
    "未知型号",
    "无",
    "磁盘",
}

# 型号里不该出现的字符（换行、制表、全角空格），以及连续空白。
_WHITESPACE = re.compile(r"[\s\u00a0\u3000]+")
# Windows 文件名里非法/别扭的字符，做 slug 时统一换成短横线。
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

MAX_MODEL_CHARS = 60


def clean_model(value: Any) -> str:
    """规范化型号：去空白、压连续空格、截断；占位值一律视为空串。"""
    # ``to_text`` 对空值返回 ``None``（约定），这里必须立刻收敛成空串：
    # 正则和字符串方法都不接受 None。
    text = _WHITESPACE.sub(" ", to_text(value) or "").strip()
    if not text:
        return ""
    if text.casefold() in _BLANK_MODELS:
        return ""
    return text[:MAX_MODEL_CHARS]


def model_of(identity: Mapping[str, Any] | Any | None) -> str:
    """从身份字典 / 硬盘摘要里取型号（``model`` 优先，其次 ``friendly_name``）。"""
    if not isinstance(identity, Mapping):
        return ""
    return clean_model(identity.get("model")) or clean_model(identity.get("friendly_name"))


def disk_label(
    number: Any,
    model: Any = None,
    *,
    style: str = "full",
    size_display: Any = None,
) -> str:
    """``型号（磁盘 N）`` / ``磁盘 N · 型号`` / ``型号``；没有型号时只给盘号。

    ``size_display`` 只用于 ``style="full"`` 且**型号为空**的退化场景
    （``磁盘 2 · 120 GB``），避免“只有盘号”的标签在导出表格里还能区分容量。
    """
    try:
        parsed = to_int(number)
        disk = int(parsed) if parsed is not None else 0
    except (TypeError, ValueError):
        parsed = None
        disk = 0
    # Windows 的系统盘常见编号就是 0；只有缺失/非法/负数才显示“未知磁盘”。
    number_text = f"磁盘 {disk}" if parsed is not None and disk >= 0 else "未知磁盘"
    name = clean_model(model)
    size_text = (to_text(size_display) or "").strip()

    if not name:
        return f"{number_text} · {size_text}" if (style == "full" and size_text) else number_text
    if style == "name":
        return name
    if style == "short":
        return f"{number_text} · {name}"
    return f"{name}（{number_text}）"


def label_from_identity(identity: Mapping[str, Any] | Any | None, number: Any = None) -> str:
    """执行器侧最常用的入口：拿 ``spec.expected_identity`` 拼标签。"""
    source = identity if isinstance(identity, Mapping) else {}
    disk = to_int(number) if number is not None else to_int(source.get("disk_number"))
    return disk_label(disk, model_of(source), size_display=source.get("size_display"))


def disk_slug(model: Any, *, limit: int = 24) -> str:
    """型号 → 文件名片段（``msft-virtual-disk``）；没有可用字符时返回空串。

    日志文件名里带它，是为了让别人在资源管理器里一眼认出哪份日志是哪块盘，
    同时**不破坏** ``disk-runner-d2-*.log`` 这个既有约定（slug 排在 ``d2-`` 之后）。
    """
    text = clean_model(model).casefold()
    slug = _SLUG_STRIP.sub("-", text).strip("-")
    if not slug:
        return ""
    return slug[:limit].strip("-")
