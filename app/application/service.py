"""应用服务：把“扫描、校验、盘符分配、执行”收敛到一个可注入的门面。

``WorkflowController`` 只依赖本类暴露的四个方法，因此界面上看到的流程
（扫描 → 计划 → 确认 → 执行）与真正的 PowerShell/diskpart 调用之间隔了一层，
既方便替换实现，也方便在测试里完全绕开磁盘。

写盘必须同时满足下列条件，缺一即抛 ``PermissionError``：

1. 目标硬盘编号在 ``allowed_disk_numbers`` 白名单里；
2. ``spec.expected_identity`` 与**现在重新扫描到的那块盘**一致（有 UniqueId /
   SerialNumber 就比标识，没有就比盘号 + 容量 + 型号；防止确认后换盘）；
3. 目标盘此刻仍然“可选”（系统盘/启动盘/含系统盘符/离线/只读都会被拒），
   与计划阶段的判定同源，防止绕过确认直接执行；
4. 注入了 ``real_runner``（真正的执行实现放在仓库外或由调用方提供）。

没有“演练模式”开关：要看计划与 diskpart 脚本，用只读的
``WorkflowController.prepare()`` 与 ``partitions.preview_segments()``；
``execute_spec()`` 一旦被调用就是要写盘。

扫描依赖 ``app.modules.disk_info``：它不可用时直接报错，不再退回一份
不查分区的内置脚本——那种摘要没有 ``partitions`` / ``drive_letters``，
会让“包含系统盘符”的保护静默失效。
"""

from __future__ import annotations

import inspect
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.application.contracts import DiskInfo, SelectionPlan
from app.application.runner_utils import to_int
from app.models import WorkerResult

# 通过模块属性调用（而不是 from ... import 绑定局部名）：测试用
# monkeypatch.setattr(disk_info, "scan_disk_summaries", ...) 替换扫描实现，
# 绑死局部名会让替身静默失效——这正是 2026-11-06 事故的形态。
# 注意要拿 service 模块本身：`from app.modules import disk_info` 拿到的是包，
# 包里没有这些函数。
from app.modules.disk_info import service as disk_info_service
from app.orchestrator import build_worker_disk_identity, identity_matches
from app.units import format_size

_LOGGER = logging.getLogger(__name__)

# 进度回调签名：on_step(step_key, status, message)；status 取 app.models.STEP_*。
StepReporter = Callable[..., None]


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


def _wanted_numbers(disk_numbers: Sequence[int] | None) -> list[int] | None:
    """整理定向扫描的盘号；``None`` 表示全盘，空列表也按全盘处理。

    空列表（例如“一个盘都没选中”）退回全盘是刻意的：调用点要么已经
    提前校验过选择非空，要么只是想拿一份快照，不能因为空列表就静默扫空。
    """
    if disk_numbers is None:
        return None
    numbers: list[int] = []
    try:
        items = list(disk_numbers)
    except TypeError:
        return None
    for item in items:
        number = to_int(item)
        if number is not None and number >= 0 and number not in numbers:
            numbers.append(number)
    return numbers or None


def _system_drive_letter() -> str | None:
    """系统盘符；``app.drive_letters`` 不可用时也能算出来。"""
    if os.name == "nt":
        drive = (os.environ.get("SYSTEMDRIVE") or "C:").rstrip("\\/") or "C:"
        return drive[0].upper()
    return Path("/").anchor[:1].upper() or None


class SispService:
    """应用服务门面；所有外部依赖都可注入，默认实现全部是只读的。"""

    def __init__(
        self,
        *,
        allowed_disk_numbers: Iterable[int] | None = None,
        real_runner: Callable[[Any, threading.Event], WorkerResult] | None = None,
        disk_scanner: Callable[[], list[dict[str, Any]]] | None = None,
        protect_system_disk: bool = True,
    ) -> None:
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

    # ------------------------------------------------------------------ 扫描
    def scan_disks(
        self, disk_numbers: Sequence[int] | None = None
    ) -> list[dict[str, Any]]:
        """返回硬盘摘要列表（纯只读）。

        ``disk_numbers`` 不为 None 时只查这几块盘：写盘闸门每一步都要核对
        “确认的盘还是不是这块盘”，以前每次都扫全部磁盘（一次 PowerShell
        冷启动 + 全部分区/卷），现在只查目标盘；实测 3 块盘约 0.99 s → 0.85 s，
        主要成本仍是 PowerShell 解释器启动。

        扫描实现是 ``app.modules.disk_info.service.scan_disk_summaries``；
        它不可用时直接报错（没有“预装之外还能跑”的降级脚本）。
        """
        wanted = _wanted_numbers(disk_numbers)
        if self._disk_scanner is not None:
            items = [dict(item) for item in self._disk_scanner()]
        else:
            items = [dict(item) for item in disk_info_service.scan_disk_summaries(wanted)]
        if wanted is None:
            return items
        return [
            item
            for item in items
            if to_int(item.get("disk_number")) in wanted
        ]

    def scan_snapshot(self) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """一次拿“磁盘摘要 + 已用盘符”（会话开始/刷新用）。

        返回 ``(summaries, used_letters)``。注入 ``disk_scanner`` 的测试
        替身没有合并脚本，此时盘符交回给 ``build_selection_plan`` 自己扫。
        """
        if self._disk_scanner is not None:
            return ([dict(item) for item in self._disk_scanner()], ())
        data = disk_info_service.scan_snapshot()
        disks = [dict(item) for item in data.get("disks") or ()]
        letters = tuple(str(item).strip().upper() for item in data.get("used_letters") or () if str(item).strip())
        return (disks, letters)

    def scan_protected_disks(self) -> list[DiskInfo]:
        """扫描并标记保护状态；被保护的盘 ``is_selectable=False``。"""
        return self._protect_summaries(self.scan_disks())

    def scan_protected_snapshot(self) -> tuple[list[DiskInfo], tuple[str, ...]]:
        """一次扫描同时返回“带保护标记的盘”和“已用盘符”。

        会话开始/刷新不再先扫盘、再由 ``build_selection_plan`` 扫一遍盘符，
        首屏少一次 PowerShell 解释器冷启动（实测约 1 秒）。
        """
        summaries, letters = self.scan_snapshot()
        return (self._protect_summaries(summaries), letters)

    def _protect_summaries(
        self, summaries: Sequence[Mapping[str, Any]]
    ) -> list[DiskInfo]:
        system_letter = _system_drive_letter()

        disks: list[DiskInfo] = []
        for summary in summaries:
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
        known_letters: Sequence[str] | None = None,
    ) -> SelectionPlan:
        """校验选中硬盘并分配盘符，返回 ``SelectionPlan``。

        ``resource_resolver`` 若提供，会被当作“这块盘至少需要多少字节”的
        计算函数；容量不足直接抛 ``ValueError``，不进入确认界面。
        ``extra_counts`` 是 ``{硬盘编号: 额外数据分区数}``（每盘 0 或 1），
        决定这块盘要分配 3 个还是 4 个盘符。
        ``known_letters`` 是会话快照里已有的系统盘符；给了就不再单独启动一次
        PowerShell 扫描（没给时保持旧行为，调用方无需改）。
        """
        _ = (log_dir, result_dir)  # 目前只透传，不落盘

        disk_numbers = [int(number) for number in selected]
        if not disk_numbers:
            raise ValueError("尚未选择任何硬盘")

        by_number: dict[int, Mapping[str, Any]] = {}
        for summary in summaries:
            number = to_int(summary.get("disk_number"))
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
        # 会话快照已经带了这份盘符时直接复用，不再开第二个 PowerShell。
        if known_letters is not None:
            for letter in known_letters:
                text = str(letter).strip().upper()
                if text:
                    used_letters.add(text)
        else:
            try:
                for letter in disk_info_service.scan_used_drive_letters():
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
                size_bytes = to_int(by_number[number].get("size_bytes")) or 0
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
        """盘符分配只有一份实现（``app.drive_letters``），这里不复制规则。"""
        from app.drive_letters import build_drive_letter_allocations

        return build_drive_letter_allocations(
            list(disk_numbers), sorted(forbidden), dict(extra_counts or {})
        )

    # ------------------------------------------------------------------ 执行
    def execute_spec(
        self,
        spec: Any,
        cancel_event: threading.Event | None = None,
        on_step: StepReporter | None = None,
    ) -> WorkerResult:
        """执行一个 ``JobSpec``（真正写盘）。

        执行前必须通过三道写盘判定（白名单 / 定向复核身份 / 保护规则），
        并且必须注入 ``real_runner``；任何一条不满足都抛 ``PermissionError``——
        失败信息会直接显示在任务状态里。

        ``on_step(key, status, message)`` 是可选的进度回调：真实执行时由
        ``WindowsDiskRunner`` 在每一步开始/结束时调用，界面据此画进度条；
        不传就完全不感知进度（CLI 场景）。
        """
        if spec is None:
            raise ValueError("缺少任务输入 JobSpec")

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

        判定链与 ``_require_write_allowed`` 完全一致：白名单 →
        **重新扫描后复核身份** → 保护规则。返回最新的硬盘摘要，
        调用方据此再决定具体命令，避免“界面上看到的盘”和“实际要动的盘”不是同一块。
        """
        return self._gate_target(
            int(disk_number),
            dict(expected_identity or {}),
        )

    def _require_write_allowed(self, spec: Any) -> None:
        """真实写盘的三道判定；任何一道不通都抛 ``PermissionError``。

        第 3 道是“目标盘现在仍然可选”：系统盘/启动盘/含系统盘符/离线/只读
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
        """三道判定的实现；返回重新扫描到的硬盘摘要。"""
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
        """按盘号定向复核（只查这一块盘，不再全盘扫描）。"""
        number = int(disk_number)
        for summary in self.scan_disks([number]):
            if to_int(summary.get("disk_number")) == number:
                return summary
        return None
