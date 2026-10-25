"""流程输出适配器。

CLI 继续使用 ``ConsoleReporter`` 保持原输出；GUI 后续可以实现同一个
``Reporter`` 协议，把每一步转换成 JobEvent 或 Qt Signal，而不需要修改
磁盘业务模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StepOutcome:
    """步骤结束信息，供内存/GUI Reporter 记录。"""

    index: int
    total: int
    name: str
    passed: bool
    message: str


class Reporter(Protocol):
    """流程输出接口。"""

    def line(self, text: str = "") -> None:
        """输出一行文本。"""

    def step_started(self, index: int, total: int, name: str) -> None:
        """开始执行某个步骤。"""

    def step_finished(self, index: int, total: int, name: str, passed: bool, message: str) -> None:
        """某个步骤执行结束（成功或失败）。"""


class ConsoleReporter:
    """兼容当前命令行的控制台输出。"""

    def line(self, text: str = "") -> None:
        print(text)

    def step_started(self, index: int, total: int, name: str) -> None:
        self.line(f"[{index}/{total}] {name}...")

    def step_finished(self, index: int, total: int, name: str, passed: bool, message: str) -> None:
        """命令行已有结果行，保持输出不变。"""


class MemoryReporter:
    """测试和后续 GUI 适配使用的内存输出收集器。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.steps: list[StepOutcome] = []

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def step_started(self, index: int, total: int, name: str) -> None:
        self.line(f"[{index}/{total}] {name}...")

    def step_finished(self, index: int, total: int, name: str, passed: bool, message: str) -> None:
        self.steps.append(
            StepOutcome(
                index=index,
                total=total,
                name=name,
                passed=passed,
                message=message,
            )
        )
