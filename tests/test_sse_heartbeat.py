"""SSE 心跳等待：不依赖 ``asyncio.TimeoutError`` 的版本差异。

背景：``asyncio.wait_for()`` 超时抛 ``asyncio.TimeoutError``，而这个异常类的继承
关系变过（3.10 是独立类、继承 ``Exception``；3.11 起才是内置 ``TimeoutError`` 的
别名，后者继承 ``OSError``）。所以实现改用 ``asyncio.wait(timeout=)``——超时不抛
异常，只在 finally 里把 ``queue.get()`` 的 task 收干净。

这组用例不依赖解释器版本：
- 超时返回 ``None``、有事件原样返回；
- 客户端断开（外层任务被取消）时不留下 "coroutine was never awaited"。
"""

import asyncio
import gc
import warnings

from app.gui_web.sse import wait_for_event


def test_timeout_returns_none() -> None:
    """等不到事件时返回 None（调用方据此发 `: ping`），不抛异常。"""

    async def main():
        return await wait_for_event(asyncio.Queue(), 0.01)

    assert asyncio.run(main()) is None


def test_event_is_returned_unchanged() -> None:
    async def main():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"seq": 7, "type": "job.step"})
        return await wait_for_event(queue, 5.0)

    assert asyncio.run(main()) == {"seq": 7, "type": "job.step"}


def test_cancelled_waiter_leaves_no_unawaited_coroutine() -> None:
    """连接断开 = 外层任务被取消：``queue.get()`` 必须收尾，不能留 RuntimeWarning。"""

    async def main():
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(wait_for_event(queue, 30))
        await asyncio.sleep(0.02)
        task.cancel()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.02)
            gc.collect()
        leaked = [str(item.message) for item in caught if "never awaited" in str(item.message)]
        assert not leaked, leaked

    asyncio.run(main())
