"""GUI/CLI 共用的工作流适配层。

界面只负责“展示 + 用户操作”，不直接拼装配置、扫描、盘符分配和任务。
``WorkflowController`` 把一次完整操作串成：

1. ``start()``：运行前检查、扫描并应用硬盘保护规则；
2. ``refresh_disks()``：重新扫描硬盘，不改变检查结果；
3. ``prepare()``：执行写前校验，为每块盘生成 ``ExecutionPlan`` 和
   ``JobSpec``，并标明检查项与阻断项；
4. ``submit()`` / ``confirm()`` / ``cancel()``：交给 ``JobManager`` 执行；
5. ``results()``：汇总已结束任务的结果。

本模块不依赖 Qt，也不执行任何 PowerShell 或磁盘操作，因此可以直接用
普通单元测试覆盖。

``prepare()`` 既接受 ``[1, 3]``，也接受
``{1: {"c_size": 400, "data_size": 800}}`` 这种“每块盘一套参数”的写法；
每个任务用合并后的任务载荷（``TaskPayload``）构造 ``JobSpec``，盘符分配仍由
一次全局调用完成，避免并发分配撞车。

参数优先级（从弱到强）：**设置页默认值 → 向导里这块盘的逐盘覆盖**。
这一版之前还有一层“业务 JSON 配置文件”，显式 ``-j`` 时它压过设置、否则被设置
压过——同一台机器因此能跑出两种结果。那一层已整体移除。
"""

from __future__ import annotations

import copy
import inspect
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.application.contracts import CheckItem, DiskInfo
from app.application.coordination import GhostStartGate, PartitionSequencer
from app.application.disk_label import disk_label
from app.application.jobs import ExecutionPlan, Job, JobManager, JobSpec, JobStatus
from app.application.partitions import (
    PartitionLayoutError,
    allow_small_partitions,
    check_layout,
    check_minimums,
    data_partition_count,
    normalise_data_partitions,
    plan_partitions,
    unallocated_bytes,
    with_auto_estimates,
)
from app.application.payload import build_task_payload
from app.application.real_runner import build_real_runner
from app.application.service import SispService
from app.application.shell import find_program
from app.application.steps import phases_payload
from app.models import WorkerResult
from app.modules.ghost.service import GHO_SUFFIXES, sniff_gho
from app.modules.image.service import configured_versions
from app.orchestrator import build_worker_disk_identity
from app.preflight import run_preflight_checks

PreflightRunner = Callable[..., dict[str, Any]]

_CHECK_KEYS = {
    "管理员权限检查": "admin",
    "PowerShell 可用性检查": "powershell",
    "镜像文件存在性检查": "image_exists",
    "镜像可应用性检查": "image_type",
    "临时目录可用空间检查": "free_space",
    "外部工具可用性检查": "tools",
    "设置路径有效性检查": "setting_paths",
}

# 基础三分区布局；额外数据分区（E）会再加一个 ``data2``
_PLAN_STEP_KEYS = ("efi", "windows", "data")
_DESTRUCTIVE_WARNING = "破坏性操作：将清空目标硬盘全部数据，请再次核对硬盘编号和身份"
_CANCEL_WARNING = "取消为协作式：当前步骤完成后才会停止"


class WorkflowError(ValueError):
    """工作流无法继续时抛出的用户可读错误。"""


@dataclass(frozen=True)
class SessionState:
    """一次工作流会话的启动检查与硬盘扫描快照。"""

    # 启动那一刻由设置页算出的任务载荷（分区默认值 + 镜像位置）。
    # 它只是快照：``prepare()`` 会再读一次设置，改完设置不必重启。
    task_payload: dict[str, Any] | None
    checks: tuple[CheckItem, ...] = ()
    disks: tuple[DiskInfo, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # 会话快照里顺带扫到的系统已用盘符（含幽灵盘符），`prepare()` 分配盘符时
    # 直接复用，不再单独启动一次 PowerShell。
    used_letters: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
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
                # “可选”检查失败不进入阻断项：例如镜像文件还没准备好，
                # 用户可以在设置页补齐后再继续。
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
    ``data_size`` / ``data_size_gb``、``data_partitions``、``win_gho``。

    ``data_partitions`` 存在时**取代** ``data_size_gb``（向导里加了 E 分区走
    这条），每项都必须显式给出 ``size_gb``。
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

    data_parts = overrides.get("data_partitions")
    if data_parts is not None:
        # 显式分区表：校验通过才存回去，顺手把老的 data_size_gb 拿掉，
        # 避免两个来源同时存在时“到底听谁”的歧义。
        try:
            partition_info["data_partitions"] = normalise_data_partitions(data_parts)
        except PartitionLayoutError as exc:
            raise WorkflowError(f"分区配置无效：{exc}") from exc
        partition_info.pop("data_size_gb", None)

    win_gho = overrides.get("win_gho")
    if isinstance(win_gho, str) and win_gho.strip():
        image_info = payload.setdefault("image_info", {})
        if isinstance(image_info, dict):
            # 归一化：用户/脚本可能写成 D:/x/test.GHO，统一成展开后的字符串。
            image_info["image_path"] = str(Path(win_gho.strip()).expanduser())

    return payload


_IMAGE_CHECK_PREFIX = "镜像文件存在性检查"
_IMAGE_TYPE_PREFIX = "镜像可应用性检查"


def _image_problems(
    payload: Mapping[str, Any] | None,
    *,
    ghost_available: bool | None = None,
) -> list[str]:
    """这块盘要用的 GHO 能不能拿来装机（类型 / 存在性 / 文件头 / 引擎）。

    会话级预检只在 ``start()`` 时按配置文件算一次；而向导第 3 步允许每块盘换
    镜像、换目录，所以计划里的镜像阻断必须按合并后的 payload 重算——否则用户
    在第 3 步选了一个真实存在的镜像，第 4 步照样挂着“镜像文件不存在”的阻断。

    这里只做“计划阶段就能判定”的事情：后缀对不对、文件在不在、Ghost 文件头
    对不对、Ghost 可执行文件找不找得到。真正的“装完能不能开机”在 ``boot``
    步骤里查（写引导与文件校验是同一步）。

    ``ghost_available=None``（默认）时不查引擎，只要路径与文件头合法就算通过；
    预检带上 ``True`` / ``False`` 就是完整的四道判定。
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
            f"{_IMAGE_TYPE_PREFIX}：{target.name} 是 ISO，Ghost 只能还原 .GHO；"
            "请先封装成 GHO 分区块镜像"
        ]
    if suffix in (".wim", ".esd", ".swm"):
        return [
            f"{_IMAGE_TYPE_PREFIX}：不支持 {suffix}，dism 路线已移除；"
            "装机只接受 GHO/GHS（Ghost 分区块镜像）"
        ]
    if suffix not in GHO_SUFFIXES:
        return [
            f"{_IMAGE_TYPE_PREFIX}：不支持 {suffix or '无后缀'} 文件，"
            "装机只接受 GHO/GHS"
        ]
    if not target.is_file():
        return [f"{_IMAGE_CHECK_PREFIX}：镜像文件不存在: {target}"]

    sniffed = sniff_gho(target)
    if not sniffed.get("ok"):
        reason = sniffed.get("reason") or "不是有效 Ghost 镜像"
        return [f"{_IMAGE_TYPE_PREFIX}：{target.name} {reason}"]

    if ghost_available is False:
        return [
            f"{_IMAGE_TYPE_PREFIX}：未找到 Ghost 可执行文件"
            "（设置 → 外部程序 → Ghost 路径）"
        ]
    return []


def _image_from_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """设置页的系统槽位 → ``image_info``。

    两个槽位（A/B）是并列的版本，默认用**第一个填了路径的**；向导第 3 步里
    每块盘可以改成另一个，逐盘覆盖在 ``_merge_payload`` 里合并，晚于这里。

    载荷里只有路径：GHO 的分区块序号固定用 Ghost 的默认值（第 1 块），
    不再开放设置。
    """
    versions = configured_versions(settings)
    if not versions:
        return {}
    return {"image_path": versions[0]["path"]}


def _check_partition_fit(disk: DiskInfo, payload: dict[str, Any]) -> list[str]:
    """检查分区布局是否放得下、以及各分区是不是小到不可用（纯算术）。

    布局由 ``app.application.partitions.plan_partitions`` 统一解析，所以加了
    额外数据分区（E）之后这里自动算上新分区；错误信息保持“硬盘 N 容量不足”/
    “硬盘 N 分区过小”，界面直接展示。

    下限（C ≥ 60 GB、数据分区 ≥ 20 GB、E ≥ 1 GB）以前只在前端表单里，直连
    ``/api/prepare`` 能绕过；这里补上，并由执行器在清盘前再查一次。单测与小盘
    实测通过 ``partition_info.allow_small_partitions`` 显式放宽。
    """
    if not isinstance(disk.size_bytes, int) or disk.size_bytes <= 0:
        return []
    title = disk_label(disk.disk_number, disk.model, size_display=disk.size_display)
    try:
        parts = plan_partitions(payload)
    except PartitionLayoutError as exc:
        return [f"{title} 分区配置无效：{exc}"]
    allow_small = allow_small_partitions(payload)
    problems = [
        f"{title} 容量不足：{problem}"
        for problem in check_layout(parts, disk.size_bytes)
    ]
    problems += [
        f"{title} 分区过小：{problem}"
        for problem in check_minimums(parts, disk.size_bytes, allow_small=allow_small)
    ]
    return problems


class WorkflowController:
    """把应用服务、计划确认门禁和任务管理器串成一个可测试的工作流。"""

    def __init__(
        self,
        service: SispService | None = None,
        job_manager: JobManager | None = None,
        *,
        preflight_runner: PreflightRunner | None = None,
        worker_interval: float = 0.0,
        max_parallel: int = 0,
        log_dir: str | None = None,
        result_dir: str | None = None,
        resource_resolver: Callable[[Any], int | None] | None = None,
        allowed_disk_numbers: Iterable[int] | None = None,
        real_runner: Callable[[Any, threading.Event], WorkerResult] | None = None,
        partition_defaults: Mapping[str, Any] | None = None,
        tool_paths: Mapping[str, str] | None = None,
        protect_system_disk: bool = True,
        settings_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._worker_interval = max(float(worker_interval), 0.0)
        # 0 表示“按本批任务数放开”：几张盘就几个线程，分区组由
        # ``PartitionSequencer`` 串行化，系统组（Ghost/引导）并行。
        self._max_parallel = max(int(max_parallel), 0)
        self._log_dir = log_dir
        self._result_dir = result_dir
        self._resource_resolver = resource_resolver
        self._allowed_disk_numbers = (
            frozenset(int(number) for number in allowed_disk_numbers)
            if allowed_disk_numbers is not None
            else None
        )
        self._real_runner = real_runner
        # 界面设置的默认分区参数：作为本次任务的默认值，逐盘覆盖优先。
        self._partition_defaults = dict(partition_defaults or {})
        # 外部工具路径（extGhost / extBcdboot）；空值交给 runner 自身探测。
        # diskpart 固定用系统自带，不再从设置读。
        self._tool_paths = {
            str(key): str(value).strip()
            for key, value in (tool_paths or {}).items()
            if str(value).strip()
        }
        # 安全开关：protectSystemDisk 决定系统盘/引导盘是否“不可选”。
        self._protect_system_disk = bool(protect_system_disk)
        # 界面设置提供者：预检、执行器可选项都从这里取最新设置。
        self._settings_provider = settings_provider
        # 上一次 prepare() 因“无显式白名单”而隐式加入的盘号；下次 prepare/start 时撤销。
        self._implicit_allowlist: set[int] = set()
        # 服务必须跟着控制器走——白名单与执行器由控制器统一装配，不各自取默认值。
        # 多盘流水线的两个闸门：分区轮次（按盘号串行）与 Ghost 启动间隔。
        # 单盘时 sequencer 只包含一张盘，效果与串行实现完全一致。
        #
        # **必须在装配 service 之前创建**：执行器（WindowsDiskRunner）要拿到同一对
        # 对象。以前这两行写在 ``_build_service()`` 之后，于是 ``build_real_runner``
        # 里根本没有它们——结果「分区全局串行」只在单测里成立，生产环境四块盘的
        # 分区脚本是交错执行的（现场 0x80070017 就出在这种交错下）。
        self._sequencer = PartitionSequencer()
        self._ghost_gate = GhostStartGate()
        self._service = service if service is not None else self._build_service()
        # 默认执行器直接调本会话的 service。
        self._manager = job_manager or JobManager(
            executor=self._execute_spec,
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
            # 多盘并发靠这两个协调器：少了它们，四块盘的分区脚本会交错执行
            # （每个 diskpart 进程虽然互斥，但「清盘 → 删 MSR → 建分区」三段的
            # 顺序会在盘之间穿插），Ghost 也可能同时启动两个实例撞上 8027。
            # 这里必须和 set_order()/ghost_gate 用的是同一个对象。
            runner = build_real_runner(
                log_dir=self._log_dir,
                ghost=self._tool_paths.get("ghost"),
                bcdboot=self._tool_paths.get("bcdboot"),
                sequencer=self._sequencer,
                ghost_gate=self._ghost_gate,
            )
        return SispService(
            allowed_disk_numbers=self._allowed_disk_numbers or (),
            real_runner=runner,
            protect_system_disk=self._protect_system_disk,
        )

    def _execute_spec(
        self,
        spec: Any,
        cancel_event: threading.Event,
        on_step: Any = None,
    ) -> WorkerResult:
        """``JobManager`` 的默认执行器：走本会话的 service，不再另造一个。

        写盘白名单与保护开关由控制面 ``start()`` / ``prepare()`` / ``confirm()``
        统一处理；任务本身不再修改共享 service 的策略，避免并发数据竞争。
        """
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
        if changed:
            # 执行层重算保护原因时看的是 service 上的开关，要保持一致。
            self._service.protect_system_disk = self._protect_system_disk

    def _explicit_allowlist(self) -> frozenset[int] | None:
        """显式白名单优先；也认 ``cli.inject_policy_flags`` 挂上的 ``allow_disks``。"""
        if self._allowed_disk_numbers is not None:
            return self._allowed_disk_numbers
        attribute = getattr(self, "allow_disks", None)
        if attribute:
            return frozenset(int(number) for number in attribute)
        return None

    def _sync_service_policy(self) -> None:
        """把控制器的白名单同步给服务；外部注入的 service 只同步白名单。"""
        allowlist = self._explicit_allowlist()
        if allowlist is not None:
            self._service.allowed_disk_numbers = frozenset(allowlist)

    def _run_preflight(self) -> dict[str, Any]:
        """调用预检；按签名探测传参，兼容只接受一部分关键字的测试替身。"""
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
        if (variadic or "settings" in accepts) and self._settings_provider is not None:
            kwargs["settings"] = self._settings()
        return self._preflight_runner(**kwargs)

    # ------------------------------------------------------------------ 设置
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

        设置页里 EFI / 系统分区 / 数据分区这三项都是“下次生成计划生效”，
        所以这里每次现读，而不是只用构造时抓的快照。文件系统不在其中：数据分区
        恒为 NTFS（见 ``partitions.DATA_FS_CHOICES``）。
        """
        defaults = dict(self._partition_defaults)
        settings = self._settings()
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

    def _runner_options(self) -> dict[str, Any]:
        """把界面设置里的执行器可选项挑出来，随任务一起下发。

        布尔开关必须显式下发（``False`` 也要传），否则执行器只能看到默认值
        ``True``，开关就失效了。``copy_source``（拷贝文件目录）留空表示不拷，
        由拷贝阶段记一条 ``skipped``。
        """
        settings = self._settings()
        try:
            settle = int(settings.get("partitionSettleMs", 1000))
        except (TypeError, ValueError):
            settle = 1000
        return {
            "release_efi_letter": bool(settings.get("releaseEfiLetter", True)),
            "copy_source": str(settings.get("copySource") or "").strip(),
            # 两次 diskpart 之间的等待（毫秒）。0 = 不等待。
            "partition_settle_ms": max(0, settle),
        }

    def authorize_disks(self, disk_numbers: Iterable[int]) -> list[int]:
        """把“刚在界面上确认过”的盘加入隐式白名单。

        整盘安装走 ``prepare()``，它会顺手授权选中的盘；而修复引导是单盘操作，
        不经过 ``prepare()``，如果这里不授权，真实执行永远会被
        “不在白名单内”挡掉。用户在界面上勾选磁盘并确认，就是与 CLI
        ``--allow-disks`` 等价的授权。
        """
        numbers = [int(number) for number in disk_numbers]
        if self._explicit_allowlist() is not None:
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
    def start(self) -> SessionState:
        """执行运行前检查、从设置算出任务载荷并扫描硬盘。"""
        # 扫描前先对齐设置里的保护开关，否则刚改的设置要重启才生效。
        self._apply_live_safety_flags()

        try:
            report = self._run_preflight()
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
            }

        checks = _build_check_items(report.get("results"))
        # 任务载荷完全由设置推导：分区默认值 + 镜像位置。
        payload = build_task_payload(
            partition_info=self._live_partition_defaults(),
            image_info=_image_from_settings(self._settings()),
        )

        blockers = [
            f"{check.label}：{check.message}"
            for check in checks
            if not check.passed and not check.optional
        ]
        warnings: list[str] = [
            f"{check.label}：{check.message}" for check in checks if not check.passed and check.optional
        ]
        disks: tuple[DiskInfo, ...] = ()
        used_letters: tuple[str, ...] = ()

        try:
            disks, used_letters = self._service.scan_protected_snapshot()
            disks = tuple(disks)
        except Exception as exc:  # noqa: BLE001 - 扫描失败记成阻断项，界面还能打开
            blockers.append(f"硬盘扫描失败：{exc}")

        if not disks:
            warnings.append("未检测到任何硬盘")
        elif not any(disk.is_selectable for disk in disks):
            warnings.append("当前没有可选硬盘，所有硬盘均被保护")

        session = SessionState(
            task_payload=payload,
            checks=checks,
            disks=disks,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(warnings),
            used_letters=tuple(used_letters),
        )
        self._session = session
        self._prepared = {}
        self._drop_implicit_allowlist()
        self._sync_service_policy()
        return session

    def refresh_disks(self) -> list[DiskInfo]:
        """重新扫描硬盘并应用保护规则。"""
        session = self._require_session()
        self._apply_live_safety_flags()

        try:
            disks, used_letters = self._service.scan_protected_snapshot()
            disks = tuple(disks)
        except Exception as exc:
            raise WorkflowError(f"硬盘扫描失败：{exc}") from exc

        # 上一次扫描失败的阻断项可以清除，配置和权限阻断项保留。
        blockers = tuple(
            blocker
            for blocker in session.blockers
            if not blocker.startswith("硬盘扫描失败")
        )
        self._session = replace(
            session,
            disks=disks,
            blockers=blockers,
            used_letters=tuple(used_letters),
        )
        self._prepared = {}
        return list(disks)

    # ------------------------------------------------------------------ 计划
    def prepare(
        self,
        selected_disk_numbers: Sequence[int] | Mapping[int, Mapping[str, Any]] | None = None,
    ) -> list[PreparedJob]:
        """为选中硬盘生成执行计划和任务输入。

        ``selected_disk_numbers`` 可以是硬盘编号序列，也可以是
        ``{硬盘编号: {"c_size": 400, "data_size": 800}}`` 形式的每盘覆盖；
        省略（``None``）表示用本次扫描结果里**所有可选定**的硬盘——REST 的
        ``POST /api/prepare`` 不带 ``selection`` 时走这条。显式传空序列仍然
        报错，避免“什么都没选却真写盘”。
        """
        session = self._require_session()
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

        # 先把服务的白名单对齐。
        # 没有显式白名单时（GUI 走这条），把“本次选中的硬盘”加入白名单——
        # 用户在界面上勾选 + 后续确认本身就是授权动作，与 CLI 的
        # --allow-disks 等价，但只对本会话选过的盘放行。
        self._drop_implicit_allowlist()
        self._sync_service_policy()
        implicit_allowlist = False
        if self._explicit_allowlist() is None:
            before = set(self._service.allowed_disk_numbers)
            added = {int(number) for number in selected} - before
            if added:
                self._service.allowed_disk_numbers = frozenset(before | added)
                self._implicit_allowlist |= added
            implicit_allowlist = True

        disk_lookup = {disk.disk_number: disk for disk in session.disks}
        payloads: dict[int, dict[str, Any]] = {}
        for disk_number in selected:
            disk = disk_lookup.get(disk_number)
            if disk is None:
                raise WorkflowError(f"硬盘编号不存在: {disk_number}")
            # 优先级（从弱到强）：设置页默认值 → 向导里这块盘的逐盘覆盖。
            # 设置每次现读（改完不必重启），逐盘覆盖最后合并——否则设置里的
            # 默认值会把用户在第 2/3 步填的大小和镜像“改回去”。
            base_payload = build_task_payload(
                base=session.task_payload,
                partition_info=self._live_partition_defaults(),
                image_info=_image_from_settings(self._settings()),
            )
            payload = _merge_payload(base_payload, overrides.get(disk_number))
            for message in _check_partition_fit(disk, payload):
                raise WorkflowError(message)
            payloads[disk_number] = payload

        # 盘符分配要知道每块盘到底几个数据分区（3 个还是 4 个盘符），
        # 所以必须晚于载荷合并——早了就只能按三分区算。
        extra_counts = {
            number: max(0, data_partition_count(payload) - 1)
            for number, payload in payloads.items()
        }
        summaries = [disk.to_dict() for disk in session.disks]
        # 会话快照里的盘符只在“这一轮还没跑过任务”时复用：跑过任务以后，
        # EFI 临时盘符可能被保留下来（releaseEfiLetter=false），快照就过期了，
        # 这时回到现场扫描，宁可多花一次 PowerShell 也不能分到已占用的字母。
        known_letters = session.used_letters or None
        if known_letters and self._manager.jobs():
            known_letters = None
        try:
            selection = self._service.build_selection_plan(
                session.task_payload or {},
                summaries,
                selected,
                log_dir=self._log_dir,
                result_dir=self._result_dir,
                resource_resolver=self._resource_resolver,
                extra_counts=extra_counts,
                known_letters=known_letters,
            )
        except Exception as exc:
            raise WorkflowError(f"无法生成执行计划：{exc}") from exc

        prepared: list[PreparedJob] = []
        for disk_number in selected:
            disk = disk_lookup[disk_number]
            drive_letters = selection.drive_letter_allocations.get(disk_number) or {}
            prepared.append(
                self._build_prepared_job(
                    session,
                    disk,
                    drive_letters,
                    payload=payloads[disk_number],
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
        payload: dict[str, Any] | None = None,
        implicit_allowlist: bool = False,
    ) -> PreparedJob:
        task_payload = payload if payload is not None else (session.task_payload or {})
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
        # 这块盘在界面/日志/导出里的统一称呼：型号（磁盘 N）。
        title = disk_label(disk.disk_number, disk.model, size_display=disk.size_display)

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
            blockers.append(f"{title} 不可操作：{reasons}")

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
            blockers.append(f"{title} 不在写盘白名单内")

        # 身份不再作为“必要条件”（设置里的 blockUnverified 已移除）：
        # 执行前的闸门会重新扫描并按盘号/容量/型号（有 UniqueId、
        # SerialNumber 时再比它们）复核，确保“确认的盘”就是“要写的盘”。
        has_identity = _is_strong_identity(identity)
        checks.append(
            CheckItem(
                key="identity",
                label="硬盘身份复核",
                passed=True,
                message=(
                    "已具备 UniqueId 或 SerialNumber，执行前按标识复核"
                    if has_identity
                    else "该盘没有 UniqueId/SerialNumber，执行前按盘号+容量+型号复核"
                ),
            )
        )

        letters_complete = all(
            str(drive_letters.get(key) or "").strip() for key in _PLAN_STEP_KEYS
        )
        # 额外数据分区（E）的盘符也要有，否则 diskpart 建完分区挂不上盘符。
        extra_letters = [
            str(drive_letters.get(role) or "").strip()
            for role in sorted(drive_letters)
            if role.startswith("data") and role != "data"
        ]
        letters_complete = letters_complete and len(extra_letters) == max(
            0, data_partition_count(task_payload) - 1
        )
        checks.append(
            CheckItem(
                key="drive_letters",
                label="盘符分配",
                passed=letters_complete,
                message=(
                    "已分配 EFI/Windows/数据分区盘符"
                    if letters_complete
                    else "盘符分配不完整"
                ),
                details={"drive_letters": dict(drive_letters)},
            )
        )
        if not letters_complete:
            blockers.append(f"{title} 盘符分配不完整")

        # 分区布局：界面第 4 步直接渲染这份计划（真实大小 + 盘符 + 文件系统）。
        planned_parts: list[Any] = []
        try:
            planned_parts = plan_partitions(task_payload, letters=drive_letters)
            # 界面显示用：让“吃剩余空间”的那个分区也能显示“约 X GB”，
            # 但 size_mb 仍然是 None —— diskpart 依旧不带 size= 执行。
            planned_parts = with_auto_estimates(planned_parts, disk.size_bytes)
        except PartitionLayoutError as exc:
            blockers.append(f"{title} 分区配置无效：{exc}")

        # 镜像能不能装机（类型/存在性/索引）；不能就把它变成阻断项，界面开始按钮会置灰。
        checks.append(
            CheckItem(
                key="image",
                label="镜像可应用性",
                passed=not image_problems,
                message=image_problems[0] if image_problems else "镜像可直接应用到分区",
            )
        )

        warnings.append(_DESTRUCTIVE_WARNING)
        warnings.append(_CANCEL_WARNING)

        plan = ExecutionPlan(
            disk_number=disk.disk_number,
            label=title,
            model=disk.model or "",
            size_display=disk.size_display,
            identity=identity,
            drive_letters=dict(drive_letters),
            warnings=tuple(warnings),
            checks=tuple(checks),
            blockers=tuple(dict.fromkeys(blockers)),
            partition_plan=tuple(part.to_dict() for part in planned_parts),
            unallocated_bytes=unallocated_bytes(planned_parts, disk.size_bytes),
            # 声明式编排：前端按分组渲染（分区组含独立的「分区验收」）。
            phases=tuple(phases_payload()),
        )
        spec = JobSpec(
            disk_number=disk.disk_number,
            task_payload=task_payload,
            expected_identity=identity,
            drive_letters=dict(drive_letters),
            runner_options=self._runner_options(),
        )
        return PreparedJob(spec=spec, plan=plan, disk=disk)

    # ------------------------------------------------------------------ 执行
    def _image_problems_for(self, payload: Mapping[str, Any] | None) -> list[str]:
        """按当前会话重算镜像可用性（含“Ghost 可执行文件在不在”）。"""
        return _image_problems(payload, ghost_available=self._ghost_available())

    def _ghost_available(self) -> bool:
        """Ghost 可执行文件在不在：先看设置里的 extGhost，再退回 PATH/System32。"""
        configured = str(self._tool_paths.get("ghost") or "").strip()
        if configured:
            return Path(configured).expanduser().is_file()
        return bool(find_program("ghost64.exe") or find_program("ghost32.exe"))

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
            self._sync_service_policy()
            self._sequencer.set_order(sorted(job.disk_number for job in jobs))
            try:
                self._manager.confirm_batch(
                    [job.id for job in jobs],
                    interval=self._worker_interval if interval is None else interval,
                    max_parallel=(
                        self._max_parallel if max_parallel is None else max_parallel
                    )
                    or len(jobs),
                )
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
        return jobs

    def confirm(self, job_ids: Sequence[str] | None = None) -> list[Job]:
        """确认等待中的任务；存在阻断项时拒绝执行。

        确认动作本身（界面勾选“明白分区会被删除”）即为授权，不再要求额外
        手输盘号：多盘场景下盘号各不相同，逐盘输入的闸门无法成立。
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

        # 用户确认（勾选“明白分区会被删除”）本身就是授权：这里重新补齐隐式白名单。
        # 否则“刷新页面（新会话）/再 prepare 别的盘”会把之前那次 prepare 的隐式授权
        # 撤掉，已排队的任务确认时报“不在允许写盘的白名单内（当前白名单: 空）”。
        real_disks = [job.disk_number for job in waiting]
        if real_disks:
            self.authorize_disks(real_disks)

        # 共享的 service 策略在这里一次性对齐（并发任务不能各自再改）。
        self._sync_service_policy()
        # 分区组按盘号升序串行；系统组（Ghost/引导）各自并行。
        self._sequencer.set_order(sorted(job.disk_number for job in waiting))
        parallel = self._max_parallel or len(waiting)

        try:
            self._manager.confirm_batch(
                [job.id for job in waiting],
                interval=self._worker_interval,
                max_parallel=parallel,
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
        if cancelled:
            # 唤醒排队中的盘，让它们立刻看到取消信号（否则要等一个轮询周期）。
            self._sequencer.cancel_waiting()
        return cancelled

    def wait_all(self, timeout: float | None = None) -> list[Job]:
        return self._manager.wait_all(timeout=timeout)

    # ------------------------------------------------------------------ 内部
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
