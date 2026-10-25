"""项目内部使用的结构化数据模型。

模块边界仍然以 dict 为主，避免一次性重构破坏现有调用；新增的
运行上下文、步骤记录和 worker 结果使用 dataclass，方便日志与汇总。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class StepRecord:
    """单个流程步骤的执行记录。"""

    name: str
    passed: bool
    message: str
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StepRecord:
        return cls(
            name=str(payload.get("name") or ""),
            passed=bool(payload.get("passed")),
            message=str(payload.get("message") or ""),
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
        )


@dataclass
class RunContext:
    """一次单盘执行的上下文，用于记录步骤、耗时和日志关联信息。"""

    disk_number: int | None = None
    dry_run: bool = False
    steps: list[StepRecord] = field(default_factory=list)
    reporter: Any | None = None
    _step_started: dict[str, float] = field(default_factory=dict, repr=False)

    def start_step(self, name: str) -> None:
        self._step_started[name] = time.perf_counter()

    def record_step(self, name: str, passed: bool, message: str) -> StepRecord:
        started = self._step_started.pop(name, None)
        duration = max(time.perf_counter() - started, 0.0) if started is not None else 0.0
        record = StepRecord(name=name, passed=passed, message=message, duration_seconds=round(duration, 3))
        self.steps.append(record)
        return record

    @property
    def passed(self) -> bool:
        return all(step.passed for step in self.steps)

    def failed_steps(self) -> list[StepRecord]:
        return [step for step in self.steps if not step.passed]


@dataclass(frozen=True)
class WorkerResult:
    """worker 进程的最终结果，会写入结果文件供主窗口汇总。"""

    disk_number: int
    success: bool
    error_code: int
    message: str
    steps: tuple[StepRecord, ...] = ()
    log_path: str | None = None
    started_at: str = ""
    finished_at: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "success": self.success,
            "error_code": self.error_code,
            "message": self.message,
            "steps": [step.to_dict() for step in self.steps],
            "log_path": self.log_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkerResult:
        raw_steps = payload.get("steps") or []
        steps = tuple(StepRecord.from_dict(item) for item in raw_steps if isinstance(item, dict))
        return cls(
            disk_number=int(payload.get("disk_number") or 0),
            success=bool(payload.get("success")),
            error_code=int(payload.get("error_code") or 0),
            message=str(payload.get("message") or ""),
            steps=steps,
            log_path=payload.get("log_path"),
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            dry_run=bool(payload.get("dry_run")),
        )
