"""CLI/GUI 共用的应用服务。

第一阶段只抽取扫描、配置检查、保护规则和选择后的写前校验，不改变现有
执行流程。CLI 仍然负责提示输入，真正的单盘/多盘执行仍由 orchestrator
完成。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.application.contracts import (
    ConfigCheckResult,
    DiskInfo,
    DiskListResult,
    SelectionPlan,
)
from app.application.planning import validate_required_paths, validate_target_resources
from app.drive_letters import build_drive_letter_allocations, build_forbidden_drive_letters
from app.modules.common.constants import ErrorCode
from app.modules.common.service import SispError
from app.modules.config_loader.service import load_config
from app.modules.disk_info.service import scan_disk_summaries, scan_used_drive_letters
from app.modules.user_interaction.service import (
    apply_disk_protection,
    validate_selected_disks_are_selectable,
)

ConfigLoader = Callable[[str | Path | None], dict[str, Any]]
DiskScanner = Callable[[], list[dict[str, Any]]]
UsedLetterScanner = Callable[[], list[str]]
ProtectionApplier = Callable[[list[dict[str, Any]], list[str] | None], list[dict[str, Any]]]


class SispService:
    """面向 CLI/GUI 的稳定入口。

    依赖通过构造函数注入，既方便测试，也允许 CLI 继续沿用模块级
    monkeypatch 的调用方式。
    """

    def __init__(
        self,
        *,
        config_loader: ConfigLoader = load_config,
        disk_scanner: DiskScanner = scan_disk_summaries,
        used_letter_scanner: UsedLetterScanner = scan_used_drive_letters,
        protection_applier: ProtectionApplier = apply_disk_protection,
    ) -> None:
        self._config_loader = config_loader
        self._disk_scanner = disk_scanner
        self._used_letter_scanner = used_letter_scanner
        self._protection_applier = protection_applier

    def load_config(self, config_path: str | Path | None = None) -> dict[str, Any]:
        """读取并解析配置。"""
        return self._config_loader(config_path)

    def scan_disks(self) -> list[dict[str, Any]]:
        """扫描原始硬盘摘要。"""
        return self._disk_scanner()

    def protect_disks(
        self,
        disks: Sequence[dict[str, Any]],
        excluded_disk_names: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """应用系统盘、启动盘、配置排除等保护规则。"""
        return self._protection_applier(list(disks), list(excluded_disk_names or []))

    def scan_protected_disks(
        self,
        excluded_disk_names: Sequence[str] | None = None,
    ) -> list[DiskInfo]:
        """扫描硬盘并应用保护规则，返回适合界面展示的 DTO。"""
        disks = self.protect_disks(self.scan_disks(), excluded_disk_names)
        return [DiskInfo.from_summary(disk) for disk in disks]

    def list_disks(
        self,
        config_path: str | Path | None = None,
        *,
        ignore_config_errors: bool = False,
    ) -> DiskListResult:
        """按配置读取排除盘型号，然后扫描并包装硬盘列表。"""
        excluded_disk_names: list[str] = []
        warning: str | None = None
        try:
            payload = self._config_loader(config_path)
            excluded_disk_names = list(payload.get("excluded_disk_names") or [])
        except Exception as exc:
            if not ignore_config_errors:
                raise
            warning = f"警告: 配置读取失败，将使用空排除列表: {exc}"

        disks = self.scan_protected_disks(excluded_disk_names)
        return DiskListResult(disks=tuple(disks), warning=warning)

    def check_config(self, config_path: str | Path | None = None) -> ConfigCheckResult:
        """检查配置及关键路径，返回结构化结果而不是直接打印。"""
        try:
            payload = self._config_loader(config_path)
            validate_required_paths(payload)
        except SispError as exc:
            return ConfigCheckResult(
                passed=False,
                error_code=exc.error_code,
                message=str(exc),
            )
        except (OSError, ValueError) as exc:
            return ConfigCheckResult(
                passed=False,
                error_code=ErrorCode.CONFIG_ERROR,
                message=str(exc),
            )
        except Exception as exc:
            return ConfigCheckResult(
                passed=False,
                error_code=ErrorCode.UNEXPECTED_ERROR,
                message=str(exc),
            )

        return ConfigCheckResult(passed=True, payload=payload)

    def build_selection_plan(
        self,
        config_payload: dict[str, Any],
        disk_summaries: Sequence[dict[str, Any]],
        selected_disk_numbers: Sequence[int],
        *,
        config_path: str | Path | None = None,
        log_dir: str | Path | None = None,
        result_dir: str | Path | None = None,
        used_letters: Sequence[str] | None = None,
        resource_resolver: Callable[[str | Path], int | None] | None = None,
    ) -> SelectionPlan:
        """执行选择后的全局写前检查，并分配本轮盘符。

        这里保持与旧主流程完全相同的检查顺序：
        校验选择 → 校验关键路径 → 校验资源所在磁盘 → 扫描已用盘符 →
        生成禁用盘符 → 分配每盘 4 个盘符。
        """
        selected = [int(number) for number in selected_disk_numbers]
        if not selected:
            return SelectionPlan(
                selected_disk_numbers=(),
                forbidden_drive_letters=(),
                drive_letter_allocations={},
            )

        summaries = list(disk_summaries)
        validate_selected_disks_are_selectable(selected, summaries)
        validate_required_paths(config_payload)
        validate_target_resources(
            config_payload,
            selected,
            config_path=config_path,
            log_dir=log_dir,
            result_dir=result_dir,
            resolver=resource_resolver,
        )

        letters = (
            list(used_letters)
            if used_letters is not None
            else list(self._used_letter_scanner())
        )
        forbidden = build_forbidden_drive_letters(summaries, selected, letters)
        allocations = build_drive_letter_allocations(
            selected,
            forbidden_letters=forbidden,
        )
        return SelectionPlan(
            selected_disk_numbers=tuple(selected),
            forbidden_drive_letters=tuple(forbidden),
            drive_letter_allocations=allocations,
        )
