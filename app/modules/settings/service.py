"""界面设置抽屉对应的独立设置模块。

目标：所有可调参数都收敛到这里（业务 JSON 配置文件那一层已移除）。

- 业务配置：装系统要用的镜像、GHO、软件目录，由运维/高手维护，随配置文件走；
- 界面设置：语言、主题、默认分区大小、外部程序路径、临时目录、安全开关，
  由普通用户在自己的机器上改，不应该污染业务配置。

模块只做四件事：加载、校验、持久化、探测路径是否有效。
不 import PowerShell、Qt 或 pywebview，可以单独用单元测试覆盖。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SETTINGS_VERSION = 1

# 设置分节：界面按这个顺序渲染分组
SECTIONS: tuple[tuple[str, str], ...] = (
    ("general", "常规"),
    ("install", "安装默认值"),
    ("external", "外部程序"),
    ("system_files", "系统文件"),
    ("safety", "安全与提醒"),
    ("logging", "日志与诊断"),
)

DEFAULTS: dict[str, Any] = {
    # 常规
    "language": "zh-CN",
    "theme": "light",
    "density": "comfortable",
    "rememberWindow": True,
    "autoRefresh": True,
    "startView": "main",
    # 安装默认值
    "confirmBeforeStart": True,
    "typeToConfirm": True,
    "sysDefaultGb": 120,
    "efiDefaultMb": 100,
    "dataDefaultGb": "auto",
    # GHO 内部的分区块序号（0 = 用 Ghost 默认的 1）
    "imageDefaultIndex": 0,
    # 外部程序（空 = 用系统自带或 PATH 里的）
    "extEnabled": True,
    "extConfirm": False,
    "extDiskpart": "",
    # 装机只走 Ghost：留空则依次找 <exe 目录>\sw\ghost64.exe 与 PATH 里的 ghost64/ghost32。
    "extGhost": "",
    "extVentoy": "",
    "ext7z": "",
    # 系统文件：两个具名系统槽位（各存一个 GHO），外加引导/临时目录
    "sysImageA": "",
    "sysImageALabel": "Windows 10",
    "sysImageB": "",
    "sysImageBLabel": "Windows 11",
    "sysBoot": "",
    "sysTemp": "",
    # 安全
    "protectSystemDisk": True,
    "dryRunByDefault": False,
    "blockUnverified": True,
    # 任务结束后是否释放 EFI 分区的临时盘符（关掉时盘符会一直留着）
    "releaseEfiLetter": True,
    "excludedDiskNames": [],
    "logRetentionDays": 30,
    # 日志
    "logDir": "",
    "logLevel": "info",
}

# 哪些键代表“文件/目录路径”，probe 时按文件或目录检查
_PATH_KEYS: dict[str, str] = {
    "extDiskpart": "file",
    "extGhost": "file",
    "extVentoy": "file",
    "ext7z": "file",
    "sysImageA": "file",
    "sysImageB": "file",
    "sysBoot": "dir",
    "sysTemp": "dir",
    "logDir": "dir",
}

# 纯文本设置（系统槽位的显示名）：单条最长字符数
_TEXT_LIMITS: dict[str, int] = {
    "sysImageALabel": 32,
    "sysImageBLabel": 32,
}

# 空值时可以尝试从系统里自动发现的命令
_BUILTIN_COMMANDS: dict[str, tuple[str, ...]] = {
    "extDiskpart": ("diskpart",),
    "extGhost": ("ghost64", "ghost64.exe", "ghost32", "ghost32.exe"),
    "extVentoy": ("Ventoy2Disk", "Ventoy2Disk.exe"),
    "ext7z": ("7z", "7z.exe"),
}

_BOOL_KEYS = {
    "rememberWindow",
    "autoRefresh",
    "confirmBeforeStart",
    "typeToConfirm",
    "protectSystemDisk",
    "dryRunByDefault",
    "blockUnverified",
    "releaseEfiLetter",
    "extEnabled",
    "extConfirm",
}

_ENUM_KEYS: dict[str, tuple[str, ...]] = {
    "language": ("zh-CN", "en-US"),
    "theme": ("light", "dark", "system"),
    "density": ("compact", "comfortable"),
    "logLevel": ("debug", "info", "warn", "error"),
    "startView": ("main", "install", "settings"),
}

_INT_RANGES: dict[str, tuple[int, int]] = {
    "sysDefaultGb": (60, 2000),
    "efiDefaultMb": (64, 1024),
    "imageDefaultIndex": (0, 99),
    "logRetentionDays": (1, 3650),
}

# 列表型设置：每项是一个字符串，用于“怎么输都能收敛”的清洗
_LIST_LIMITS: dict[str, tuple[int, int]] = {
    # key: (最多多少条, 单条最长字符)
    "excludedDiskNames": (50, 120),
}


class SettingsError(ValueError):
    """某个设置项不合法时抛出；``field`` 指向具体键。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


@dataclass(frozen=True)
class ProbeResult:
    """路径探测结果，直接对应设置页右侧那一行状态文字。"""

    key: str
    state: str  # ok / miss / na / builtin
    path: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "state": self.state, "path": self.path, "message": self.message}


def default_settings_path() -> Path:
    """``%APPDATA%\\Sisp\\settings.json``；没有 APPDATA 时退回用户目录。"""
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / ".config")
    return Path(base) / "Sisp" / "settings.json"


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value)).strip()


def _same_path(left: str, right: str) -> bool:
    """两个路径是否指向同一个位置（Windows 盘符/大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(_expand(left))) == os.path.normcase(
        os.path.normpath(_expand(right))
    )


def coerce_value(key: str, raw: Any) -> Any:
    """把界面传来的任意值收敛成合法值；不合法时抛 ``SettingsError``。"""
    if key in _BOOL_KEYS:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
        raise SettingsError(key, "需要布尔值")

    if key in _ENUM_KEYS:
        allowed = _ENUM_KEYS[key]
        text = str(raw or "").strip()
        if text not in allowed:
            raise SettingsError(key, f"只接受 {' / '.join(allowed)}")
        return text

    if key in _INT_RANGES:
        low, high = _INT_RANGES[key]
        try:
            number = int(raw)
        except (TypeError, ValueError) as exc:
            raise SettingsError(key, "需要整数") from exc
        if not (low <= number <= high):
            raise SettingsError(key, f"需要在 {low}~{high} 之间")
        return number

    if key == "dataDefaultGb":
        if isinstance(raw, str) and raw.strip().lower() in ("auto", ""):
            return "auto"
        try:
            number = int(float(raw))
        except (TypeError, ValueError) as exc:
            raise SettingsError(key, "需要 auto 或整数 GB") from exc
        if not (20 <= number <= 100_000):
            raise SettingsError(key, "需要在 20~100000 GB 之间，或填 auto")
        return number

    if key in _PATH_KEYS:
        if raw is None:
            return ""
        return _expand(str(raw))

    if key in _TEXT_LIMITS:
        text = " ".join(str(raw or "").split())
        limit = _TEXT_LIMITS[key]
        if len(text) > limit:
            raise SettingsError(key, f"不能超过 {limit} 个字符")
        return text

    if key in _LIST_LIMITS:
        return _coerce_name_list(key, raw)

    raise SettingsError(key, "未知设置项")


def _coerce_name_list(key: str, raw: Any) -> list[str]:
    """把“列表或整段文本”收敛成干净的字符串列表。

    界面上的控件是一个多行输入框（一行一条），API 调用方也可能直接给数组，
    所以两种形态都要接：字符串按换行切分，数组逐项转字符串。
    规则：丢空项、按不区分大小写去重（保留首次出现的原始写法）、限制条数与单条长度。
    """
    limit, item_max = _LIST_LIMITS[key]
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise SettingsError(key, "需要字符串列表")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        if len(text) > item_max:
            raise SettingsError(key, f"单条不能超过 {item_max} 个字符")
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        cleaned.append(text)
    if len(cleaned) > limit:
        raise SettingsError(key, f"最多 {limit} 条")
    return cleaned


class SettingsStore:
    """读写 + 校验 + 路径探测。所有方法都可注入 ``path`` 便于测试。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        defaults: dict[str, Any] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.path = Path(path) if path else default_settings_path()
        self._defaults = dict(DEFAULTS if defaults is None else defaults)
        self._which = which or shutil.which
        self._warnings: list[str] = []
        self._data: dict[str, Any] = dict(self._defaults)
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> dict[str, Any]:
        """读取磁盘；坏值逐项丢弃并记录警告，不让界面因为一个字段打不开。"""
        self._warnings = []
        data = dict(self._defaults)
        if self.path.exists():
            try:
                # utf-8-sig：兼容记事本 / Windows PowerShell 5.1 保存的 BOM。
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                self._warnings.append(f"设置文件无法解析，已使用默认值：{exc}")
                raw = {}
            if isinstance(raw, dict):
                # save() 写的是 {"version": 1, "settings": {...}} 封套，
                # 读的时候必须拆开 settings 那一层，否则顶层只有 version/settings 两个键，
                # 全被下面的 "key not in defaults" 跳过，等于永远读默认值。
                # 顺带兼容直接把设置键放在顶层的旧文件。
                stored = raw.get("settings")
                if not isinstance(stored, dict):
                    stored = raw
                for key, value in stored.items():
                    if key not in self._defaults:
                        continue
                    try:
                        data[key] = coerce_value(key, value)
                    except SettingsError as exc:
                        self._warnings.append(f"已忽略无效设置 {exc}")
        self._data = data
        return dict(self._data)

    def get(self) -> dict[str, Any]:
        return dict(self._data)

    def get_warnings(self) -> list[str]:
        return list(self._warnings)

    def update(self, patch: dict[str, Any] | None) -> dict[str, Any]:
        """合并、整体校验、落盘；任何一项不合法都不写入。"""
        if not isinstance(patch, dict):
            raise SettingsError("patch", "需要对象")

        merged = dict(self._data)
        for key, value in patch.items():
            if key not in self._defaults:
                raise SettingsError(str(key), "未知设置项")
            merged[key] = coerce_value(key, value)

        self._validate_relations(merged)
        self._data = merged
        self.save()
        return dict(self._data)

    def reset(self) -> dict[str, Any]:
        self._data = dict(self._defaults)
        self.save()
        return dict(self._data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": SETTINGS_VERSION, "settings": self._data},
            ensure_ascii=False,
            indent=2,
        )
        handle, tmp_name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ 校验
    def _validate_relations(self, data: dict[str, Any]) -> None:
        sys_gb = int(data.get("sysDefaultGb") or 0)
        data_gb = data.get("dataDefaultGb")
        if isinstance(data_gb, int) and sys_gb + data_gb <= 60:
            raise SettingsError("dataDefaultGb", "C 与 D 的大小之和过小，至少留 60GB 给系统盘")

        temp_dir = str(data.get("sysTemp") or "").strip()
        log_dir = str(data.get("logDir") or "").strip()
        if temp_dir and log_dir and Path(temp_dir) == Path(log_dir):
            raise SettingsError("sysTemp", "临时目录不能与日志目录相同")

        # 两个系统槽位是并列的两个“版本”，重了就没得选：同一个 GHO 装两次没有意义，
        # 同名会让向导上两个按钮长得一模一样。
        image_a = str(data.get("sysImageA") or "").strip()
        image_b = str(data.get("sysImageB") or "").strip()
        if image_a and image_b and _same_path(image_a, image_b):
            raise SettingsError("sysImageB", "系统 B 与系统 A 指向同一个镜像文件")
        label_a = str(data.get("sysImageALabel") or "").strip()
        label_b = str(data.get("sysImageBLabel") or "").strip()
        if image_a and image_b and label_a and label_b and label_a.casefold() == label_b.casefold():
            raise SettingsError("sysImageBLabel", "两个系统的名称不能相同")

    # ------------------------------------------------------------------ 探测
    def probe(self, key: str, path: str | None = None) -> ProbeResult:
        """检查某个路径型设置的状态。

        ``path`` 不为 ``None`` 时检查传进来的值（界面上的草稿），否则检查已保存
        的设置值 —— 设置页改成「选路径只回填草稿、保存才落盘」之后，探测必须能
        跟着草稿走，否则角标描述的是磁盘上那个旧值。
        """
        if key not in _PATH_KEYS:
            return ProbeResult(key, "na", "", "该项不是路径")

        raw = self._data.get(key) if path is None else path
        value = str(raw or "").strip()
        if not value:
            builtin = self._find_builtin(key)
            if builtin:
                return ProbeResult(key, "builtin", builtin, "使用系统自带")
            return ProbeResult(key, "na", "", "未设置")

        resolved = _expand(value)
        target = Path(resolved)
        kind = _PATH_KEYS[key]
        if kind == "file" and target.is_file():
            return ProbeResult(key, "ok", resolved)
        if kind == "dir" and target.is_dir():
            return ProbeResult(key, "ok", resolved)
        return ProbeResult(key, "miss", resolved, "路径不存在")

    def _find_builtin(self, key: str) -> str:
        for command in _BUILTIN_COMMANDS.get(key, ()):
            found = self._which(command)
            if found:
                return str(found)
        return ""

    def probe_all(self) -> dict[str, dict[str, Any]]:
        return {key: self.probe(key).to_dict() for key in _PATH_KEYS}

    # ------------------------------------------------------------------ 业务桥接
    def partition_info(self) -> dict[str, Any]:
        """把界面设置转成后端认的 ``partition_info``。"""
        sys_gb = int(self._data.get("sysDefaultGb") or 120)
        data_default = self._data.get("dataDefaultGb")
        data_size = None if data_default == "auto" else int(data_default)
        efi_mb = int(self._data.get("efiDefaultMb") or 100)
        return {
            "efi_size_mb": efi_mb,
            "c_size_gb": sys_gb,
            "data_size_gb": data_size,
            # 数据分区的文件系统不再可配：恒为 NTFS（见 partitions.DATA_FS_CHOICES）。
            # 这里显式写出来，让下游拿到的载荷形状不随“设置里有没有这一项”变化。
            "data_fs": "NTFS",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": dict(self._data),
            "warnings": list(self._warnings),
            "sections": [{"key": key, "label": label} for key, label in SECTIONS],
            "pathKeys": sorted(_PATH_KEYS),
        }


def build_store(path: str | Path | None = None) -> SettingsStore:
    """给 gui_web 用的默认构造入口（可被测试替换）。"""
    return SettingsStore(path)
