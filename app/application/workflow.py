"""GUI/CLI 共用的工作流适配层。

界面只负责“展示 + 用户操作”，不直接拼装配置、扫描、盘符分配和任务。
``WorkflowController`` 把一次完整操作串成：

1. ``start()``：运行前检查、读取配置、扫描并应用硬盘保护规则；
2. ``refresh_disks()``：重新扫描硬盘，不改变配置和检查结果；
3. ``prepare()``：执行写前校验，为每块盘生成 ``ExecutionPlan`` 和
   ``JobSpec``，并标明检查项与阻断项；
4. ``submit()`` / ``confirm()`` / ``cancel()``：交给 ``JobManager`` 执行；
5. ``results()``：汇总已结束任务的结果。

本模块不依赖 Qt，也不执行任何 PowerShell 或磁盘操作，因此可以直接用
普通单元测试覆盖。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.application.contracts import CheckItem, DiskInfo
from app.application.jobs import ExecutionPlan, Job, JobManager, JobSpec, JobStatus
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

_PLAN_STEP_KEYS = ("efi", "windows", "data1", "data2")
_DRY_RUN_WARNING = "dry-run 模式：只展示执行计划，不会执行任何写盘操作"
_DESTRUCTIVE_WARNING = "破坏性操作：将清空目标硬盘全部数据，请再次核对硬盘编号和身份"
_CANCEL_WARNING = "取消为协作式：当前步骤完成后才会停止"


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
    ) -> None:
        self._service = service or SispService()
        self._manager = job_manager or JobManager(event_dir=event_dir)
        self._preflight_runner = preflight_runner or run_preflight_checks
        self._dry_run = bool(dry_run)
        self._worker_interval = max(float(worker_interval), 0.0)
        self._max_parallel = max(int(max_parallel), 1)
        self._log_dir = log_dir
        self._result_dir = result_dir
        self._resource_resolver = resource_resolver
        self._session: SessionState | None = None
        self._prepared: dict[int, PreparedJob] = {}
        self._jobs: dict[str, PreparedJob] = {}

    # ------------------------------------------------------------------ 查询
    @property
    def session(self) -> SessionState | None:
        return self._session

    @property
    def job_manager(self) -> JobManager:
        return self._manager

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
            self._dry_run = bool(dry_run)

        try:
            report = self._preflight_runner(config_path)
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

    # ------------------------------------------------------------------ 计划
    def prepare(
        self,
        selected_disk_numbers: Sequence[int],
        *,
        dry_run: bool | None = None,
    ) -> list[PreparedJob]:
        """为选中硬盘生成执行计划和任务输入。"""
        session = self._require_session()
        if session.config_payload is None:
            raise WorkflowError("配置不可用，无法生成执行计划")

        selected = [int(number) for number in selected_disk_numbers]
        if not selected:
            raise WorkflowError("尚未选择任何硬盘")

        active_dry_run = self._dry_run if dry_run is None else bool(dry_run)
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
            prepared.append(
                self._build_prepared_job(
                    session,
                    disk,
                    drive_letters,
                    dry_run=active_dry_run,
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
    ) -> PreparedJob:
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
                    "已分配 EFI/Windows/Data1/Data2 盘符"
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
            config_payload=session.config_payload or {},
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
