"""SSE 的纯逻辑（不 import FastAPI / uvicorn），便于单测与低版本解释器实测。

只放一件事：**等待下一条事件，超时算心跳**。

为什么要单独一个模块：``asyncio.TimeoutError`` 的继承关系在版本之间变过——

- 3.10：独立类，继承 ``Exception``（**不是**内置 ``TimeoutError``，后者继承 ``OSError``）；
- 3.11：成为内置 ``TimeoutError`` 的别名（``__cause__`` 之类行为一起对齐）；
- 3.12+：文档正式统一。

``asyncio.wait_for()`` 超时抛的始终是 ``asyncio.TimeoutError``，所以 ``except`` 必须
两个都写：只写内置 ``TimeoutError`` 在 3.10 上会漏捕，表现成“SSE 每 15 秒断一次”；
只写 ``asyncio.TimeoutError`` 虽然当前各版本都对，但 3.12+ 的静态检查（ruff UP041）
会提示它与内置同义。两个都列，任何版本都不会漏。

``tests/test_sse_heartbeat.py`` 钉住这三条（超时返回 ``None``、有事件原样返回、
两种异常都被吞掉）；没有 3.10 解释器时也能把“双写”的意图测出来。
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["TIMEOUT_ERRORS", "wait_for_event"]

#: 心跳等待要一并吞掉的超时异常（理由见模块 docstring）。
TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (asyncio.TimeoutError, TimeoutError)


async def wait_for_event(queue: asyncio.Queue[Any], timeout: float) -> Any | None:
    """等队列里的一条事件；``timeout`` 秒内没有就返回 ``None``（不抛异常）。

    调用方拿到 ``None`` 就发一行 SSE 心跳（``: ping``），然后继续循环。
    """
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except TIMEOUT_ERRORS:
        return None
