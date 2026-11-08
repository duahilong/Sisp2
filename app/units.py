"""字节换算与显示：整个仓库只此一份。

应用层（``contracts`` / ``partitions`` / ``service`` / ``workflow``）和
PowerShell 采集模块（``modules.disk_info``）都要格式化为 ``12.34 GB``，
以前各自抄了一份；分母或小数位的差异会让“界面显示的容量”和“验收比对
的容量”对不上。这里只依赖标准库，谁都可以 import，不产生分层倒挂。
"""

from __future__ import annotations

import math
from typing import Any

BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024


def format_size(size_bytes: Any) -> str:
    """把字节数格式化成 ``12.34 GB``；非法值给一句人话而不是抛异常。"""
    if size_bytes is None or isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)):
        return "未知"
    value = float(size_bytes)
    if math.isnan(value) or math.isinf(value) or value < 0:
        return "大小异常"
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


__all__ = ["BYTES_PER_GB", "BYTES_PER_MB", "format_size"]
