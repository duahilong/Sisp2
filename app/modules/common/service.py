"""公共底座：PowerShell 执行、扫描超时、CLI 步骤输出、系统盘盘符。

这个模块只放“多个业务模块都要用、又不属于任何单一业务”的东西，保持零业务
依赖（只 import 标准库），避免出现循环导入：

- ``run_powershell``：统一执行 PowerShell 脚本，负责超时、退出码和中文编码；
- ``POWERSHELL_SCAN_TIMEOUT``：磁盘扫描类脚本的默认超时（秒）；
- ``print_step_results``：CLI 里打印“步骤 -> 结果”清单的统一格式；
- ``get_system_drive_letter``：取系统盘盘符，返回单字母（``"C"``，不带冒号）。

约定（来自调用点，改动前务必先看调用方）：

- ``run_powershell`` 返回的是 ``subprocess.CompletedProcess``，调用方用
  ``completed.returncode`` / ``completed.stdout`` / ``completed.stderr`` 判断
  成功与否，因此超时和“找不到 powershell.exe”也做成“返回失败结果”，而不是
  抛异常，让调用方能拼出一条完整的中文错误信息。
- ``get_system_drive_letter`` 返回单字母（大写、不带冒号）。盘符模块直接把它塞进
  ``{"A", "B", "C", get_system_drive_letter()}`` 这样的单字母集合里比较。
"""

from __future__ import annotations

import locale
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------- 常量

# 磁盘扫描（Get-Disk / Get-Partition / Get-Volume）在慢盘、USB 机械盘上会明显
# 变慢，60 秒足够覆盖正常机器，又不至于让界面卡死太久。
POWERSHELL_SCAN_TIMEOUT = 60

# 统一的 PowerShell 入口；走 PATH 解析，等价于 ``powershell.exe``。
_POWERSHELL_EXECUTABLE = "powershell"

# 超时用 124（与 GNU timeout 一致），找不到解释器用 127（与 shell 约定一致），
# 这两个值都会原样出现在调用方的“退出码: N”报错里。
_POWERSHELL_TIMEOUT_EXIT_CODE = 124
_POWERSHELL_NOT_FOUND_EXIT_CODE = 127

# GUI 进程里不应该弹出黑色控制台窗口；非 Windows 平台取不到这个标志。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

_UTF8_BOM = b"\xef\xbb\xbf"

# 步骤结果里用来取“是否通过”的候选键，按优先级排列。
_PASSED_KEYS = ("passed", "ok", "success")

# 步骤结果里用来取“这一步是什么”的候选键。
_NAME_KEYS = ("name", "step", "title", "label")

# 步骤结果里用来取“说明文字”的候选键。
_MESSAGE_KEYS = ("message", "detail", "text", "reason")

_SEPARATOR = "-" * 60

__all__ = [
    "POWERSHELL_SCAN_TIMEOUT",
    "get_system_drive_letter",
    "print_step_results",
    "run_powershell",
]


# ---------------------------------------------------------------- 编码


def _console_encodings() -> tuple[str, ...]:
    """返回解码 PowerShell 输出时要依次尝试的编码（去重、保序）。

    Windows 上 PowerShell 的重定向输出既可能是 UTF-8（系统开了 UTF-8 控制台，
    或者用的是 PowerShell 7），也可能是 OEM/ANSI 代码页（简体中文机器上是
    ``cp936``）。没人能提前确定，所以按“UTF-8 优先、再退回本机代码页”的顺序
    逐个严格尝试。
    """
    candidates: list[str] = ["utf-8"]
    for encoding in (
        locale.getpreferredencoding(False),
        "gbk",
        "cp936",
        "mbcs",
    ):
        if encoding:
            candidates.append(encoding)

    unique: list[str] = []
    for encoding in candidates:
        normalized = encoding.strip().lower()
        if normalized and normalized not in unique:
            unique.append(normalized)
    return tuple(unique)


def _decode_console_output(data: bytes | str | None) -> str:
    """把 PowerShell 的原始输出解码成 ``str``。

    先剥 UTF-8 BOM；随后逐个编码严格解码，全失败才用 ``utf-8`` + ``replace``
    兜底，保证任何情况下都不会因为编码问题炸掉扫描流程。
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    if data.startswith(_UTF8_BOM):
        return data[len(_UTF8_BOM):].decode("utf-8", errors="replace")

    for encoding in _console_encodings():
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- PowerShell


def run_powershell(script: str, timeout: float | None = POWERSHELL_SCAN_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """执行一段 PowerShell 脚本，返回 ``CompletedProcess[str]``。

    参数：
        script：要执行的脚本文本，通过 ``-Command`` 传入（不是临时文件）。
        timeout：超时秒数，``None`` 表示不限制；默认 ``POWERSHELL_SCAN_TIMEOUT``。

    返回：
        ``subprocess.CompletedProcess``，``stdout`` / ``stderr`` 已经是 ``str``
        （编码按 UTF-8 -> 本机代码页 的顺序自动识别），调用方直接判
        ``returncode != 0`` 即可。

    注意：超时和“找不到 powershell.exe”同样以返回值表达（退出码 124 / 127，
    中文说明放在 ``stderr``），这样调用方抛出的错误信息里既有退出码也有原因。
    """
    if not isinstance(script, str) or not script.strip():
        raise ValueError("PowerShell 脚本不能为空")

    argv = [_POWERSHELL_EXECUTABLE, "-NoProfile", "-NonInteractive", "-Command", script]

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        detail = _decode_console_output(exc.stderr).strip()
        timeout_message = f"PowerShell 执行超时（超过 {timeout} 秒），已终止"
        if detail:
            timeout_message = f"{timeout_message}\n{detail}"
        return subprocess.CompletedProcess(
            argv,
            _POWERSHELL_TIMEOUT_EXIT_CODE,
            _decode_console_output(exc.stdout),
            timeout_message,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            argv,
            _POWERSHELL_NOT_FOUND_EXIT_CODE,
            "",
            f"无法启动 PowerShell，请确认系统已安装 Windows PowerShell：{exc}",
        )

    return subprocess.CompletedProcess(
        argv,
        completed.returncode,
        _decode_console_output(completed.stdout),
        _decode_console_output(completed.stderr),
    )


# ---------------------------------------------------------------- 步骤输出


def _first_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    """按 ``keys`` 顺序取第一个非空文本字段。"""
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _is_passed(item: Mapping[str, Any]) -> bool:
    """判断一条步骤结果是否通过；缺省视为未通过。"""
    for key in _PASSED_KEYS:
        if key in item:
            return bool(item.get(key))
    return False


def _describe_item(item: Mapping[str, Any], index: int) -> str:
    """生成单条结果的标题：优先自定义名称，其次硬盘编号，最后用序号。"""
    name = _first_text(item, _NAME_KEYS)
    if name:
        return name

    disk_number = item.get("disk_number")
    if isinstance(disk_number, int) and not isinstance(disk_number, bool):
        return f"硬盘 {disk_number}"
    if disk_number is not None and str(disk_number).strip():
        return f"硬盘 {disk_number}"
    return f"第 {index} 项"


def print_step_results(
    title: str,
    results: Iterable[Mapping[str, Any]] | None,
    line: Callable[[str], None] | None = None,
) -> list[str]:
    """按统一格式打印一组步骤结果，并返回打印过的每一行。

    参数：
        title：这组结果的标题，例如 ``"分区格式化验证"``。
        results：结果序列，每项至少有 ``passed`` 和 ``message`` 两个字段
            （也兼容 ``ok`` / ``success`` 和 ``detail`` / ``text``）；
            ``disk_number``、``name`` 用来生成更直观的行首标题。
        line：可选的逐行回调。传入时不再直接 ``print``，而是把每一行交给回调
            （CLI 可以打印到 stdout，GUI 可以塞进日志缓冲）；传 ``None`` 时
            直接 ``print``。

    返回：
        本次实际输出的所有行（已去掉行尾换行），方便调用方二次利用或写日志。
    """
    header = str(title or "").strip() or "步骤结果"
    items = [item for item in (results or []) if isinstance(item, Mapping)]

    lines: list[str] = [f"== {header} =="]
    passed_count = 0

    if not items:
        lines.append("（没有需要展示的结果）")
    else:
        for index, item in enumerate(items, start=1):
            passed = _is_passed(item)
            if passed:
                passed_count += 1
            mark = "通过" if passed else "失败"
            detail = _first_text(item, _MESSAGE_KEYS) or ("通过" if passed else "未通过")
            lines.append(f"[{mark}] {_describe_item(item, index)}: {detail}")

        failed_count = len(items) - passed_count
        lines.append(_SEPARATOR)
        lines.append(f"共 {len(items)} 项，通过 {passed_count} 项，失败 {failed_count} 项")

    for text in lines:
        if line is not None:
            line(text)
        else:
            print(text, flush=True)
    return lines


# ---------------------------------------------------------------- 盘符


def _normalize_drive_letter(value: Any) -> str | None:
    """把 ``"C:"`` / ``"C:\\"`` / ``"c"`` / ``"C:\\Windows"`` 归一成 ``"C"``。

    只接受 A-Z 的单字母盘符，其余一律返回 ``None``，交给下一条兜底路径。
    """
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None

    letter = text[0].upper()
    if "A" <= letter <= "Z":
        # 必须真的是盘符开头（形如 "C"、"C:"、"C:\\"），避免把任意单词首字母当盘符。
        rest = text[1:]
        if not rest or rest.startswith(":") or rest.startswith("\\") or rest.startswith("/"):
            return letter
    return None


def get_system_drive_letter() -> str:
    """返回系统盘盘符，单字母大写、不带冒号（本机为 ``"C"``）。

    取值顺序：
        1. 环境变量 ``SystemDrive``（Windows 上一定有，形如 ``"C:"``）；
        2. 环境变量 ``SystemRoot``（形如 ``"C:\\Windows"``）；
        3. 当前 Python 解释器所在盘；
        4. 当前工作目录所在盘；
        5. 全部拿不到时退回 ``"C"``。

    不使用 PowerShell，保证在 GUI 启动早期、权限受限时也能立刻拿到结果。
    """
    candidates = (
        os.environ.get("SystemDrive"),
        os.environ.get("SystemRoot"),
        sys.executable,
        os.getcwd(),
    )
    for candidate in candidates:
        letter = _normalize_drive_letter(candidate)
        if letter:
            return letter
    return "C"
