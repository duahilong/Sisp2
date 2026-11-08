"""应用服务：把“扫描、校验、盘符分配、执行”收敛到一个可注入的门面。

``WorkflowController`` 只依赖本类暴露的四个方法，因此界面上看到的流程
（扫描 → 计划 → 确认 → 执行）与真正的 PowerShell/diskpart 调用之间隔了一层，
既方便替换实现，也方便在测试里完全绕开磁盘。

**安全默认值（最重要的一条）**：``SispService()`` 默认 ``dry_run=True``，
``execute_spec()`` 只返回“将要执行什么”的计划，不会调用 diskpart 或 Ghost，
也不会写任何字节。真实写盘必须同时满足下列条件，缺一即抛 ``PermissionError``：

1. 构造时显式传入 ``dry_run=False``；
2. 目标硬盘编号在 ``allowed_disk_numbers`` 白名单里；
3. ``spec.expected_identity`` 与**现在重新扫描到的那块盘**一致（有 UniqueId /
   SerialNumber 就比标识，没有就比盘号 + 容量 + 型号；防止确认后换盘）；
4. 目标盘此刻仍然“可选”（系统盘/启动盘/含系统盘符/离线/只读都会被拒），
   与计划阶段的判定同源，防止绕过确认直接执行；
5. 注入了 ``real_runner``（真正的执行实现放在仓库外或由调用方提供）。

也就是说，默认构造 + 默认调用一定不会碰磁盘；这不是配置项，而是代码路径本身。

导入说明：本模块对 ``app.modules.disk_info`` / ``app.drive_letters`` 的引用
放在函数内部并带 ``ImportError`` 兜底，这两个模块不可用时仍能 ``import``
本模块，并退回到内置的只读扫描与同规则盘符分配。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.application.contracts import (
    DiskInfo,
    SelectionPlan,
    format_size,
)
from app.application.partitions import (
    PartitionLayoutError,
    plan_partitions,
    preview_segments,
    preview_text,
)
from app.application.steps import STEP_TITLES
from app.models import STEP_SKIPPED, StepResult, WorkerResult
from app.orchestrator import build_worker_disk_identity, identity_matches

_LOGGER = logging.getLogger(__name__)

# 与 app.drive_letters 保持一致的三分区布局；本地兜底时复用同一套常量。
# E–Z 优先，A/B 兜底（见 app/drive_letters.py 的说明）。
AVAILABLE_DRIVE_LETTERS = [*list("EFGHIJKLMNOPQRSTUVWXYZ"), "A", "B"]
LETTERS_PER_DISK = 3
DRIVE_LETTER_ROLES = ("efi", "windows", "data")

# 进度回调签名：on_step(step_key, status, message)；status 取 app.models.STEP_*。
StepReporter = Callable[..., None]

POWERSHELL_TIMEOUT = 60.0
BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024

# 只读扫描脚本：不 select、不 clean，只 Get-Disk / Get-Partition。
_SCAN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$items = foreach ($disk in Get-Disk) {
    $letters = @(
        Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue |
            Where-Object { $_.DriveLetter } |
            ForEach-Object { [string]$_.DriveLetter }
    )
    [pscustomobject]@{
        disk_number     = [int]$disk.Number
        model           = [string]$disk.FriendlyName
        serial_number   = [string]$disk.SerialNumber
        unique_id       = [string]$disk.UniqueId
        size_bytes      = [long]$disk.Size
        partition_style = [string]$disk.PartitionStyle
        bus_type        = [string]$disk.BusType
        is_boot         = [bool]$disk.IsBoot
        is_system       = [bool]$disk.IsSystem
        is_offline      = [bool]$disk.IsOffline
        is_read_only    = [bool]$disk.IsReadOnly
        drive_letters   = $letters
    }
}
@($items) | ConvertTo-Json -Depth 4 -Compress
"""


def _call_runner(
    runner: Callable[..., WorkerResult],
    spec: Any,
    cancel_event: threading.Event,
    on_step: StepReporter | None,
) -> WorkerResult:
    """按执行器签名决定要不要传进度回调（兼容只收两个参数的旧执行器）。"""
    if on_step is not None:
        try:
            accepts = "on_step" in inspect.signature(runner).parameters
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            return runner(spec, cancel_event, on_step=on_step)
    return runner(spec, cancel_event)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
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


def _system_drive_letter() -> str | None:
    """系统盘符；``app.drive_letters`` 不可用时也能算出来。"""
    if os.name == "nt":
        drive = (os.environ.get("SYSTEMDRIVE") or "C:").rstrip("\\/") or "C:"
        return drive[0].upper()
    return Path("/").anchor[:1].upper() or None


def _normalize_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把 PowerShell 的原始字段统一成 ``DiskInfo.from_summary`` 认得的形状。"""
    size_bytes = _as_int(raw.get("size_bytes", raw.get("Size")))
    letters = raw.get("drive_letters") or raw.get("DriveLetters") or []
    if isinstance(letters, str):
        letters = [letters]
    normalized_letters = [
        str(letter).strip().upper()
        for letter in letters
        if str(letter).strip()
    ]
    return {
        "disk_number": _as_int(raw.get("disk_number", raw.get("Number"))) or 0,
        "model": _text(raw.get("model") or raw.get("friendly_name") or raw.get("FriendlyName")),
        "serial_number": _text(raw.get("serial_number") or raw.get("SerialNumber")),
        "unique_id": _text(raw.get("unique_id") or raw.get("UniqueId")),
        "size_bytes": size_bytes,
        "size_display": format_size(size_bytes),
        "partition_style": _text(raw.get("partition_style") or raw.get("PartitionStyle")),
        "bus_type": _text(raw.get("bus_type") or raw.get("BusType")),
        "drive_letters": normalized_letters,
        "is_boot": bool(raw.get("is_boot", raw.get("IsBoot", False))),
        "is_system": bool(raw.get("is_system", raw.get("IsSystem", False))),
        "is_offline": bool(raw.get("is_offline", raw.get("IsOffline", False))),
        "is_read_only": bool(raw.get("is_read_only", raw.get("IsReadOnly", False))),
        "partitions": list(raw.get("partitions") or []),
    }


class SispService:
    """应用服务门面；所有外部依赖都可注入，默认实现全部是只读的。"""

    def __init__(
        self,
        *,
        dry_run: bool = True,
        allowed_disk_numbers: Iterable[int] | None = None,
        real_runner: Callable[[Any, threading.Event], WorkerResult] | None = None,
        disk_scanner: Callable[[], list[dict[str, Any]]] | None = None,
        powershell: str | None = None,
        protect_system_disk: bool = True,
    ) -> None:
        # 默认 True：只要调用方没明确要求，就永远不写盘。
        self.dry_run = bool(dry_run)
        # 安全开关由界面设置里的 protectSystemDisk 驱动。
        # protect_system_disk=False 只是“允许列出并操作系统盘”，
        # 白名单与执行前的身份复核依旧生效。
        self.protect_system_disk = bool(protect_system_disk)
        self.allowed_disk_numbers = (
            frozenset(int(number) for number in allowed_disk_numbers)
            if allowed_disk_numbers is not None
            else frozenset()
        )
        self._real_runner = real_runner
        self._disk_scanner = disk_scanner
        self._powershell = powershell

    # ------------------------------------------------------------------ 扫描
    def scan_disks(self) -> list[dict[str, Any]]:
        """返回硬盘摘要列表（纯只读）。

        优先用 ``app.modules.disk_info.service.scan_disk_summaries``；
        该模块不可用时退回到本模块内置的 ``Get-Disk`` 扫描。
        """
        if self._disk_scanner is not None:
            return [dict(item) for item in self._disk_scanner()]

        try:
            from app.modules.disk_info.service import scan_disk_summaries
        except ImportError:
            _LOGGER.debug("disk_info 模块不可用，使用内置只读扫描")
        else:
            return [dict(item) for item in scan_disk_summaries()]

        return self._scan_with_powershell()

    def _scan_with_powershell(self) -> list[dict[str, Any]]:
        shell = self._powershell or shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            raise RuntimeError("未找到 powershell/pwsh，无法扫描硬盘")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(  # noqa: S603 - 固定脚本，不含用户输入
            [shell, "-NoProfile", "-NonInteractive", "-Command", _SCAN_SCRIPT],
            capture_output=True,
            timeout=POWERSHELL_TIMEOUT,
            creationflags=creationflags,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("mbcs", errors="replace").strip()
            raise RuntimeError(f"硬盘扫描失败（退出码 {completed.returncode}）: {detail[:500]}")

        text = completed.stdout.decode("utf-8-sig", errors="replace").strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"硬盘扫描输出不是合法 JSON: {exc}") from exc
        if isinstance(payload, Mapping):
            payload = [payload]
        if not isinstance(payload, list):
            raise TypeError("硬盘扫描输出格式异常")
        return [_normalize_summary(item) for item in payload if isinstance(item, Mapping)]

    def scan_protected_disks(self) -> list[DiskInfo]:
        """扫描并标记保护状态；被保护的盘 ``is_selectable=False``。"""
        system_letter = _system_drive_letter()

        disks: list[DiskInfo] = []
        for summary in self.scan_disks():
            disk = DiskInfo.from_summary(summary)
            reasons = self._protection_reasons(disk, system_letter)
            disks.append(
                replace(
                    disk,
                    is_selectable=not reasons,
                    protection_reasons=tuple(reasons),
                )
            )
        disks.sort(key=lambda item: item.disk_number)
        return disks

    def _protection_reasons(
        self,
        disk: DiskInfo,
        system_letter: str | None = None,
    ) -> list[str]:
        """一块盘“不可操作”的全部理由；空列表表示可选。

        ``protect_system_disk`` 关掉时系统盘/引导盘/含系统盘符的理由不产生，
        但离线、只读这两种保护永远生效。
        """
        reasons: list[str] = []
        if self.protect_system_disk:
            if disk.is_system:
                reasons.append("系统盘")
            if disk.is_boot:
                reasons.append("启动盘")
            letter = system_letter or _system_drive_letter()
            if letter and letter in disk.drive_letters:
                reasons.append(f"包含系统盘符 {letter}:")
        if disk.is_offline:
            reasons.append("硬盘离线")
        if disk.is_read_only:
            reasons.append("硬盘只读")
        return list(dict.fromkeys(reasons))

    # ------------------------------------------------------------------ 计划
    def build_selection_plan(
        self,
        payload: Mapping[str, Any] | None,
        summaries: Sequence[Mapping[str, Any]],
        selected: Sequence[int],
        *,
        log_dir: str | None = None,
        result_dir: str | None = None,
        resource_resolver: Callable[[Any], int | None] | None = None,
        extra_counts: Mapping[int, int] | None = None,
    ) -> SelectionPlan:
        """校验选中硬盘并分配盘符，返回 ``SelectionPlan``。

        ``resource_resolver`` 若提供，会被当作“这块盘至少需要多少字节”的
        计算函数；容量不足直接抛 ``ValueError``，不进入确认界面。
        ``extra_counts`` 是 ``{硬盘编号: 额外数据分区数}``（每盘 0 或 1），
        决定这块盘要分配 3 个还是 4 个盘符。
        """
        _ = (log_dir, result_dir)  # 目前只透传，不落盘

        disk_numbers = [int(number) for number in selected]
        if not disk_numbers:
            raise ValueError("尚未选择任何硬盘")

        by_number: dict[int, Mapping[str, Any]] = {}
        for summary in summaries:
            number = _as_int(summary.get("disk_number"))
            if number is None:
                continue
            by_number[number] = summary

        missing = [number for number in disk_numbers if number not in by_number]
        if missing:
            raise ValueError(f"硬盘编号不存在: {', '.join(str(number) for number in missing)}")

        used_letters: set[str] = set()
        for summary in summaries:
            for letter in summary.get("drive_letters") or ():
                text = str(letter).strip().upper()
                if text:
                    used_letters.add(text)
        system_letter = _system_drive_letter()
        if system_letter:
            used_letters.add(system_letter)

        # 摘要只能看到 Get-Partition 报告的盘符；EFI 等隐藏卷或已删除卷留下的
        # 幽灵盘符（DriveInfo IsReady=False）会被漏掉，导致 assign letter 失败。
        try:
            from app.modules.disk_info.service import scan_used_drive_letters
        except ImportError:
            _LOGGER.debug("disk_info 模块不可用，跳过系统盘符扫描")
        else:
            try:
                for letter in scan_used_drive_letters():
                    text = str(letter).strip().upper()
                    if text:
                        used_letters.add(text)
            except Exception as exc:  # noqa: BLE001 - 扫描失败不阻断建计划
                _LOGGER.warning("扫描系统已用盘符失败，仅使用磁盘摘要中的盘符: %s", exc)

        if resource_resolver is not None:
            for number in disk_numbers:
                try:
                    required = resource_resolver(payload)
                except Exception as exc:  # noqa: BLE001 - 解析失败按“无法判断”放行
                    _LOGGER.warning("资源解析失败（硬盘 %s）: %s", number, exc)
                    break
                if not isinstance(required, int) or required <= 0:
                    break
                size_bytes = _as_int(by_number[number].get("size_bytes")) or 0
                if size_bytes and size_bytes < required:
                    raise ValueError(
                        f"硬盘 {number} 容量不足：需要 {format_size(required)}，"
                        f"实际 {format_size(size_bytes)}"
                    )

        allocations = self._allocate_letters(disk_numbers, used_letters, extra_counts)
        return SelectionPlan(
            selected_disk_numbers=tuple(disk_numbers),
            forbidden_drive_letters=tuple(sorted(used_letters)),
            drive_letter_allocations=allocations,
        )

    def _allocate_letters(
        self,
        disk_numbers: list[int],
        forbidden: set[str],
        extra_counts: Mapping[int, int] | None = None,
    ) -> dict[int, dict[str, str]]:
        """优先复用 ``app.drive_letters``；不可用时用同规则本地分配。"""
        extra_counts = dict(extra_counts or {})
        try:
            from app.drive_letters import build_drive_letter_allocations
        except ImportError:
            pass
        else:
            return build_drive_letter_allocations(
                list(disk_numbers), sorted(forbidden), extra_counts
            )

        from app.drive_letters import letter_roles

        pool = [letter for letter in AVAILABLE_DRIVE_LETTERS if letter not in forbidden]
        allocations: dict[int, dict[str, str]] = {}
        index = 0
        for number in disk_numbers:
            extra = max(0, min(int(extra_counts.get(number, 0)), 1))
            need = LETTERS_PER_DISK + extra
            if index + need > len(pool):
                raise ValueError(f"可用盘符不足，无法为硬盘 {number} 分配 {need} 个盘符")
            letters = pool[index:index + need]
            allocations[number] = dict(zip(letter_roles(extra), letters, strict=False))
            index += need
        return allocations

    # ------------------------------------------------------------------ 执行
    def execute_spec(
        self,
        spec: Any,
        cancel_event: threading.Event | None = None,
        on_step: StepReporter | None = None,
    ) -> WorkerResult:
        """执行（或演练）一个 ``JobSpec``。

        dry-run 只生成步骤预览；真实执行必须通过五道闸门，否则抛
        ``PermissionError``——失败信息会直接显示在任务状态里。

        ``on_step(key, status, message)`` 是可选的进度回调：真实执行时由
        ``WindowsDiskRunner`` 在每一步开始/结束时调用，界面据此画进度条；
        不传就完全不感知进度（CLI 场景）。
        """
        if spec is None:
            raise ValueError("缺少任务输入 JobSpec")

        disk_number = int(getattr(spec, "disk_number", 0) or 0)
        spec_dry_run = bool(getattr(spec, "dry_run", False))

        if spec_dry_run:
            return self._dry_run_result(spec, disk_number, cancel_event, on_step)
        if self.dry_run:
            # 明确拒绝，而不是悄悄降级成 dry-run：否则任务会显示“成功”，
            # 使用者会误以为盘已经写过了。
            raise PermissionError(
                "SispService 处于 dry-run 模式，拒绝执行任何写盘操作；"
                "真实执行需要显式构造 SispService(dry_run=False, ...)"
            )

        self._require_write_allowed(spec)
        runner = self._real_runner
        if runner is None:
            raise PermissionError(
                "未注入 real_runner：SispService 不内置写盘实现，"
                "真实执行需要调用方显式提供执行器"
            )
        return _call_runner(runner, spec, cancel_event or threading.Event(), on_step)

    def require_disk_writable(
        self,
        disk_number: int,
        expected_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非整盘任务（分区操作、修复引导）复用的同一条写盘闸门。

        判定链与 ``_require_write_allowed`` 完全一致：dry-run → 白名单 →
        **重新扫描后复核身份** → 保护规则。返回最新的硬盘摘要，
        调用方据此再决定具体命令，避免“界面上看到的盘”和“实际要动的盘”不是同一块。
        """
        return self._gate_target(
            int(disk_number),
            dict(expected_identity or {}),
        )

    def _require_write_allowed(self, spec: Any) -> None:
        """真实写盘的四道闸门；任何一道不通都抛 ``PermissionError``。

        第 4 道是“目标盘现在仍然可选”：系统盘/启动盘/含系统盘符/离线/只读
        一律拒绝。计划阶段已经有同样的判定，这里是执行层的纵深防御，防止绕过
        ``confirm()`` 直接调 ``execute_spec``。
        """
        self._gate_target(
            int(getattr(spec, "disk_number", 0) or 0),
            dict(getattr(spec, "expected_identity", None) or {}),
        )

    def _gate_target(
        self,
        disk_number: int,
        expected: Mapping[str, Any],
    ) -> dict[str, Any]:
        """四道闸门的实现；返回重新扫描到的硬盘摘要。"""
        if self.dry_run:
            raise PermissionError(
                "SispService 处于 dry-run 模式，拒绝执行任何写盘操作；"
                "真实执行需要显式构造 SispService(dry_run=False, ...)"
            )
        if disk_number not in self.allowed_disk_numbers:
            raise PermissionError(
                f"硬盘 {disk_number} 不在允许写盘的白名单内"
                f"（当前白名单: {sorted(self.allowed_disk_numbers) or '空'}）"
            )

        summary = self._find_disk_summary(disk_number)
        if summary is None:
            raise PermissionError(f"硬盘 {disk_number} 当前不存在，拒绝写盘")
        actual = build_worker_disk_identity(summary)
        # require_strong=False：没有 UniqueId/SerialNumber 的盘也能写，那就按
        # 盘号 + 容量 + 型号比对；有标识的盘仍逐个比对标识，换盘一样会被挡住。
        if not identity_matches(dict(expected), actual, require_strong=False):
            raise PermissionError(
                f"硬盘 {disk_number} 身份与确认时不一致，拒绝写盘"
            )

        reasons = self._protection_reasons(DiskInfo.from_summary(summary))
        if reasons:
            raise PermissionError(
                f"硬盘 {disk_number} 受保护，拒绝写盘：{'、'.join(reasons)}"
            )
        return summary

    def _find_disk_summary(self, disk_number: int) -> dict[str, Any] | None:
        for summary in self.scan_disks():
            if _as_int(summary.get("disk_number")) == disk_number:
                return summary
        return None

    def _find_disk_identity(self, disk_number: int) -> dict[str, Any] | None:
        summary = self._find_disk_summary(disk_number)
        return build_worker_disk_identity(summary) if summary is not None else None

    def _dry_run_result(
        self,
        spec: Any,
        disk_number: int,
        cancel_event: threading.Event | None,
        on_step: StepReporter | None = None,
    ) -> WorkerResult:
        """dry-run：产出计划步骤和 diskpart 预览，绝不执行命令。"""
        if cancel_event is not None and cancel_event.is_set():
            return WorkerResult.failure(disk_number, "任务已取消", dry_run=True)

        letters = dict(getattr(spec, "drive_letters", None) or {})
        payload = dict(getattr(spec, "task_payload", None) or {})
        options = dict(getattr(spec, "runner_options", None) or {})
        copy_source = str(options.get("copy_source") or "").strip()
        script = self.build_diskpart_preview(disk_number, payload, letters)

        steps = (
            StepResult(
                key="efi",
                title="创建 EFI 分区",
                status=STEP_SKIPPED,
                message="dry-run 未执行",
                details={"letter": letters.get("efi"), "size_mb": self._efi_size_mb(payload)},
            ),
            StepResult(
                key="windows",
                title=STEP_TITLES["windows"],
                status=STEP_SKIPPED,
                message="dry-run 未执行",
                details={"letter": letters.get("windows"), "size_gb": self._c_size_gb(payload)},
            ),
            StepResult(
                key="data",
                title="创建数据分区",
                status=STEP_SKIPPED,
                message="dry-run 未执行",
                details={"letter": letters.get("data"), "size_gb": self._data_size_gb(payload)},
            ),
            StepResult(
                key="layout",
                title=STEP_TITLES["layout"],
                status=STEP_SKIPPED,
                message="dry-run 未执行",
                details={"check": "partition_phase"},
            ),
            StepResult(
                key="image",
                title=STEP_TITLES["image"],
                status=STEP_SKIPPED,
                message="dry-run 未执行",
                details={"image_path": self._image_path(payload)},
            ),
            StepResult(
                key="boot",
                title=STEP_TITLES["boot"],
                status=STEP_SKIPPED,
                message="dry-run 未执行",
                details={},
            ),
            StepResult(
                key="copy",
                title=STEP_TITLES["copy"],
                status=STEP_SKIPPED,
                message=(
                    "dry-run 未执行"
                    if copy_source
                    else "未设置拷贝目录，跳过"
                ),
                details={"source": copy_source, "target": letters.get("data")},
            ),
            StepResult(
                key="diskpart_preview",
                title="diskpart 脚本预览",
                status=STEP_SKIPPED,
                message="仅预览，未执行",
                details={"script": script},
            ),
        )
        if on_step is not None:
            for step in steps:
                on_step(step.key, step.status, step.message)
        return WorkerResult.dry_run_plan(
            disk_number,
            steps=steps,
            message="dry-run 模式：已生成执行计划，未执行任何写盘命令",
        )

    # ------------------------------------------------------------------ 预览
    @staticmethod
    def _partition_info(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        info = payload.get("partition_info")
        return info if isinstance(info, Mapping) else {}

    def _efi_size_mb(self, payload: Mapping[str, Any]) -> float:
        raw = self._partition_info(payload).get("efi_size_mb")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 100.0
        return value if value > 0 else 100.0

    def _c_size_gb(self, payload: Mapping[str, Any]) -> float | None:
        raw = self._partition_info(payload).get("c_size_gb")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _image_path(payload: Mapping[str, Any]) -> str:
        info = payload.get("image_info")
        if not isinstance(info, Mapping):
            return ""
        return str(info.get("image_path") or "")

    def _data_size_gb(self, payload: Mapping[str, Any]) -> float | str | None:
        raw = self._partition_info(payload).get("data_size_gb")
        if raw in (None, "", "auto"):
            return "剩余空间"
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def build_diskpart_preview(
        self,
        disk_number: int,
        payload: Mapping[str, Any],
        letters: Mapping[str, str],
    ) -> str:
        """生成 diskpart 脚本文本用于预览；**不会**被本模块执行。

        脚本来自 :func:`app.application.partitions.preview_segments`——与真实
        执行器同一套生成器，所以"预览和真跑不一样"这类漂移在机制上不存在。
        """
        image_info = payload.get("image_info")
        image_path = None
        if isinstance(image_info, Mapping):
            image_path = _text(image_info.get("image_path"))
        try:
            parts = plan_partitions(payload, letters=letters)
        except PartitionLayoutError as exc:
            # 分区配置不合法也要能看完预览，而不是把演练步弄成异常。
            lines = ["REM === dry-run 预览：以下命令不会被执行 ===", f"REM 分区配置不合法：{exc}"]
            return chr(10).join(lines)
        script = preview_text(disk_number, parts)
        if image_path:
            script += chr(10) + f"REM 镜像：{image_path}（由应用层接管，不经 diskpart）"
        return script

    def build_diskpart_segments(
        self,
        disk_number: int,
        payload: Mapping[str, Any],
        letters: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """分段预览（``POST /api/partition/preview`` 的 ``data.segments``）。"""
        parts = plan_partitions(payload, letters=letters)
        return preview_segments(disk_number, parts)

