"""SSE 的纯逻辑（不 import FastAPI / uvicorn），便于单测与低版本解释器实测。

只放一件事：**等待下一条事件，超时算心跳**。

为什么不用 ``asyncio.wait_for()``：它在超时时抛 ``asyncio.TimeoutError``，而这个
异常的继承关系在版本之间变过——

- 3.10：独立类，继承 ``Exception``（3.10.11 实测 MRO: ``TimeoutError → Exception``）；
- 3.11 起：成为内置 ``TimeoutError`` 的别名（后者 MRO 是 ``TimeoutError → OSError``）。

``except`` 里少写一个，在 3.10 上就会漏捕，表现成“SSE 每 15 秒断一次”
（3.10.11 实测：只写内置 ``TimeoutError`` 时，``wait_for`` 的异常会直接漏出去）。

``asyncio.wait(..., timeout=)`` 超时时**不抛异常**（返回空的 done 集合），所以这里
自己管 task：任何解释器版本行为完全一致，而且连接断开（外层任务被取消）时会把
``queue.get()`` 收干净，不会留下 "coroutine was never awaited" 警告。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

__all__ = ["wait_for_event"]


async def wait_for_event(queue: asyncio.Queue[Any], timeout: float) -> Any | None:
    """等队列里的一条事件；``timeout`` 秒内没有就返回 ``None``（不抛异常）。

    调用方拿到 ``None`` 就发一行 SSE 心跳（``: ping``），然后继续循环。
    """
    getter: asyncio.Future[Any] = asyncio.ensure_future(queue.get())
    try:
        done, _pending = await asyncio.wait({getter}, timeout=timeout)
        return getter.result() if getter in done else None
    finally:
        # 超时、或外层被取消（客户端断开）时都要把 getter 收尾：
        # 少这一步会出现 "coroutine 'Queue.get' was never awaited"。
        if not getter.done():
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await getter
