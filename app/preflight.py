"""运行前检查（preflight）。

在动任何硬盘之前先回答四个问题：有没有管理员权限、PowerShell 能不能用、
配置文件在不在、配置文件能不能解析。任何一项不通过都会变成
``WorkflowController.start()`` 里的阻断项，界面据此禁用“开始”。

返回值是给 ``workflow._build_check_items`` 消费的普通 dict 列表
（``{"name", "passed", "message"}``），而不是 ``CheckItem``：检查层不需要
知道界面契约，``app.application.contracts`` 也不必反向依赖本模块。

两条设计约定：

- **只读**：本模块不执行任何写操作，最坏情况是启动一次 ``powershell`` 查询版本；
- **不猜配置语义**：“参数够不够用”由 ``prepare()`` 用实际分区大小判断，
  这里只保证“读得出来、结构说得通”，避免把可用配置误判成不可用。

P0 说明：``config_path`` 为空时按 ``--config`` 的历史默认值找
``<仓库根>/json/win11.json``；也支持用环境变量 ``SISP_CONFIG`` 指定。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import resources
from app.modules.image.service import IMAGE_SUFFIXES
from app.resources import app_root

REPO_ROOT = app_root()
DEFAULT_CONFIG_RELATIVE = resources.DEFAULT_CONFIG_RELATIVE
CONFIG_ENV_VAR = "SISP_CONFIG"
# 临时目录/日志目录至少要有这么多余量，低于它就先别开工。
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024

# 检查项名称必须和 workflow._CHECK_KEYS 对齐，否则界面拿不到稳定 key。
CHECK_ADMIN = "管理员权限检查"
CHECK_POWERSHELL = "PowerShell 可用性检查"
CHECK_CONFIG_EXISTS = "配置文件存在性检查"
CHECK_CONFIG_PARSE = "配置文件可解析性检查"
CHECK_IMAGE = "镜像文件存在性检查"
CHECK_IMAGE_TYPE = "镜像可应用性检查"
CHECK_FREE_SPACE = "临时目录可用空间检查"
CHECK_TOOLS = "外部工具可用性检查"
CHECK_SETTING_PATHS = "设置路径有效性检查"

# 界面设置里的可选外部工具：配了就必须存在，没配就跳过（内置 dism/bcdboot 够用）。
OPTIONAL_TOOLS: tuple[tuple[str, str], ...] = (
    ("extWimlib", "wimlib-imagex"),
    ("extVentoy", "Ventoy"),
    ("ext7z", "7-Zip"),
)

# 界面设置里的系统文件路径：(键名, 中文名, 期望类型)
SETTING_PATHS: tuple[tuple[str, str, str], ...] = (
    ("sysIsoDir", "ISO 目录", "dir"),
    ("sysWim", "系统 WIM 文件", "file"),
    ("sysBoot", "引导文件目录", "dir"),
    ("sysUnattend", "无人值守应答文件", "file"),
    ("sysDrivers", "驱动目录", "dir"),
    ("sysTemp", "临时目录", "dir"),
)

# 配置文件不存在/不可读；JSON 语法错误；结构不合法。
ERROR_MISSING = 2
ERROR_PARSE = 3
ERROR_STRUCTURE = 4

_POWERSHELL_NAMES = ("powershell", "pwsh", "powershell.exe", "pwsh.exe")


@dataclass(frozen=True)
class ConfigLoad:
    """一次配置文件加载的中间结果，preflight 与 ``SispService.check_config`` 共用。"""

    path: Path | None
    exists: bool
    payload: dict[str, Any] | None
    error_code: int
    message: str

    @property
    def passed(self) -> bool:
        return self.payload is not None and self.error_code == 0


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_number(value: Any) -> float | None:
    """int/float/数字字符串 → float；bool、空串、非数字 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def resolve_config_path(config_path: str | os.PathLike[str] | None = None) -> Path | None:
    """确定这次要检查的配置文件。

    优先级：显式参数 → ``SISP_CONFIG`` → ``<仓库根>/json/win11.json``。
    显式传入的相对路径按当前工作目录解析，方便命令行 ``-j ../x.json``。
    """
    candidate = config_path if config_path not in (None, "") else os.environ.get(CONFIG_ENV_VAR)
    if candidate in (None, ""):
        # 冻结版优先用 exe 目录里的配置（用户可改），没有再回退随包模板。
        return resources.ensure_default_config()
    return Path(str(candidate)).expanduser()


def validate_config_payload(payload: Mapping[str, Any]) -> list[str]:
    """检查配置的“结构”是否说得通，返回问题列表（空列表 = 通过）。

    只校验 workflow 真正会读的键，其余键原样透传：
    ``partition_info``（efi_size_mb / c_size_gb / data_size_gb）、
    ``image_info.image_path``、``excluded_disk_names``。
    """
    problems: list[str] = []

    partition_info = payload.get("partition_info")
    if partition_info is not None and not isinstance(partition_info, Mapping):
        problems.append("partition_info 必须是对象")
    elif isinstance(partition_info, Mapping):
        for key in ("efi_size_mb", "c_size_gb"):
            value = partition_info.get(key)
            if value is None:
                continue
            number = _as_number(value)
            if number is None or number <= 0:
                problems.append(f"partition_info.{key} 必须是正数")
        data_size = partition_info.get("data_size_gb")
        if data_size not in (None, "", "auto"):
            number = _as_number(data_size)
            if number is None or number < 0:
                problems.append("partition_info.data_size_gb 必须是正数或 auto")

    image_info = payload.get("image_info")
    if image_info is not None and not isinstance(image_info, Mapping):
        problems.append("image_info 必须是对象")
    elif isinstance(image_info, Mapping):
        image_path = image_info.get("image_path")
        if image_path is not None and not isinstance(image_path, str):
            problems.append("image_info.image_path 必须是字符串")

    excluded = payload.get("excluded_disk_names")
    if excluded is not None and not isinstance(excluded, (list, tuple)):
        problems.append("excluded_disk_names 必须是数组")

    return problems


def load_config(config_path: str | os.PathLike[str] | None = None) -> ConfigLoad:
    """读取并校验配置文件。

    解析失败绝不抛异常：返回带 ``error_code`` 的 ``ConfigLoad``，
    让 GUI 能把“文件不存在”和“JSON 写坏了”区分开。
    """
    path = resolve_config_path(config_path)
    if path is None:
        return ConfigLoad(None, False, None, ERROR_MISSING, "未指定配置文件")

    if not path.is_file():
        return ConfigLoad(path, False, None, ERROR_MISSING, f"配置文件不存在: {path}")

    try:
        # utf-8-sig：容忍记事本/VS Code 保存时带的 BOM。
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return ConfigLoad(path, True, None, ERROR_MISSING, f"配置文件无法读取: {exc}")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return ConfigLoad(
            path,
            True,
            None,
            ERROR_PARSE,
            f"配置文件不是合法 JSON: 第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}",
        )

    if not isinstance(raw, dict):
        return ConfigLoad(path, True, None, ERROR_STRUCTURE, "配置文件顶层必须是 JSON 对象")

    problems = validate_config_payload(raw)
    if problems:
        return ConfigLoad(path, True, None, ERROR_STRUCTURE, "配置文件结构不合法: " + "；".join(problems))

    payload = dict(raw)
    # workflow.start() 会在配置里没有 config_path 时用它兜底，这里补齐。
    payload.setdefault("config_path", str(path))
    return ConfigLoad(path, True, payload, 0, "")


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
    config_path: str | os.PathLike[str] | None = None,
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

    # --------------------------------------------------------------- 配置
    # 业务配置是**可选预设**：分区大小、镜像、排除名单都能在界面设置里维护。
    # 因此“文件不存在”只作为提示（optional），不阻断；而“文件存在但坏了”
    # 仍然阻断——那是用户主动写的内容，静默忽略比报错更危险。
    loaded = load_config(config_path)
    missing = loaded.error_code == ERROR_MISSING
    if loaded.path is None:
        add(CHECK_CONFIG_EXISTS, False, "未指定配置文件", optional=True)
    elif loaded.exists:
        add(CHECK_CONFIG_EXISTS, True, f"配置文件存在: {loaded.path}")
    elif missing:
        add(
            CHECK_CONFIG_EXISTS,
            False,
            f"{loaded.message}（可选，未找到就全部使用界面设置）",
            optional=True,
        )
    else:
        add(CHECK_CONFIG_EXISTS, False, loaded.message)
    add(
        CHECK_CONFIG_PARSE,
        loaded.passed or missing,
        "配置可解析" if loaded.passed else (f"{loaded.message}（可选）" if missing else loaded.message),
        optional=missing,
    )

    payload = loaded.payload
    if payload is None and missing:
        payload = {}

    # --------------------------------------------------------------- 镜像
    # 与运行时保持同一套优先级：无 -j 时界面设置的 sysWim 覆盖配置里的
    # image_path（否则模板占位路径会让“填了真镜像”的用户一直看到阻断）；
    # 显式 -j 时配置优先，设置只作兜底。
    config_image = None
    if payload is not None:
        image_info = payload.get("image_info")
        if isinstance(image_info, Mapping):
            config_image = _text(image_info.get("image_path"))
    settings_image = _text((settings or {}).get("sysWim"))
    explicit_config = bool(str(config_path or "").strip() or os.environ.get(CONFIG_ENV_VAR))
    image_path = (
        (config_image or settings_image) if explicit_config else (settings_image or config_image)
    )
    if image_path is not None:
        target = Path(image_path).expanduser()
        if target.is_file():
            add(CHECK_IMAGE, True, f"镜像文件存在: {target}")
            # 类型检查：dism 只能 ``/Apply-Image`` 到分区，ISO 必须先把 install.wim 取出来。
            suffix = target.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                add(CHECK_IMAGE_TYPE, True, f"可直接应用到分区：{suffix}")
            elif suffix == ".iso":
                add(
                    CHECK_IMAGE_TYPE,
                    False,
                    f"{target.name} 是 ISO：dism 不能直接应用，请先取出 sources\\install.wim",
                )
            else:
                add(
                    CHECK_IMAGE_TYPE,
                    False,
                    f"不支持 {suffix or '无后缀'} 文件：装机只接受 WIM/ESD/SWM",
                )
        else:
            add(CHECK_IMAGE, False, f"镜像文件不存在: {target}")

    # ----------------------------------------------------------- 剩余空间
    # 配置都没读出来时不额外加噪声；空间检查本身与配置无关，
    # 但只要开始准备镜像就需要它，所以有配置才检查。
    if payload is not None:
        ok_space, space_message = _check_free_space(disk_usage)
        add(CHECK_FREE_SPACE, ok_space, space_message)

    # --------------------------------------------------- 界面设置里的工具/路径
    if settings:
        _add_tool_checks(add, settings)
        _add_setting_path_checks(add, settings)

    return {
        # 可选检查（例如没放业务配置文件）失败不算“没通过”
        "all_passed": all(item["passed"] for item in results if not item.get("optional")),
        "dry_run": bool(dry_run),
        "results": results,
        "config_path": str(loaded.path) if loaded.path is not None else None,
        "config_payload": payload,
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
        add(CHECK_TOOLS, True, "未配置外部工具，使用内置 dism/bcdboot")


def _add_setting_path_checks(
    add: Callable[[str, bool, str], None],
    settings: Mapping[str, Any],
) -> None:
    """系统文件路径（ISO/WIM/引导/应答/驱动/临时目录）。

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
