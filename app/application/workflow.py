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
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.application.contracts import CheckItem, DiskInfo
from app.application.jobs import ExecutionPlan, Job, JobManager, JobSpec, JobStatus
from app.application.real_runner import build_real_runner
from app.application.service import SispService
from app.models import WorkerResult
from app.orchestrator import build_worker_disk_identity
from app.preflight import run_preflight_checks

PreflightRunner = Callable[..., dict[str, Any]]

_CHECK_KEYS = {
    "管理员权限检查": "admin",
    "PowerShell 可用性检查": "powershell",
    "配置文件存在性检查": "config_exists",
    "配置文件可解析性检查": "config_parse",
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
    ``data_size`` / ``data_size_gb``、``win_gho``。
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
            runner = build_real_runner(log_dir=self._log_dir)
        return SispService(
            dry_run=bool(self._dry_run),
            allowed_disk_numbers=self._allowed_disk_numbers or (),
            real_runner=runner,
        )

    def _execute_spec(self, spec: Any, cancel_event: threading.Event) -> WorkerResult:
        """``JobManager`` 的默认执行器：走本会话的 service，不再另造一个。"""
        self._sync_service_policy(dry_run=bool(getattr(spec, "dry_run", False)))
        return self._service.execute_spec(spec, cancel_event)

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
        try:
            signature = inspect.signature(self._preflight_runner)
        except (TypeError, ValueError):
            accepts_dry_run = False
        else:
            accepts_dry_run = (
                "dry_run" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
        if accepts_dry_run:
            return self._preflight_runner(config_path, dry_run=self._dry_run)
        return self._preflight_runner(config_path)

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

        try:
            report = self._run_preflight(config_path)
        except Exception as exc:
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
            f"{check.label}：{check.message}" for check in checks if not check.passed
        ]
        warnings: list[str] = []
        disks: tuple[DiskInfo, ...] = ()

        if payload is None:
            warnings.append("配置不可用，硬盘列表未应用配置中的排除规则")
        excluded_names = payload.get("excluded_disk_names") if payload else None
        try:
            disks = tuple(self._service.scan_protected_disks(excluded_names))
        except Exception as exc:
            blockers.append(f"硬盘扫描失败：{exc}")

        if payload is None:
            blockers.append("配置不可用，无法生成执行计划")
        if not disks:
            warnings.append("未检测到任何硬盘")
        elif not any(disk.is_selectable for disk in disks):
            warnings.append("当前没有可选硬盘，所有硬盘均被保护")

        session = SessionState(
            config_path=self._resolve_config_path(config_path, payload),
            config_payload=payload,
            checks=checks,
            disks=disks,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(warnings),
            dry_run=self._dry_run,
        )
        self._session = session
        self._prepared = {}
        # 会话是“这次运行的真实模式”，服务必须立刻跟上（覆盖构造时的默认值）。
        self._sync_service_policy(dry_run=self._dry_run)
        return session

    def refresh_disks(self) -> list[DiskInfo]:
        """重新扫描硬盘并应用保护规则。"""
        session = self._require_session()
        if session.config_payload is None:
            raise WorkflowError("配置不可用，无法刷新硬盘列表")

        try:
            disks = tuple(
                self._service.scan_protected_disks(
                    session.config_payload.get("excluded_disk_names")
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
        selected_disk_numbers: Sequence[int] | Mapping[int, Mapping[str, Any]],
        *,
        dry_run: bool | None = None,
    ) -> list[PreparedJob]:
        """为选中硬盘生成执行计划和任务输入。

        ``selected_disk_numbers`` 可以是硬盘编号序列，也可以是
        ``{硬盘编号: {"c_size": 400, "data_size": 800}}`` 形式的每盘覆盖。
        """
        session = self._require_session()
        if session.config_payload is None:
            raise WorkflowError("配置不可用，无法生成执行计划")

        overrides: dict[int, dict[str, Any]] = {}
        if isinstance(selected_disk_numbers, Mapping):
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
        self._sync_service_policy(dry_run=active_dry_run)
        implicit_allowlist = False
        if not active_dry_run and self._explicit_allowlist() is None:
            merged = set(self._service.allowed_disk_numbers)
            merged.update(int(number) for number in selected)
            self._service.allowed_disk_numbers = frozenset(merged)
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
            payload = _merge_payload(session.config_payload, overrides.get(disk_number))
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
        blockers: list[str] = list(session.blockers)
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
        )
        return PreparedJob(spec=spec, plan=plan, disk=disk)

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

    def confirm(self, job_ids: Sequence[str] | None = None) -> list[Job]:
        """确认等待中的任务；存在阻断项时拒绝执行。"""
        jobs = self._select_jobs(job_ids)
        waiting = [job for job in jobs if job.status == JobStatus.WAITING_CONFIRMATION]
        if not waiting:
            return jobs

        blocked = [job for job in waiting if job.plan.blockers]
        if blocked:
            details = "；".join(
                f"硬盘 {job.plan.disk_number}: {'；'.join(job.plan.blockers)}"
                for job in blocked
            )
            raise WorkflowError(f"计划存在阻断项，拒绝执行：{details}")

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
