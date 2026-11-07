"""只读校验：引导文件 + 系统文件到底写没写进去。

判据只留两项，因为其余项（GPT、分区数量、无残留 MSR）由「分区验收」步骤用
``partitions.partition_verdict`` 已经查过一遍——同一件事查两次只会让报错位置
变模糊。这里只回答一个问题：**bcdboot 与 Ghost 真的落盘了吗**。

- 引导文件：``<ESP>:\\EFI\\Microsoft\\Boot\\bootmgfw.efi`` 或
  ``<ESP>:\\EFI\\BOOT\\BOOTX64.EFI``；
- 系统文件：``<Windows>:\\Windows\\System32\\config\\SYSTEM`` 或
  ``<Windows>:\\Windows\\System32\\ntoskrnl.exe``。

只读：不改盘、不写盘。必须在释放 EFI 盘符之前跑（ESP 是隐藏卷，靠临时盘符
才能查文件），这一点由 ``UefiBootPhase`` 的内部顺序保证。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.application.runner_utils import tail
from app.application.shell import VERIFY_TIMEOUT
from app.application.uefi.target import BootTarget

#: 在目标系统之外查文件，所以用 PowerShell；输出必须是最后一行合法 JSON。
VERIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$efiLetter = [string]$env:VIX_EFI_LETTER
$windowsLetter = [string]$env:VIX_WINDOWS_LETTER
function Find-BootFile($paths) {
  foreach ($path in $paths) { if ($path -and (Test-Path -LiteralPath $path)) { return $path } }
  return $null
}
$bootFile = $null
if ($efiLetter) {
  $bootFile = Find-BootFile @("${efiLetter}:\EFI\Microsoft\Boot\bootmgfw.efi", "${efiLetter}:\EFI\BOOT\BOOTX64.EFI")
}
$systemFile = $null
if ($windowsLetter) {
  $systemFile = Find-BootFile @("${windowsLetter}:\Windows\System32\config\SYSTEM", "${windowsLetter}:\Windows\System32\ntoskrnl.exe")
}
[pscustomobject]@{ boot_file = $bootFile; system_file = $systemFile } | ConvertTo-Json -Compress
"""


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def boot_verdict(payload: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """纯判定：只看引导文件与系统文件（可脱离 shell 单测）。"""
    boot_file = str(payload.get("boot_file") or "").strip()
    system_file = str(payload.get("system_file") or "").strip()
    problems: list[str] = []
    if not boot_file:
        problems.append("EFI 分区里没有 bootmgfw.efi（bcdboot 没写进去）")
    if not system_file:
        problems.append("系统分区里没有 Windows 系统文件（镜像没应用完整）")
    details: dict[str, Any] = {
        "boot_file": boot_file or None,
        "system_file": system_file or None,
        "problems": problems,
    }
    if problems:
        return False, "引导校验不符合预期：" + "；".join(problems), details
    return (
        True,
        f"引导 {Path(boot_file).name}；系统 {Path(system_file).name}",
        details,
    )


def _extract_json(raw: str) -> dict[str, Any] | None:
    """取输出里最后一个 JSON 对象（PowerShell 首启可能先吐别的行）。"""
    for line in reversed([item.strip() for item in str(raw or "").splitlines() if item.strip()]):
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class BootProbe:
    """跑校验脚本并把输出翻成 ``ProbeResult``；只认 ``BootTarget``。"""

    def __init__(self, shell: Any, script: str = VERIFY_SCRIPT,
                 powershell: str | None = None,
                 command_builder: Callable[[BootTarget], list[str]] | None = None) -> None:
        self.shell = shell
        self.script = script
        self.powershell = powershell or getattr(shell, "powershell", None)
        self.command_builder = command_builder

    def command(self, target: BootTarget) -> list[str]:
        if self.command_builder is not None:
            return list(self.command_builder(target))
        return [str(self.powershell or "powershell"), "-NoProfile", "-NonInteractive", "-Command", self.script]

    def environment(self, target: BootTarget) -> dict[str, str]:
        # 带上当前进程环境：PowerShell 需要 SystemRoot 才能启动；
        # 三个 VIX_* 变量是脚本唯一读取的入参。
        env = dict(os.environ)
        env.update(
            {
                "VIX_DISK_NUMBER": str(target.disk_number),
                "VIX_EFI_LETTER": target.efi_letter,
                "VIX_WINDOWS_LETTER": target.windows_letter,
            }
        )
        return env

    def inspect(self, target: BootTarget) -> ProbeResult:
        command = self.command(target)
        try:
            code, output, elapsed = self.shell.run(
                command, VERIFY_TIMEOUT, env=self.environment(target)
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(ok=False, message=f"校验命令无法执行：{exc}")
        if code != 0:
            return ProbeResult(
                ok=False,
                message=f"校验命令失败（退出码 {code}）：{tail(output, 800)}",
                details={"exit": code},
            )
        payload = _extract_json(output)
        if payload is None:
            return ProbeResult(
                ok=False,
                message=f"校验输出不是合法 JSON：{tail(output, 800)}",
                details={"exit": code},
            )
        ok, message, details = boot_verdict(payload)
        details.update({"exit": code, "seconds": round(elapsed, 1)})
        return ProbeResult(ok=ok, message=message, details=details)


__all__ = ["VERIFY_SCRIPT", "BootProbe", "ProbeResult", "boot_verdict"]
