"""修复引导：扫描各磁盘的 Windows 目录与 EFI 引导文件，必要时重写 BCD。

为什么单独一个模块：装系统时 ``bcdboot`` 是第 5 步，一定会跑；但“已经装好的
盘突然开不了机”是另一类任务——用户只想重写引导，不想清盘重装。

本模块只做三件事：

1. ``scan()``：一次 PowerShell 扫描，拿到每块盘的分区、访问路径、是否含
   ``Windows\\System32\\config\\SYSTEM``、是否含 ``EFI\\Microsoft\\Boot\\bootmgfw.efi``；
2. ``analyze()``：纯函数，把扫描结果翻成“诊断结论 + 可修复目标”；
3. ``repair()``：给目标盘临时分配 ESP 盘符 → ``bcdboot ... /f UEFI`` → 释放盘符
   → 重新扫描验证。写盘前必须过 ``SispService.require_disk_writable``。

设计取舍：ESP 常常没有盘符，``bcdboot /s`` 需要一个可寻址的卷，所以修复流程
里会临时 ``assign letter``，结束后立刻 ``remove letter``——即使 bcdboot 失败也
在 ``finally`` 里释放，不留幽灵盘符。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.safety_guard import ensure_command_allowed

BCDBOOT_TIMEOUT = 120.0
DISKPART_TIMEOUT = 120.0

ESP_GPT_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"

# 只在这些盘符里挑临时盘符，避免占用 A/B（软驱位）和已经很常见的 C/D。
CANDIDATE_LETTERS = "EFGHIJKLMNOPQRSTUVWXYZ"

_FREE_LETTERS_FALLBACK = "ZYXWVUTSRQPONMLKJIHGFED"


class BootError(ValueError):
    """无法修复时抛出的用户可读错误。"""


SCAN_SCRIPT = r"""
$disks = Get-Disk | Sort-Object Number
$result = @()
foreach ($disk in $disks) {
    $parts = @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue)
    $list = @()
    foreach ($part in $parts) {
        $paths = @($part.AccessPaths | Where-Object { $_ })
        $hasWindows = $false
        $hasBootMgr = $false
        foreach ($path in $paths) {
            if (Test-Path (Join-Path $path 'Windows\System32\config\SYSTEM')) { $hasWindows = $true }
            if (Test-Path (Join-Path $path 'EFI\Microsoft\Boot\bootmgfw.efi')) { $hasBootMgr = $true }
        }
        $list += [PSCustomObject]@{
            partition_number = $part.PartitionNumber
            drive_letter = $part.DriveLetter
            gpt_type = $part.GptType
            size_bytes = $part.Size
            offset_bytes = $part.Offset
            is_system = $part.IsSystem
            is_boot = $part.IsBoot
            access_paths = $paths
            has_windows = $hasWindows
            has_bootmgr = $hasBootMgr
        }
    }
    $result += [PSCustomObject]@{
        disk_number = $disk.Number
        model = $disk.FriendlyName
        size_bytes = $disk.Size
        partitions = $list
    }
}
$firmware = @(bcdedit /enum firmware 2>$null | Select-String -Pattern 'identifier' -SimpleMatch:$false)
[PSCustomObject]@{
    disks = $result
    firmware_entries = $firmware.Count
    scanned_at = (Get-Date).ToString('o')
} | ConvertTo-Json -Depth 8
""".strip()


def scan() -> dict[str, Any]:
    """执行扫描；PowerShell 不可用时抛 ``BootError``。"""
    from app.modules.disk_info.service import run_powershell_json

    try:
        raw = run_powershell_json(SCAN_SCRIPT)
    except Exception as exc:
        raise BootError(f"扫描引导信息失败：{exc}") from exc

    payload = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(payload, Mapping):
        raise BootError("扫描引导信息失败：PowerShell 没有返回可解析的结果")

    disks = []
    for disk in payload.get("disks") or []:
        if not isinstance(disk, Mapping):
            continue
        partitions = []
        for part in disk.get("partitions") or []:
            if not isinstance(part, Mapping):
                continue
            paths = [str(path) for path in (part.get("access_paths") or []) if path]
            partitions.append(
                {
                    "partition_number": _int(part.get("partition_number")),
                    "drive_letter": (str(part.get("drive_letter") or "").strip().upper()[:1] or None),
                    "gpt_type": str(part.get("gpt_type") or "").strip("{}").lower(),
                    "size_bytes": _int(part.get("size_bytes")),
                    "offset_bytes": _int(part.get("offset_bytes")),
                    "is_system": bool(part.get("is_system")),
                    "is_boot": bool(part.get("is_boot")),
                    "access_paths": paths,
                    "has_windows": bool(part.get("has_windows")),
                    "has_bootmgr": bool(part.get("has_bootmgr")),
                }
            )
        disks.append(
            {
                "disk_number": _int(disk.get("disk_number")),
                "model": str(disk.get("model") or "").strip() or None,
                "size_bytes": _int(disk.get("size_bytes")),
                "partitions": partitions,
            }
        )
    return {
        "disks": disks,
        "firmware_entries": _int(payload.get("firmware_entries")) or 0,
        "scanned_at": str(payload.get("scanned_at") or ""),
    }


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_esp(partition: Mapping[str, Any]) -> bool:
    return str(partition.get("gpt_type") or "").startswith(ESP_GPT_TYPE)


def _used_letters(snapshot: Mapping[str, Any]) -> set[str]:
    letters: set[str] = set()
    for disk in snapshot.get("disks") or []:
        for part in disk.get("partitions") or []:
            letter = str(part.get("drive_letter") or "").strip().upper()
            if letter:
                letters.add(letter)
    # 系统盘符一定被占用（扫描里 C: 通常已经出现，这里兜底）。
    letters.update({"A", "B", "C"})
    return letters


def pick_letter(snapshot: Mapping[str, Any]) -> str:
    """挑一个没被占用的盘符给 ESP 临时用。"""
    used = _used_letters(snapshot)
    for letter in CANDIDATE_LETTERS:
        if letter not in used:
            return letter
    for letter in _FREE_LETTERS_FALLBACK:
        if letter not in used:
            return letter
    raise BootError("没有空闲盘符可用于临时挂载 EFI 分区")


def analyze(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """把扫描结果翻成诊断结论 + 可修复目标。纯函数。"""
    findings: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []

    disks = [disk for disk in (snapshot.get("disks") or []) if isinstance(disk, Mapping)]
    if not disks:
        findings.append(
            _finding("error", "no_disk", "未检测到硬盘", "请确认硬盘已连接并以管理员身份运行。")
        )
        return {"findings": findings, "targets": targets, "firmware_entries": 0}

    windows_parts = [
        (disk, part)
        for disk in disks
        for part in disk.get("partitions") or []
        if part.get("has_windows")
    ]
    esp_parts = [
        (disk, part)
        for disk in disks
        for part in disk.get("partitions") or []
        if _is_esp(part)
    ]

    if not windows_parts:
        findings.append(
            _finding(
                "error",
                "no_windows",
                "没有找到可引导的 Windows 目录",
                "所有分区里都没有 Windows\\System32\\config\\SYSTEM，请先用「系统安装」部署镜像。",
            )
        )
    if not esp_parts:
        findings.append(
            _finding(
                "error",
                "no_esp",
                "没有找到 EFI 系统分区",
                "UEFI 引导必须写在 ESP 上；请在「系统安装」里重建分区表。",
            )
        )

    for disk, part in windows_parts:
        disk_number = disk.get("disk_number")
        windows_letter = part.get("drive_letter")
        esp_on_disk = next(
            (esp for owner, esp in esp_parts if owner.get("disk_number") == disk_number), None
        )
        boot_ok = bool(esp_on_disk and esp_on_disk.get("has_bootmgr"))
        target = {
            "disk_number": disk_number,
            "model": disk.get("model"),
            "windows_letter": windows_letter,
            "windows_partition": part.get("partition_number"),
            "esp_partition": esp_on_disk.get("partition_number") if esp_on_disk else None,
            "esp_letter": esp_on_disk.get("drive_letter") if esp_on_disk else None,
            "boot_ok": boot_ok,
            "repairable": bool(windows_letter and esp_on_disk),
            "reason": "",
        }
        if not windows_letter:
            target["reason"] = "Windows 分区没有盘符，无法执行 bcdboot"
        elif esp_on_disk is None:
            target["reason"] = "该硬盘上没有 EFI 分区"
        elif boot_ok:
            target["reason"] = "引导文件已存在"
        targets.append(target)

        if boot_ok:
            findings.append(
                _finding(
                    "ok",
                    "boot_ok",
                    f"硬盘 {disk_number} 引导正常",
                    f"ESP（分区 {esp_on_disk.get('partition_number')}）里已有 bootmgfw.efi。",
                    disk_number=disk_number,
                )
            )
        elif esp_on_disk is not None:
            findings.append(
                _finding(
                    "warn",
                    "boot_missing",
                    f"硬盘 {disk_number} 缺少引导文件",
                    "ESP 存在但没有 EFI\\Microsoft\\Boot\\bootmgfw.efi，可以一键重写引导。",
                    disk_number=disk_number,
                )
            )

    orphan_esps = [
        esp
        for disk, esp in esp_parts
        if not any(owner.get("disk_number") == disk.get("disk_number") for owner, _ in windows_parts)
    ]
    if orphan_esps:
        findings.append(
            _finding(
                "warn",
                "orphan_esp",
                "存在没有 Windows 的 EFI 分区",
                "这类分区通常是重装后残留的引导分区，本身不需要修复。",
            )
        )

    return {
        "findings": findings,
        "targets": targets,
        "firmware_entries": int(snapshot.get("firmware_entries") or 0),
        "scanned_at": snapshot.get("scanned_at") or "",
    }


def _finding(
    level: str,
    code: str,
    title: str,
    message: str = "",
    *,
    disk_number: int | None = None,
) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "title": title,
        "message": message,
        "disk_number": disk_number,
    }


def build_script(disk_number: int, partition_number: int, letter: str, *, release: bool) -> list[str]:
    """临时挂载/释放 ESP 盘符的 diskpart 脚本。"""
    lines = [
        f"select disk {int(disk_number)}",
        f"select partition {int(partition_number)}",
    ]
    if release:
        lines.append(f"remove letter={letter}")
    else:
        lines.append(f"assign letter={letter}")
    lines.append("exit")
    return lines


class BootRepairer:
    """扫描 → 诊断 → 重写引导；依赖全部可注入，便于离线测试。"""

    def __init__(
        self,
        gate: Callable[..., Mapping[str, Any]] | None = None,
        *,
        scanner: Callable[[], dict[str, Any]] | None = None,
        bcdboot: str | None = None,
        diskpart: str | None = None,
        temp_dir: str | None = None,
        log_dir: str | None = None,
        dry_run: bool = False,
        which: Callable[[str], str | None] | None = None,
        allow_real_disk: bool = False,
    ) -> None:
        # 同 WindowsDiskRunner：默认不开真实写盘授权，只有 GUI/CLI 入口显式打开。
        self.allow_real_disk = bool(allow_real_disk)
        self._gate = gate
        self._which = which or shutil.which
        self._scanner = scanner or scan
        self.bcdboot = bcdboot or self._which("bcdboot.exe")
        # diskpart 固定用系统自带的；参数只供测试注入（界面已不提供这个设置）。
        self.diskpart = diskpart or self._which("diskpart.exe")
        self.temp_dir = temp_dir
        self.log_dir = log_dir
        self.dry_run = bool(dry_run)

    # ------------------------------------------------------------------ 只读
    def report(self) -> dict[str, Any]:
        return analyze(self._scanner())

    # ------------------------------------------------------------------ 修复
    def repair(
        self,
        disk_number: int,
        *,
        identity: Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        effective_dry_run = self.dry_run if dry_run is None else bool(dry_run)
        snapshot = self._scanner()
        report = analyze(snapshot)
        target = next(
            (item for item in report["targets"] if item.get("disk_number") == int(disk_number)),
            None,
        )
        if target is None:
            raise BootError(
                f"硬盘 {disk_number} 上没有找到 Windows 目录，无需/无法修复引导"
            )
        if not target.get("windows_letter"):
            raise BootError(target.get("reason") or "Windows 分区没有盘符，无法执行 bcdboot")
        if target.get("esp_partition") is None:
            raise BootError("该硬盘上没有 EFI 分区，无法写入 UEFI 引导")

        temp_letter: str | None = None
        esp_letter = target.get("esp_letter")
        steps: list[dict[str, Any]] = []
        if not effective_dry_run:
            if self._gate is None:
                raise BootError("缺少写盘闸门：修复引导必须经过 SispService 的白名单校验")
            self._gate(int(disk_number), dict(identity or {}))
            if not self.bcdboot:
                raise BootError("找不到 bcdboot.exe，无法重写引导")

        plan = {
            "disk_number": int(disk_number),
            "windows_letter": target.get("windows_letter"),
            "esp_partition": target.get("esp_partition"),
            "esp_letter": esp_letter,
            "temp_letter": "",
            "command": "",
        }
        if not esp_letter:
            temp_letter = pick_letter(snapshot)
            esp_letter = temp_letter
            plan["temp_letter"] = temp_letter
        command = [self.bcdboot or "bcdboot", f"{target['windows_letter']}:\\Windows", "/s", f"{esp_letter}:", "/f", "UEFI"]
        plan["command"] = " ".join(command)
        plan["dry_run"] = effective_dry_run

        if effective_dry_run:
            steps.append(_step("mount", "挂载 EFI 分区", "skipped", f"将使用盘符 {esp_letter}"))
            steps.append(_step("bcdboot", "重写引导", "skipped", plan["command"]))
            steps.append(_step("verify", "校验引导文件", "skipped", "演练模式不执行"))
            return {
                **plan,
                "success": True,
                "message": "演练模式：只生成修复步骤，未执行任何命令",
                "steps": steps,
            }

        log = _RunLog(self.log_dir, f"boot-disk{disk_number}")
        steps = []
        try:
            if temp_letter:
                lines = build_script(
                    int(disk_number), int(target["esp_partition"]), temp_letter, release=False
                )
                log.write("挂载 EFI：\n" + "\n".join(lines))
                code, output = self._run_diskpart(lines)
                if code != 0:
                    steps.append(_step("mount", "挂载 EFI 分区", "failed", _tail(output)))
                    raise BootError(f"给 EFI 分区分配盘符失败（退出码 {code}）：{_tail(output, 500)}")
            steps.append(_step("mount", "挂载 EFI 分区", "ok", f"盘符 {esp_letter}"))

            log.write("bcdboot：\n" + " ".join(command))
            code, output = self._run(command)
            if code != 0:
                steps.append(_step("bcdboot", "重写引导", "failed", _tail(output)))
                raise BootError(f"bcdboot 失败（退出码 {code}）：{_tail(output, 500)}")
            steps.append(_step("bcdboot", "重写引导", "ok", _tail(output, 300)))

            verified = analyze(self._scanner())
            ok = any(
                item.get("disk_number") == int(disk_number) and item.get("boot_ok")
                for item in verified["targets"]
            )
            steps.append(
                _step(
                    "verify",
                    "校验引导文件",
                    "ok" if ok else "failed",
                    "ESP 中已存在 bootmgfw.efi" if ok else "重新扫描后仍未找到 bootmgfw.efi",
                )
            )
            if not ok:
                raise BootError("重写引导后仍未检测到 bootmgfw.efi，请检查 ESP 是否可写")
            return {
                **plan,
                "success": True,
                "message": f"硬盘 {disk_number} 引导已重写",
                "steps": steps,
                "log_path": str(log.path) if log.path else "",
            }
        finally:
            if temp_letter:
                release = build_script(
                    int(disk_number), int(target["esp_partition"]), temp_letter, release=True
                )
                log.write("释放盘符：\n" + "\n".join(release))
                self._run_diskpart(release)
            log.flush()

    # ------------------------------------------------------------------ 命令
    def _run(self, command: Sequence[str]) -> tuple[int, str]:
        return _spawn(command, BCDBOOT_TIMEOUT, allow_real_disk=self.allow_real_disk)

    def _run_diskpart(self, lines: Sequence[str]) -> tuple[int, str]:
        if not self.diskpart:
            raise BootError("找不到 diskpart.exe")
        _handle, script_path = tempfile.mkstemp(
            prefix="sisp-boot-", suffix=".txt", dir=self.temp_dir
        )
        try:
            Path(script_path).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
            return _spawn(
                [self.diskpart, "/s", script_path],
                DISKPART_TIMEOUT,
                allow_real_disk=self.allow_real_disk,
            )
        finally:
            try:
                Path(script_path).unlink()
            except OSError:
                pass


def _step(key: str, title: str, status: str, message: str = "") -> dict[str, Any]:
    return {"key": key, "title": title, "status": status, "message": message}


def _spawn(
    command: Sequence[str],
    timeout: float,
    *,
    allow_real_disk: bool = False,
) -> tuple[int, str]:
    # 护栏：bcdboot / diskpart 这类写盘命令必须先过授权检查。
    ensure_command_allowed(command, allow_real_disk=allow_real_disk, source="BootRepairer")
    try:
        completed = subprocess.run(  # noqa: S603 - 固定命令 + 已校验参数
            list(command),
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootError(f"命令超时（{timeout:.0f} 秒），已终止：{' '.join(command)}") from exc
    except OSError as exc:
        raise BootError(f"无法启动命令 {command[0]}：{exc}") from exc
    output = (completed.stdout or b"").decode("mbcs", errors="replace") + (
        completed.stderr or b""
    ).decode("mbcs", errors="replace")
    return completed.returncode, output


def _tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


class _RunLog:
    def __init__(self, log_dir: str | None, prefix: str) -> None:
        self.path: Path | None = None
        self._handle: Any = None
        if not log_dir:
            return
        try:
            base = Path(log_dir)
            base.mkdir(parents=True, exist_ok=True)
            from datetime import datetime

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = base / f"{prefix}-{stamp}.log"
            self._handle = self.path.open("w", encoding="utf-8")
        except OSError:
            self.path = None
            self._handle = None

    def write(self, text: str) -> None:
        if self._handle is not None:
            self._handle.write(text + "\n")
            self._handle.flush()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
