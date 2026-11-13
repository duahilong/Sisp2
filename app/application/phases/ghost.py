"""装机阶段（Ghost 版）：把 ``.GHO`` 还原到 Windows 分区。

与已经移除的 dism 路线相比，差别只有两点：

1. 目标是 **磁盘:分区号**（Ghost 自己 1 基编号），不是盘符路径；
2. 失败原因除了退出码，还要看 ``ghosterr.txt``（Ghost 写进当前工作目录）。

其余约定完全不变：只吃 :class:`PhaseContext`，不 import diskpart、不查分区表、
不认识 ``image/boot`` 之外的步骤；分区做没做对由编排层决定要不要进来。
"""

from __future__ import annotations

import time
from pathlib import Path

from app.application import activity
from app.application.partitions import ghost_target
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import tail
from app.application.shell import GHOST_TIMEOUT, ProcessShell
from app.application.streaming import parse_percent
from app.modules.ghost.service import ghost_command, parse_ghosterr

GROUP = "system"

GHOSTERR_NAME = "ghosterr.txt"

# 多盘并发时 Ghost 偶发在「卷枚举」阶段就退出（还没写任何数据）：
# ``Error Number: (8027) / A GeneralException occurred / The device is not ready``。
# 实测错开 2 s 启动就不会出现，但流水线里下一块盘的分区时长不可控，所以这里
# 再兜一层：认出这类瞬时错误就等 3 秒重试一次。
TRANSIENT_ERRORS = ("8027", "device is not ready", "generalexception")
RETRY_DELAY = 3.0
MAX_ATTEMPTS = 2


class GhostImagePhase:
    """应用 GHO 镜像（Ghost 11.x 命令行）。接口与分区/引导阶段一致：``run(ctx)``。"""

    group = GROUP

    def __init__(
        self,
        shell: ProcessShell,
        ghost: str | None = None,
        *,
        cwd: str | None = None,
        start_gate: object | None = None,
    ) -> None:
        self.shell = shell
        self.ghost = ghost or getattr(shell, "ghost", None)
        # ghosterr.txt 落在哪儿：优先用调用方给的日志目录，否则用进程默认目录。
        self.cwd = cwd
        # ``coordination.GhostStartGate``：多盘时保证两个 Ghost 不同时启动。
        # 用鸭子类型（只需要 ``wait()``），避免 phases 包反向依赖编排层。
        self.start_gate = start_gate

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
        # 诊断用：写镜像期间登记成 image，让别的盘分区时能在日志里看到
        # 「磁盘 N 正在写镜像」，而不是笼统的 system。
        activity.begin(ctx.disk_number, "image", ctx.label)
        try:
            return self._run_phase(ctx)
        finally:
            activity.begin(ctx.disk_number, "system", ctx.label)

    def _run_phase(self, ctx: PhaseContext) -> PhaseOutcome:
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
        attempts = 0
        while True:
            attempts += 1
            if self.start_gate is not None:
                self.start_gate.wait()
            try:
                code, output, elapsed, cancelled = self._run(ctx)
            except Exception as exc:  # noqa: BLE001 - 缺工具/启动失败/超时都算这一步失败
                return PhaseOutcome.failed(
                    PhaseResult.failed("image", f"Ghost 无法执行：{exc}"),
                    message=f"应用镜像失败：{exc}",
                )

            if cancelled:
                # 进程已被终止：盘上可能已有半个镜像，必须说清楚“不是什么都没发生”。
                return PhaseOutcome(
                    ok=False,
                    cancelled=True,
                    disk_touched=True,
                    results=(
                        PhaseResult.skipped("image", "任务已取消（镜像写入已中止）"),
                    ),
                    message="任务已取消（镜像写入被中止，分区已建好）",
                )

            if code == 0:
                break

            reason = self._ghosterr() or tail(output, 800) or "Ghost 没有输出"
            if attempts < MAX_ATTEMPTS and self._is_transient(reason):
                ctx.log.write(
                    f"Ghost 退出码 {code}（{reason.splitlines()[0] if reason else '无输出'}），"
                    f"疑似并发冲突，{RETRY_DELAY:.0f} 秒后重试（第 {attempts + 1}/{MAX_ATTEMPTS} 次）"
                )
                self._clear_ghosterr()
                if self._pause(ctx, RETRY_DELAY):
                    return PhaseOutcome(
                        ok=False,
                        cancelled=True,
                        results=(PhaseResult.skipped("image", "任务已取消"),),
                        message="任务已取消（重试等待中被取消）",
                    )
                continue

            return PhaseOutcome.failed(
                PhaseResult.failed(
                    "image",
                    f"Ghost 失败（退出码 {code}）：{reason}",
                    exit=code,
                    engine="ghost",
                    image=ctx.image_path,
                    seconds=round(elapsed, 1),
                    attempts=attempts,
                ),
                message=f"应用镜像失败（Ghost 退出码 {code}）",
            )

        disk, partition = ghost_target(ctx.disk_number, ctx.parts)
        name = Path(str(ctx.image_path)).name
        details = {
            "engine": "ghost",
            "image": ctx.image_path,
            "seconds": round(elapsed, 1),
            "tail": tail(output, 1200),
        }
        if attempts > 1:
            details["attempts"] = attempts
        return PhaseOutcome.succeeded(PhaseResult.ok(
            "image",
            f"已用 Ghost 还原 {name} 到 {disk}:{partition}",
            **details,
        ))

    # ------------------------------------------------------------------ 内部
    def _run(self, ctx: PhaseContext) -> tuple[int | None, str, float, bool]:
        """跑一次 Ghost：有流式能力就边跑边解析百分比、随时可取消。"""
        command = self.command(ctx)
        streaming = getattr(self.shell, "run_streaming", None)
        if streaming is None:
            code, output, elapsed = self.shell.run(command, GHOST_TIMEOUT, cwd=self.cwd)
            return code, output, elapsed, False
        outcome = streaming(
            command,
            GHOST_TIMEOUT,
            cwd=self.cwd,
            cancel=ctx.cancel,
            on_line=self._progress_callback(ctx),
        )
        return outcome.returncode, outcome.output, outcome.elapsed, outcome.cancelled

    @staticmethod
    def _progress_callback(ctx: PhaseContext):
        """把 Ghost 的 `` 42%`` 输出翻成 ``fraction``；只前进不后退。"""
        last = -1

        def on_line(line: str) -> None:
            nonlocal last
            percent = parse_percent(line)
            if percent is None or percent <= last:
                return
            last = percent
            ctx.notify(
                "image",
                "running",
                f"应用镜像（Ghost） {percent}%",
                fraction=percent / 100,
            )

        return on_line

    @staticmethod
    def _is_transient(reason: str) -> bool:
        text = (reason or "").lower()
        return any(token in text for token in TRANSIENT_ERRORS)

    @staticmethod
    def _pause(ctx: PhaseContext, seconds: float, poll: float = 0.2) -> bool:
        """可取消的等待；被取消返回 True。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if ctx.cancelled():
                return True
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        return ctx.cancelled()

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
        """读 Ghost 写下的失败原因（优先 UTF-8 / GBK，再兼容当前系统 ACP）。"""
        path = self._ghosterr_path()
        if path is None:
            return ""
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        # Ghost 的中文错误通常是 GBK；不能优先按当前 Windows ACP/mbcs 猜，
        # 否则英文系统会把 GBK 当 cp1252 且“解码成功”为乱码。
        # ASCII 在 utf-8/gb18030 下结果一致；显式 UTF-8 也优先保留。
        for encoding in ("utf-8", "gb18030", "mbcs", "latin-1"):
            try:
                return parse_ghosterr(raw.decode(encoding))
            except (UnicodeDecodeError, LookupError):
                continue
        return parse_ghosterr(raw.decode("utf-8", errors="replace"))


__all__ = ["GHOSTERR_NAME", "GROUP", "GhostImagePhase"]
