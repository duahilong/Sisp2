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


def runtime_env(options: Mapping[str, Any] | None, log: Any) -> dict[str, str]:
    """子进程环境：界面里的临时目录优先生效（TMP/TEMP）。"""
    env = dict(os.environ)
    configured = to_text((options or {}).get("temp_dir"))
    if not configured:
        return env
    temp_dir = existing_dir(configured)
    if not temp_dir:
        log.write(f"临时目录不可用，改用系统默认：{configured}")
        return env
    env["TMP"] = temp_dir
    env["TEMP"] = temp_dir
    log.write(f"临时目录：{temp_dir}")
    return env


__all__ = [
    "RunnerError",
    "decode_output",
    "existing_dir",
    "option_flag",
    "runtime_env",
    "tail",
    "to_int",
    "to_text",
]
