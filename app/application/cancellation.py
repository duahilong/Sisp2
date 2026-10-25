"""任务取消支持。

第一阶段只有同步流程；第二阶段引入可协作取消的 ``CancellationToken``。
磁盘操作本身是阻塞的 PowerShell 调用，因此取消是协作式的：任务在步骤
边界检查令牌，已开始的底层命令会继续完成，但不会再进入下一步破坏性操作。
"""

from __future__ import annotations

import threading

from app.modules.common.service import OperationCancelled

# 任务层沿用项目统一的取消异常，保证 CLI、worker 和 GUI 的错误码一致。
JobCancelled = OperationCancelled


class CancellationToken:
    """线程安全的取消令牌。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        """等待取消信号，返回 ``True`` 表示已取消。"""
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled()