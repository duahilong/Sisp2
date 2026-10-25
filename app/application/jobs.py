"""任务管理与实时事件。

``JobManager`` 是 GUI 与执行流程之间的薄层：

1. ``submit`` 创建任务并进入 ``WAITING_CONFIRMATION``，由界面展示
   ``ExecutionPlan``；
2. ``confirm`` 才真正启动执行线程；
3. 执行期间通过 ``JobEvent`` 推送日志、步骤和结果事件；
4. ``cancel`` 发出协作取消信号，流程在步骤边界停止。

现有 CLI 不依赖本模块，因此引入任务层不会改变命令行行为。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.application.cancellation import CancellationToken, JobCancelled
from app.application.events import EventLog, JobEvent, JobEventType, now_iso
from app.logging_setup import get_log_path, get_logger
from app.models import RunContext, StepRecord, WorkerResult
from app.modules.common.constants import ErrorCode
from app.modules.common.service import SispError

DEFAULT_PLAN_STEPS = (
    "初始化硬盘",
    "验证初始化结果",
    "分区并格式化",
    "验证分区结果",
    "写入 Ghost 镜像",
    "拷贝目录",
    "创建引导记录",
)


class JobStatus(StrEnum):
    """任务状态。"""

    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


@dataclass(frozen=True)
class JobSpec:
    """执行任务所需的输入。"""

    disk_number: int
    config_payload: dict[str, Any]
    expected_identity: dict[str, Any] | None = None
    drive_letters: dict[str, str] | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "config_path": self.config_payload.get("config_path"),
            "expected_identity": self.expected_identity,
            "drive_letters": self.drive_letters,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """供确认界面展示的执行计划快照。"""

    disk_number: int
    steps: tuple[str, ...] = DEFAULT_PLAN_STEPS
    model: str = ""
    size_display: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    drive_letters: dict[str, str] = field(default_factory=dict)
    config_path: str | None = None
    dry_run: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "steps": list(self.steps),
            "model": self.model,
            "size_display": self.size_display,
            "identity": dict(self.identity),
            "drive_letters": dict(self.drive_letters),
            "config_path": self.config_path,
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_spec(cls, spec: JobSpec) -> ExecutionPlan:
        identity = dict(spec.expected_identity or {})
        drive_letters = dict(spec.drive_letters or {})
        config_path = spec.config_payload.get("config_path")
        return cls(
            disk_number=spec.disk_number,
            identity=identity,
            drive_letters=drive_letters,
            model=str(identity.get("model") or ""),
            config_path=str(config_path) if config_path else None,
            dry_run=spec.dry_run,
        )

    @classmethod
    def from_disk(
        cls,
        disk: dict[str, Any],
        *,
        config_payload: dict[str, Any],
        drive_letters: dict[str, str] | None = None,
        expected_identity: dict[str, Any] | None = None,
        dry_run: bool = False,
        warnings: Sequence[str] | None = None,
    ) -> ExecutionPlan:
        identity = dict(expected_identity or {})
        if not identity:
            identity = {
                "disk_number": disk.get("disk_number"),
                "unique_id": disk.get("unique_id"),
                "serial_number": disk.get("serial_number"),
                "model": disk.get("model"),
                "size_bytes": disk.get("size_bytes"),
            }
        config_path = config_payload.get("config_path")
        return cls(
            disk_number=int(disk.get("disk_number") or 0),
            model=str(disk.get("model") or ""),
            size_display=str(disk.get("size_display") or ""),
            identity=identity,
            drive_letters=dict(drive_letters or {}),
            config_path=str(config_path) if config_path else None,
            dry_run=dry_run,
            warnings=tuple(warnings or ()),
        )


@dataclass
class Job:
    """一个硬盘任务的运行时状态。"""

    id: str
    spec: JobSpec
    plan: ExecutionPlan
    status: JobStatus = JobStatus.WAITING_CONFIRMATION
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    steps: tuple[StepRecord, ...] = ()
    result: WorkerResult | None = None
    error: str | None = None
    events: list[JobEvent] = field(default_factory=list)
    cancel_token: CancellationToken = field(default_factory=CancellationToken, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "spec": self.spec.to_dict(),
            "plan": self.plan.to_dict(),
            "error": self.error,
            "result": self.result.to_dict() if self.result else None,
            "steps": [step.to_dict() for step in self.steps],
        }


JobRunner = Callable[[JobSpec, RunContext], None]
EventSubscriber = Callable[[JobEvent], None]


class JobReporter:
    """把 Reporter 输出转换成任务事件。"""

    def __init__(self, manager: JobManager, job_id: str) -> None:
        self._manager = manager
        self._job_id = job_id

    def line(self, text: str = "") -> None:
        self._manager._emit(self._job_id, JobEventType.LOG, {"text": text})

    def step_started(self, index: int, total: int, name: str) -> None:
        self._manager._emit(
            self._job_id,
            JobEventType.STEP_STARTED,
            {"index": index, "total": total, "name": name},
        )

    def step_finished(self, index: int, total: int, name: str, passed: bool, message: str) -> None:
        self._manager._emit(
            self._job_id,
            JobEventType.STEP_FINISHED,
            {
                "index": index,
                "total": total,
                "name": name,
                "passed": passed,
                "message": message,
            },
        )


def _default_runner(spec: JobSpec, context: RunContext) -> None:
    # 延迟导入，避免 application 层与 orchestrator 在模块加载时形成环。
    from app.orchestrator import run_single_disk_flow

    run_single_disk_flow(
        spec.disk_number,
        spec.config_payload,
        expected_identity=spec.expected_identity,
        drive_letters=spec.drive_letters,
        run_context=context,
        dry_run=spec.dry_run,
        reporter=context.reporter,
        cancel_token=context.cancel_token,
    )


class JobManager:
    """创建、确认、执行和取消硬盘任务。"""

    def __init__(
        self,
        runner: JobRunner | None = None,
        *,
        event_dir: str | Path | None = None,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self._runner = runner or _default_runner
        self._event_dir = Path(event_dir) if event_dir else None
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._logs: dict[str, EventLog] = {}
        self._subscribers: list[EventSubscriber] = []
        self._counter = 0
        self._logger = get_logger("jobs")

    # ------------------------------------------------------------------ 查询
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def events(self, job_id: str, *, since: int = 0) -> tuple[JobEvent, ...]:
        with self._lock:
            job = self._require(job_id)
            return tuple(event for event in job.events if event.sequence > since)

    def subscribe(self, callback: EventSubscriber) -> Callable[[], None]:
        """订阅所有任务事件，返回取消订阅函数。"""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # ------------------------------------------------------------------ 生命周期
    def submit(
        self,
        spec: JobSpec,
        plan: ExecutionPlan | None = None,
        *,
        require_confirmation: bool = True,
    ) -> Job:
        """创建任务；默认进入等待确认状态。"""
        execution_plan = plan or ExecutionPlan.from_spec(spec)
        if execution_plan.disk_number != spec.disk_number:
            raise ValueError(
                f"计划硬盘编号与任务不一致: {execution_plan.disk_number} != {spec.disk_number}"
            )

        with self._lock:
            self._counter += 1
            job_id = f"job-{self._counter:04d}"
            job = Job(id=job_id, spec=spec, plan=execution_plan)
            self._jobs[job_id] = job
            if self._event_dir is not None:
                self._logs[job_id] = EventLog(self._event_dir / f"{job_id}.jsonl")

        self._emit(job_id, JobEventType.CREATED, {"plan": execution_plan.to_dict()})
        self._emit(job_id, JobEventType.AWAITING_CONFIRMATION, {"plan": execution_plan.to_dict()})
        if not require_confirmation:
            self.confirm(job_id)
        return job

    def confirm(self, job_id: str) -> Job:
        """用户确认计划后启动任务。"""
        with self._lock:
            job = self._require(job_id)
            if job.status == JobStatus.RUNNING:
                return job
            if job.status != JobStatus.WAITING_CONFIRMATION:
                raise ValueError(f"任务状态不允许确认: {job.status.value}")

            job.status = JobStatus.RUNNING
            job.started_at = self._clock()
            thread = threading.Thread(
                target=self._run_job,
                args=(job,),
                name=f"sisp-{job.id}",
                daemon=True,
            )
            job.thread = thread

        self._emit(job_id, JobEventType.CONFIRMED, {})
        self._emit(job_id, JobEventType.STARTED, {"started_at": job.started_at})
        thread.start()
        return job

    def cancel(self, job_id: str, reason: str = "用户取消") -> bool:
        """请求取消任务。等待确认的任务立即取消，运行中的任务协作取消。"""
        with self._lock:
            job = self._require(job_id)
            if job.status.is_terminal:
                return False
            if job.status == JobStatus.WAITING_CONFIRMATION:
                job.status = JobStatus.CANCELLED
                job.finished_at = self._clock()
                job.error = reason
                immediate = True
            else:
                if job.cancel_token.cancelled:
                    return False
                job.cancel_token.cancel()
                immediate = False

        self._emit(job_id, JobEventType.CANCEL_REQUESTED, {"reason": reason})
        if immediate:
            self._finish_cancelled(job, reason)
        return True

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        """等待任务线程结束并返回任务。"""
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"任务不存在: {job_id}")
        thread = job.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return job

    def wait_all(self, timeout: float | None = None) -> list[Job]:
        jobs = self.jobs()
        for job in jobs:
            self.wait(job.id, timeout=timeout)
        return jobs

    # ------------------------------------------------------------------ 执行
    def _run_job(self, job: Job) -> None:
        context = RunContext(
            disk_number=job.spec.disk_number,
            dry_run=job.spec.dry_run,
            reporter=JobReporter(self, job.id),
            cancel_token=job.cancel_token,
        )
        error_code = ErrorCode.SUCCESS
        message = "当前阶段处理完成" if not job.spec.dry_run else "dry-run 完成"
        success = True

        try:
            job.cancel_token.raise_if_cancelled()
            self._runner(job.spec, context)
            job.cancel_token.raise_if_cancelled()
        except JobCancelled as exc:
            success = False
            error_code = ErrorCode.CANCELLED
            message = str(exc)
        except KeyboardInterrupt:
            success = False
            error_code = ErrorCode.CANCELLED
            message = "操作已取消"
        except SispError as exc:
            success = False
            error_code = exc.error_code
            message = str(exc)
            self._logger.warning("任务 %s 失败: %s", job.id, exc)
        except Exception as exc:
            success = False
            error_code = ErrorCode.UNEXPECTED_ERROR
            message = f"{type(exc).__name__}: {exc}"
            self._logger.exception("任务 %s 未预期异常", job.id)

        with self._lock:
            job.steps = tuple(context.steps)
            job.finished_at = self._clock()
            job.result = self._build_result(job, success, error_code, message)
            job.error = None if success else message
            if success:
                job.status = JobStatus.SUCCEEDED
            elif error_code == ErrorCode.CANCELLED:
                job.status = JobStatus.CANCELLED
            else:
                job.status = JobStatus.FAILED

        if job.status == JobStatus.SUCCEEDED:
            self._emit(job.id, JobEventType.SUCCEEDED, self._result_payload(job))
        elif job.status == JobStatus.CANCELLED:
            self._emit(job.id, JobEventType.CANCELLED, self._result_payload(job))
        else:
            self._emit(job.id, JobEventType.FAILED, self._result_payload(job))
        self._emit(job.id, JobEventType.FINISHED, self._result_payload(job))

    def _finish_cancelled(self, job: Job, reason: str) -> None:
        with self._lock:
            job.result = self._build_result(job, False, ErrorCode.CANCELLED, reason)
            job.finished_at = job.finished_at or self._clock()
            job.error = reason
        self._emit(job.id, JobEventType.CANCELLED, self._result_payload(job))
        self._emit(job.id, JobEventType.FINISHED, self._result_payload(job))

    def _build_result(
        self,
        job: Job,
        success: bool,
        error_code: int,
        message: str,
    ) -> WorkerResult:
        log_path = get_log_path()
        return WorkerResult(
            disk_number=job.spec.disk_number,
            success=success,
            error_code=error_code,
            message=message,
            steps=tuple(job.steps),
            log_path=str(log_path) if log_path else None,
            started_at=job.started_at or "",
            finished_at=job.finished_at or self._clock(),
            dry_run=job.spec.dry_run,
        )

    @staticmethod
    def _result_payload(job: Job) -> dict[str, Any]:
        return {
            "status": job.status.value,
            "result": job.result.to_dict() if job.result else None,
            "error": job.error,
        }

    # ------------------------------------------------------------------ 事件
    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"任务不存在: {job_id}")
        return job

    def _emit(
        self,
        job_id: str,
        event_type: JobEventType,
        payload: dict[str, Any] | None = None,
    ) -> JobEvent:
        with self._lock:
            job = self._require(job_id)
            event = JobEvent(
                job_id=job_id,
                sequence=len(job.events) + 1,
                type=event_type.value,
                timestamp=self._clock(),
                payload=payload or {},
            )
            job.events.append(event)
            log = self._logs.get(job_id)
            if log is not None:
                try:
                    log.append(event)
                except OSError:
                    self._logger.exception("写入任务事件文件失败: %s", job_id)
            subscribers = list(self._subscribers)

        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                self._logger.exception("任务事件订阅者执行失败: %s", job_id)
        return event
