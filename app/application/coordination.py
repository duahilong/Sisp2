"""多盘并发的两个协调器：分区轮次闸门 + Ghost 启动间隔闸门。

## 为什么需要

Ghost 的「分区 → 写镜像 → 写引导」三段里，只有 **分区** 必须独占（diskpart 是
全局状态机，两块盘同时 partition 会互相踩），而 **写镜像** 与 **写引导** 是按盘
独立的。于是可以流水：盘 1 分区完就去写镜像，同时盘 2 开始分区。

实测（VM，3 块 VHD + ghost64 11.5.1）Ghost 不能在同一瞬间启动两个实例——
同时启动的一方必定以 8027 ``The device is not ready`` 退出（发生在卷枚举阶段，
还没写任何数据）。错开 2 s 就完全正常。流水线里天然有「下一块盘的分区时长」
作为间隔（几十秒到几分钟），所以 :class:`GhostStartGate` 只是一道保险，防止
将来有人把间隔调成 0 时撞上这个坑。

## 不变量（改这两个类时别破坏）

1. 分区阶段**任意时刻最多一块盘**在跑；
2. 一块盘只在**按顺序轮到它**时才能进入分区；
3. 一块盘的分区组结束（成功、失败、取消、异常）都必须 :meth:`~PartitionSequencer.done`，
   否则后面的盘永远等下去。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

__all__ = ["GhostStartGate", "PartitionSequencer", "SequencerLease"]

# 排队期间的轮询间隔：取消要能在一个可接受的时间内被感知（测试断言 300 ms 内）。
POLL_SECONDS = 0.2

# 兜底租约：持有闸门的盘超过这个时间还没 done()，放行下一块盘并告警。
# 真实分区最坏也就几分钟（GHOST_TIMEOUT 之外的部分），30 分钟足够宽松。
LEASE_SECONDS = 30 * 60.0


class PartitionSequencer:
    """按给定顺序串行化「分区组」，与 Ghost/引导无关。

    用法::

        seq = PartitionSequencer()
        seq.set_order([1, 2, 3])
        if not seq.enter(1, ctx.cancelled):
            ...  # 排队期间被取消
        try:
            run_partition_group()
        finally:
            seq.done(1)

    ``enter()`` 返回 ``False`` 只有一种原因：排队期间 ``cancelled()`` 为真。
    """

    def __init__(self, order: Iterable[int] = ()) -> None:
        self._cond = threading.Condition()
        self._order: list[int] = []
        self._done: set[int] = set()
        self._running: int | None = None
        self._lease: float = 0.0
        self.set_order(order)

    # ------------------------------------------------------------------ 配置
    def set_order(self, order: Iterable[int]) -> None:
        """设定本批盘的顺序（去重、保持给定顺序）。重复调用会重置状态。"""
        with self._cond:
            self._order = []
            for number in order:
                value = int(number)
                if value not in self._order:
                    self._order.append(value)
            self._done = set()
            self._running = None
            self._lease = 0.0
            self._cond.notify_all()

    def reset(self) -> None:
        self.set_order(())

    # ------------------------------------------------------------------ 排队
    def enter(
        self,
        disk_number: int,
        cancelled: Callable[[], bool] | None = None,
        *,
        poll: float = POLL_SECONDS,
        lease: float = LEASE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """等到轮到 ``disk_number`` 再返回 True；排队期间被取消返回 False。

        不在顺序表里的盘（例如单盘任务、或调用方忘了 set_order）直接放行，
        避免一个配置疏漏把任务永久卡死。
        """
        target = int(disk_number)
        with self._cond:
            if target not in self._order:
                self._running = target
                return True
            while True:
                if cancelled is not None and cancelled():
                    return False
                head = self._head_locked()
                if head is None or head == target:
                    self._running = target
                    self._lease = clock() + lease
                    return True
                # 持有闸门的那块盘超时未完成：放行，避免永久死锁。
                if self._running is not None and self._lease and clock() > self._lease:
                    self._done.add(self._running)
                    self._running = None
                    self._lease = 0.0
                    self._cond.notify_all()
                    continue
                self._cond.wait(poll)

    def done(self, disk_number: int) -> None:
        """标记该盘的分区组结束（幂等）。必须放在 finally 里调用。"""
        target = int(disk_number)
        with self._cond:
            self._done.add(target)
            if self._running == target:
                self._running = None
            self._lease = 0.0
            self._cond.notify_all()

    def cancel_waiting(self) -> None:
        """唤醒所有排队者，让它们各自检查 cancelled()（取消时不必逐个通知）。"""
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------------ 观察
    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            finished = [number for number in self._order if number in self._done]
            head = self._head_locked()
            return {
                "order": list(self._order),
                "head": head,
                "running": self._running,
                "finished": finished,
            }

    def _head_locked(self) -> int | None:
        for number in self._order:
            if number not in self._done:
                return number
        return None


class SequencerLease:
    """``with seq.lease(disk):`` —— 无论正常/异常退出都会 :meth:`PartitionSequencer.done`。"""

    def __init__(self, sequencer: PartitionSequencer, disk_number: int) -> None:
        self._sequencer = sequencer
        self._disk = int(disk_number)

    def __enter__(self) -> PartitionSequencer:
        return self._sequencer

    def __exit__(self, *_exc: object) -> None:
        self._sequencer.done(self._disk)


class GhostStartGate:
    """保证两个 Ghost 进程的启动间隔不小于 ``min_gap`` 秒。

    同一瞬间启动会触发 Ghost 的 8027（卷枚举阶段 "device is not ready"），
    错开 2 s 即可。这里用单调时钟 + 进程内锁，第一次启动不受限制。
    """

    def __init__(
        self,
        min_gap: float = 2.5,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_gap = max(0.0, float(min_gap))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait(self) -> float:
        """必要时睡眠，返回实际等待的秒数（0 表示不需要等）。"""
        if self.min_gap <= 0:
            return 0.0
        with self._lock:
            waited = 0.0
            if self._last is not None:
                remaining = self.min_gap - (self._clock() - self._last)
                if remaining > 0:
                    waited = remaining
                    self._sleep(remaining)
            self._last = self._clock()
            return waited
