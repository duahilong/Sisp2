"""装机结果校验：装完还能不能开机（引导文件 + 系统文件 + 分区表）。

与 ``layout``（分区验收）的区别，这正是本次解耦要讲清楚的两件事：

- ``layout``：**分区做对了没有**（GPT、分区数、顺序类型、大小、盘符、卷标、吃满）；
- ``verify``：**装完能不能开机**（EFI 里有 ``bootmgfw.efi``、C 里有系统文件）。

前者在 ``image``（Ghost）之后、``boot`` 之前跑，后者在所有写入完成之后跑。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.application.contracts import format_size
from app.application.partitions import is_reserved_partition
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import tail
from app.application.shell import VERIFY_TIMEOUT, ProcessShell

GROUP = "verify"

VERIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$number = [int]$env:VIX_DISK_NUMBER
$efiLetter = [string]$env:VIX_EFI_LETTER
$winLetter = [string]$env:VIX_WINDOWS_LETTER
$disk = Get-Disk -Number $number
$items = foreach ($part in Get-Partition -DiskNumber $number -ErrorAction SilentlyContinue) {
    [pscustomobject]@{
        number        = [int]$part.PartitionNumber
        size_bytes    = [long]$part.Size
        offset_bytes  = [long]$part.Offset
        type          = [string]$part.Type
        gpt_type      = [string]$part.GptType
        drive_letter  = if ($part.DriveLetter) { [string]$part.DriveLetter } else { $null }
        is_active     = [bool]$part.IsActive
        is_hidden     = [bool]$part.IsHidden
    }
}
function Find-First($paths) {
    foreach ($path in $paths) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}
# bcdboot 写完后，EFI 分区里必须有引导文件
$bootFile = $null
if ($efiLetter) {
    $bootFile = Find-First @(
        "${efiLetter}:\EFI\Microsoft\Boot\bootmgfw.efi",
        "${efiLetter}:\EFI\BOOT\BOOTX64.EFI"
    )
}
# Windows 分区里必须有系统文件（真实镜像一定会有）
$systemFile = $null
if ($winLetter) {
    $systemFile = Find-First @(
        "${winLetter}:\Windows\System32\config\SYSTEM",
        "${winLetter}:\Windows\System32\ntoskrnl.exe"
    )
}
[pscustomobject]@{
    disk_number     = $number
    partition_style = [string]$disk.PartitionStyle
    partitions      = @($items)
    boot_file       = $bootFile
    system_file     = $systemFile
} | ConvertTo-Json -Depth 4 -Compress
"""


def verify_verdict(
    payload: Mapping[str, Any],
    min_partitions: int = 3,
) -> tuple[bool, str, dict[str, Any]]:
    """把 verify 脚本的输出变成"这块盘现在能不能用"的结论。

    只靠"GPT + 分区数"是不够的：bcdboot 可能退出码 0 但没写入任何文件，镜像
    也可能只应用了一半。所以这里四个条件都查：分区表、分区数、EFI 引导文件、
    Windows 系统文件。纯函数，方便用假 payload 测失败分支。

    "分区数"是**相等**而不是"至少"：``convert gpt`` 自动建的那个 16 MB MSR
    应该在建分区前被删掉，盘上多一个 Reserved 分区就说明删除没生效。
    """
    parts = payload.get("partitions") or []
    if isinstance(parts, Mapping):
        parts = [parts]
    parts = [part for part in parts if isinstance(part, Mapping)]
    style = str(payload.get("partition_style") or "?").strip().upper()
    boot_file = str(payload.get("boot_file") or "").strip()
    system_file = str(payload.get("system_file") or "").strip()

    summary = "、".join(
        f"#{part.get('number')}"
        f"{('(' + str(part.get('drive_letter')) + ':)' if part.get('drive_letter') else '')}"
        f"={format_size(part.get('size_bytes'))}"
        for part in parts
    )

    problems: list[str] = []
    if style != "GPT":
        problems.append(f"分区表不是 GPT（{style}）")
    # 分区数必须与计划一致：MSR 已被删掉，多一个/少一个都是问题。
    expected = max(3, int(min_partitions))
    if len(parts) != expected:
        problems.append(f"分区数与计划不符（期望 {expected} 个，实际 {len(parts)} 个）")
    reserved = [part for part in parts if is_reserved_partition(part)]
    if reserved:
        problems.append("盘上还有保留分区（MSR），预期布局是 EFI + C + D(+E)")
    if not boot_file:
        problems.append("EFI 分区里没有 bootmgfw.efi（bcdboot 没写进去）")
    if not system_file:
        problems.append("系统分区里没有 Windows 系统文件（镜像没应用完整）")

    details: dict[str, Any] = {
        "partition_style": style,
        "partitions": parts,
        "boot_file": boot_file or None,
        "system_file": system_file or None,
        "problems": problems,
    }
    if problems:
        return False, "校验不符合预期：" + "；".join(problems) + f"（{summary}）", details
    return (
        True,
        f"GPT、{len(parts)} 个分区：{summary}；引导 {Path(boot_file).name}；"
        f"系统 {Path(system_file).name}",
        details,
    )


def _extract_json(text: str) -> Any | None:
    """从输出里取出 JSON 对象：优先整段，其次最后一行能解析的 ``{...}``。

    ``ProcessShell`` 返回的是 stdout + stderr（ghost/powershell 可能往 stderr 写
    编码提示），所以不能假设整段就是 JSON；但仍然只接受合法 JSON，不猜内容。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for line in reversed(raw.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


class BootVerifyPhase:
    """校验组：装完能不能开机（引导文件 + 系统文件）。"""

    group = GROUP

    def __init__(
        self,
        shell: ProcessShell,
        powershell: str | None = None,
        verdict: Any = verify_verdict,
    ) -> None:
        self.shell = shell
        self.powershell = powershell or getattr(shell, "powershell", None)
        self._verdict = verdict

    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        if not self.powershell:
            return PhaseOutcome.failed(
                PhaseResult.failed("verify", "找不到 powershell.exe，无法校验"),
                message="找不到 powershell.exe，无法校验",
            )

        ctx.notify("verify", "running", "校验引导文件与系统文件")
        env = dict(os.environ)
        env["VIX_DISK_NUMBER"] = str(int(ctx.disk_number))
        # 校验时 EFI / Windows 的盘符还没释放（释放发生在编排层的 finally），
        # 正好用来查文件。
        env["VIX_EFI_LETTER"] = str((ctx.letters or {}).get("efi") or "")
        env["VIX_WINDOWS_LETTER"] = str((ctx.letters or {}).get("windows") or "")

        try:
            code, output, _elapsed = self.shell.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    VERIFY_SCRIPT,
                ],
                VERIFY_TIMEOUT,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return PhaseOutcome.failed(
                PhaseResult.failed("verify", f"校验执行失败：{exc}"),
                message=f"校验执行失败：{exc}",
            )

        raw = (output or "").strip()
        ctx.log.block("verify 输出", raw)
        if code != 0:
            return PhaseOutcome.failed(
                PhaseResult.failed(
                    "verify",
                    f"校验脚本失败（退出码 {code}）：{tail(raw, 500)}",
                    exit=code,
                ),
                message="装机结果校验失败",
            )
        try:
            payload = json.loads(raw.splitlines()[-1] if raw else "{}")
        except json.JSONDecodeError:
            return PhaseOutcome.failed(
                PhaseResult.failed("verify", f"校验输出不是合法 JSON：{tail(raw, 300)}"),
                message="装机结果校验失败（输出不是 JSON）",
            )
        ok, message, details = self._verdict(
            payload if isinstance(payload, Mapping) else {},
            min_partitions=len(ctx.parts),
        )
        if not ok:
            return PhaseOutcome.failed(
                PhaseResult.failed("verify", message, **details),
                message="校验不符合预期",
            )
        return PhaseOutcome.succeeded(PhaseResult.ok("verify", message, **details))


__all__ = ["GROUP", "VERIFY_SCRIPT", "BootVerifyPhase", "verify_verdict"]
