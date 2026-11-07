"""阶段门面：一次「写引导 → 立刻校验」= 一个 ``boot`` 步骤。

之所以把写与验放在同一个阶段里：bcdboot 与"查 ESP/系统分区文件"用的是同一组
盘符，而盘符要到收尾的 ``finally`` 才释放。以前它们是两个阶段（system 组 →
verify 组），顺序靠编排，约束靠注释；现在顺序在对象内部，破坏它需要改代码。

对外只产出一个步骤结果 ``boot``：写失败 → ``boot`` 失败；写成功但校验不过 →
同样是 ``boot`` 失败（消息里同时带 bcdboot 与判据两段），不会出现"引导写成功
了，但机器起不来，而进度条显示全绿"。
"""

from __future__ import annotations

from typing import Any

from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.uefi.probe import BootProbe
from app.application.uefi.target import BootTarget, BootTargetError
from app.application.uefi.writer import BcdbootWriter


class UefiBootPhase:
    """写 UEFI 引导并立即校验（组：system，步骤键：boot）。"""

    group = "system"

    def __init__(
        self,
        shell: Any,
        *,
        bcdboot: str | None = None,
        writer: BcdbootWriter | None = None,
        probe: BootProbe | None = None,
    ) -> None:
        self.shell = shell
        self.bcdboot = bcdboot or getattr(shell, "bcdboot", None)
        self.writer = writer or BcdbootWriter(shell, self.bcdboot)
        self.probe = probe or BootProbe(shell)

    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        if ctx.cancelled():
            message = "任务已取消（镜像已写入，引导未写）"
            return PhaseOutcome(
                ok=False,
                cancelled=True,
                message=message,
                results=(PhaseResult.skipped("boot", "任务已取消"),),
            )
        try:
            target = BootTarget.from_context(ctx)
            command = self.writer.command(target)
        except (BootTargetError, RuntimeError) as exc:
            message = str(exc)
            return PhaseOutcome.failed(PhaseResult.failed("boot", message), message=message)

        if target.source_from_settings:
            ctx.log.write(f"引导源目录来自界面设置：{target.source}")
        ctx.notify("boot", "running", "写入 UEFI 引导（bcdboot）")
        ctx.log.write(" ".join(command))

        write = self.writer.write(target)
        if not write.ok:
            return PhaseOutcome.failed(
                PhaseResult.failed("boot", write.message, **write.details),
                message=f"写引导失败：{write.message}",
            )

        checked = self.probe.inspect(target)
        ctx.log.write(f"校验：{checked.message}")
        details = {**write.details, **checked.details}
        if not checked.ok:
            message = f"{write.message}；{checked.message}"
            return PhaseOutcome.failed(
                PhaseResult.failed("boot", message, **details),
                message=f"引导校验失败：{checked.message}",
            )
        return PhaseOutcome.succeeded(
            PhaseResult.ok("boot", f"{write.message}；{checked.message}", **details)
        )


__all__ = ["UefiBootPhase"]
