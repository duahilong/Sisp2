"""工作线程与任务层共用的结果模型。

一次硬盘操作跑完之后，界面/CLI/日志只认一个东西：``WorkerResult``。
它回答三个问题——这块盘整体成没成、每一步做了什么、失败时错在哪。

约定：

- ``success`` 只表示“这次任务整体是否成功”。单步失败时 ``steps`` 仍然完整，
  界面据此定位是分区、格式化还是写镜像出的问题；
- dry-run 任务同样返回 ``WorkerResult``，但 ``dry_run=True``、每个步骤的
  ``status`` 为 ``skipped``，``details`` 里带“将要执行什么”的预览；
- 对象只装可序列化数据，不含文件句柄、线程或 Qt 控件，``to_dict()``
  的结果可以直接塞进 JSON 响应或日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 单步状态：任务整体成功与否看 ``WorkerResult.success``，不看这三个值。
STEP_OK = "ok"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


@dataclass(frozen=True)
class StepResult:
    """任务里的一步，例如“清盘并重建分区表”“应用镜像”。

    ``key`` 用稳定的英文短标识（``partition`` / ``image``），便于界面按 key
    取图标；``title`` 是给人看的中文短标题。
    """

    key: str
    title: str
    status: str = STEP_OK
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STEP_OK

    @property
    def failed(self) -> bool:
        return self.status == STEP_FAILED

    @property
    def skipped(self) -> bool:
        return self.status == STEP_SKIPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepResult:
        details = data.get("details")
        return cls(
            key=str(data.get("key") or ""),
            title=str(data.get("title") or ""),
            status=str(data.get("status") or STEP_OK),
            message=str(data.get("message") or ""),
            details=dict(details) if isinstance(details, dict) else {},
        )


@dataclass(frozen=True)
class WorkerResult:
    """一次硬盘操作的完整结果。"""

    disk_number: int
    success: bool
    message: str = ""
    dry_run: bool = False
    steps: tuple[StepResult, ...] = ()
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float = 0.0

    @classmethod
    def dry_run_plan(
        cls,
        disk_number: int,
        steps: tuple[StepResult, ...] | list[StepResult] = (),
        message: str = "dry-run 模式：仅生成执行计划，未执行任何命令",
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_seconds: float = 0.0,
    ) -> WorkerResult:
        """构造一份 dry-run 结果：整体成功，但所有步骤都是“未执行”。"""
        return cls(
            disk_number=disk_number,
            success=True,
            message=message,
            dry_run=True,
            steps=tuple(steps),
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
        )

    @classmethod
    def failure(
        cls,
        disk_number: int,
        error: str,
        *,
        message: str = "",
        dry_run: bool = False,
        steps: tuple[StepResult, ...] | list[StepResult] = (),
    ) -> WorkerResult:
        """构造一份失败结果；``error`` 保留原始异常文本方便排查。"""
        return cls(
            disk_number=disk_number,
            success=False,
            message=message or error,
            dry_run=dry_run,
            steps=tuple(steps),
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "disk_number": self.disk_number,
            "success": self.success,
            "message": self.message,
            "dry_run": self.dry_run,
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(float(self.duration_seconds), 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerResult:
        raw_steps = data.get("steps")
        steps = tuple(
            StepResult.from_dict(item)
            for item in (raw_steps if isinstance(raw_steps, (list, tuple)) else ())
            if isinstance(item, dict)
        )
        try:
            duration = float(data.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        return cls(
            disk_number=int(data.get("disk_number") or 0),
            success=bool(data.get("success")),
            message=str(data.get("message") or ""),
            dry_run=bool(data.get("dry_run")),
            steps=steps,
            error=str(data["error"]) if data.get("error") else None,
            started_at=str(data["started_at"]) if data.get("started_at") else None,
            finished_at=str(data["finished_at"]) if data.get("finished_at") else None,
            duration_seconds=duration,
        )
