"""装机阶段：应用镜像（dism）→ 写 UEFI 引导（bcdboot）。

与分区阶段的关系：**只有入参关系**。这里不 import diskpart、不查分区表、
不碰 ``PhaseOutcome.efi_partition``；分区做没做对由编排层决定要不要进来。
"""

from __future__ import annotations

from pathlib import Path

from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import tail, to_text
from app.application.shell import BCDBOOT_TIMEOUT, DISM_TIMEOUT, ProcessShell

GROUP = "system"


class ImagePhase:
    """应用镜像（dism）。"""

    group = GROUP

    def __init__(self, shell: ProcessShell, dism: str | None = None) -> None:
        self.shell = shell
        self.dism = dism or (getattr(shell, "dism", None))

    def command(self, ctx: PhaseContext) -> list[str]:
        """dism 命令行（纯拼装，测试可直接断言）。"""
        if not self.dism:
            raise RuntimeError("找不到 dism.exe，无法应用镜像")
        options = dict(ctx.options or {})
        target = f"{ctx.letters['windows']}:\\"
        command = [
            self.dism,
            "/English",
            "/Apply-Image",
            f"/ImageFile:{ctx.image_path}",
            f"/Index:{ctx.image_index}",
            f"/ApplyDir:{target}",
            "/CheckIntegrity",
        ]
        # 界面设置里的「无人值守应答文件」和「驱动目录」直接交给 DISM，
        # 在应用镜像的同一步里生效，失败会让整步失败（不静默省略）。
        unattend = to_text(options.get("unattend_path"))
        if unattend:
            command.append(f"/Apply-Unattend:{unattend}")
        drivers = to_text(options.get("drivers_path"))
        if drivers:
            command.extend([f"/Add-Driver:{drivers}", "/Recurse"])
        return command

    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        if ctx.cancelled():
            return PhaseOutcome(
                ok=False,
                cancelled=True,
                results=(PhaseResult.skipped("image", "任务已取消"),),
                message="任务已取消（分区已建好，镜像未应用）",
            )

        ctx.notify("image", "running", "应用镜像（dism）")
        target = f"{ctx.letters['windows']}:\\"
        try:
            code, output, elapsed = self.shell.run(self.command(ctx), DISM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 缺工具/启动失败都算这一步失败
            return PhaseOutcome.failed(
                PhaseResult.failed("image", f"dism 无法执行：{exc}"),
                message=f"应用镜像失败：{exc}",
            )
        if code != 0:
            return PhaseOutcome.failed(
                PhaseResult.failed(
                    "image",
                    f"dism 失败（退出码 {code}）：{tail(output, 800)}",
                    exit=code,
                    image=ctx.image_path,
                    index=ctx.image_index,
                    seconds=round(elapsed, 1),
                ),
                message=f"应用镜像失败（dism 退出码 {code}）",
            )
        return PhaseOutcome.succeeded(
            PhaseResult.ok(
                "image",
                f"已应用 {Path(str(ctx.image_path)).name}（索引 {ctx.image_index}）到 {target}",
                image=ctx.image_path,
                index=ctx.image_index,
                seconds=round(elapsed, 1),
                tail=tail(output, 1200),
            )
        )


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

    def __init__(self, image: ImagePhase, boot: BootPhase) -> None:
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


__all__ = ["GROUP", "BootPhase", "ImagePhase", "SystemPhase"]
