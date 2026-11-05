"""真实磁盘操作的运行时护栏。

**背景（2026-11-06 事故）**：重构时把分区实现从 ``WindowsDiskRunner`` 搬到了
``PartitionService``，但测试里仍是 ``runner._run_diskpart = fake`` 这种"直接赋值"。
Python 允许给实例挂任意属性，赋值成功但没人调用它——于是测试真的执行了

    diskpart /s <脚本>   →   select disk N / clean / convert gpt / create / format

本机一块数据盘被清空。这类事故的可怕之处是"测试看起来 mock 好了，实际上在写盘"。

护栏要解决的问题：**任何"改分区表 / 格式化 / 写引导"的命令，只要不是在明确授权的
生产环境里执行，就必须直接报错，而不是交给操作系统去执行。**

三道判断（任一命中 → 拦截）：

1. 实例没有显式授权（``allow_real_disk=False``，代码里的默认值）；
2. 进程处于测试环境（``PYTEST_CURRENT_TEST`` 存在）；
3. 环境变量 ``SISP_BLOCK_REAL_DISK=1``（``tests/conftest.py`` 会全局打开它）。

只拦"写"，不拦"读"：``Get-Disk`` / ``Get-Partition`` 这类只读 PowerShell 查询放行，
否则界面连硬盘列表都拿不到。判定规则：

- 外部程序只看**程序名**（``diskpart`` / ``format`` / ``bcdboot`` / ``dism`` / ``bootrec``）；
- PowerShell 只看脚本里的**写盘 cmdlet**（``Format-Volume`` / ``Clear-Disk`` /
  ``New-Partition`` …），并且先剥掉注释——校验脚本的注释里出现 "bcdboot" 是正常的，
  按文本匹配会误伤（实测踩过这个坑）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

# 环境变量：设为 1/true/yes/on 时拦截所有危险命令（测试里全局打开）。
BLOCK_ENV = "SISP_BLOCK_REAL_DISK"

# 一旦执行就可能改分区表 / 格式化 / 写引导的程序。
DANGEROUS_PROGRAMS = frozenset(
    {
        "diskpart",
        "diskpart.exe",
        "format",
        "format.com",
        "bcdboot",
        "bcdboot.exe",
        "dism",
        "dism.exe",
        "bootrec",
        "bootrec.exe",
    }
)

# PowerShell 只有这些 cmdlet 会写盘；只读的 Get-* 一律放行。
# 注意：这里**只列 PowerShell cmdlet**，不列外部程序名——注释里的词不该算危险。
DANGEROUS_POWERSHELL = (
    "format-volume",
    "clear-disk",
    "initialize-disk",
    "new-partition",
    "remove-partition",
    "set-partition",
    "resize-partition",
    "remove-volume",
    "format-partition",
    "new-volume",
    "set-disk",
    "add-partition",
)

_POWERSHELL_PROGRAMS = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
_DANGEROUS_CALL = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in DANGEROUS_POWERSHELL) + r")\b",
    re.IGNORECASE,
)
NEWLINE = chr(10)


class RealDiskBlocked(RuntimeError):
    """危险磁盘命令被护栏拦下；消息里说明原因与被拦的命令。"""


def block_reason() -> str | None:
    """当前进程是否处于"禁止真实写盘"的状态；返回原因，允许时返回 ``None``。"""
    value = os.environ.get(BLOCK_ENV, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return f"环境变量 {BLOCK_ENV}={value}"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "检测到 pytest 运行环境（PYTEST_CURRENT_TEST）"
    return None


def program_name(command: Sequence[str]) -> str:
    """取命令名（去掉路径与大小写）：``C:\\Windows\\System32\\diskpart.exe`` → ``diskpart.exe``。"""
    if not command:
        return ""
    first = str(command[0]).strip().strip('"')
    return first.replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()


def strip_comments(script: str) -> str:
    """去掉 ``#`` 之后的内容：注释里的词不该影响危险判定。"""
    return NEWLINE.join(line.split("#", 1)[0] for line in script.splitlines())


def is_dangerous_command(command: Sequence[str]) -> bool:
    """这条命令会不会改磁盘。

    - 外部程序只看程序名（``diskpart`` / ``format`` / ``bcdboot`` / ``dism``）；
    - PowerShell 只看脚本里有没有写盘 cmdlet（注释已剥离），只读查询一律放行。
    """
    name = program_name(command)
    if name in DANGEROUS_PROGRAMS:
        return True
    if name not in _POWERSHELL_PROGRAMS:
        return False
    script = strip_comments(" ".join(str(part) for part in command))
    return bool(_DANGEROUS_CALL.search(script))


def ensure_command_allowed(
    command: Sequence[str],
    *,
    allow_real_disk: bool = False,
    source: str = "",
) -> None:
    """执行前的唯一守门函数：不安全就抛 :class:`RealDiskBlocked`。

    ``allow_real_disk`` 默认 **False**：调用方必须显式声明"我这是生产环境、要真写盘"。
    这样即使将来重构又一次把 mock 搬丢，测试也只会失败，不会清盘。
    """
    if not is_dangerous_command(command):
        return
    if not allow_real_disk:
        raise RealDiskBlocked(
            f"{source or '调用方'} 未获得真实写盘授权（allow_real_disk=False），"
            f"已阻止危险命令：{program_name(command)}。"
            "生产入口请用 build_real_runner()，测试请注入 fake shell。"
        )
    reason = block_reason()
    if reason:
        raise RealDiskBlocked(
            f"已阻止危险命令（{reason}）：{program_name(command)}。"
            "测试或调试环境不允许真实写盘。"
        )
