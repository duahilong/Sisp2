"""阶段实现：分区（partition）与装机（system）在这里彻底分开。

- :mod:`app.application.phases.partition` —— 只认识 ``DiskpartShell``；
- :mod:`app.application.phases.ghost` —— 只认识 ``ProcessShell``（Ghost 写 GHO）；
- :mod:`app.application.phases.system` —— 组合：镜像 + 引导（引导实现在
  :mod:`app.application.uefi`）。

编排（谁先跑、失败后谁跳过）在 :mod:`app.application.pipeline`。
"""

from app.application.phases.base import (
    Phase,
    PhaseContext,
    PhaseOutcome,
    PhaseResult,
)

__all__ = ["Phase", "PhaseContext", "PhaseOutcome", "PhaseResult"]
