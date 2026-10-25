"""任务事件与事件日志。

事件使用 JSON Lines 保存，一行一个事件，方便 GUI 实时读取、日志留档和
worker 进程与主进程之间传递进度。内存中的 ``JobManager`` 与文件事件日志
共享同一份 ``JobEvent`` 结构。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

EVENT_LOG_VERSION = 1


class JobEventType(StrEnum):
    """任务事件类型。"""

    CREATED = "job.created"
    AWAITING_CONFIRMATION = "job.awaiting_confirmation"
    CONFIRMED = "job.confirmed"
    STARTED = "job.started"
    LOG = "job.log"
    STEP_STARTED = "job.step_started"
    STEP_FINISHED = "job.step_finished"
    CANCEL_REQUESTED = "job.cancel_requested"
    SUCCEEDED = "job.succeeded"
    FAILED = "job.failed"
    CANCELLED = "job.cancelled"
    FINISHED = "job.finished"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class JobEvent:
    """单个任务事件。"""

    job_id: str
    sequence: int
    type: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)
    version: int = EVENT_LOG_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "sequence": self.sequence,
            "type": self.type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobEvent:
        raw_payload = payload.get("payload")
        return cls(
            job_id=str(payload.get("job_id") or ""),
            sequence=int(payload.get("sequence") or 0),
            type=str(payload.get("type") or ""),
            timestamp=str(payload.get("timestamp") or ""),
            payload=raw_payload if isinstance(raw_payload, dict) else {},
            version=int(payload.get("version") or EVENT_LOG_VERSION),
        )


class EventLog:
    """追加写入单个任务的事件文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, event: JobEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = event.to_json() + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)


def read_events(path: str | Path, *, strict: bool = False) -> list[JobEvent]:
    """读取事件文件。

    默认跳过空行和损坏行（例如进程崩溃留下的半行）；``strict=True`` 时
    直接抛出 ``ValueError``，便于测试或诊断。
    """
    event_path = Path(path)
    if not event_path.exists():
        return []

    events: list[JobEvent] = []
    with event_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                if strict:
                    raise ValueError(f"事件日志第 {line_number} 行不是有效 JSON: {event_path}") from exc
                continue
            if not isinstance(raw, dict):
                if strict:
                    raise ValueError(f"事件日志第 {line_number} 行不是对象: {event_path}")
                continue
            events.append(JobEvent.from_dict(raw))
    return events
