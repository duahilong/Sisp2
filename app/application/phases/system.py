"""装机阶段：应用镜像（Ghost）→ 写 UEFI 引导（bcdboot）。

镜像写入本身在 :mod:`app.application.phases.ghost`（``GhostImagePhase``）；
这里只负责"组合"和"顺序"：镜像没成功就不写引导。

与分区阶段的关系：**只有入参关系**。这里不 import diskpart、不查分区表、
不碰 ``PhaseOutcome.efi_partition``；分区做没做对由编排层决定要不要进来。
"""

from __future__ import annotations

from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.phases.ghost import GhostImagePhase
from app.application.runner_utils import tail, to_text
from app.application.shell import BCDBOOT_TIMEOUT, ProcessShell

GROUP = "system"


class BootPhase:
    """写 UEFI 引导（bcdboot）。"""

    group = GROUP

    def __init__(self, shell: ProcessShell, bcdboot: str | None = None) -> None:
        self.shell = shell
        self.bcdboot = bcdboot or (getattr(shell, "bcdboot", None))

    def command(self, ctx: PhaseContext) -> list[str]:
        if not self.bcdboot:
            raise RuntimeError("找不到 bcdboot.exe，无法写入引导")
        options = dict(ctx.options or {})
        windows = f"{ctx.letters['windows']}:\\Windows"
        efi = f"{ctx.letters['efi']}:"
        # ``sysBoot`` 是离线引导文件目录：镜像里没有 Windows\\Boot 时
        # （精简/自定义镜像）用它当 bcdboot 的源目录。
        boot_source = to_text(options.get("boot_source"))
        if boot_source:
            ctx.log.write(f"引导源目录来自界面设置：{boot_source}")
        return [self.bcdboot, boot_source or windows, "/s", efi, "/f", "UEFI"]

    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        if ctx.cancelled():
            return PhaseOutcome(
                ok=False,
                cancelled=True,
                results=(PhaseResult.skipped("boot", "任务已取消"),),
                message="任务已取消（镜像已写入，引导未写）",
            )

        ctx.notify("boot", "running", "写入 UEFI 引导（bcdboot）")
        try:
            code, output, elapsed = self.shell.run(self.command(ctx), BCDBOOT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return PhaseOutcome.failed(
                PhaseResult.failed("boot", f"bcdboot 无法执行：{exc}"),
                message=f"写引导失败：{exc}",
            )
        if code != 0:
            return PhaseOutcome.failed(
                PhaseResult.failed(
                    "boot",
                    f"bcdboot 失败（退出码 {code}）：{tail(output, 800)}",
                    exit=code,
                    seconds=round(elapsed, 1),
                ),
                message=f"写引导失败（bcdboot 退出码 {code}）",
            )
        return PhaseOutcome.succeeded(
            PhaseResult.ok(
                "boot",
                f"已写入 UEFI 引导（{ctx.letters['efi']}: ← {ctx.letters['windows']}:\\Windows）",
                exit=code,
                seconds=round(elapsed, 1),
                tail=tail(output, 800),
            )
        )


class SystemPhase:
    """系统组：镜像 + 引导，顺序执行；前一步失败就不再写引导。"""

    group = GROUP

    def __init__(self, image: GhostImagePhase, boot: BootPhase) -> None:
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


__all__ = ["GROUP", "BootPhase", "SystemPhase"]
