"""执行层共用的小工具：类型转换、输出解码、可选项解析、错误类型。

放这里的原因：``real_runner`` / ``shell`` / ``phases/*`` 三层都要用，
放在任何一层都会让另外两层反向依赖它。本模块只依赖标准库。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class RunnerError(RuntimeError):
    """执行器内部的可读错误；会被包进对应步骤的 ``message``。"""


def to_int(value: Any) -> int | None:
    """宽松转 int：``bool`` / 非法值一律返回 ``None``（``"3.0"`` 也接受）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def to_text(value: Any) -> str | None:
    """去空白后的字符串；空串返回 ``None``。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def option_flag(
    options: Mapping[str, Any] | None,
    key: str,
    *,
    default: bool = True,
) -> bool:
    """读一个布尔型 runner option；缺省/无法识别时用 ``default``。

    只接受明确的假值（``False`` / ``0`` / ``"false"`` / ``"off"`` …），其余一律
    当作 ``default``——这样"设置里没写"和"写了个奇怪的值"都不会把功能默认关掉。
    """
    if not isinstance(options, Mapping) or key not in options:
        return default
    value = options.get(key)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def option_int(
    options: Mapping[str, Any] | None,
    key: str,
    *,
    default: int = 0,
    low: int | None = None,
    high: int | None = None,
) -> int:
    """读一个整数型 runner option；缺省/非法用 ``default``，并按需夹到区间内。

    为什么不用 ``int(options.get(...))``：``options`` 来自界面设置与历史任务载荷，
    里面可能是 ``None`` / ``"1000"`` / ``1200.0``，甚至负数。这里统一收敛，
    坏值退化成默认值而不是抛异常把任务打挂。
    """
    if not isinstance(options, Mapping) or key not in options:
        number = int(default)
    else:
        parsed = to_int(options.get(key))
        number = int(default) if parsed is None else parsed
    if low is not None:
        number = max(int(low), number)
    if high is not None:
        number = min(int(high), number)
    return number


def existing_dir(value: Any) -> str | None:
    """返回可用的临时目录（不存在则尝试创建）；不可用则返回 ``None``。"""
    text = to_text(value)
    if not text:
        return None
    try:
        Path(text).mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return text if Path(text).is_dir() else None


def decode_output(data: bytes | None) -> str:
    """diskpart/ghost 在中文 Windows 上按 OEM 代码页输出，先 mbcs 再兜底。"""
    if not data:
        return ""
    for encoding in ("mbcs", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…（前略）\n" + text[-limit:]


def runtime_env() -> dict[str, str]:
    """子进程环境：直接继承系统环境（``TMP``/``TEMP`` 用系统默认）。

    以前这里会把界面设置的临时目录塞进 ``TMP``/``TEMP``；那个设置项已经移除，
    子进程统一用系统临时目录，行为与手工在命令行里跑 Ghost/bcdboot 一致。
    """
    return dict(os.environ)


__all__ = [
    "RunnerError",
    "decode_output",
    "existing_dir",
    "option_flag",
    "option_int",
    "runtime_env",
    "tail",
    "to_int",
    "to_text",
]
