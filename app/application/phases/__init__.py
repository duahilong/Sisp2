"""阶段实现：分区（partition）与装机（system）在这里彻底分开。

- :mod:`app.application.phases.partition` —— 只认识 ``DiskpartShell``；
- :mod:`app.application.phases.system` —— 只认识 ``ProcessShell``（dism/bcdboot）；
- :mod:`app.application.phases.verify` —— 装机结果校验（引导文件 + 系统文件）。

编排（谁先跑、失败后谁跳过）在 :mod:`app.application.pipeline`。
"""

from app.application.phases.base import (
    Phase,
    PhaseContext,
    PhaseOutcome,
    PhaseResult,
)

__all__ = ["Phase", "PhaseContext", "PhaseOutcome", "PhaseResult"]
