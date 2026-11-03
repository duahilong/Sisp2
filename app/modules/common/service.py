"""公共底座：PowerShell 执行与扫描超时。

这个模块只放“多个业务模块都要用、又不属于任何单一业务”的东西，保持零业务
依赖（只 import 标准库），避免出现循环导入：

- ``run_powershell``：统一执行 PowerShell 脚本，负责超时、退出码和中文编码；
- ``POWERSHELL_SCAN_TIMEOUT``：磁盘扫描类脚本的默认超时（秒）。

约定（来自调用点，改动前务必先看调用方）：``run_powershell`` 返回的是
``subprocess.CompletedProcess``，调用方用 ``completed.returncode`` /
``completed.stdout`` / ``completed.stderr`` 判断成功与否，因此超时和“找不到
powershell.exe”也做成“返回失败结果”，而不是抛异常，让调用方能拼出一条完整的
中文错误信息。
"""

from __future__ import annotations

import locale
import subprocess
import sys

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

__all__ = [
    "POWERSHELL_SCAN_TIMEOUT",
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


# 先把 PowerShell 的输出编码钉成 UTF-8，再交给 _decode_console_output 解。
#
# 默认情况下（Windows PowerShell 5.1、输出被重定向）它按控制台代码页写字节，
# 简体中文机器上是 cp936。个别汉字的 cp936 字节恰好也是合法 UTF-8，例如
# 「系统」= CF B5 CD B3 —— UTF-8 解码器会“成功”解出 ϵͳ，于是卷标变成乱码，
# 而且不会抛异常。所以不能靠事后猜编码，必须在源头写 UTF-8。
# 没有控制台（--windowed / 服务进程）时设置会抛错，吞掉即可。
_UTF8_OUTPUT_PROLOGUE = (
    "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }; "
    "$OutputEncoding = [Text.Encoding]::UTF8; "
)


def run_powershell(script: str, timeout: float | None = POWERSHELL_SCAN_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """执行一段 PowerShell 脚本，返回 ``CompletedProcess[str]``。

    参数：
        script：要执行的脚本文本，通过 ``-Command`` 传入（不是临时文件）。
        timeout：超时秒数，``None`` 表示不限制；默认 ``POWERSHELL_SCAN_TIMEOUT``。

    返回：
        ``subprocess.CompletedProcess``，``stdout`` / ``stderr`` 已经是 ``str``。
        脚本前面会先挂一段把输出编码设成 UTF-8 的引子（见
        ``_UTF8_OUTPUT_PROLOGUE``），所以正常情况按 UTF-8 解就对；返回值的解码
        仍保留“UTF-8 → 本机代码页”的兜底，调用方直接判 ``returncode != 0`` 即可。

    注意：超时和“找不到 powershell.exe”同样以返回值表达（退出码 124 / 127，
    中文说明放在 ``stderr``），这样调用方抛出的错误信息里既有退出码也有原因。
    """
    if not isinstance(script, str) or not script.strip():
        raise ValueError("PowerShell 脚本不能为空")

    argv = [
        _POWERSHELL_EXECUTABLE,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _UTF8_OUTPUT_PROLOGUE + script,
    ]

    try:
        completed = subprocess.run(  # noqa: S603 - argv 由本模块按固定模板拼装
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
