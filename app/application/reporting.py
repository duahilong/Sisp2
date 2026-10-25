"""流程输出适配器。

CLI 继续使用 ``ConsoleReporter`` 保持原输出；GUI 后续可以实现同一个
``Reporter`` 协议，把每一步转换成 JobEvent 或 Qt Signal，而不需要修改
磁盘业务模块。
"""

from __future__ import annotations

from typing import Protocol


class Reporter(Protocol):
    """流程输出接口。"""

    def line(self, text: str = "") -> None:
        """输出一行文本。"""

    def step_started(self, index: int, total: int, name: str) -> None:
        """开始执行某个步骤。"""


class ConsoleReporter:
    """兼容当前命令行的控制台输出。"""

    def line(self, text: str = "") -> None:
        print(text)

    def step_started(self, index: int, total: int, name: str) -> None:
        self.line(f"[{index}/{total}] {name}...")


class MemoryReporter:
    """测试和后续 GUI 适配使用的内存输出收集器。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def step_started(self, index: int, total: int, name: str) -> None:
        self.line(f"[{index}/{total}] {name}...")
