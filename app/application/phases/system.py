"""装机阶段：应用镜像（Ghost）→ 写 UEFI 引导并校验。

镜像写入本身在 :mod:`app.application.phases.ghost`（``GhostImagePhase``）；
写引导与校验在 :mod:`app.application.uefi`（``UefiBootPhase``）。这里只负责
"组合"和"顺序"：镜像没成功就不写引导。

与分区阶段的关系：**只有入参关系**。这里不 import diskpart、不查分区表、
不碰 ``PhaseOutcome.efi_partition``；分区做没做对由编排层决定要不要进来。
"""

from __future__ import annotations

from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.phases.ghost import GhostImagePhase
from app.application.uefi.phase import UefiBootPhase

GROUP = "system"


class SystemPhase:
    """系统组：镜像 + 引导（含校验），顺序执行；前一步失败就不再写引导。"""

    group = GROUP

    def __init__(self, image: GhostImagePhase, boot: UefiBootPhase) -> None:
        self.image = image
        self.boot = boot

    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        image = self.image.run(ctx)
        results = list(image.results)
        if not image.ok:
            if not image.cancelled:
                results.append(PhaseResult.skipped("boot", "镜像未应用成功，跳过引导"))
            return PhaseOutcome(
                ok=False,
                results=tuple(results),
                message=image.message,
                cancelled=image.cancelled,
                disk_touched=image.disk_touched,
            )
        boot = self.boot.run(ctx)
        results.extend(boot.results)
        return PhaseOutcome(
            ok=boot.ok,
            results=tuple(results),
            message=boot.message,
            cancelled=boot.cancelled,
            disk_touched=boot.disk_touched,
        )


__all__ = ["GROUP", "SystemPhase"]
