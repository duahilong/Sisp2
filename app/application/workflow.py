"""GUI/CLI 共用的工作流适配层。

界面只负责“展示 + 用户操作”，不直接拼装配置、扫描、盘符分配和任务。
``WorkflowController`` 把一次完整操作串成：

1. ``start()``：运行前检查、读取配置、扫描并应用硬盘保护规则；
2. ``refresh_disks()``：重新扫描硬盘，不改变配置和检查结果；
3. ``check_config()``：只重新校验配置文件，不重建会话；
4. ``prepare()``：执行写前校验，为每块盘生成 ``ExecutionPlan`` 和
   ``JobSpec``，并标明检查项与阻断项；
5. ``submit()`` / ``confirm()`` / ``cancel()``：交给 ``JobManager`` 执行；
6. ``results()``：汇总已结束任务的结果。

本模块不依赖 Qt，也不执行任何 PowerShell 或磁盘操作，因此可以直接用
普通单元测试覆盖。

``prepare()`` 既接受 ``[1, 3]``，也接受
``{1: {"c_size": 400, "data_size": 800}}`` 这种“每块盘一套参数”的写法；
每个任务用合并后的配置载荷构造 ``JobSpec``，盘符分配仍由一次全局调用完成，
避免并发分配撞车。
"""

from __future__ import annotations

import copy
import inspect
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.application.contracts import CheckItem, DiskInfo
from app.application.jobs import ExecutionPlan, Job, JobManager, JobSpec, JobStatus
from app.application.real_runner import build_real_runner
from app.application.service import SispService
from app.models import WorkerResult
from app.modules.image.service import IMAGE_SUFFIXES
from app.orchestrator import build_worker_disk_identity
from app.preflight import run_preflight_checks

PreflightRunner = Callable[..., dict[str, Any]]

_CHECK_KEYS = {
    "管理员权限检查": "admin",
    "PowerShell 可用性检查": "powershell",
    "配置文件存在性检查": "config_exists",
    "配置文件可解析性检查": "config_parse",
    "镜像文件存在性检查": "image_exists",
    "镜像可应用性检查": "image_type",
    "临时目录可用空间检查": "free_space",
    "外部工具可用性检查": "tools",
    "设置路径有效性检查": "setting_paths",
}

# 三分区布局，只需 3 个盘符
_PLAN_STEP_KEYS = ("efi", "windows", "data")
_DRY_RUN_WARNING = "dry-run 模式：只展示执行计划，不会执行任何写盘操作"
_DESTRUCTIVE_WARNING = "破坏性操作：将清空目标硬盘全部数据，请再次核对硬盘编号和身份"
_CANCEL_WARNING = "取消为协作式：当前步骤完成后才会停止"
BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024


class WorkflowError(ValueError):
    """工作流无法继续时抛出的用户可读错误。"""


@dataclass(frozen=True)
class SessionState:
    """一次工作流会话的启动检查与硬盘扫描快照。"""

    config_path: str | None
    config_payload: dict[str, Any] | None
    checks: tuple[CheckItem, ...] = ()
    disks: tuple[DiskInfo, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    dry_run: bool = False
    # 用户是否显式指定了业务配置文件（``-j/--config`` 或 ``SISP_CONFIG``）。
    # 显式指定时配置优先于界面设置，否则界面设置优先。
    config_explicit: bool = False

    @property
    def ready(self) -> bool:
        """配置可用且没有阻断项时才能继续确认执行。"""
        return self.config_payload is not None and not self.blockers

    @property
    def can_prepare(self) -> bool:
        """配置可用即可生成计划，阻断项会展示在计划中并阻止确认。"""
        return self.config_payload is not None

    @property
    def selectable_disks(self) -> tuple[DiskInfo, ...]:
        return tuple(disk for disk in self.disks if disk.is_selectable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "config_explicit": self.config_explicit,
            "dry_run": self.dry_run,
            "checks": [check.to_dict() for check in self.checks],
            "disks": [disk.to_dict() for disk in self.disks],
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PreparedJob:
    """确认界面上的一块硬盘及其执行计划。"""

    spec: JobSpec
    plan: ExecutionPlan
    disk: DiskInfo


def _build_check_items(results: Sequence[dict[str, Any]] | None) -> tuple[CheckItem, ...]:
    checks: list[CheckItem] = []
    for index, result in enumerate(results or (), start=1):
        name = str(result.get("name") or f"检查 {index}")
        checks.append(
            CheckItem(
                key=_CHECK_KEYS.get(name, f"check_{index}"),
                label=name,
                passed=bool(result.get("passed")),
                message=str(result.get("message") or ""),
                # “可选”检查失败不进入阻断项：业务配置文件缺失属于可选，
                # 界面设置已经能覆盖它的全部内容。
                optional=bool(result.get("optional")),
            )
        )
    return tuple(checks)


def _is_strong_identity(identity: dict[str, Any]) -> bool:
    unique_id = str(identity.get("unique_id") or "").strip()
    serial_number = str(identity.get("serial_number") or "").strip()
    return bool(unique_id or serial_number)


def _as_number(value: Any) -> float | None:
    """把 int/float/数字字符串转成 float；其他（含 bool、空串、非数字）→ None。"""
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


def _merge_payload(
    base_payload: dict[str, Any] | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把单块盘的参数覆盖合并进配置载荷。

    支持的最小键集（其余键透传）：
    ``efi_size`` / ``efi_size_mb``、``c_size`` / ``c_size_gb``、
    ``data_size`` / ``data_size_gb``、``win_gho``、``image_index``。
    """
    payload = copy.deepcopy(dict(base_payload or {}))
    if not overrides:
        return payload

    partition_info = payload.setdefault("partition_info", {})
    if not isinstance(partition_info, dict):
        partition_info = {}
        payload["partition_info"] = partition_info

    efi = _as_number(overrides.get("efi_size", overrides.get("efi_size_mb")))
    if efi is not None:
        partition_info["efi_size_mb"] = efi

    c_size = _as_number(overrides.get("c_size", overrides.get("c_size_gb")))
    if c_size is not None:
        partition_info["c_size_gb"] = c_size

    data_size = overrides.get("data_size", overrides.get("data_size_gb"))
    if data_size in ("auto", None, ""):
        partition_info["data_size_gb"] = None
    else:
        value = _as_number(data_size)
        if value is not None:
            partition_info["data_size_gb"] = value

    win_gho = overrides.get("win_gho")
    if isinstance(win_gho, str) and win_gho.strip():
        image_info = payload.setdefault("image_info", {})
        if isinstance(image_info, dict):
            image_info["image_path"] = win_gho.strip()

    # 界面第 3 步：每块盘可以选同一个镜像文件里的不同版本（索引）。
    image_index = overrides.get("image_index")
    if image_index not in (None, ""):
        value = _as_number(image_index)
        if value is not None and value >= 1:
            image_info = payload.setdefault("image_info", {})
            if isinstance(image_info, dict):
                image_info["image_index"] = int(value)

    return payload


_IMAGE_CHECK_PREFIX = "镜像文件存在性检查"
_IMAGE_TYPE_PREFIX = "镜像可应用性检查"


def _image_problems(
    payload: Mapping[str, Any] | None,
    read_indexes: Callable[[str], Sequence[int]] | None = None,
) -> list[str]:
    """这块盘要用的镜像能不能拿来装机（类型 / 存在性 / 索引）。

    会话级预检只在 ``start()`` 时按配置文件算一次；而向导第 3 步允许每块盘换
    镜像、换目录，所以计划里的镜像阻断必须按合并后的 payload 重算——否则用户
    在第 3 步选了一个真实存在的镜像，第 4 步照样挂着“镜像文件不存在”的阻断。

    这里只做“计划阶段就能判定”的事情：ISO 不能 apply、后缀不对、文件不在、
    dism 里没有这个索引。真正的“装完能不能开机”在 ``verify`` 步骤里查。

    ``read_indexes`` 不传时只查类型与存在性（纯函数，测试不需要 dism）。
    """
    if not isinstance(payload, Mapping):
        return []
    image_info = payload.get("image_info")
    if not isinstance(image_info, Mapping):
        return []
    raw = image_info.get("image_path")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return []

    target = Path(text).expanduser()
    suffix = target.suffix.lower()
    if suffix == ".iso":
        return [
            f"{_IMAGE_TYPE_PREFIX}：{target.name} 是 ISO，dism 不能直接应用到分区；"
            "请先挂载 ISO 取出 sources\\install.wim"
        ]
    if suffix not in IMAGE_SUFFIXES:
        return [
            f"{_IMAGE_TYPE_PREFIX}：不支持 {suffix or '无后缀'} 文件，"
            "装机只接受 WIM/ESD/SWM"
        ]
    if not target.is_file():
        return [f"{_IMAGE_CHECK_PREFIX}：镜像文件不存在: {target}"]

    if read_indexes is None:
        return []

    # 执行器在缺省时用索引 1，所以没写索引也要校验 1 是否存在。
    index = int(_as_number(image_info.get("image_index")) or 1)
    try:
        available = sorted({int(item) for item in read_indexes(str(target))})
    except Exception as exc:  # noqa: BLE001 - dism 读不出来就是不可用
        return [f"{_IMAGE_TYPE_PREFIX}：无法读取 {target.name} 的索引（{exc}）"]
    if not available:
        return [f"{_IMAGE_TYPE_PREFIX}：{target.name} 里没有可安装的版本"]
    if index not in available:
        return [
            f"{_IMAGE_TYPE_PREFIX}：镜像索引 {index} 不存在"
            f"（{target.name} 可用索引：{available}）"
        ]
    return []


def _image_blocker(payload: Mapping[str, Any] | None) -> str | None:
    """只查类型与存在性的便捷包装（给测试与轻量调用方用）。"""
    problems = _image_problems(payload)
    return problems[0] if problems else None


def _apply_partition_defaults(
    payload: dict[str, Any],
    defaults: Mapping[str, Any] | None,
    *,
    settings_win: bool = True,
) -> dict[str, Any]:
    """把界面设置里的分区默认值合并进载荷。

    ``settings_win=True``（默认，界面设置优先）时用设置值覆盖配置；显式指定了
    ``-j/--config`` 的会话会传 ``False``，退回到“配置优先、设置只补缺失字段”。

    ``data_size_gb=None`` 表示“用剩余空间”，与配置缺省等价，因此不写入。
    """
    if not defaults:
        return payload

    partition_info = payload.get("partition_info")
    if not isinstance(partition_info, dict):
        partition_info = {}
        payload["partition_info"] = partition_info

    for key in ("efi_size_mb", "c_size_gb", "data_size_gb", "data_fs"):
        value = defaults.get(key)
        if value in (None, ""):
            continue
        if not settings_win and partition_info.get(key) not in (None, ""):
            continue
        partition_info[key] = value
    return payload


def _config_is_explicit(config_path: str | None) -> bool:
    """用户是否显式指定了业务配置文件。

    显式指定（``-j/--config`` 或环境变量 ``SISP_CONFIG``）时保持“配置优先于界面
    设置”的老行为，方便批量脚本用预设文件覆盖用户设置；默认走界面设置。
    """
    if str(config_path or "").strip():
        return True
    return bool(str(os.environ.get("SISP_CONFIG") or "").strip())


def _apply_image_defaults(
    payload: dict[str, Any],
    settings: Mapping[str, Any] | None,
    *,
    override: bool = False,
) -> dict[str, Any]:
    """用设置里的“系统镜像文件 + 默认版本”决定镜像。

    ``override=True``（界面设置优先的会话）时，设置里的 ``sysWim`` 直接覆盖配置
    里的同名字段——否则模板里的占位路径（``D:\\images\\win11.wim``）会一直挂着
    「镜像文件不存在」的阻断，用户在设置页填了真镜像也不生效。
    ``override=False``（显式 ``-j`` 的会话）时只补缺失字段，配置优先。

    向导第 3 步的逐盘选择在 ``_merge_payload`` 里合并，晚于这里，所以永远优先。
    """
    if not settings:
        return payload
    info = payload.get("image_info")
    if not isinstance(info, dict):
        info = {}
        payload["image_info"] = info
    if not override and str(info.get("image_path") or "").strip():
        return payload
    wim = str(settings.get("sysWim") or "").strip()
    if not wim:
        return payload
    info["image_path"] = wim
    try:
        index = int(settings.get("imageDefaultIndex") or 0)
    except (TypeError, ValueError):
        index = 0
    if index > 0:
        info["image_index"] = index
    else:
        info.pop("image_index", None)
    return payload


def _check_partition_fit(disk: DiskInfo, payload: dict[str, Any]) -> list[str]:
    """检查三个分区是否放得下（不依赖 PowerShell，纯算术）。"""
    if not isinstance(disk.size_bytes, int) or disk.size_bytes <= 0:
        return []
    info = payload.get("partition_info") or {}
    if not isinstance(info, dict):
        return []

    efi_mb = _as_number(info.get("efi_size_mb")) or 100.0
    c_gb = _as_number(info.get("c_size_gb"))
    data_gb = _as_number(info.get("data_size_gb"))
    if c_gb is None:
        return []

    required = int(efi_mb * BYTES_PER_MB + c_gb * BYTES_PER_GB)
    if data_gb is not None:
        required += int(data_gb * BYTES_PER_GB)
    # 留 1MB 给分区对齐与 GPT 表
    if required + BYTES_PER_MB > disk.size_bytes:
        return [
            f"硬盘 {disk.disk_number} 容量不足：EFI {efi_mb:.0f}MB + C {c_gb:.0f}GB"
            + (f" + D {data_gb:.0f}GB" if data_gb is not None else "")
            + f" 超过 {disk.size_display}"
        ]
    return []


class WorkflowController:
    """把应用服务、计划确认门禁和任务管理器串成一个可测试的工作流。"""

    def __init__(
        self,
        service: SispService | None = None,
        job_manager: JobManager | None = None,
        *,
        preflight_runner: PreflightRunner | None = None,
        dry_run: bool = False,
        worker_interval: float = 6.0,
        max_parallel: int = 1,
        log_dir: str | None = None,
        result_dir: str | None = None,
        resource_resolver: Callable[[Any], int | None] | None = None,
        event_dir: str | None = None,
        allowed_disk_numbers: Iterable[int] | None = None,
        real_runner: Callable[[Any, threading.Event], WorkerResult] | None = None,
        partition_defaults: Mapping[str, Any] | None = None,
        tool_paths: Mapping[str, str] | None = None,
        protect_system_disk: bool = True,
        block_unverified: bool = True,
        type_to_confirm: bool = False,
        settings_provider: Callable[[], dict[str, Any]] | None = None,
        image_indexes: Callable[[str], Sequence[int]] | None = None,
    ) -> None:
        self._dry_run = bool(dry_run)
        # 构造时的 True 是硬上限：--dry-run / dryRunByDefault 启动后，
        # UI 或 API 只能把会话降级为演练，不能反过来升级为真实写盘。
        self._forced_dry_run = bool(dry_run)
        self._worker_interval = max(float(worker_interval), 0.0)
        self._max_parallel = max(int(max_parallel), 1)
        self._log_dir = log_dir
        self._result_dir = result_dir
        self._resource_resolver = resource_resolver
        self._allowed_disk_numbers = (
            frozenset(int(number) for number in allowed_disk_numbers)
            if allowed_disk_numbers is not None
            else None
        )
        self._real_runner = real_runner
        # 界面设置的默认分区参数：只补业务配置没写的字段，不覆盖配置。
        self._partition_defaults = dict(partition_defaults or {})
        # 外部工具路径（extDiskpart / extDism）；空值交给 runner 自身探测。
        self._tool_paths = {
            str(key): str(value).strip()
            for key, value in (tool_paths or {}).items()
            if str(value).strip()
        }
        # 安全开关：protectSystemDisk / blockUnverified 决定哪些盘“不可选”，
        # typeToConfirm 决定真实执行前是否必须手输盘号/型号。
        self._protect_system_disk = bool(protect_system_disk)
        self._block_unverified = bool(block_unverified)
        self._type_to_confirm = bool(type_to_confirm)
        # 界面设置提供者：预检、执行器可选项都从这里取最新设置。
        self._settings_provider = settings_provider
        # 镜像索引来源：默认用 dism 读（带 mtime 缓存），测试可整体替掉。
        self._image_indexes = image_indexes
        self._image_index_cache: dict[str, tuple[tuple[int, int], list[int]]] = {}
        # 上一次 prepare() 因“无显式白名单”而隐式加入的盘号；下次 prepare/start 时撤销。
        self._implicit_allowlist: set[int] = set()
        # 服务必须跟着控制器的开关走——dry-run、写盘白名单、执行器
        # 三者由控制器统一装配，不各自取默认值。
        self._service_injected = service is not None
        self._service = service if service is not None else self._build_service()
        # 默认执行器直接调本会话的 service；旧的 JobManager 会另造一个 dry-run 服务。
        self._manager = job_manager or JobManager(
            executor=self._execute_spec,
            event_dir=event_dir,
        )
        self._preflight_runner = preflight_runner or run_preflight_checks
        self._session: SessionState | None = None
        self._prepared: dict[int, PreparedJob] = {}
        self._jobs: dict[str, PreparedJob] = {}

    # ------------------------------------------------------------------ 装配
    def _build_service(self) -> SispService:
        """按当前开关构造应用服务；真实模式的执行器默认是 ``WindowsDiskRunner``。"""
        runner = self._real_runner
        if runner is None:
            runner = build_real_runner(
                log_dir=self._log_dir,
                diskpart=self._tool_paths.get("diskpart"),
                dism=self._tool_paths.get("dism"),
            )
        return SispService(
            dry_run=bool(self._dry_run),
            allowed_disk_numbers=self._allowed_disk_numbers or (),
            real_runner=runner,
            protect_system_disk=self._protect_system_disk,
            block_unverified=self._block_unverified,
        )

    def _execute_spec(
        self,
        spec: Any,
        cancel_event: threading.Event,
        on_step: Any = None,
    ) -> WorkerResult:
        """``JobManager`` 的默认执行器：走本会话的 service，不再另造一个。"""
        self._sync_service_policy(dry_run=bool(getattr(spec, "dry_run", False)))
        execute = self._service.execute_spec
        try:
            accepts = "on_step" in inspect.signature(execute).parameters
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            return execute(spec, cancel_event, on_step)
        return execute(spec, cancel_event)

    def _apply_live_safety_flags(self) -> None:
        """设置页改了安全开关后就生效，不必重启进程。

        只覆盖设置里确实存在的键；没有设置提供者（纯代码构造）时保持构造参数。
        """
        settings = self._settings()
        if not settings:
            return
        changed = False
        if "protectSystemDisk" in settings:
            value = bool(settings.get("protectSystemDisk"))
            if value != self._protect_system_disk:
                self._protect_system_disk = value
                changed = True
        if "blockUnverified" in settings:
            value = bool(settings.get("blockUnverified"))
            if value != self._block_unverified:
                self._block_unverified = value
                changed = True
        if "typeToConfirm" in settings:
            self._type_to_confirm = bool(settings.get("typeToConfirm"))
        if changed:
            # 执行层重算保护原因时看的是 service 上的开关，要保持一致。
            self._service.protect_system_disk = self._protect_system_disk
            self._service.block_unverified = self._block_unverified

    def _explicit_allowlist(self) -> frozenset[int] | None:
        """显式白名单优先；也认 ``cli.inject_policy_flags`` 挂上的 ``allow_disks``。"""
        if self._allowed_disk_numbers is not None:
            return self._allowed_disk_numbers
        attribute = getattr(self, "allow_disks", None)
        if attribute:
            return frozenset(int(number) for number in attribute)
        return None

    def _resolve_dry_run(self, dry_run: bool | None = None) -> bool:
        """计算本次操作的有效模式；``_forced_dry_run`` 为真时永远是演练。"""
        if self._forced_dry_run:
            return True
        if dry_run is None:
            return bool(self._dry_run)
        return bool(dry_run)

    def _sync_service_policy(self, *, dry_run: bool | None = None) -> None:
        """把控制器的当前开关同步给服务；外部注入的 service 只同步白名单。"""
        if dry_run is not None and not self._service_injected:
            # 即使调用方误传 False，构造时锁定的演练上限也不能被突破。
            self._service.dry_run = self._resolve_dry_run(dry_run)
        allowlist = self._explicit_allowlist()
        if allowlist is not None:
            self._service.allowed_disk_numbers = frozenset(allowlist)

    def _run_preflight(self, config_path: str | None) -> dict[str, Any]:
        """调用预检；兼容只接受 config_path 的测试替身。"""
        accepts: set[str] = set()
        variadic = False
        try:
            signature = inspect.signature(self._preflight_runner)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            accepts = set(signature.parameters)
            variadic = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        kwargs: dict[str, Any] = {}
        if variadic or "dry_run" in accepts:
            kwargs["dry_run"] = self._dry_run
        if (variadic or "settings" in accepts) and self._settings_provider is not None:
            kwargs["settings"] = self._settings()
        return self._preflight_runner(config_path, **kwargs)

    # ------------------------------------------------------------------ 设置
    def set_settings_provider(
        self, provider: Callable[[], dict[str, Any]] | None
    ) -> None:
        """注入设置读取函数；界面改完设置后无需重建控制器。"""
        self._settings_provider = provider

    def _settings(self) -> dict[str, Any]:
        if self._settings_provider is None:
            return {}
        try:
            data = self._settings_provider()
        except Exception:  # noqa: BLE001 - 设置读取失败不能拖死执行
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    def _live_partition_defaults(self) -> dict[str, Any]:
        """构造时的默认值 + 设置里的最新值（改完设置不必重启）。

        设置页里 EFI / 系统分区 / 数据分区 / 文件系统这四项都是“下次生成计划生效”，
        所以这里每次现读，而不是只用构造时抓的快照。
        """
        defaults = dict(self._partition_defaults)
        settings = self._settings()
        fs = str(settings.get("defaultFs") or "").strip()
        if fs:
            defaults["data_fs"] = fs
        for key, source in (
            ("efi_size_mb", "efiDefaultMb"),
            ("c_size_gb", "sysDefaultGb"),
        ):
            value = settings.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                defaults[key] = value
        data_size = settings.get("dataDefaultGb")
        if data_size == "auto":
            defaults["data_size_gb"] = None
        elif isinstance(data_size, (int, float)) and not isinstance(data_size, bool):
            defaults["data_size_gb"] = data_size
        return defaults

    def _merged_excluded_names(self, payload: Mapping[str, Any] | None) -> list[str]:
        """禁止操作的硬盘型号：设置页的名单 ∪ 业务配置里的名单。

        两处都要认：设置页是普通用户的入口（改了立即生效），业务配置是给
        ``-j`` 指定预设文件的高级用法。
        """
        names: list[str] = []
        seen: set[str] = set()
        for source in (
            (payload or {}).get("excluded_disk_names"),
            self._settings().get("excludedDiskNames"),
        ):
            if not isinstance(source, (list, tuple)):
                continue
            for item in source:
                text = str(item).strip()
                # 匹配本身是 casefold 的，重复项也按 casefold 判断，避免同一块盘
                # 在两处名单里写法不同就出现两条“命中排除名单”。
                marker = text.casefold()
                if not text or marker in seen:
                    continue
                seen.add(marker)
                names.append(text)
        return names

    def _runner_options(self) -> dict[str, Any]:
        """把界面设置里的执行器可选项挑出来，随任务一起下发。"""
        settings = self._settings()
        options: dict[str, Any] = {}
        for key, target in (
            ("sysTemp", "temp_dir"),
            ("sysUnattend", "unattend_path"),
            ("sysDrivers", "drivers_path"),
            ("sysBoot", "boot_source"),
        ):
            value = str(settings.get(key) or "").strip()
            if value:
                options[target] = value
        return options

    def authorize_disks(
        self, disk_numbers: Iterable[int], *, dry_run: bool | None = None
    ) -> list[int]:
        """把“刚在界面上确认过”的盘加入隐式白名单。

        整盘安装走 ``prepare()``，它会顺手授权选中的盘；而硬盘分区、修复引导是
        单盘操作，不经过 ``prepare()``，如果这里不授权，真实执行永远会被
        “不在白名单内”挡掉（演练模式不看白名单，所以本地测不出来）。
        用户在主页面选中磁盘 + 输入确认文本，就是与 CLI ``--allow-disks`` 等价的授权。
        """
        numbers = [int(number) for number in disk_numbers]
        active_dry_run = self._resolve_dry_run(dry_run)
        self._sync_service_policy(dry_run=active_dry_run)
        if active_dry_run or self._explicit_allowlist() is not None:
            return numbers
        before = set(self._service.allowed_disk_numbers)
        added = {number for number in numbers} - before
        if added:
            self._service.allowed_disk_numbers = frozenset(before | added)
            self._implicit_allowlist |= added
        return numbers

    def _drop_implicit_allowlist(self) -> None:
        """撤销上一次 prepare() 隐式加入的盘号，避免会话间累积授权。"""
        if not self._implicit_allowlist:
            return
        remaining = set(self._service.allowed_disk_numbers) - self._implicit_allowlist
        self._service.allowed_disk_numbers = frozenset(remaining)
        self._implicit_allowlist.clear()

    # ------------------------------------------------------------------ 查询
    @property
    def session(self) -> SessionState | None:
        return self._session

    @property
    def job_manager(self) -> JobManager:
        return self._manager

    @property
    def service(self) -> SispService:
        """暴露应用服务，界面层不再直接够私有属性。"""
        return self._service

    @property
    def prepared_jobs(self) -> tuple[PreparedJob, ...]:
        return tuple(self._prepared.values())

    def jobs(self) -> list[Job]:
        jobs: list[Job] = []
        for job_id in self._jobs:
            job = self._manager.get(job_id)
            if job is not None:
                jobs.append(job)
        return jobs

    def results(self) -> list[WorkerResult]:
        return [job.result for job in self.jobs() if job.result is not None]

    # ------------------------------------------------------------------ 启动与扫描
    def start(
        self,
        config_path: str | None = None,
        *,
        dry_run: bool | None = None,
    ) -> SessionState:
        """执行运行前检查、读取配置并扫描硬盘。"""
        if dry_run is not None:
            self._dry_run = self._resolve_dry_run(dry_run)
        # 扫描前先对齐设置里的保护开关，否则刚改的设置要重启才生效。
        self._apply_live_safety_flags()

        try:
            report = self._run_preflight(config_path)
        except Exception as exc:  # noqa: BLE001 - 预检失败也要变成会话阻断项而不是崩栈
            report = {
                "all_passed": False,
                "results": [
                    {
                        "name": "运行前检查",
                        "passed": False,
                        "message": f"运行前检查执行失败: {exc}",
                    }
                ],
                "config_payload": None,
            }

        checks = _build_check_items(report.get("results"))
        payload = report.get("config_payload")
        if not isinstance(payload, dict):
            payload = None

        blockers = [
            f"{check.label}：{check.message}"
            for check in checks
            if not check.passed and not check.optional
        ]
        warnings: list[str] = [
            f"{check.label}：{check.message}" for check in checks if not check.passed and check.optional
        ]
        disks: tuple[DiskInfo, ...] = ()

        # 业务配置从“必需”降级为“可选预设”：没有配置文件时用空载荷继续，
        # 分区大小、镜像、排除名单都能从界面设置里拿到。
        if payload is None:
            payload = {}
            warnings.append("未找到业务配置文件，全部使用界面设置")
        payload = _apply_image_defaults(
            payload,
            self._settings(),
            override=not _config_is_explicit(config_path),
        )
        excluded_names = self._merged_excluded_names(payload)
        try:
            disks = tuple(self._service.scan_protected_disks(excluded_names))
        except Exception as exc:  # noqa: BLE001 - 扫描失败记成阻断项，界面还能打开
            blockers.append(f"硬盘扫描失败：{exc}")

        if not disks:
            warnings.append("未检测到任何硬盘")
        elif not any(disk.is_selectable for disk in disks):
            warnings.append("当前没有可选硬盘，所有硬盘均被保护")

        session = SessionState(
            config_path=self._resolve_config_path(config_path, payload),
            config_payload=payload,
            config_explicit=_config_is_explicit(config_path),
            checks=checks,
            disks=disks,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(warnings),
            dry_run=self._dry_run,
        )
        self._session = session
        self._prepared = {}
        # 会话是“这次运行的真实模式”，服务必须立刻跟上（覆盖构造时的默认值）。
        self._drop_implicit_allowlist()
        self._sync_service_policy(dry_run=self._dry_run)
        return session

    def refresh_disks(self) -> list[DiskInfo]:
        """重新扫描硬盘并应用保护规则。"""
        session = self._require_session()
        self._apply_live_safety_flags()

        try:
            disks = tuple(
                self._service.scan_protected_disks(
                    self._merged_excluded_names(session.config_payload)
                )
            )
        except Exception as exc:
            raise WorkflowError(f"硬盘扫描失败：{exc}") from exc

        # 上一次扫描失败的阻断项可以清除，配置和权限阻断项保留。
        blockers = tuple(
            blocker
            for blocker in session.blockers
            if not blocker.startswith("硬盘扫描失败")
        )
        self._session = replace(session, disks=disks, blockers=blockers)
        self._prepared = {}
        return list(disks)

    def check_config(self, config_path: str | None = None) -> Any:
        """单独重新校验配置文件，不重建会话。

        返回 ``ConfigCheckResult``；界面只需要 ``passed/message/error_code``。
        """
        target = config_path or (self._session.config_path if self._session else None)
        return self._service.check_config(target)

    # ------------------------------------------------------------------ 计划
    def prepare(
        self,
        selected_disk_numbers: Sequence[int] | Mapping[int, Mapping[str, Any]] | None = None,
        *,
        dry_run: bool | None = None,
    ) -> list[PreparedJob]:
        """为选中硬盘生成执行计划和任务输入。

        ``selected_disk_numbers`` 可以是硬盘编号序列，也可以是
        ``{硬盘编号: {"c_size": 400, "data_size": 800}}`` 形式的每盘覆盖；
        省略（``None``）表示用本次扫描结果里**所有可选定**的硬盘——REST 的
        ``POST /api/prepare`` 不带 ``selection`` 时走这条。显式传空序列仍然
        报错，避免“什么都没选却真写盘”。
        """
        session = self._require_session()
        if session.config_payload is None:
            raise WorkflowError("配置不可用，无法生成执行计划")
        self._apply_live_safety_flags()

        overrides: dict[int, dict[str, Any]] = {}
        if selected_disk_numbers is None:
            selected = [disk.disk_number for disk in session.disks if disk.is_selectable]
            if not selected:
                raise WorkflowError("没有可操作的硬盘：全部被保护或未连接")
        elif isinstance(selected_disk_numbers, Mapping):
            raw_selection = selected_disk_numbers
            selected = [int(number) for number in raw_selection]
            overrides = {
                int(number): dict(values or {})
                for number, values in raw_selection.items()
            }
        else:
            selected = [int(number) for number in selected_disk_numbers]

        if not selected:
            raise WorkflowError("尚未选择任何硬盘")

        active_dry_run = self._resolve_dry_run(dry_run)
        # 真实模式下先把服务的 dry_run 与白名单对齐。
        # 没有显式白名单时（GUI 走这条），把“本次选中的硬盘”加入白名单——
        # 用户在界面上勾选 + 后续确认本身就是授权动作，与 CLI 的
        # --allow-disks 等价，但只对本会话选过的盘放行。
        self._drop_implicit_allowlist()
        self._sync_service_policy(dry_run=active_dry_run)
        implicit_allowlist = False
        if not active_dry_run and self._explicit_allowlist() is None:
            before = set(self._service.allowed_disk_numbers)
            added = {int(number) for number in selected} - before
            if added:
                self._service.allowed_disk_numbers = frozenset(before | added)
                self._implicit_allowlist |= added
            implicit_allowlist = True

        summaries = [disk.to_dict() for disk in session.disks]
        try:
            selection = self._service.build_selection_plan(
                session.config_payload,
                summaries,
                selected,
                config_path=session.config_path,
                log_dir=self._log_dir,
                result_dir=self._result_dir,
                resource_resolver=self._resource_resolver,
            )
        except Exception as exc:
            raise WorkflowError(f"无法生成执行计划：{exc}") from exc

        disk_lookup = {disk.disk_number: disk for disk in session.disks}
        prepared: list[PreparedJob] = []
        for disk_number in selected:
            disk = disk_lookup.get(disk_number)
            if disk is None:
                raise WorkflowError(f"硬盘编号不存在: {disk_number}")
            drive_letters = selection.drive_letter_allocations.get(disk_number) or {}
            # 优先级（从弱到强）：业务配置 → 界面设置 → 向导里这块盘的逐盘覆盖。
            # 所以先把设置叠到会话载荷上，再合并逐盘覆盖——否则设置里的默认值
            # 会把用户在向导第 2/3 步填的大小和镜像“改回去”。
            base_payload = _apply_image_defaults(
                session.config_payload,
                self._settings(),
                override=not session.config_explicit,
            )
            base_payload = _apply_partition_defaults(
                base_payload,
                self._live_partition_defaults(),
                settings_win=not session.config_explicit,
            )
            payload = _merge_payload(base_payload, overrides.get(disk_number))
            for message in _check_partition_fit(disk, payload):
                raise WorkflowError(message)
            prepared.append(
                self._build_prepared_job(
                    session,
                    disk,
                    drive_letters,
                    dry_run=active_dry_run,
                    payload=payload,
                    implicit_allowlist=implicit_allowlist,
                )
            )

        self._prepared = {item.spec.disk_number: item for item in prepared}
        return prepared

    def _build_prepared_job(
        self,
        session: SessionState,
        disk: DiskInfo,
        drive_letters: dict[str, str],
        *,
        dry_run: bool,
        payload: dict[str, Any] | None = None,
        implicit_allowlist: bool = False,
    ) -> PreparedJob:
        task_payload = payload if payload is not None else (session.config_payload or {})
        identity = build_worker_disk_identity(disk.to_dict())
        checks: list[CheckItem] = []
        # 会话级阻断项里，“镜像文件存在性检查”/“镜像可应用性检查”是按配置文件算的；
        # 这块盘可能在第 3 步换了镜像，所以先剔掉旧的，等下面按合并后的 payload 重算。
        blockers: list[str] = [
            item for item in session.blockers
            if not item.startswith(_IMAGE_CHECK_PREFIX)
            and not item.startswith(_IMAGE_TYPE_PREFIX)
        ]
        image_problems = self._image_problems_for(task_payload)
        blockers.extend(image_problems)
        warnings: list[str] = []

        checks.append(
            CheckItem(
                key="selectable",
                label="硬盘未被保护",
                passed=disk.is_selectable,
                message=(
                    "硬盘可选择"
                    if disk.is_selectable
                    else "、".join(disk.protection_reasons) or "硬盘受保护"
                ),
            )
        )
        if not disk.is_selectable:
            reasons = "、".join(disk.protection_reasons) or "受保护"
            blockers.append(f"硬盘 {disk.disk_number} 不可操作：{reasons}")

        if not dry_run:
            allowed = sorted(int(number) for number in self._service.allowed_disk_numbers)
            checks.append(
                CheckItem(
                    key="write_allowlist",
                    label="写盘白名单",
                    passed=disk.disk_number in allowed,
                    message=(
                        "按本会话选中的硬盘授权（未显式指定白名单）"
                        if implicit_allowlist
                        else f"白名单：{allowed or '空'}"
                    ),
                    details={"allowed_disk_numbers": allowed},
                )
            )
            if disk.disk_number not in allowed:
                blockers.append(f"硬盘 {disk.disk_number} 不在写盘白名单内")

        if dry_run:
            checks.append(
                CheckItem(
                    key="identity",
                    label="硬盘强身份",
                    passed=True,
                    message="dry-run 不要求 UniqueId 或 SerialNumber",
                )
            )
        else:
            has_identity = _is_strong_identity(identity)
            checks.append(
                CheckItem(
                    key="identity",
                    label="硬盘强身份",
                    passed=has_identity,
                    message=(
                        "已具备 UniqueId 或 SerialNumber"
                        if has_identity
                        else "缺少 UniqueId 和 SerialNumber，拒绝写盘"
                    ),
                )
            )
            if not has_identity:
                blockers.append(
                    f"硬盘 {disk.disk_number} 缺少 UniqueId 和 SerialNumber，拒绝写盘"
                )

        letters_complete = all(
            str(drive_letters.get(key) or "").strip() for key in _PLAN_STEP_KEYS
        )
        checks.append(
            CheckItem(
                key="drive_letters",
                label="盘符分配",
                passed=letters_complete,
                message=(
                    "已分配 EFI/Windows/Data 盘符"
                    if letters_complete
                    else "盘符分配不完整"
                ),
                details={"drive_letters": dict(drive_letters)},
            )
        )
        if not letters_complete:
            blockers.append(f"硬盘 {disk.disk_number} 盘符分配不完整")

        # 镜像能不能装机（类型/存在性/索引）；不能就把它变成阻断项，界面开始按钮会置灰。
        checks.append(
            CheckItem(
                key="image",
                label="镜像可应用性",
                passed=not image_problems,
                message=image_problems[0] if image_problems else "镜像可直接应用到分区",
            )
        )

        warnings.append(_DRY_RUN_WARNING if dry_run else _DESTRUCTIVE_WARNING)
        warnings.append(_CANCEL_WARNING)

        plan = ExecutionPlan(
            disk_number=disk.disk_number,
            model=disk.model or "",
            size_display=disk.size_display,
            identity=identity,
            drive_letters=dict(drive_letters),
            config_path=session.config_path,
            dry_run=dry_run,
            warnings=tuple(warnings),
            checks=tuple(checks),
            blockers=tuple(dict.fromkeys(blockers)),
        )
        spec = JobSpec(
            disk_number=disk.disk_number,
            config_payload=task_payload,
            expected_identity=identity,
            drive_letters=dict(drive_letters),
            dry_run=dry_run,
            runner_options=self._runner_options(),
        )
        return PreparedJob(spec=spec, plan=plan, disk=disk)

    # ------------------------------------------------------------------ 执行
    def _image_problems_for(self, payload: Mapping[str, Any] | None) -> list[str]:
        """按当前会话的 dism 重算镜像可用性（结果会缓存，避免每次 prepare 都读一遍）。"""
        return _image_problems(payload, self._read_image_indexes)

    def _read_image_indexes(self, path: str) -> Sequence[int]:
        if self._image_indexes is not None:
            return self._image_indexes(path)
        return self._probe_image_indexes(path)

    def _probe_image_indexes(self, path: str) -> list[int]:
        """用 dism 读索引；同一文件（mtime+大小没变）只读一次。"""
        target = Path(path).expanduser()
        try:
            stat = target.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = (0, 0)
        cached = self._image_index_cache.get(str(target))
        if cached is not None and cached[0] == stamp:
            return cached[1]

        from app.modules.image.service import ImageProbe

        probe = ImageProbe(dism=self._tool_paths.get("dism") or None)
        indexes = [int(item["index"]) for item in probe.read_indexes(str(target)) if item.get("index")]
        self._image_index_cache[str(target)] = (stamp, indexes)
        return indexes

    # ------------------------------------------------------------------ 执行
    def submit(
        self,
        prepared_jobs: Sequence[PreparedJob] | None = None,
        *,
        require_confirmation: bool = True,
        interval: float | None = None,
        max_parallel: int | None = None,
    ) -> list[Job]:
        """提交任务；默认停在等待确认状态。"""
        items = (
            list(prepared_jobs)
            if prepared_jobs is not None
            else list(self._prepared.values())
        )
        if not items:
            raise WorkflowError("没有可提交的任务，请先生成执行计划")

        numbers = [item.spec.disk_number for item in items]
        if len(set(numbers)) != len(numbers):
            raise WorkflowError("同一次提交中不能包含重复的硬盘任务")
        active = {
            job.disk_number
            for job in self._manager.jobs()
            if not job.status.is_finished
        }
        conflicts = sorted(set(numbers) & active)
        if conflicts:
            raise WorkflowError(
                "以下硬盘已有未结束任务，拒绝重复提交："
                + "、".join(str(number) for number in conflicts)
            )

        if not require_confirmation:
            blocked = [item for item in items if item.plan.blockers]
            if blocked:
                details = "；".join(
                    f"硬盘 {item.plan.disk_number}: {'；'.join(item.plan.blockers)}"
                    for item in blocked
                )
                raise WorkflowError(f"计划存在阻断项，拒绝执行：{details}")

        jobs: list[Job] = []
        for item in items:
            job = self._manager.submit(item.spec, item.plan)
            self._jobs[job.id] = item
            jobs.append(job)

        if not require_confirmation:
            try:
                self._manager.confirm_batch(
                    [job.id for job in jobs],
                    interval=self._worker_interval if interval is None else interval,
                    max_parallel=self._max_parallel if max_parallel is None else max_parallel,
                )
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
        return jobs

    def confirm(
        self,
        job_ids: Sequence[str] | None = None,
        *,
        confirm_text: str | None = None,
    ) -> list[Job]:
        """确认等待中的任务；存在阻断项时拒绝执行。

        ``typeToConfirm`` 打开时，真实任务还必须提供 ``confirm_text``（盘号或
        型号），否则拒绝——防止手滑点错盘。
        """
        jobs = self._select_jobs(job_ids)
        waiting = [job for job in jobs if job.status == JobStatus.WAITING_CONFIRMATION]
        if not waiting:
            return jobs

        self._apply_live_safety_flags()
        blocked = [job for job in waiting if job.plan.blockers]
        if blocked:
            details = "；".join(
                f"硬盘 {job.plan.disk_number}: {'；'.join(job.plan.blockers)}"
                for job in blocked
            )
            raise WorkflowError(f"计划存在阻断项，拒绝执行：{details}")

        if self._type_to_confirm:
            self._require_confirm_text(waiting, confirm_text)

        # 用户确认（勾选 + 输盘号）本身就是授权：这里重新补齐隐式白名单。
        # 否则“刷新页面（新会话）/再 prepare 别的盘”会把之前那次 prepare 的隐式授权
        # 撤掉，已排队的任务确认时报“不在允许写盘的白名单内（当前白名单: 空）”。
        real_disks = [job.disk_number for job in waiting if not bool(job.spec.dry_run)]
        if real_disks:
            self.authorize_disks(real_disks)

        try:
            self._manager.confirm_batch(
                [job.id for job in waiting],
                interval=self._worker_interval,
                max_parallel=self._max_parallel,
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        return jobs

    def cancel(
        self,
        job_ids: Sequence[str] | None = None,
        reason: str = "用户取消",
    ) -> list[str]:
        """请求取消任务，返回成功发出取消请求的任务编号。"""
        cancelled: list[str] = []
        for job in self._select_jobs(job_ids):
            if self._manager.cancel(job.id, reason=reason):
                cancelled.append(job.id)
        return cancelled

    def wait_all(self, timeout: float | None = None) -> list[Job]:
        return self._manager.wait_all(timeout=timeout)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def confirm_keywords(job: Job) -> tuple[str, ...]:
        """``typeToConfirm`` 认可的关键词：盘号或型号（大小写、空格不敏感）。"""
        number = int(job.plan.disk_number)
        identity = getattr(job.spec, "expected_identity", None) or {}
        model = str(identity.get("model") or "").strip()
        keywords = [str(number), f"disk {number}", f"disk{number}", f"硬盘{number}", f"硬盘 {number}"]
        if model:
            keywords.append(model)
        return tuple(keywords)

    def _require_confirm_text(
        self,
        jobs: Sequence[Job],
        confirm_text: str | None,
    ) -> None:
        """校验 ``typeToConfirm`` 的手输确认文本。"""
        real_jobs = [job for job in jobs if not bool(getattr(job.spec, "dry_run", False))]
        if not real_jobs:
            return
        text = " ".join(str(confirm_text or "").split()).casefold()
        if not text:
            raise WorkflowError(
                "已开启“危险操作需输入确认”：请在确认时输入盘号（如 1）或磁盘型号。"
            )
        for job in real_jobs:
            accepted = {
                " ".join(keyword.split()).casefold()
                for keyword in self.confirm_keywords(job)
                if keyword
            }
            if text not in accepted:
                raise WorkflowError(
                    f"确认文本与硬盘 {job.plan.disk_number} 不匹配"
                    f"（可输入盘号或型号：{'、'.join(sorted(accepted))}）。"
                )

    def _require_session(self) -> SessionState:
        if self._session is None:
            raise WorkflowError("工作流尚未启动，请先调用 start()")
        return self._session

    def _select_jobs(self, job_ids: Sequence[str] | None) -> list[Job]:
        if job_ids is None:
            return self.jobs()

        jobs: list[Job] = []
        for job_id in job_ids:
            job = self._manager.get(job_id)
            if job is None:
                raise WorkflowError(f"任务不存在: {job_id}")
            jobs.append(job)
        return jobs

    @staticmethod
    def _resolve_config_path(
        config_path: str | None,
        payload: dict[str, Any] | None,
    ) -> str | None:
        if config_path:
            return str(config_path)
        if payload and payload.get("config_path"):
            return str(payload["config_path"])
        return None
