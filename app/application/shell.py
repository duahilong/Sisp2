"""执行壳：**所有子进程都从这里出去**，也是测试的注入缝。

拆出来的原因（2026-11-06 清盘事故的教训 + 解耦需求）：

1. 以前 ``_spawn`` 是 ``WindowsDiskRunner`` 的私有方法，测试只能
   ``runner._spawn = fake`` 这种"直接赋值"——实现搬家后 mock 会静默失效，
   pytest 真的执行了 ``diskpart clean``。现在改为构造期注入 ``process=``/``shell=``，
   注入不进就会走真实子进程（并由 ``app.safety_guard`` 拦下）。
2. 分区阶段与装机阶段都需要"跑命令 / 查分区"，但它们是两件事。壳只负责
   "跑命令"，不知道自己在分区还是在装镜像，于是两个阶段可以各自独立测试。

约定：``DiskpartShell`` / ``ProcessShell`` 只做四件事——拼命令、过护栏、
执行、写日志。任何业务判断（能不能继续、要不要跳过）都在 phases 里。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from app.application.command_log import CommandLog
from app.application.partitions import ESP_GPT, is_reserved_partition
from app.application.runner_utils import RunnerError, decode_output, tail, to_int
from app.application.streaming import StreamOutcome, stream_process
from app.safety_guard import ensure_command_allowed

DISKPART_TIMEOUT = 600.0
QUERY_TIMEOUT = 60.0
QUERY_ATTEMPTS = 3
QUERY_INTERVAL = 0.3
#: ``format.com`` 兜底格式化的超时：快速格式化通常几秒，慢盘/USB 桥接留足余量。
FORMAT_TIMEOUT = 900.0

RELEASE_TIMEOUT = 120.0
VERIFY_TIMEOUT = 120.0
# Ghost 还原 14 GB 的 GHO 在 SATA 上也够用；NVMe 通常 5–15 分钟。
GHOST_TIMEOUT = 2 * 3600.0
BCDBOOT_TIMEOUT = 120.0
# 收尾的附加文件拷贝：素材目录可能是几十 GB，给足 4 小时；取消在步骤边界生效。
COPY_TIMEOUT = 4 * 3600.0

# diskpart 是全局状态机（select disk / select partition 是进程级状态），两块盘同时
# 跑 diskpart 会互相踩。多盘流水线里分区本身已经由 ``PartitionSequencer`` 串行化，
# 这里再上一把模块级锁：执行期释放盘符、boot 修复等旁路调用也不会和分区撞车。
_DISKPART_LOCK = threading.Lock()

# diskpart 的退出码就是 HRESULT：0x8007xxxx 里的 xxxx 是 Win32 错误码。
# 值：(Win32 名字, 中文说明, 给用户/维护者的下一步建议)
DISKPART_EXITS: dict[int, tuple[str, str, str]] = {
    0x80070017: (
        "ERROR_CRC",
        "数据错误（循环冗余检查）",
        "磁盘链路上读到了坏数据：先换数据线/换接口（USB 盘尽量直连主板、别串同一个 hub），"
        "再查硬盘健康（CrystalDiskInfo 看 SMART 的 C7/05）",
    ),
    0x80070015: (
        "ERROR_NOT_READY",
        "设备未就绪",
        "盘刚上电或桥接板还在初始化：等几秒重试；反复出现说明供电/桥接板不稳",
    ),
    0x8007001F: (
        "ERROR_GEN_FAILURE",
        "设备故障（一般性错误）",
        "设备拒绝了这次写操作：换接口/换线；同一块盘反复出现要怀疑盘本身",
    ),
    0x8007045D: (
        "ERROR_IO_DEVICE",
        "I/O 设备错误",
        "读写过程中设备掉了：检查电源、USB 供电、SATA/桥接板是否松动",
    ),
    0x80070020: (
        "ERROR_SHARING_VIOLATION",
        "卷被占用",
        "盘上还有卷/文件被别的程序打开：关掉资源管理器窗口、杀软扫描或其它分区工具后重试",
    ),
    0x80070021: (
        "ERROR_LOCK_VIOLATION",
        "文件被锁定",
        "同上：先把占用该盘的进程（杀软/备份/索引）停掉再重试",
    ),
    0x80070005: (
        "ERROR_ACCESS_DENIED",
        "拒绝访问",
        "权限不够或盘被保护：确认以管理员运行；只读/受保护（如 Intel RST、BitLocker、"
        "写保护开关）的盘要先解除保护",
    ),
    0x80070013: (
        "ERROR_WRITE_PROTECT",
        "磁盘被写保护",
        "整盘写保护：检查盘上的写保护开关、机箱热插拔策略、以及是否被策略设为只读",
    ),
    0x80070057: (
        "ERROR_INVALID_PARAMETER",
        "参数错误",
        "脚本里的分区参数不被接受（例如 size 超出磁盘可用空间）：核对向导里的分区大小",
    ),
    0x8007000B: (
        "ERROR_BAD_FORMAT",
        "介质格式无法识别",
        "分区表/卷里有磁盘不认的结构：先清盘重来；反复出现说明盘有坏道，换盘",
    ),
    0x80070490: (
        "ERROR_NOT_FOUND",
        "找不到指定的分区/卷",
        "分区号已经变了（前一条命令刚重建过分区表）：重扫后按真实分区表重试",
    ),
    0x8007000E: (
        "ERROR_OUTOFMEMORY",
        "资源不足",
        "diskpart 的虚拟磁盘服务分配失败：关掉其它分区/镜像工具后重试",
    ),
    # ---- 下面是 VDS（Virtual Disk Service）自己的错误码，0x800424xx ----
    # 它们不经过 Win32 层，所以看上去和 0x8007xxxx 完全不像；现场最常遇到的是
    # 0x80042413：format/clean 的时候卷被占用（diskpart 原话是
    # “设备使用中 / 所选卷或分区正在使用，若要继续操作请使用 OVERRIDE 参数”）。
    0x80042413: (
        "VDS_E_VOLUME_IN_USE",
        "所选卷或分区正在使用（虚拟磁盘服务：设备使用中）",
        "有进程正占着这个卷：关掉该盘的资源管理器窗口、杀毒软件的实时扫描/索引，"
        "或稍等几秒重试；程序会自动按真实分区表续跑一次",
    ),
    0x80042408: (
        "VDS_E_PARTITION_NOT_EMPTY",
        "分区非空（虚拟磁盘服务）",
        "分区里还有文件系统/卷：先删除卷再删分区",
    ),
    0x8004240C: (
        "VDS_E_MISSING_DISK",
        "磁盘已不在（虚拟磁盘服务）",
        "盘在这期间被拔掉/掉线：检查数据线与供电后重新扫描磁盘",
    ),
    0x8004240E: (
        "VDS_E_DISK_OFFLINE",
        "磁盘处于脱机状态（虚拟磁盘服务）",
        "先把磁盘设为联机（磁盘管理 / Set-Disk -IsOffline $false）再重试",
    ),
    0x8004240F: (
        "VDS_E_DISK_NOT_ONLINE",
        "磁盘不可用（虚拟磁盘服务）",
        "磁盘还没上线或只读：检查连接、写保护与磁盘管理里的状态",
    ),
    0x8004240A: (
        "VDS_E_OPERATION_DENIED",
        "操作被拒绝（虚拟磁盘服务）",
        "权限不足或磁盘受保护：确认以管理员运行，并解除 BitLocker/写保护",
    ),
    0x80042406: (
        "VDS_E_INVALID_SPACE",
        "分区大小不被接受（虚拟磁盘服务）",
        "请求的 size 超出磁盘可用空间：核对向导里的分区大小",
    ),
    0x80042405: (
        "VDS_E_OBJECT_NOT_FOUND",
        "找不到目标对象（虚拟磁盘服务）",
        "磁盘/分区的编号已经变了：重新扫描后重试（程序会自动续跑一次）",
    ),
    0x80042404: (
        "VDS_E_ANOTHER_CALL_IN_PROGRESS",
        "另一个磁盘操作还没结束（虚拟磁盘服务）",
        "有别的分区/镜像工具正在操作磁盘：关掉它们后重试",
    ),
}

# 可以放心重试的错误：命令是幂等的（清盘 + 转 GPT），且失败原因多半是瞬时的。
# 权限/参数类错误重试也不会变好，直接报出来。
DISKPART_TRANSIENT_EXITS: frozenset[int] = frozenset(
    {
        0x80070017,  # CRC
        0x80070015,  # 设备未就绪
        0x8007001F,  # 一般故障
        0x8007045D,  # I/O 设备错误
        0x80070020,  # 卷被占用
        0x80070021,  # 文件被锁定
        0x8007000E,  # 资源不足
        0x80042413,  # VDS：卷/分区正在使用（占用它的进程退出后就能过）
        0x80042404,  # VDS：另一个磁盘操作还在进行
        0x80042405,  # VDS：找不到目标（编号变了，重扫后可能就有了）
    }
)


def normalize_exit_code(code: int | None) -> int | None:
    """把退出码统一成 32 位无符号 HRESULT 形式（负数也认）。"""
    if code is None:
        return None
    return int(code) & 0xFFFFFFFF


def describe_diskpart_exit(code: int | None) -> str:
    """``2147942423`` → ``"2147942423 = 0x80070017 数据错误（循环冗余检查）"``。

    认不出来的码原样给出数值和十六进制，不做猜测。
    """
    if code is None:
        return "未知"
    value = normalize_exit_code(code)
    known = DISKPART_EXITS.get(value)
    if value is None:  # pragma: no cover - normalize 不会返回 None
        return str(code)
    hexed = f"0x{value:08X}"
    if known is None:
        return f"{value} = {hexed}"
    _name, text, _hint = known
    return f"{value} = {hexed} {text}"


def diskpart_exit_hint(code: int | None) -> str:
    """给用户的下一步建议；认不出来的码返回空串（不编造原因）。"""
    value = normalize_exit_code(code)
    known = DISKPART_EXITS.get(value) if value is not None else None
    return known[2] if known else ""


def is_transient_diskpart_exit(code: int | None) -> bool:
    """这个退出码值不值得重试（幂等命令的前提下）。"""
    return normalize_exit_code(code) in DISKPART_TRANSIENT_EXITS

_LOGGER = logging.getLogger(__name__)

# 只读：查分区表 + 卷信息（盘符/卷标/文件系统）+ 盘容量。
# 其中 ``Win32_LogicalDiskToPartition`` 用来关联 ESP 的临时盘符——ESP 是隐藏卷，
# ``Get-Partition.DriveLetter`` 对它永远为空，只有 WMI 关联看得到。
PARTITIONS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$number = [int]$env:VIX_DISK_NUMBER
$disk = Get-Disk -Number $number

# 分区 → 盘符的映射：ESP 是隐藏卷，Get-Partition 的 DriveLetter 是空的，
# 但 WMI 的 Win32_LogicalDiskToPartition 能看到它临时分配的字母。
# 注意 Win32_DiskPartition 的编号从 0 开始，Get-Partition 从 1 开始。
$mounted = @{}
foreach ($rel in Get-CimInstance Win32_LogicalDiskToPartition -ErrorAction SilentlyContinue) {
    $m1 = [regex]::Match([string]$rel.Antecedent, 'Disk #(\d+), Partition #(\d+)')
    $m2 = [regex]::Match([string]$rel.Dependent, '([A-Z]):')
    if ($m1.Success -and $m2.Success) {
        $key = $m1.Groups[1].Value + ':' + ([int]$m1.Groups[2].Value + 1)
        $mounted[$key] = $m2.Groups[1].Value
    }
}

$items = foreach ($part in Get-Partition -DiskNumber $number -ErrorAction SilentlyContinue) {
    $vol = $part | Get-Volume -ErrorAction SilentlyContinue
    $key = [string]$number + ':' + [string]([int]$part.PartitionNumber)
    [pscustomobject]@{
        number         = [int]$part.PartitionNumber
        size_bytes     = [long]$part.Size
        offset_bytes   = [long]$part.Offset
        type           = [string]$part.Type
        gpt_type       = [string]$part.GptType
        drive_letter   = if ($part.DriveLetter) { [string]$part.DriveLetter } else { $null }
        mounted_letter = $mounted[$key]
        file_system    = if ($vol) { [string]$vol.FileSystem } else { $null }
        label          = if ($vol) { [string]$vol.FileSystemLabel } else { $null }
    }
}

[pscustomobject]@{
    disk_number     = $number
    partition_style = [string]$disk.PartitionStyle
    disk_size_bytes = [long]$disk.Size
    largest_free    = [long]$disk.LargestFreeExtent
    partitions      = @($items)
} | ConvertTo-Json -Depth 4 -Compress
"""


#: 只读查询：一块盘的“身份 + 健康状态”。
#:
#: 用途是**每段 diskpart 之前复核**「马上要动的还是不是确认时那块盘」，顺便把
#: 脱机/只读/健康状态拿出来，好在清盘前拦住。比 ``PARTITIONS_SCRIPT`` 便宜得多
#: （不枚举分区、不查卷），单次约 0.4 s。
#:
#: 盘不存在时 ``Get-Disk`` 直接报错退出（非 0），调用方据此判定“盘掉线了”。
DISK_STATE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$number = [int]$env:VIX_DISK_NUMBER
$disk = Get-Disk -Number $number
[pscustomobject]@{
    disk_number      = $number
    model            = [string]$disk.FriendlyName
    friendly_name    = [string]$disk.FriendlyName
    serial_number    = [string]$disk.SerialNumber
    unique_id        = [string]$disk.UniqueId
    size_bytes       = [long]$disk.Size
    bus_type         = [string]$disk.BusType
    partition_style  = [string]$disk.PartitionStyle
    is_offline       = [bool]$disk.IsOffline
    is_read_only     = [bool]$disk.IsReadOnly
    health_status    = [string]$disk.HealthStatus
    operational_status = [string]$disk.OperationalStatus
} | ConvertTo-Json -Compress
"""


@cache
def _find_program_cached(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        system32 = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows") / "System32"
        candidate = system32 / name
        if candidate.is_file():
            return str(candidate)
    return None


def find_program(name: str) -> str | None:
    """在 PATH / System32 里找可执行文件（找不到返回 ``None``）。

    结果按进程缓存：``PATH`` 在执行期间不会变，而 ``WindowsDiskRunner`` 是按任务造
    ``DiskpartShell`` 的——8 块盘就是几十次 ``shutil.which`` + 目录探测。
    装完新工具后调 ``find_program.cache_clear()`` 重置（与“改 extGhost 要重启”的
    现有约定一致）。
    """
    return _find_program_cached(str(name))


# 供测试/设置变更后重置缓存（与 ``functools.cache`` 同名）。
find_program.cache_clear = _find_program_cached.cache_clear  # type: ignore[attr-defined]


def format_command_line(command: Sequence[str], quiet: str = "") -> str:
    """日志里的命令行：``quiet`` 非空时只留一行说明（只读查询用）。

    只读查询的 PowerShell 脚本有几十行，一块盘要查 4–6 次；原样落日志会把
    真正要看的内容冲散。``quiet`` 里写清"这条是什么查询"，需要脚本本身时
    去 ``shell.py`` 看常量即可。
    """
    text = str(quiet or "").strip()
    return f"$ <{text}>" if text else f"$ {' '.join(command)}"


def run_process(
    command: Sequence[str],
    timeout: float,
    *,
    log: CommandLog,
    allow_real_disk: bool,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    source: str = "ProcessShell",
    quiet: str = "",
) -> tuple[int, str, float]:
    """执行一条命令：先过护栏，再写日志，最后才交给操作系统。

    护栏放在"写日志"之前——被拦下的命令不应该看起来像执行过。
    ``cwd`` 给 Ghost 这类"把失败原因写到当前目录"的程序用；``quiet`` 非空时
    只记一行说明、不打印完整命令行——只读查询用的 PowerShell 脚本有几十行，
    每次都整段落日志会把真正要看的内容冲散。
    """
    ensure_command_allowed(command, allow_real_disk=allow_real_disk, source=source)
    started = datetime.now()
    log.write(format_command_line(command, quiet))
    try:
        completed = subprocess.run(  # noqa: S603 - 命令由本模块拼装，参数来自配置
            list(command),
            capture_output=True,
            timeout=timeout,
            cwd=cwd or None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
            env=dict(env) if env else None,
        )
    except subprocess.TimeoutExpired as exc:
        output = decode_output(exc.stdout) + decode_output(exc.stderr)
        log.block(f"超时（{timeout:.0f}s）", output)
        raise RunnerError(f"命令超时（{timeout:.0f} 秒）：{command[0]}") from exc
    except OSError as exc:
        raise RunnerError(f"无法启动 {command[0]}：{exc}") from exc

    output = decode_output(completed.stdout) + decode_output(completed.stderr)
    elapsed = (datetime.now() - started).total_seconds()
    log.block(f"退出码 {completed.returncode}，用时 {elapsed:.1f}s", output)
    return completed.returncode, output, elapsed


class ProcessShell:
    """通用命令执行器（Ghost / bcdboot / 校验脚本都用它）。

    ``runner`` 可注入 ``(command, timeout, *, env=None, cwd=None) -> (code, output, elapsed)``，
    测试用它替代真实子进程；生产用模块级 :func:`run_process`。
    """

    def __init__(
        self,
        *,
        log: CommandLog,
        allow_real_disk: bool = False,
        env: Mapping[str, str] | None = None,
        source: str = "ProcessShell",
        runner: Callable[..., tuple[int, str, float]] | None = None,
    ) -> None:
        self.log = log
        self.allow_real_disk = bool(allow_real_disk)
        self.env = dict(env) if env else None
        self.source = source
        self._runner = runner

    def run(
        self,
        command: Sequence[str],
        timeout: float,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        quiet: str = "",
    ) -> tuple[int, str, float]:
        """执行命令。注入的 ``runner`` 签名：``(command, timeout, *, env=None, cwd=None)``。

        ``quiet`` 只影响日志：只读查询（PowerShell 脚本几十行）不该把日志刷满，
        但替身 runner 的签名保持不变（不把 quiet 透给替身）。
        """
        if self._runner is not None:
            return self._runner(list(command), timeout, env=env, cwd=cwd)
        return run_process(
            command,
            timeout,
            log=self.log,
            allow_real_disk=self.allow_real_disk,
            env=env if env is not None else self.env,
            cwd=cwd,
            source=self.source,
            quiet=quiet,
        )

    def run_streaming(
        self,
        command: Sequence[str],
        timeout: float,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        cancel: threading.Event | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> StreamOutcome:
        """流式执行：边跑边落日志，``cancel`` 置位时终止进程。

        注入 ``runner`` 的测试替身没法“边跑边读”，这里退化成“跑完回放输出”，
        保持与真实现相同的返回值形状；取消已经置位时直接返回 cancelled，
        不调用替身（这样取消路径也能被单测覆盖）。
        """
        if self._runner is not None:
            if cancel is not None and cancel.is_set():
                return StreamOutcome(returncode=None, output="", elapsed=0.0, cancelled=True)
            code, output, elapsed = self._runner(list(command), timeout, env=env, cwd=cwd)
            text = str(output or "")
            if on_line is not None:
                for line in text.splitlines():
                    try:
                        on_line(line)
                    except Exception:
                        _LOGGER.debug("进度回调失败", exc_info=True)
            return StreamOutcome(returncode=code, output=text, elapsed=elapsed)
        return stream_process(
            command,
            timeout,
            log=self.log,
            allow_real_disk=self.allow_real_disk,
            env=env if env is not None else self.env,
            cwd=cwd,
            source=self.source,
            cancel=cancel,
            on_line=on_line,
        )


class DiskpartShell:
    """diskpart + 只读 PowerShell 查询。

    这是"分区阶段"唯一需要的依赖：拿到它就能清盘、删 MSR、建分区、查分区表、
    定位 ESP、释放盘符——不需要知道 Ghost / bcdboot 的存在。
    """

    def __init__(
        self,
        *,
        log: CommandLog,
        diskpart: str | None = None,
        powershell: str | None = None,
        format_tool: str | None = None,
        allow_real_disk: bool = False,
        temp_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        process: ProcessShell | None = None,
        attempts: int = QUERY_ATTEMPTS,
        interval: float = QUERY_INTERVAL,
    ) -> None:
        self.log = log
        self.diskpart = diskpart if diskpart is not None else find_program("diskpart.exe")
        self.powershell = (
            powershell if powershell is not None else find_program("powershell.exe")
        )
        # format.com：diskpart 的 format 拿不到卷锁时（0x80042413）的兜底工具。
        # 它的 /X 是官方"必要时强制卸载卷"，专治"设备使用中"。
        self.format_tool = (
            format_tool if format_tool is not None else find_program("format.com")
        )
        self.temp_dir = temp_dir
        self.attempts = max(1, int(attempts))
        self.interval = max(0.0, float(interval))
        self.process = process or ProcessShell(
            log=log, allow_real_disk=allow_real_disk, env=env, source="DiskpartShell"
        )

    # ------------------------------------------------------------------ 写
    def run_diskpart(
        self,
        lines: Sequence[str],
        *,
        title: str = "diskpart 脚本",
        timeout: float = DISKPART_TIMEOUT,
    ) -> tuple[int, str]:
        """把脚本写进临时文件再 ``diskpart /s``（diskpart 只认 ANSI 脚本）。"""
        if not self.diskpart:
            raise RunnerError("找不到 diskpart.exe")
        # 全局互斥：多个任务（多盘流水线、boot 修复）可能同时需要 diskpart。
        with _DISKPART_LOCK:
            return self._run_diskpart_locked(lines, title=title, timeout=timeout)

    def _run_diskpart_locked(
        self,
        lines: Sequence[str],
        *,
        title: str,
        timeout: float,
    ) -> tuple[int, str]:
        self.log.block(title, "\n".join(lines))
        handle, path = tempfile.mkstemp(
            prefix="sisp-diskpart-", suffix=".txt", dir=self.temp_dir
        )
        os.close(handle)
        try:
            # diskpart 的 ``/s`` 只认 ANSI（系统代码页）：卷标/说明里一旦出现非 ASCII
            # 字符，``encoding="ascii"`` 会在真正调起 diskpart 之前抛
            # ``UnicodeEncodeError``——那是最难查的一种“写盘前崩”。
            script_text = "\r\n".join(lines) + "\r\n"
            encoding = "mbcs" if os.name == "nt" else "utf-8"
            try:
                Path(path).write_text(script_text, encoding=encoding)
            except UnicodeEncodeError:
                # 极端字符（emoji 等）在当前代码页映射不了：替换成 ``?`` 总比中止强，
                # 日志里已经原样留了一份脚本（上面那行 ``log.block``）。
                Path(path).write_text(script_text, encoding=encoding, errors="replace")
            runner = getattr(self.process, "run_streaming", None)
            if runner is not None:
                # 流式：diskpart 的输出也会实时出现在任务日志里（取消不在这里做，
                # 分区中途被强杀比等它跑完更危险，取消检查点在阶段边界）。
                outcome = runner([self.diskpart, "/s", path], timeout)
                return (outcome.returncode if outcome.returncode is not None else -1), outcome.output
            code, output, _elapsed = self.process.run([self.diskpart, "/s", path], timeout)
            return code, output
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def rescan(self, *, timeout: float = RELEASE_TIMEOUT) -> bool:
        """让 diskpart 重新扫描总线（重试前调用，让枚举跟上真实状态）。

        失败只记日志：扫描不了不代表重试一定失败，不该因此改变任务结论。
        """
        if not self.diskpart:
            return False
        try:
            code, _output = self.run_diskpart(["rescan", "exit"], title="diskpart 重新扫描磁盘", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 重扫失败不影响重试
            self.log.write(f"重新扫描磁盘失败（已忽略）：{exc}")
            return False
        if code == 0:
            self.log.write("已重新扫描磁盘总线")
            return True
        self.log.write(f"重新扫描磁盘失败（退出码 {describe_diskpart_exit(code)}），继续重试")
        return False

    def format_letter(
        self,
        letter: str,
        *,
        file_system: str = "NTFS",
        label: str = "",
        timeout: float = FORMAT_TIMEOUT,
    ) -> tuple[int, str]:
        """用 Win32 的 ``format.com`` 格式化一个**已挂盘符**的卷。

        为什么需要第二个格式化工具：``diskpart`` 的 ``format`` 在卷被系统/杀软
        短暂占用时只会返回 ``0x80042413 设备使用中``，没有任何"强制"手段；
        ``format.com`` 的 ``/X`` 是官方的"必要时强制卸载卷"开关
        （"Forces the volume to dismount first if necessary. All opened handles
        to the volume would then be invalid."），这正是这类失败的解法。

        参数：``/Q`` 快速格式化、``/FS:<fs>``、``/V:<label>``、``/Y`` 免确认、
        ``/X`` 强制卸载。调用方**必须**先确认这个盘符真的挂在自己的目标分区上
        （否则会格式化别人的卷）。
        """
        wanted = str(letter or "").strip().upper()[:1]
        if not wanted:
            raise RunnerError("format.com 需要盘符")
        tool = self.format_tool
        if not tool:
            raise RunnerError("找不到 format.com，无法回退格式化")
        fs = str(file_system or "NTFS").strip().upper()
        volume = f"{wanted}:"
        args = [tool, volume, f"/FS:{fs}", "/Q", "/Y", "/X"]
        text = str(label or "").strip()
        if text:
            args.append(f"/V:{text}")
        code, output, _elapsed = self.process.run(args, timeout)
        return code, output

    def release_letter(self, disk_number: int, partition: int, letter: str) -> bool:
        """按"磁盘号 + 分区号"摘掉盘符（不按字母选卷，见 ``EfiLetterReleaser``）。"""
        wanted = str(letter).strip().upper()
        lines = [
            f"select disk {int(disk_number)}",
            f"select partition {int(partition)}",
            f"remove letter={wanted}",
            "exit",
        ]
        try:
            code, _output = self.run_diskpart(
                lines, title="释放 EFI 盘符", timeout=RELEASE_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - 释放失败不能影响任务结果
            self.log.write(f"释放 EFI 盘符 {wanted} 失败（已忽略）")
            return False
        if code == 0:
            self.log.write(f"已释放 EFI 盘符 {wanted}（磁盘 {disk_number} 分区 #{partition}）")
            return True
        self.log.write(f"释放 EFI 盘符 {wanted} 失败（退出码 {code}），不影响任务结果")
        return False

    # ------------------------------------------------------------------ 读
    def query(self, disk_number: int) -> dict[str, Any]:
        """查一次真实分区表（分区列表 + 盘容量 + 最大空闲块），不碰盘。"""
        if not self.powershell:
            raise RunnerError("找不到 powershell.exe，无法查询分区")
        env = dict(os.environ)
        env["VIX_DISK_NUMBER"] = str(int(disk_number))
        code, output, _elapsed = self.process.run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                PARTITIONS_SCRIPT,
            ],
            QUERY_TIMEOUT,
            env=env,
            quiet=f"查询磁盘 {int(disk_number)} 的分区表（PowerShell 脚本见 shell.py）",
        )
        if code != 0:
            raise RunnerError(f"查询分区失败（退出码 {code}）：{tail(output, 400)}")
        raw = (output or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"查询分区的输出不是合法 JSON：{tail(raw, 300)}") from exc
        if not isinstance(payload, Mapping):
            raise RunnerError(f"查询分区的输出格式不对：{tail(raw, 300)}")
        return dict(payload)

    def disk_state(self, disk_number: int) -> dict[str, Any]:
        """查一块盘的身份与健康字段（不碰盘）。

        盘不存在 / PowerShell 报错时抛 ``RunnerError``：调用方据此判断“盘掉线了”。
        """
        if not self.powershell:
            raise RunnerError("找不到 powershell.exe，无法复核磁盘")
        env = dict(os.environ)
        env["VIX_DISK_NUMBER"] = str(int(disk_number))
        code, output, _elapsed = self.process.run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                DISK_STATE_SCRIPT,
            ],
            QUERY_TIMEOUT,
            env=env,
            quiet=f"复核磁盘 {int(disk_number)} 的身份与健康状态（PowerShell 脚本见 shell.py）",
        )
        if code != 0:
            raise RunnerError(
                f"复核磁盘 {int(disk_number)} 失败（退出码 {code}）：{tail(output, 300)}"
            )
        raw = (output or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"复核磁盘的输出不是合法 JSON：{tail(raw, 200)}") from exc
        if not isinstance(payload, Mapping):
            raise RunnerError(f"复核磁盘的输出格式不对：{tail(raw, 200)}")
        return dict(payload)

    def find_partition(
        self,
        disk_number: int,
        *,
        gpt_type: str,
        match_reserved: bool = False,
        label: str = "分区",
        attempts: int | None = None,
    ) -> int | None:
        """按 GPT 类型（或"保留分区"）找分区号；找不到返回 ``None``。

        带重试：``convert gpt`` 刚结束时，慢盘/RAID/USB 桥接上分区枚举可能还没跟上，
        一次查不到就直接"跳过删除"会把 MSR 留在盘上。
        **查不到就是查不到**：这里只重试，绝不猜一个分区号去操作。
        """
        wanted = str(gpt_type).strip("{}").lower()
        total = max(1, int(attempts if attempts is not None else self.attempts))
        for attempt in range(1, total + 1):
            payload = self.query(disk_number)
            for part in payload.get("partitions") or ():
                if not isinstance(part, Mapping):
                    continue
                if match_reserved and is_reserved_partition(part):
                    found = to_int(part.get("number"))
                else:
                    gpt = str(part.get("gpt_type") or "").strip("{}").lower()
                    found = to_int(part.get("number")) if gpt == wanted else None
                if found is not None:
                    self.log.write(f"{label}：发现分区 #{found}（第 {attempt} 次查询）")
                    return found
            if attempt < total and self.interval:
                import time

                time.sleep(self.interval)
        self.log.write(f"{label}：盘上没有匹配的分区（已查 {total} 次）")
        return None

    def find_msr(self, disk_number: int, *, label: str = "MSR") -> int | None:
        """``convert gpt`` 自动生成的 MSR 分区号（查不到返回 ``None``）。"""
        return self.find_partition(
            disk_number, gpt_type="", match_reserved=True, label=label
        )

    def find_esp(self, disk_number: int) -> int | None:
        """ESP 分区号（释放临时盘符时定位用；找不到返回 ``None``）。"""
        return self.find_partition(disk_number, gpt_type=ESP_GPT, label="ESP")


__all__ = [
    "BCDBOOT_TIMEOUT",
    "COPY_TIMEOUT",
    "DISKPART_EXITS",
    "DISKPART_TIMEOUT",
    "DISKPART_TRANSIENT_EXITS",
    "DISK_STATE_SCRIPT",
    "FORMAT_TIMEOUT",
    "GHOST_TIMEOUT",
    "PARTITIONS_SCRIPT",
    "QUERY_ATTEMPTS",
    "QUERY_INTERVAL",
    "QUERY_TIMEOUT",
    "RELEASE_TIMEOUT",
    "VERIFY_TIMEOUT",
    "DiskpartShell",
    "ProcessShell",
    "describe_diskpart_exit",
    "diskpart_exit_hint",
    "find_program",
    "format_command_line",
    "is_transient_diskpart_exit",
    "normalize_exit_code",
    "run_process",
]
