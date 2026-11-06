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
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.application.command_log import CommandLog
from app.application.partitions import ESP_GPT, is_reserved_partition
from app.application.runner_utils import RunnerError, decode_output, tail, to_int
from app.safety_guard import ensure_command_allowed

DISKPART_TIMEOUT = 600.0
QUERY_TIMEOUT = 60.0
QUERY_ATTEMPTS = 3
QUERY_INTERVAL = 0.3
RELEASE_TIMEOUT = 120.0
VERIFY_TIMEOUT = 120.0
# Ghost 还原 14 GB 的 GHO 在 SATA 上也够用；NVMe 通常 5–15 分钟。
GHOST_TIMEOUT = 2 * 3600.0
BCDBOOT_TIMEOUT = 120.0

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


def find_program(name: str) -> str | None:
    """在 PATH / System32 里找可执行文件（找不到返回 ``None``）。"""
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        system32 = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows") / "System32"
        candidate = system32 / name
        if candidate.is_file():
            return str(candidate)
    return None


def run_process(
    command: Sequence[str],
    timeout: float,
    *,
    log: CommandLog,
    allow_real_disk: bool,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    source: str = "ProcessShell",
) -> tuple[int, str, float]:
    """执行一条命令：先过护栏，再写日志，最后才交给操作系统。

    护栏放在"写日志"之前——被拦下的命令不应该看起来像执行过。
    ``cwd`` 给 Ghost 这类"把失败原因写到当前目录"的程序用。
    """
    ensure_command_allowed(command, allow_real_disk=allow_real_disk, source=source)
    started = datetime.now()
    log.write(f"$ {' '.join(command)}")
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
    ) -> tuple[int, str, float]:
        """执行命令。注入的 ``runner`` 签名：``(command, timeout, *, env=None, cwd=None)``。"""
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
        self.log.block(title, "\n".join(lines))
        handle, path = tempfile.mkstemp(
            prefix="sisp-diskpart-", suffix=".txt", dir=self.temp_dir
        )
        os.close(handle)
        try:
            Path(path).write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
            code, output, _elapsed = self.process.run([self.diskpart, "/s", path], timeout)
            return code, output
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

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
    "DISKPART_TIMEOUT",
    "GHOST_TIMEOUT",
    "PARTITIONS_SCRIPT",
    "QUERY_ATTEMPTS",
    "QUERY_INTERVAL",
    "QUERY_TIMEOUT",
    "RELEASE_TIMEOUT",
    "VERIFY_TIMEOUT",
    "DiskpartShell",
    "ProcessShell",
    "find_program",
    "run_process",
]
