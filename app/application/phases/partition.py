"""分区阶段：清盘 → 删 MSR（删完复查）→ 建分区 → **分区验收**，外加收尾释放盘符。

这里的代码**只认识 diskpart**：

- 依赖只有 :class:`~app.application.shell.DiskpartShell` 与 ``CommandLog``；
- 不 import Ghost / bcdboot，也不读 ``image_info``（镜像写不写、写哪个，不归它管）；
- 结论通过 :class:`~app.application.phases.base.PhaseOutcome` 交回编排层。

分区与装机解耦之后，"分区没做对"不会再被镜像阶段的失败掩盖，反过来也一样。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.application import activity
from app.application.partitions import (
    PlannedPartition,
    create_partition_lines,
    msr_delete_lines,
    partition_verdict,
    wipe_lines,
)
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import option_flag, option_int, tail
from app.application.shell import (
    DiskpartShell,
    describe_diskpart_exit,
    diskpart_exit_hint,
    is_transient_diskpart_exit,
)
from app.application.steps import steps_after

GROUP = "partition"

# 「清盘 + 转 GPT」是幂等的：同一块盘重跑一遍没有副作用。真实批量装机里遇到过
# 磁盘链路抖动导致的 0x80070017（ERROR_CRC）/0x80070015（设备未就绪）：clean 已经
# 成功、convert gpt 那一步读盘失败，一块盘整单作废。所以这一段允许重试，
# 重试前先 rescan，让 diskpart 的枚举跟上真实状态。
WIPE_ATTEMPTS = 3
WIPE_RETRY_DELAY = 2.0

# 「两次 diskpart 之间等多久」的默认值（毫秒）。设置项 partitionSettleMs 覆盖它。
# 为什么要有：一条 diskpart 刚改完分区表，下一条立刻回读同一片扇区，桥接板/慢盘
# 还没落盘就会报 0x80070017（数据错误）。给 1 秒缓冲能显著降低这类抖动。
DEFAULT_SETTLE_MS = 1000

# 验收判据可替换（默认就是 ``partitions.partition_verdict``）。
Acceptance = Callable[
    [Mapping[str, Any], Sequence[PlannedPartition]], tuple[bool, str, dict[str, Any]]
]


class PartitionAcceptance:
    """分区验收：查一次真实分区表，对照计划逐项比。

    独立成类（也独立成 ``layout`` 步骤）的原因：这是"分区阶段自己的验收"，
    与装机之后"能不能开机"的校验是两件事，以前混在一起，界面上看不出来。
    """

    def __init__(self, shell: DiskpartShell, verdict: Acceptance = partition_verdict) -> None:
        self.shell = shell
        self._verdict = verdict

    def run(
        self,
        disk_number: int,
        parts: Sequence[PlannedPartition],
        log: Any,
    ) -> tuple[bool, str, dict[str, Any]]:
        payload = self.shell.query(disk_number)
        ok, message, details = self._verdict(payload, parts)
        log.write(f"分区验收：{'通过' if ok else '不通过'} —— {message}")
        for warning in details.get("warnings") or ():
            log.write(f"分区验收（警告）：{warning}")
        return ok, message, details


class PartitionPhase:
    """分区组：4 个步骤（efi / windows / data / layout）。"""

    group = GROUP

    def __init__(
        self,
        shell: DiskpartShell,
        acceptance: PartitionAcceptance | None = None,
        *,
        verdict: Acceptance = partition_verdict,
        wipe_attempts: int = WIPE_ATTEMPTS,
        retry_delay: float = WIPE_RETRY_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.shell = shell
        self.acceptance = acceptance or PartitionAcceptance(shell, verdict)
        self.wipe_attempts = max(1, int(wipe_attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self._sleep = sleep
        self._settle_ms = DEFAULT_SETTLE_MS

    # ------------------------------------------------------------------ 入口
    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        log = ctx.log
        self._settle_ms = option_int(
            ctx.options, "partition_settle_ms", default=DEFAULT_SETTLE_MS, low=0, high=5000
        )
        if ctx.cancelled():
            return PhaseOutcome(ok=False, cancelled=True, message="任务已取消（未开始写盘）")

        # 诊断：分区是全局串行的，但此时别的盘可能正在写镜像——真实现场里
        # 0x80070017 就发生在那种重叠时刻，先把当时的情况记下来。
        others = activity.describe(exclude=ctx.disk_number)
        log.write(
            f"[诊断] 开始分区（diskpart 间隔 {self._settle_ms} ms；"
            f"其它盘活动：{others or '无'}）"
        )
        if self._settle_ms:
            log.write(f"每次 diskpart 之间等待 {self._settle_ms} ms（设置 partitionSettleMs）")

        # ===== ① 清盘 + 转 GPT（幂等，允许重试）=====
        ctx.notify("efi", "running", "清盘并重建分区表")
        try:
            code, output, attempts = self._run_wipe(ctx, log)
        except Exception as exc:  # noqa: BLE001 - 统一翻成步骤失败
            return self._fail(f"无法执行 diskpart：{exc}")
        if code != 0:
            return self._fail(
                self._wipe_failure_message(code, attempts),
                code=code,
                output=output,
                extra={
                    "diskpart_attempts": attempts,
                    "settle_ms": self._settle_ms,
                    # 失败那一刻别的盘在干什么（用于判断是不是 I/O 抢占）
                    "parallel_activity": activity.describe(exclude=ctx.disk_number, style="plain"),
                },
            )

        # 盘已经空了：从这里开始取消也"不是什么都没发生"，消息要说清楚状态。
        if ctx.cancelled():
            return PhaseOutcome(
                ok=False,
                cancelled=True,
                disk_touched=True,
                message="任务已取消（盘已清空，未建分区）",
            )

        # ===== ② 找到 convert gpt 自动建的 MSR =====
        # 查询本身失败 → 中止，绝不盲删（select partition 猜错会删掉真分区）。
        try:
            msr = self.shell.find_msr(ctx.disk_number)
        except Exception as exc:  # noqa: BLE001 - 查不了就别动盘
            return self._fail(
                f"无法确认 MSR 分区，已中止（避免误删）：{exc}", disk_touched=True
            )

        # ===== ③ 单独删 MSR + 立刻复查 =====
        if msr is not None:
            self._settle(ctx, log, "删除 MSR 前")
            try:
                code, output = self.shell.run_diskpart(
                    msr_delete_lines(ctx.disk_number, msr), title="diskpart 脚本（删除 MSR）"
                )
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    f"删除 MSR 分区时无法执行 diskpart：{exc}", disk_touched=True
                )
            if code != 0:
                return self._fail(
                    f"删除 MSR 分区失败（退出码 {describe_diskpart_exit(code)}）："
                    f"{tail(output, 500)}",
                    code=code,
                    output=output,
                    disk_touched=True,
                )
            try:
                leftover = self.shell.find_msr(ctx.disk_number, label="复查 MSR")
            except Exception as exc:  # noqa: BLE001
                return self._fail(f"删除 MSR 后无法复查：{exc}", disk_touched=True)
            if leftover is not None:
                return self._fail(
                    f"MSR 分区删除失败：删除后仍然存在（分区 #{leftover}），已中止，未建任何分区",
                    disk_touched=True,
                )
            log.write("MSR 分区已删除，复查确认不存在")

        # ===== ④ 建分区（最后一项不带 size=）=====
        self._settle(ctx, log, "建分区前")
        try:
            code, output = self.shell.run_diskpart(
                create_partition_lines(ctx.disk_number, ctx.parts),
                title="diskpart 脚本（建分区）",
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"无法执行 diskpart：{exc}", disk_touched=True)
        if code != 0:
            return self._fail(
                f"建分区失败（退出码 {describe_diskpart_exit(code)}）",
                code=code,
                output=output,
                disk_touched=True,
            )

        # ===== ⑤ 分区验收（独立的 layout 步骤）=====
        try:
            ok, message, details = self.acceptance.run(ctx.disk_number, ctx.parts, log)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"无法验证分区结果：{exc}", disk_touched=True)
        if not ok:
            return PhaseOutcome.failed(
                *self._created_results(ctx, verified=False),
                PhaseResult.failed("layout", f"分区验收不通过：{message}", **details),
                message=f"分区验收不通过：{message}",
                disk_touched=True,
            )

        results = [
            *self._created_results(ctx, verified=True),
            PhaseResult.ok("layout", message, **details),
        ]
        return PhaseOutcome.succeeded(
            *results,
            disk_touched=True,
            efi_partition=details.get("efi_partition"),
            details={"verification": details},
        )

    # ------------------------------------------------------------------ 清盘重试
    def _run_wipe(self, ctx: PhaseContext, log: Any) -> tuple[int, str, int]:
        """跑「清盘 + 转 GPT」，返回 ``(退出码, 输出, 实际尝试次数)``。

        只在**瞬时类**失败（0x80070017 数据错误 / 0x80070015 设备未就绪 / I/O 类）
        上重试，且重试前 rescan + 退避等待：权限不足、参数不对这类错误重试也没用，
        早点把真实原因报给用户。
        """
        total = self.wipe_attempts
        code = -1
        output = ""
        for attempt in range(1, total + 1):
            code, output = self.shell.run_diskpart(
                wipe_lines(ctx.disk_number), title="diskpart 脚本（清盘 + 转 GPT）"
            )
            if code == 0:
                return 0, output, attempt
            if attempt >= total or not is_transient_diskpart_exit(code):
                return code, output, attempt
            wait = self.retry_delay * attempt
            log.write(
                f"清盘第 {attempt}/{total} 次失败（退出码 {describe_diskpart_exit(code)}），"
                f"{wait:.0f}s 后重新扫描并重试"
            )
            if wait > 0:
                self._sleep(wait)
            self.shell.rescan()
        return code, output, total

    def _settle(self, ctx: PhaseContext, log: Any, reason: str) -> None:
        """两次 diskpart 之间的等待（``partitionSettleMs``，0 = 不等待）。

        顺便记一条诊断：如果此时有别的盘在写镜像，说明这条命令是在 I/O 争用下跑的，
        真出问题时有据可查。
        """
        others = activity.describe(exclude=ctx.disk_number)
        if self._settle_ms <= 0:
            if others:
                log.write(f"[诊断] {reason}：其它盘正在 {others}（未启用间隔等待）")
            return
        seconds = self._settle_ms / 1000.0
        suffix = f"；其它盘正在 {others}" if others else ""
        log.write(f"{reason}：等待 {self._settle_ms} ms 让磁盘链路落盘{suffix}")
        self._sleep(seconds)

    def _wipe_failure_message(self, code: int, attempts: int) -> str:
        message = f"diskpart 失败（退出码 {describe_diskpart_exit(code)}）"
        if attempts > 1:
            message += f"；已自动重试 {attempts - 1} 次仍失败"
        hint = diskpart_exit_hint(code)
        if hint:
            message += f"。{hint}"
        return message

    # ------------------------------------------------------------------ 组装
    def _created_results(self, ctx: PhaseContext, *, verified: bool) -> list[PhaseResult]:
        """建好分区后给 EFI / Windows / Data 各一条结果（数据在 details 里）。

        ``verified`` 只影响文案：验收未通过时说“已创建并格式化”，不冒充通过。
        """
        suffix = "已创建、格式化并通过验证" if verified else "已创建并格式化（验收未通过）"
        by_role = {part.role: part for part in ctx.parts}
        results = [
            PhaseResult.ok(
                "efi",
                f"{suffix}（{by_role['efi'].size_text}，盘符 {by_role['efi'].letter}）",
                letter=by_role["efi"].letter,
                size=by_role["efi"].size_text,
                file_system=by_role["efi"].file_system,
                label=by_role["efi"].label,
            )
        ]
        windows = by_role["windows"]
        results.append(
            PhaseResult.ok(
                "windows",
                f"{suffix}（{windows.size_text}，盘符 {windows.letter}）",
                letter=windows.letter,
                size=windows.size_text,
                file_system=windows.file_system,
                label=windows.label,
            )
        )
        data_parts = [part for part in ctx.parts if part.is_data]
        data_summary = " + ".join(f"{part.label} {part.size_text}" for part in data_parts)
        data_letters = "/".join(str(part.letter or "") for part in data_parts)
        results.append(
            PhaseResult.ok(
                "data",
                f"{suffix}（{data_summary}，盘符 {data_letters}）",
                letters=[part.letter for part in data_parts],
                partitions=[part.to_dict() for part in data_parts],
            )
        )
        return results

    def _fail(
        self,
        message: str,
        *,
        code: int | None = None,
        output: str | None = None,
        disk_touched: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> PhaseOutcome:
        """分区失败：``efi`` 记为失败，同组其余步骤补 ``skipped``。

        "失败归到 efi 步骤"是历史行为（四段 diskpart 的分区组在界面上是 3 个
        子步骤），保持不改；系统组的 ``skipped`` 由编排层统一补——
        分区阶段不需要知道它们叫什么。
        """
        details: dict[str, Any] = {}
        if code is not None:
            details["diskpart_exit"] = code
        if output is not None:
            details["diskpart_tail"] = tail(output)
        if extra:
            details.update(extra)
        results = [PhaseResult.failed("efi", message, **details)]
        for key in steps_after("partition", "efi"):
            results.append(PhaseResult.skipped(key, "分区未完成，后续步骤未执行"))
        return PhaseOutcome.failed(
            *results, message=message, disk_touched=disk_touched, details=details
        )


class EfiLetterReleaser:
    """任务收尾：按设置决定是否释放 EFI 临时盘符。

    这是"释放"这段逻辑的唯一入口，编排层在 ``finally`` 里只调它一次：

    - 设置 ``releaseEfiLetter`` 为真（默认）→ 按"磁盘 + 分区号"摘掉临时盘符
      （避免隐藏卷的盘符残留成幽灵盘符，下次 ``assign`` 会失败）；
    - 关掉 → 只写一条日志，**不动盘**，盘符留着给用户手动查看引导文件；
    - 本次任务没动过盘（前置失败）→ 直接返回。

    为什么不按字母选卷：计划阶段的字母只保证"当时空闲"，执行期间可能被其它卷
    占用；ESP 又是隐藏卷，``Get-Partition -DriveLetter`` 根本查不到它，按字母
    反查会稳定地"查空 → 跳过释放"。
    """

    def __init__(self, shell: DiskpartShell) -> None:
        self.shell = shell

    def release(
        self,
        *,
        disk_number: int,
        letter: str | None,
        outcome: PhaseOutcome | None,
        options: Mapping[str, Any] | None,
        log: Any,
    ) -> None:
        if outcome is None or not outcome.disk_touched:
            return
        if not letter:
            return
        wanted = str(letter).strip().upper()
        if not option_flag(options, "release_efi_letter", default=True):
            log.write(
                f"已关闭 EFI 盘符释放（设置 releaseEfiLetter=false），保留 {wanted}: "
                "不执行 remove letter"
            )
            return
        number = outcome.efi_partition
        if number is None:
            try:
                number = self.shell.find_esp(disk_number)
            except Exception as exc:  # noqa: BLE001 - 查不了就不动
                log.write(f"释放 EFI 盘符 {wanted} 前无法定位 ESP 分区，已跳过：{exc}")
                return
        if number is None:
            log.write(
                f"磁盘 {disk_number} 上找不到 ESP 分区，跳过释放盘符 {wanted}"
                "（盘可能还没分区，或分区表已被改动）"
            )
            return
        self.shell.release_letter(disk_number, int(number), wanted)


__all__ = ["EfiLetterReleaser", "PartitionAcceptance", "PartitionPhase"]
