r"""真实磁盘操作的运行时护栏。

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

只拦"写"，不拦"读"：``Get-Disk`` / ``Get-Partition`` / ``dism /Get-WimInfo`` 这类只读
查询放行，否则界面连硬盘列表和镜像索引都拿不到。判定规则：

- 外部程序看**程序名**（``diskpart`` / ``format`` / ``bcdboot`` / ``bootrec``）与**参数**
  （``dism`` 只有 ``/Get-*`` 只读动词放行，``/Apply-Image`` / ``/Add-Driver`` 一律危险）；
- PowerShell 看四件事，任一命中即危险：

  1. 脚本里的**写盘 cmdlet**（``Format-Volume`` / ``Clear-Disk`` / ``New-Partition`` /
     ``Mount-DiskImage`` …）；
  2. 脚本里的**外部写盘程序调用**（``Start-Process diskpart``、``& 'C:\…\bcdboot.exe'``），
     否则 ``-Command "Start-Process diskpart -ArgumentList '/s','x.txt'"`` 会绕过程序名检查；
  3. **看不到内容的调用**：``-File script.ps1``（脚本不在命令行上）、``-EncodedCommand``
     （base64）与 ``Invoke-Expression`` / ``iex``（动态执行拼出来的字符串），
     文本无法判定，只能按危险处理；
  4. 以上文本判定都要**先剥掉注释**——校验脚本的注释里出现 "bcdboot" 是正常的，
     按原文匹配会误伤（实测踩过这个坑）。剥注释必须识别引号：``'# '`` 是字符串内容，
     不是注释开头（旧实现按 ``#`` 硬切，会把引号后的 ``Clear-Disk`` 一起切掉而漏判）。

已知残余：变量拼接的调用（``& $cmd``）与 ``-File`` 指向的脚本内容都不做解析。
``-File`` / ``-EncodedCommand`` / ``Invoke-Expression`` 这类看不到内容的调用直接按危险处理
（要求显式授权）；真要靠变量拼出一条写盘命令绕护栏，仍然得先过“实例授权 + 测试环境”两道闸。
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

# dism 的只读动词都是 ``/Get-*``（``/Get-WimInfo`` / ``/Get-ImageInfo`` / ``/Get-Drivers``…）；
# 其余动词（``/Apply-Image`` / ``/Add-Driver`` / ``/Cleanup-Image``…）会写东西。
DISM_READ_ONLY_PREFIX = "/get-"

# PowerShell 只有这些 cmdlet 会改盘/挂盘；只读的 Get-* 一律放行。
# 注意：这里**只列 PowerShell cmdlet**，不列外部程序名——注释里的词不该算危险。
# ``Mount-DiskImage`` / ``Dismount-DiskImage`` 不改分区表，但会改变系统挂载状态，
# 因此和写盘命令一样要求显式授权（ISO 探测会用到，生产入口会传 allow_real_disk=True）。
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
    "mount-diskimage",
    "dismount-diskimage",
)

# 脚本里看不到内容的动态执行：一律当危险（无法静态判定它执行了什么东西）。
DANGEROUS_DYNAMIC = ("invoke-expression", "iex")

# PowerShell 脚本里可能出现的外部写盘程序（必须带“调用”上下文才算，见下）。
DANGEROUS_SCRIPT_PROGRAMS = ("diskpart", "bcdboot", "bootrec", "dism", "format")

_POWERSHELL_PROGRAMS = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
_DANGEROUS_CALL = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in (*DANGEROUS_POWERSHELL, *DANGEROUS_DYNAMIC)) + r")\b",
    re.IGNORECASE,
)
# 脚本里“调用外部程序”的形态：行首 / ``;`` / ``&`` / 管道 / 括号 / 脚本块 / Start-Process。
# 尾部的 lookahead 是必要的：没有它 ``Get-Disk | Format-Table`` 会被当成 ``format``。
_SCRIPT_PROGRAM_CALL = re.compile(
    r"(?:\bstart-process\b[^;&|\n]*?|\bsaps\b[^;&|\n]*?|^|[;&|(`{])\s*[\"']?[\w:\\.-]*"
    r"\b(?:" + "|".join(DANGEROUS_SCRIPT_PROGRAMS) + r")(?:\.exe)?(?=[\s\"'/]|$)",
    re.IGNORECASE | re.MULTILINE,
)
# powershell.exe 的"看不到内容"参数：-File（脚本文件）与 -EncodedCommand（base64）。
# -f / -fi / -fil 是 -File 的合法缩写；-enc… 是 -EncodedCommand 的缩写
# （只收长度 ≥ 3 的缩写，避免把 -ExecutionPolicy 之类误判）。
_UNINSPECTABLE_PARAM = re.compile(
    r"^-(?:f|fi|fil|file|enc|enco|encod|encode|encoded\w*)$", re.IGNORECASE
)
# 卡在命令行上的 base64 大块：无法阅读，按危险处理。
_ENCODED_PAYLOAD = re.compile(r"^[A-Za-z0-9+/]{80,}={0,2}$")
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


def _strip_line_comment(line: str) -> str:
    r"""去掉这一行里**引号之外**的 ``#`` 及其后面的内容。

    必须识别引号：``-ImagePath 'D:\a#b\x.iso'`` 里的 ``#`` 是路径字符，不是注释。
    旧实现直接 ``line.split('#', 1)[0]``，遇到这种路径会把整行后半截切掉——
    如果后半截正好是 ``Clear-Disk``，护栏就漏判了（比误伤更糟）。
    """
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "`":  # PowerShell 转义符：跳过下一个字符
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
        index += 1
    return line


def strip_comments(script: str) -> str:
    """去掉 ``#`` 之后的内容：注释里的词不该影响危险判定。"""
    return NEWLINE.join(_strip_line_comment(line) for line in script.splitlines())


def _dism_is_read_only(args: Sequence[str]) -> bool:
    """``dism`` 走的是只读动词（``/Get-WimInfo`` 之类）吗。"""
    return any(str(arg).strip().lower().startswith(DISM_READ_ONLY_PREFIX) for arg in args)


def _script_text(parts: Sequence[str]) -> str:
    """取 PowerShell 的**脚本文本**：``-Command`` 之后的参数；没写 ``-Command`` 就全算。

    只扫脚本文本，不扫 ``-NoProfile`` 之类参数：否则 ``dism.exe /Apply-Image …`` 这种
    “脚本第一个词就是危险程序”的写法会因为前面有 ``-Command`` 而依赖行首判定，漏掉。
    """
    for index, part in enumerate(parts):
        if part.strip().lower() in {"-command", "-c", "-cmd", "/command"}:
            return " ".join(parts[index + 1 :])
    return " ".join(parts)


def _powershell_is_dangerous(args: Sequence[str]) -> bool:
    """PowerShell 命令行里有没有写盘动作（或看不到内容的调用）。"""
    parts = [str(part) for part in args]
    if any(_UNINSPECTABLE_PARAM.match(part.strip()) for part in parts):
        return True
    if any(_ENCODED_PAYLOAD.match(part.strip()) for part in parts):
        return True
    script = strip_comments(_script_text(parts))
    return bool(_DANGEROUS_CALL.search(script) or _SCRIPT_PROGRAM_CALL.search(script))


def is_dangerous_command(command: Sequence[str]) -> bool:
    """这条命令会不会改磁盘（或改挂载状态）。

    - 外部程序：``diskpart`` / ``format`` / ``bcdboot`` / ``bootrec`` 按程序名拦；
      ``dism`` 再看参数——``/Get-*`` 只读放行，其余拦；
    - PowerShell：写盘 cmdlet、脚本里调用外部写盘程序、``-File`` / ``-EncodedCommand``
      这类看不到内容的调用，任一命中即拦；注释先剥掉。
    """
    name = program_name(command)
    args = [str(part) for part in command[1:]]
    if name in DANGEROUS_PROGRAMS:
        # dism 的 ``/Get-*`` 是只读查询（ImageProbe 读索引靠它），其余动词才算写。
        return not (name in {"dism", "dism.exe"} and _dism_is_read_only(args))
    if name in _POWERSHELL_PROGRAMS:
        return _powershell_is_dangerous(args)
    return False


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
