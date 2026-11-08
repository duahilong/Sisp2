"""任务编排单元：``JobSpec`` / ``Job`` / ``ExecutionPlan`` / ``JobManager``。

这一层只回答“什么时候、按什么顺序、以多大并发去跑”，不关心具体怎么
分区或写镜像——真正的执行放在 ``SispService`` 里，通过可注入的
``executor`` 调进来，所以任务管理可以脱离磁盘环境做单元测试。

状态机（``JobStatus``）::

    pending ──submit(默认)──> waiting_confirmation ──confirm_batch──> running
       │                              │                                │
       └──────cancel──────────────────┴────────────cancel───────────────┤
                                                                        ├─> ok
                                                                        └─> failed
    未开始即取消: cancelled；执行器判定“无需执行”: skipped

三条约定：

- **默认停在等待确认**：``submit()`` 不会自动开跑，必须显式
  ``confirm_batch()``，避免界面误触直接清盘；
- **取消是分档的**：``cancel()`` 只置位 ``cancel_event`` 并立刻返回；排队/等分区的任务
  一个命令都不跑，分区在步骤边界停，写镜像的 Ghost 会被执行层直接终止。
- **事件是旁路**：``subscribe()`` 的回调在 ``jobs_lock`` 之外调用，
  回调抛异常只记日志，不影响任务状态；
- **提交/确认幂等**：同一块盘同时只允许一个未结束任务，``confirm_batch()``
  会在锁内认领任务，重复的 ``submit`` / ``confirm`` 请求被拒绝。
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from app.application.steps import STEP_KEYS, group_progress
from app.models import WorkerResult

_LOGGER = logging.getLogger(__name__)

# 回调签名：executor(spec, cancel_event) -> WorkerResult
# 执行器也可以额外接受 ``on_step`` 关键字参数来上报进度；
# 只收两个参数的旧执行器继续可用（见 ``JobManager._execute``）。
Executor = Callable[..., WorkerResult]

# 进度回调能看到的状态：running 表示“这一步正在做”，其余表示“这一步结束了”。
STEP_RUNNING = "running"


def _step_keys() -> tuple[str, ...]:
    """真实写盘的步骤键（``efi/windows/data/layout/image/boot``）。

    从 :mod:`app.application.steps` 取：步骤契约是纯数据，进度层不再反向
    import 执行器（以前 ``jobs`` 依赖 ``real_runner.STEP_TITLES``）。
    """
    return STEP_KEYS


def _step_total() -> int:
    """一次真实写盘的总步数（六步，含独立的「分区验收」）。"""
    return len(STEP_KEYS)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobStatus(str, Enum):
    """任务状态；值直接进 JSON，界面对字符串做比较。"""

    PENDING = "pending"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in _FINISHED_STATUSES

    @property
    def is_waiting(self) -> bool:
        return self in (JobStatus.PENDING, JobStatus.WAITING_CONFIRMATION)


_FINISHED_STATUSES = frozenset(
    {JobStatus.OK, JobStatus.FAILED, JobStatus.SKIPPED, JobStatus.CANCELLED}
)


@dataclass(frozen=True)
class ExecutionPlan:
    """给界面看的执行计划；``blockers`` 非空时 workflow 会拒绝执行。"""

    disk_number: int
    model: str = ""
    size_display: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    drive_letters: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    warnings: tuple[str, ...] = ()
    checks: tuple[Any, ...] = ()
    blockers: tuple[str, ...] = ()
    # 这块盘要建哪些分区（role/label/文件系统/大小/盘符）；界面第 4 步直接渲染。
    partition_plan: tuple[dict[str, Any], ...] = ()
    # 按计划建完之后剩下的未分配空间；界面提示“容量会闲置”。
    unallocated_bytes: int = 0
    # 声明式编排（``steps.phases_payload()``）：前端按它渲染分组进度，
    # 分区组（含「分区验收」）与系统组在界面上是两段，不再硬编码步骤表。
    phases: tuple[dict[str, Any], ...] = ()

    @property
    def executable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "model": self.model,
            "size_display": self.size_display,
            "identity": dict(self.identity),
            "drive_letters": dict(self.drive_letters),
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
            "checks": [
                check.to_dict() if hasattr(check, "to_dict") else dict(check)
                for check in self.checks
            ],
            "blockers": list(self.blockers),
            "partition_plan": [dict(item) for item in self.partition_plan],
            "unallocated_bytes": self.unallocated_bytes,
            "phases": [dict(item) for item in self.phases],
            "executable": self.executable,
        }


@dataclass(frozen=True)
class JobSpec:
    """交给执行器的输入；必须可 JSON 序列化，便于落盘与重放。"""

    disk_number: int
    # 这次装机要用的全部参数（形状见 ``app.application.payload.TaskPayload``）。
    task_payload: dict[str, Any] = field(default_factory=dict)
    expected_identity: dict[str, Any] = field(default_factory=dict)
    drive_letters: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    # 执行器的可选项（临时目录、无人值守应答文件、驱动目录等）；
    # 由界面设置注入，不写进业务配置文件。
    runner_options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "task_payload": dict(self.task_payload),
            "expected_identity": dict(self.expected_identity),
            "drive_letters": dict(self.drive_letters),
            "dry_run": self.dry_run,
            "runner_options": dict(self.runner_options),
        }


class Job:
    """一个任务的运行时状态；可变对象，由 ``JobManager`` 负责加锁更新。"""

    def __init__(self, job_id: str, spec: JobSpec, plan: ExecutionPlan) -> None:
        self.id = job_id
        self.spec = spec
        self.plan = plan
        self.status: JobStatus = JobStatus.PENDING
        self.result: WorkerResult | None = None
        self.error: str | None = None
        self.created_at = _now()
        self.updated_at = self.created_at
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.cancel_reason: str | None = None
        self.cancel_event = threading.Event()
        # 进度：只放小字段，界面直接画进度条/阶段列表。
        self.progress: dict[str, Any] = {
            "total": 0,
            "completed": [],
            "current": None,
            "percent": 0,
            "message": "",
        }
        # 同一任务只允许被 confirm_batch 认领一次，防止双击/重试导致重复写同一块盘。
        self.scheduled = False

    @property
    def disk_number(self) -> int:
        return self.spec.disk_number

    @property
    def finished(self) -> bool:
        return self.status.is_finished

    @property
    def cancelled(self) -> bool:
        return self.status == JobStatus.CANCELLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "disk_number": self.disk_number,
            "status": self.status.value,
            "dry_run": bool(self.spec.dry_run),
            "plan": self.plan.to_dict(),
            "spec": self.spec.to_dict(),
            "result": self.result.to_dict() if self.result is not None else None,
            "error": self.error,
            "cancel_reason": self.cancel_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
        }


@dataclass(frozen=True)
class JobEvent:
    """推给界面的事件；``payload`` 只放小字段，完整任务状态走 ``jobs`` 列表。"""

    type: str
    job_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "job_id": self.job_id,
            "payload": dict(self.payload),
            "seq": self.seq,
            "timestamp": self.timestamp,
        }


class JobManager:
    """创建任务、推进状态、按并发/间隔调度，并广播事件。

    ``executor`` 不传时使用默认实现：懒加载 ``SispService().execute_spec``。
    默认服务是 dry-run 且没有任何写盘白名单，因此**真实执行会失败**——
    这是刻意的：真跑必须显式注入 ``SispService(dry_run=False, ...)`` 或自定义执行器。
    """

    def __init__(
        self,
        executor: Executor | None = None,
        *,
        event_dir: str | Path | None = None,
    ) -> None:
        self._executor = executor
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._all_done = threading.Condition(self._lock)
        self._subscribers: list[Callable[[JobEvent], None]] = []
        self._seq = 0
        self._event_dir = Path(event_dir) if event_dir else None
        self._event_file: Path | None = None
        if self._event_dir is not None:
            self._event_file = self._event_dir / "jobs.jsonl"

    # ------------------------------------------------------------------ 查询
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    # ------------------------------------------------------------------ 事件
    def subscribe(self, callback: Callable[[JobEvent], None]) -> Callable[[], None]:
        """注册事件回调，返回取消订阅函数（重复调用安全）。"""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def _emit(self, event_type: str, job: Job, **payload: Any) -> None:
        with self._lock:
            self._seq += 1
            event = JobEvent(
                type=event_type,
                job_id=job.id,
                payload=payload,
                seq=self._seq,
            )
            subscribers = list(self._subscribers)

        self._persist_event(event)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                _LOGGER.exception("任务事件回调失败: %s", event_type)

    def _persist_event(self, event: JobEvent) -> None:
        if self._event_file is None:
            return
        try:
            self._event_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            with self._event_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            _LOGGER.warning("任务事件落盘失败: %s", self._event_file, exc_info=True)

    # ------------------------------------------------------------------ 创建
    def submit(
        self,
        spec: JobSpec,
        plan: ExecutionPlan,
        *,
        require_confirmation: bool = True,
    ) -> Job:
        """登记任务；默认停在 ``waiting_confirmation``，等 ``confirm_batch``。"""
        job_id = f"disk{spec.disk_number}-{uuid.uuid4().hex[:8]}"
        job = Job(job_id, spec, plan)
        job.status = (
            JobStatus.WAITING_CONFIRMATION if require_confirmation else JobStatus.PENDING
        )
        with self._lock:
            # 同一块盘同时只允许一个未结束任务，避免重复点击/重复调用 submit
            # 产生两个任务，后续 confirm 时并发清同一块盘。
            for existing in self._jobs.values():
                if (
                    existing.disk_number == spec.disk_number
                    and not existing.status.is_finished
                ):
                    raise ValueError(
                        f"硬盘 {spec.disk_number} 已有未结束任务（{existing.id}），"
                        "拒绝重复提交"
                    )
            self._jobs[job_id] = job
        self._emit("job.created", job, status=job.status.value, disk_number=spec.disk_number)
        return job

    # ------------------------------------------------------------------ 执行
    def confirm_batch(
        self,
        job_ids: Sequence[str],
        *,
        interval: float = 0.0,
        max_parallel: int = 1,
    ) -> list[Job]:
        """确认一批任务并开始调度；立即返回，不阻塞调用方。

        只接受处于 ``pending`` / ``waiting_confirmation`` 的任务；
        有任何一个状态不对就整体拒绝（``ValueError``），避免“确认了一半”。
        """
        interval = max(float(interval), 0.0)
        max_parallel = max(int(max_parallel), 1)

        with self._lock:
            selected: list[Job] = []
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if job is None:
                    raise ValueError(f"任务不存在: {job_id}")
                if not job.status.is_waiting:
                    raise ValueError(f"任务 {job_id} 当前状态不允许确认: {job.status.value}")
                if job.scheduled:
                    raise ValueError(f"任务 {job_id} 已确认，不能重复确认")
                selected.append(job)
            if not selected:
                raise ValueError("没有可确认的任务")
            # 在锁内一次性认领，后续重复的 confirm 请求会在上面的检查处失败。
            for job in selected:
                job.scheduled = True
                job.updated_at = _now()
            schedulable = list(selected)

        dispatcher = threading.Thread(
            target=self._dispatch,
            args=(schedulable, interval, max_parallel),
            name="job-dispatcher",
            daemon=True,
        )
        try:
            dispatcher.start()
        except BaseException:
            with self._lock:
                for job in schedulable:
                    job.scheduled = False
            raise
        return selected

    def _dispatch(self, jobs: list[Job], interval: float, max_parallel: int) -> None:
        semaphore = threading.Semaphore(max_parallel)
        threads: list[threading.Thread] = []
        for job in jobs:
            if job.cancel_event.is_set():
                continue
            semaphore.acquire()
            thread = threading.Thread(
                target=self._run_job,
                args=(job, semaphore),
                name=f"job-{job.id}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
            if interval:
                # 启动间隔只影响“开跑”，不阻塞已开跑的任务。
                threading.Event().wait(interval)
        for thread in threads:
            thread.join()

    def _run_job(self, job: Job, semaphore: threading.Semaphore) -> None:
        try:
            if job.cancel_event.is_set():
                self._finish(job, JobStatus.CANCELLED, error=job.cancel_reason or "任务已取消")
                return
            with self._lock:
                job.status = JobStatus.RUNNING
                job.started_at = _now()
                job.updated_at = job.started_at
            self._emit("job.started", job, disk_number=job.disk_number, dry_run=job.spec.dry_run)

            try:
                result = self._execute(job)
            except Exception as exc:
                _LOGGER.exception("任务执行失败: %s", job.id)
                self._finish(job, JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
                return

            if result is None:
                self._finish(job, JobStatus.FAILED, error="执行器没有返回结果")
                return

            status = JobStatus.OK if result.success else JobStatus.FAILED
            if job.cancel_event.is_set():
                # 用户已请求取消：即使执行器返回失败（例如“任务已取消（镜像未写入）”），
                # 任务状态也应是 cancelled；失败详情仍保留在 result 中。
                status = JobStatus.CANCELLED
            self._finish(job, status, result=result, error=result.error)
        finally:
            semaphore.release()

    def _make_reporter(self, job: Job) -> Callable[..., None]:
        """构造 ``on_step`` 回调：更新 ``Job.progress`` 并广播 ``job.step``。

        只有真实写盘的七个阶段算进度（含独立的「分区验收」）；像
        ``diskpart_preview`` 这种演练期附加步骤照样发事件，但不进 ``completed``。
        """
        total = _step_total()
        keys = list(_step_keys())
        stages = set(keys)

        def report(
            key: str,
            status: str,
            message: str = "",
            fraction: float | None = None,
        ) -> None:
            counted = not stages or key in stages
            with self._lock:
                completed = list(job.progress.get("completed") or [])
                if not counted:
                    current = job.progress.get("current")
                elif status == STEP_RUNNING:
                    current: str | None = key
                else:
                    current = None
                    if key not in completed:
                        completed.append(key)
                percent = round(len(completed) * 100 / total) if total else 0
                # 同一个任务里进度只允许前进：上一次报过的 percent 也要参与 max，
                # 否则重试/低百分比回调会把它拉回去。
                previous = int(job.progress.get("percent") or 0)
                percent = max(percent, previous)
                # 子步骤进度（例如 Ghost 自己的百分比）：把"当前步"按 fraction
                # 折进总进度。只允许前进：重试时从 3% 回到 0% 会让界面看起来
                # 像卡住或倒退。是否多传这一个参数由 PhaseContext 按回调签名决定。
                if counted and status == STEP_RUNNING and fraction is not None and total:
                    ratio = min(max(float(fraction), 0.0), 1.0)
                    try:
                        index = keys.index(key)
                    except ValueError:
                        index = 0
                    percent = max(percent, round((index + ratio) * 100 / total))
                job.progress = {
                    "total": total,
                    "completed": completed,
                    "current": current,
                    "percent": min(percent, 100),
                    "message": message,
                    # 声明式分组进度：前端按"硬盘分区 / 写入系统"两段渲染，
                    # 分区失败时后面一段可以直接标成"未执行"。
                    "groups": group_progress(completed, current),
                }
                job.updated_at = _now()
                snapshot = dict(job.progress)
            self._emit(
                "job.step",
                job,
                key=key,
                status=status,
                message=message,
                progress=snapshot,
            )

        return report

    def _execute(self, job: Job) -> WorkerResult:
        executor = self._executor or self._default_executor()
        if executor is None:
            raise RuntimeError("No executor")
        reporter = self._make_reporter(job)
        try:
            accepts_step = "on_step" in inspect.signature(executor).parameters
        except (TypeError, ValueError):
            accepts_step = False
        if accepts_step:
            return executor(job.spec, job.cancel_event, on_step=reporter)
        return executor(job.spec, job.cancel_event)

    def _default_executor(self) -> Executor | None:
        if self._executor is None:
            from app.application.service import SispService

            service = SispService()
            self._executor = service.execute_spec
        return self._executor

    def _finish(
        self,
        job: Job,
        status: JobStatus,
        *,
        result: WorkerResult | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            job.status = status
            job.result = result
            job.error = error
            job.finished_at = _now()
            job.updated_at = job.finished_at
            if status == JobStatus.OK:
                job.progress = {**job.progress, "current": None, "percent": 100}
            else:
                job.progress = {**job.progress, "current": None}
        self._emit(
            "job.finished",
            job,
            status=status.value,
            success=bool(result.success) if result is not None else False,
            error=error,
        )
        # 先广播再唤醒等待者：wait_all() 返回时 finished 事件一定已经发出。
        with self._lock:
            self._all_done.notify_all()

    # ------------------------------------------------------------------ 取消
    def cancel(self, job_id: str, reason: str = "用户取消") -> bool:
        """请求取消：等待中的直接取消，运行中的置位协作标志。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status.is_finished:
                return False
            job.cancel_reason = reason
            job.cancel_event.set()
            if job.status.is_waiting:
                job.status = JobStatus.CANCELLED
                job.error = reason
                job.finished_at = _now()
                job.updated_at = job.finished_at
                immediately = True
            else:
                immediately = False
        if immediately:
            self._emit("job.cancelled", job, reason=reason)
            with self._lock:
                self._all_done.notify_all()
        else:
            self._emit("job.cancelling", job, reason=reason)
        return True

    # ------------------------------------------------------------------ 等待
    def wait_all(self, timeout: float | None = None) -> list[Job]:
        """等到所有任务结束或超时；返回当前任务列表（可能仍在运行）。"""
        with self._all_done:
            self._all_done.wait_for(
                lambda: not self._jobs or all(job.finished for job in self._jobs.values()),
                timeout=timeout,
            )
            return list(self._jobs.values())
