"""运行前检查（preflight）。

在动任何硬盘之前先回答几个问题：有没有管理员权限、PowerShell 能不能用、
镜像在不在、接受不接受、临时目录放得下吗、外部工具和系统文件路径对不对。
任何一项不通过都会变成 ``WorkflowController.start()`` 里的阻断项，
界面据此禁用“开始”。

返回值是给 ``workflow._build_check_items`` 消费的普通 dict 列表
（``{"name", "passed", "message"}``），而不是 ``CheckItem``：检查层不需要
知道界面契约，``app.application.contracts`` 也不必反向依赖本模块。

两条设计约定：

- **只读**：本模块不执行任何写操作，最坏情况是启动一次 ``powershell`` 查询版本
  （``sysTemp`` 目录会被创建，见 ``_add_setting_path_checks``）；
- **不猜业务语义**：“参数够不够用”由 ``prepare()`` 用实际分区大小判断，
  这里只回答“有没有、能不能用”。

输入只有一处：界面设置（``settings``）。这一版之前还有“业务 JSON 配置文件”
这一层，优先级规则（显式 ``-j`` 就配置赢、否则设置赢）让同一台机器跑出过两种
结果，已整体移除——分区大小、镜像位置、排除的硬盘全部在设置页维护。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.modules.ghost.service import GHO_SUFFIXES
from app.modules.image.service import configured_versions

# 临时目录/日志目录至少要有这么多余量，低于它就先别开工。
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024

# 检查项名称必须和 workflow._CHECK_KEYS 对齐，否则界面拿不到稳定 key。
CHECK_ADMIN = "管理员权限检查"
CHECK_POWERSHELL = "PowerShell 可用性检查"
CHECK_IMAGE = "镜像文件存在性检查"
CHECK_IMAGE_TYPE = "镜像可应用性检查"
CHECK_FREE_SPACE = "临时目录可用空间检查"
CHECK_TOOLS = "外部工具可用性检查"
CHECK_SETTING_PATHS = "设置路径有效性检查"

# 界面设置里的可选外部工具：配了就必须存在，没配就跳过（bcdboot 是系统自带的）。
OPTIONAL_TOOLS: tuple[tuple[str, str], ...] = (
    ("extGhost", "ghost64"),
    ("extVentoy", "Ventoy"),
    ("ext7z", "7-Zip"),
)

# 界面设置里的系统文件路径：(键名, 中文名, 期望类型)
SETTING_PATHS: tuple[tuple[str, str, str], ...] = (
    ("sysImageA", "系统 A 镜像", "file"),
    ("sysImageB", "系统 B 镜像", "file"),
    ("sysBoot", "引导文件目录", "dir"),
    ("sysTemp", "临时目录", "dir"),
)

_POWERSHELL_NAMES = ("powershell", "pwsh", "powershell.exe", "pwsh.exe")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_admin() -> bool:
    """当前进程是否已提权；非 Windows 平台返回 False。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 权限查询失败一律按“没有权限”处理
        return False


def find_powershell() -> str | None:
    """找到可用的 PowerShell 可执行文件；找不到返回 None。"""
    for name in _POWERSHELL_NAMES:
        found = shutil.which(name)
        if found:
            return str(found)
    return None


def _system_drive_root() -> str:
    if os.name == "nt":
        drive = (os.environ.get("SYSTEMDRIVE") or "C:").rstrip("\\/") or "C:"
        return drive + "\\"
    return "/"


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def _check_free_space(
    disk_usage: Callable[[str], Any] | None = None,
) -> tuple[bool, str]:
    """检查系统盘（临时目录所在盘）的剩余空间。"""
    root = _system_drive_root()
    try:
        usage = (disk_usage or shutil.disk_usage)(root)
        free = int(usage[2]) if isinstance(usage, tuple) else int(getattr(usage, "free", 0))
    except Exception as exc:  # noqa: BLE001 - 查不到空间不应该阻断整个流程
        return True, f"无法读取 {root} 剩余空间，已跳过: {exc}"
    if free < MIN_FREE_BYTES:
        return False, f"{root} 剩余 {_format_bytes(free)}，低于建议的 {_format_bytes(MIN_FREE_BYTES)}"
    return True, f"{root} 剩余 {_format_bytes(free)}"


def run_preflight_checks(
    *,
    dry_run: bool = False,
    admin_checker: Callable[[], bool] | None = None,
    command_lookup: Callable[[str], str | None] | None = None,
    disk_usage: Callable[[str], Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """执行全部运行前检查，返回 ``{"all_passed", "results", ...}``。

    参数都是可注入的，方便单元测试在没有管理员权限、没有 PowerShell
    的机器上覆盖失败分支。

    ``dry_run=True`` 时把管理员权限检查降级为“不要求”：演练模式不碰硬盘，
    普通权限也应该能看计划。
    """
    results: list[dict[str, Any]] = []

    def add(name: str, passed: bool, message: str, *, optional: bool = False) -> None:
        results.append(
            {
                "name": name,
                "passed": bool(passed),
                "message": str(message),
                "optional": bool(optional),
            }
        )

    # ---------------------------------------------------------------- 权限
    checker = admin_checker or is_admin
    try:
        elevated = bool(checker())
    except Exception as exc:  # noqa: BLE001
        add(CHECK_ADMIN, False, f"无法判断当前权限: {exc}")
    else:
        if dry_run:
            add(CHECK_ADMIN, True, "dry-run 模式不要求管理员权限")
        elif elevated:
            add(CHECK_ADMIN, True, "已以管理员身份运行")
        else:
            add(
                CHECK_ADMIN,
                False,
                "需要管理员权限：请以管理员身份重新启动，否则 diskpart 无法清盘",
            )

    # ----------------------------------------------------------- PowerShell
    lookup = command_lookup or shutil.which
    shell_path = None
    for name in _POWERSHELL_NAMES:
        try:
            shell_path = lookup(name)
        except Exception:  # noqa: BLE001
            shell_path = None
        if shell_path:
            break
    if shell_path:
        add(CHECK_POWERSHELL, True, f"已找到 PowerShell: {shell_path}")
    else:
        add(CHECK_POWERSHELL, False, "未找到 powershell/pwsh，无法扫描硬盘或执行 diskpart")

    # --------------------------------------------------------------- 镜像
    # 镜像来源只有设置页的两个系统槽位：A / B 各存一个 GHO，向导第 3 步里
    # 每块盘二选一。一个槽位坏了不应该拦住另一个能装的版本，所以“存在/可应用”
    # 两项都是**至少一个**可用就算通过，消息里列清每个槽位的状态。
    versions = configured_versions(settings)
    if versions:
        present: list[str] = []
        missing: list[str] = []
        usable: list[str] = []
        unusable: list[str] = []
        for version in versions:
            label = version["label"]
            target = Path(version["path"])
            if not target.is_file():
                missing.append(f"{label} 镜像文件不存在: {target}")
                continue
            present.append(f"{label} 镜像文件存在: {target}")
            # 类型检查：装机只走 Ghost，只认 GHO/GHS 分区块镜像。
            suffix = target.suffix.lower()
            if suffix in GHO_SUFFIXES:
                usable.append(f"{label} Ghost 分区块镜像：{suffix}")
            elif suffix == ".iso":
                unusable.append(f"{label} 是 ISO：Ghost 只能还原 .GHO，请先封装成 GHO")
            else:
                unusable.append(
                    f"{label} 不支持 {suffix or '无后缀'} 文件：装机只接受 GHO/GHS"
                )
        add(
            CHECK_IMAGE,
            bool(present),
            "；".join(present + missing) or "设置里还没有配系统镜像",
        )
        add(
            CHECK_IMAGE_TYPE,
            bool(usable),
            "；".join(usable + unusable) or "没有可用的系统镜像",
        )

    # ----------------------------------------------------------- 剩余空间
    # 与业务配置无关：只要做镜像/读盘就需要临时空间，永远检查。
    ok_space, space_message = _check_free_space(disk_usage)
    add(CHECK_FREE_SPACE, ok_space, space_message)

    # --------------------------------------------------- 界面设置里的工具/路径
    if settings:
        _add_tool_checks(add, settings)
        _add_setting_path_checks(add, settings)

    return {
        # 可选检查（例如没配镜像、没配外部工具）失败不算“没通过”
        "all_passed": all(item["passed"] for item in results if not item.get("optional")),
        "dry_run": bool(dry_run),
        "results": results,
    }


def _add_tool_checks(add: Callable[[str, bool, str], None], settings: Mapping[str, Any]) -> None:
    """外部工具：配了就必须存在，没配就明确说“跳过”。"""
    configured: list[str] = []
    missing: list[str] = []
    for key, label in OPTIONAL_TOOLS:
        value = _text(settings.get(key))
        if not value:
            continue
        target = Path(value).expanduser()
        if target.exists():
            configured.append(f"{label}={target}")
        else:
            missing.append(f"{label}={target}")
    if missing:
        add(CHECK_TOOLS, False, "配置的外部工具不存在：" + "；".join(missing))
    elif configured:
        add(CHECK_TOOLS, True, "外部工具已就绪：" + "；".join(configured))
    else:
        add(CHECK_TOOLS, True, "未配置外部工具，使用系统自带的 bcdboot/diskpart")


def _add_setting_path_checks(
    add: Callable[[str, bool, str], None],
    settings: Mapping[str, Any],
) -> None:
    """系统文件路径（镜像目录/默认 GHO/引导/临时目录）。

    ``sysTemp`` 不存在是正常的（执行时会自动创建），其余路径配错了必须报错，
    否则真实写盘会在半路失败。
    """
    problems: list[str] = []
    present: list[str] = []
    for key, label, kind in SETTING_PATHS:
        value = _text(settings.get(key))
        if not value:
            continue
        target = Path(value).expanduser()
        if key == "sysTemp":
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problems.append(f"{label}={target} 不可用（{exc}）")
            else:
                present.append(f"{label}={target}")
            continue
        exists = target.is_file() if kind == "file" else target.is_dir()
        kind_label = "文件" if kind == "file" else "目录"
        if exists:
            present.append(f"{label}={target}")
        elif target.exists():
            problems.append(f"{label}={target} 类型不对（需要{kind_label}）")
        else:
            problems.append(f"{label}={target} 不存在")
    if problems:
        add(CHECK_SETTING_PATHS, False, "；".join(problems))
    elif present:
        add(CHECK_SETTING_PATHS, True, "已配置：" + "；".join(present))
    else:
        add(CHECK_SETTING_PATHS, True, "未配置系统文件路径，使用默认值")


def summarize_failures(report: Mapping[str, Any]) -> list[str]:
    """把报告里未通过的检查拼成阻断项文案，方便 CLI 直接打印。"""
    failures: list[str] = []
    raw_results = report.get("results") if isinstance(report, Mapping) else None
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, str):
        return failures
    for item in raw_results:
        if not isinstance(item, Mapping) or item.get("passed"):
            continue
        name = _text(item.get("name")) or "检查"
        message = _text(item.get("message")) or ""
        failures.append(f"{name}：{message}" if message else name)
    return failures


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    report = run_preflight_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"all_passed={report['all_passed']} platform={sys.platform}")
