"""SSE 心跳的版本兼容（``asyncio.TimeoutError`` vs 内置 ``TimeoutError``）。

背景：``asyncio.wait_for()`` 超时抛 ``asyncio.TimeoutError``。

- 3.10：它是独立类，继承 ``Exception``；内置 ``TimeoutError`` 继承 ``OSError``，**两者不同**；
- 3.11 起：``asyncio.TimeoutError`` 成为内置 ``TimeoutError`` 的别名；
- 3.12+：文档正式统一。

所以 SSE 心跳的 ``except`` 必须两种都列。这三条用例不依赖解释器版本：
在 3.15 上“两个类相同”这件事让人无法从行为上区分，只能用 mock 分别抛两种异常
来钉住“两种都要被吞掉”的意图。
"""

import asyncio
from unittest import mock

from app.gui_web.sse import TIMEOUT_ERRORS, wait_for_event


def test_timeout_returns_none() -> None:
    """15 秒等不到事件时返回 None（调用方据此发 `: ping`），不抛异常。"""

    async def main():
        return await wait_for_event(asyncio.Queue(), 0.01)

    assert asyncio.run(main()) is None


def test_event_is_returned_unchanged() -> None:
    async def main():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"seq": 7, "type": "job.step"})
        return await wait_for_event(queue, 5.0)

    assert asyncio.run(main()) == {"seq": 7, "type": "job.step"}


def test_timeout_errors_cover_both_names() -> None:
    """3.10 与 3.12+ 的两种写法都要在元组里，否则 3.10 会漏捕。"""
    assert TimeoutError in TIMEOUT_ERRORS
    assert asyncio.TimeoutError in TIMEOUT_ERRORS


def test_builtin_timeout_error_is_swallowed() -> None:
    """内置 ``TimeoutError``（3.10 上与 asyncio 版不同类）也必须被吞掉。"""

    async def main():
        with mock.patch(
            "app.gui_web.sse.asyncio.wait_for", side_effect=TimeoutError("boom")
        ):
            return await wait_for_event(asyncio.Queue(), 1.0)

    assert asyncio.run(main()) is None


def test_asyncio_timeout_error_is_swallowed() -> None:
    """``asyncio.TimeoutError``（3.10 的独立类）同样要被吞掉。"""

    async def main():
        with mock.patch(
            "app.gui_web.sse.asyncio.wait_for",
            side_effect=asyncio.TimeoutError("boom"),
        ):
            return await wait_for_event(asyncio.Queue(), 1.0)

    assert asyncio.run(main()) is None
