"""Qt 与任务层之间的桥接。

``JobManager`` 的事件可能来自后台线程，不能直接更新控件。这里用 Qt 信号
把事件排队回主线程。耗时的扫描/计划操作通过 ``CallWorker`` 放到 QThread，
避免界面冻结。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from app.application.jobs import JobManager


class JobEventBridge(QObject):
    """把 ``JobManager`` 的事件转成 Qt 信号。"""

    event_received = Signal(object)

    def __init__(self, manager: JobManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._unsubscribe = manager.subscribe(self.event_received.emit)

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None


class CallWorker(QThread):
    """在后台线程执行一个返回 Python 对象的函数。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            self.succeeded.emit(self._func())
        except Exception as exc:  # noqa: BLE001 - 需要把异常文本送回界面
            self.failed.emit(str(exc))
