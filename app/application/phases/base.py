"""阶段契约：``PhaseContext`` / ``PhaseResult`` / ``PhaseOutcome``。

一次真实写盘被拆成三组互不依赖的阶段：

    partition  → 清盘 / 删 MSR / 建分区 / 分区验收（layout）
    system     → 应用镜像（Ghost） / 写引导并校验（bcdboot + 文件检查）
    extras     → 拷附加文件到第一数据分区（设置留空则整步跳过）

阶段之间只通过这里的类型交换信息：**没有 ``self``、没有共享可变 dict**。
分区阶段说"我做完了、盘动过、ESP 是第 k 个分区"；装机阶段只管读自己的入参。
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.application.command_log import CommandLog
from app.application.partitions import PlannedPartition
from app.application.steps import STEP_TITLES
from app.models import STEP_FAILED, STEP_OK, STEP_SKIPPED

_LOGGER = logging.getLogger(__name__)


@dataclass
class PhaseContext:
    """一次任务里所有阶段共享的**只读**入参（可选项与取消信号）。"""

    disk_number: int
    parts: Sequence[PlannedPartition]
    letters: Mapping[str, str]
    image_path: str | None
    cancel: threading.Event
    log: CommandLog
    options: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] | None = None
    on_step: Any = None
    # 本任务独占的 Ghost 工作目录（放 ghosterr.txt / ghost32.dmp）；结束后由
    # runner 归档失败原因并删除。None 时退回日志目录（单测用）。
    work_dir: str | None = None
    # 演练任务：阶段可以报“将会做什么”，但绝不执行命令（拷贝阶段用它区分文案）。
    dry_run: bool = False

    def __post_init__(self) -> None:
        # 回调可以是老的 ``on_step(key, status, message)``，也可以多接一个
        # ``fraction``（0–1，用于 Ghost 这类有内部百分比的步骤）。签名在构造期
        # 判定一次，运行期不再反复反射；老回调不会被多塞一个参数。
        self._accepts_fraction = _accepts_fraction(self.on_step)

    def notify(
        self,
        key: str,
        status: str,
        message: str = "",
        fraction: float | None = None,
    ) -> None:
        """上报“某一步正在执行”；进度回调可选，异常一律吞掉。"""
        if self.on_step is None:
            return
        try:
            if self._accepts_fraction:
                self.on_step(key, status, message, fraction)
            else:
                self.on_step(key, status, message)
        except Exception:
            _LOGGER.warning("进度回调失败", exc_info=True)

    def cancelled(self) -> bool:
        return self.cancel.is_set()


def _accepts_fraction(callback: Any) -> bool:
    """进度回调是否接受第 4 个参数 ``fraction``。

    三参回调是历史契约（测试与外部执行器都在用），不能因为新增了子步骤
    进度就把它直接撑破；``*args`` / ``**kwargs`` 视为可以接受。
    """
    if callback is None:
        return False
    try:
        parameters = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return False
    for parameter in parameters:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            return True
        if parameter.name == "fraction":
            return True
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 4


@dataclass(frozen=True)
class PhaseResult:
    """一个步骤的结果（键与 ``steps.STEP_KEYS`` 对齐）。"""

    key: str
    status: str = STEP_OK
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, key: str, message: str = "", **details: Any) -> PhaseResult:
        return cls(key=key, status=STEP_OK, message=message, details=details)

    @classmethod
    def failed(cls, key: str, message: str, **details: Any) -> PhaseResult:
        return cls(key=key, status=STEP_FAILED, message=message, details=details)

    @classmethod
    def skipped(cls, key: str, message: str = "") -> PhaseResult:
        return cls(key=key, status=STEP_SKIPPED, message=message)

    @property
    def title(self) -> str:
        return STEP_TITLES.get(self.key, self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class PhaseOutcome:
    """一个阶段的结论。

    ``disk_touched`` / ``efi_partition`` 取代了历史上的 ``tracker`` 可变 dict：
    分区阶段显式告诉编排层"盘动过没有、ESP 是第几个"，收尾释放盘符据此决定。
    """

    ok: bool
    results: tuple[PhaseResult, ...] = ()
    message: str = ""
    cancelled: bool = False
    disk_touched: bool = False
    efi_partition: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def succeeded(cls, *results: PhaseResult, **kwargs: Any) -> PhaseOutcome:
        return cls(ok=True, results=tuple(results), **kwargs)

    @classmethod
    def failed(cls, *results: PhaseResult, message: str, **kwargs: Any) -> PhaseOutcome:
        return cls(ok=False, results=tuple(results), message=message, **kwargs)


@runtime_checkable
class Phase(Protocol):
    """阶段协议：拿到上下文，返回结论。"""

    group: str

    def run(self, ctx: PhaseContext) -> PhaseOutcome: ...


__all__ = ["Phase", "PhaseContext", "PhaseOutcome", "PhaseResult"]
