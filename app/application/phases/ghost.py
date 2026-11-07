"""装机阶段（Ghost 版）：把 ``.GHO`` 还原到 Windows 分区。

与已经移除的 dism 路线相比，差别只有两点：

1. 目标是 **磁盘:分区号**（Ghost 自己 1 基编号），不是盘符路径；
2. 失败原因除了退出码，还要看 ``ghosterr.txt``（Ghost 写进当前工作目录）。

其余约定完全不变：只吃 :class:`PhaseContext`，不 import diskpart、不查分区表、
不认识 ``image/boot`` 之外的步骤；分区做没做对由编排层决定要不要进来。
"""

from __future__ import annotations

from pathlib import Path

from app.application.partitions import ghost_target
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import tail
from app.application.shell import GHOST_TIMEOUT, ProcessShell
from app.modules.ghost.service import ghost_command, parse_ghosterr

GROUP = "system"

GHOSTERR_NAME = "ghosterr.txt"


class GhostImagePhase:
    """应用 GHO 镜像（Ghost 11.x 命令行）。接口与分区/引导阶段一致：``run(ctx)``。"""

    group = GROUP

    def __init__(
        self,
        shell: ProcessShell,
        ghost: str | None = None,
        *,
        cwd: str | None = None,
    ) -> None:
        self.shell = shell
        self.ghost = ghost or getattr(shell, "ghost", None)
        # ghosterr.txt 落在哪儿：优先用调用方给的日志目录，否则用进程默认目录。
        self.cwd = cwd

    # ------------------------------------------------------------------ 拼装
    def command(self, ctx: PhaseContext) -> list[str]:
        """纯拼装：测试可以直接断言命令行，不需要任何子进程。"""
        if not self.ghost:
            raise RuntimeError("找不到 ghost64.exe，无法写入 GHO 镜像")
        disk, partition = ghost_target(ctx.disk_number, ctx.parts)
        return ghost_command(
            str(self.ghost),
            str(ctx.image_path),
            ghost_disk=disk,
            partition=partition,
        )

    # ------------------------------------------------------------------ 执行
    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        if ctx.cancelled():
            return PhaseOutcome(
                ok=False,
                cancelled=True,
                results=(PhaseResult.skipped("image", "任务已取消"),),
                message="任务已取消（分区已建好，镜像未应用）",
            )

        ctx.notify("image", "running", "应用镜像（Ghost）")
        # 上一次失败的 ghosterr.txt 不能当成本次的原因，先删掉。
        self._clear_ghosterr()
        try:
            code, output, elapsed = self.shell.run(
                self.command(ctx), GHOST_TIMEOUT, cwd=self.cwd
            )
        except Exception as exc:  # noqa: BLE001 - 缺工具/启动失败/超时都算这一步失败
            return PhaseOutcome.failed(
                PhaseResult.failed("image", f"Ghost 无法执行：{exc}"),
                message=f"应用镜像失败：{exc}",
            )

        if code != 0:
            reason = self._ghosterr() or tail(output, 800) or "Ghost 没有输出"
            return PhaseOutcome.failed(
                PhaseResult.failed(
                    "image",
                    f"Ghost 失败（退出码 {code}）：{reason}",
                    exit=code,
                    engine="ghost",
                    image=ctx.image_path,
                    seconds=round(elapsed, 1),
                ),
                message=f"应用镜像失败（Ghost 退出码 {code}）",
            )

        disk, partition = ghost_target(ctx.disk_number, ctx.parts)
        name = Path(str(ctx.image_path)).name
        return PhaseOutcome.succeeded(
            PhaseResult.ok(
                "image",
                f"已用 Ghost 还原 {name} 到 {disk}:{partition}",
                engine="ghost",
                image=ctx.image_path,
                seconds=round(elapsed, 1),
                tail=tail(output, 1200),
            )
        )

    # ------------------------------------------------------------------ 内部
    def _ghosterr_path(self) -> Path | None:
        if not self.cwd:
            return None
        return Path(self.cwd) / GHOSTERR_NAME

    def _clear_ghosterr(self) -> None:
        path = self._ghosterr_path()
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _ghosterr(self) -> str:
        """读 Ghost 写下的失败原因（GBK/OEM 编码，先 mbcs 再兜底）。"""
        path = self._ghosterr_path()
        if path is None:
            return ""
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        for encoding in ("mbcs", "utf-8", "latin-1"):
            try:
                return parse_ghosterr(raw.decode(encoding))
            except (UnicodeDecodeError, LookupError):
                continue
        return parse_ghosterr(raw.decode("utf-8", errors="replace"))


__all__ = ["GHOSTERR_NAME", "GROUP", "GhostImagePhase"]
