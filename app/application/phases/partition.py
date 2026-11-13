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
from app.application.disk_label import disk_label
from app.application.partitions import (
    FORMAT_OVERRIDE,
    PlannedPartition,
    create_geometry_lines,
    format_lines,
    missing_partition_lines,
    msr_delete_lines,
    partition_verdict,
    wipe_lines,
)
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import option_flag, option_int, tail, to_int
from app.application.shell import (
    DiskpartShell,
    describe_diskpart_exit,
    diskpart_exit_hint,
    is_transient_diskpart_exit,
)
from app.application.steps import steps_after
from app.orchestrator import build_worker_disk_identity, identity_matches
from app.units import BYTES_PER_MB

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

# 「建分区」中途失败（典型：某个 format 报 0x80042413 卷被占用）后，
# 按真实分区表续跑的次数。format 失败通常是别的进程刚抓了一下新卷，
# 重扫 + 等一下就能过；续跑是按现场状态补齐，不会重复 create。
CREATE_REPAIR_ATTEMPTS = 2

# 每段 diskpart 之前复核「要动的还是不是确认时那块盘」的查询次数。
# 身份缺失、查询失败或状态不一致都必须中止：这里宁可 fail-closed，
# 也不能因为 PowerShell 抖动而继续 clean 一块无法确认的盘。
DISK_GUARD_ATTEMPTS = 2

# 单个分区的格式化尝试次数与等待（秒）。现场证据：
#  - 2026-11-10：建完分区立刻 format → 0x80042413，同一条命令重试也失败；
#  - 2026-11-11：三块盘全部卡在 EFI 分区，diskpart 明确提示「请使用 OVERRIDE 参数」。
# 所以第一次就带 override（见 ``partitions.FORMAT_OVERRIDE``），
# ``format.com /X`` 作为第 2、4 次手段，每次都 rescan + 退避。
FORMAT_ATTEMPTS = 4
FORMAT_BACKOFF: tuple[float, ...] = (1.0, 5.0, 10.0)
# 每次尝试用哪个格式化途径（第 2 / 4 次用 format.com /X 强制卸载）。
FORMAT_PLAN: tuple[str, ...] = ("diskpart", "format.com", "diskpart", "format.com")

# 我们自己的哨兵码（不是 Windows 错误码，绝不能当成 diskpart 退出码去翻译）：
#  ``TOOL_NOT_STARTED`` 进程没起来（找不到/被拦/不是有效程序）→ 换下一种途径；
#  ``FALLBACK_REFUSED`` 安全闸拒绝（盘符归属核对不上/拿不到盘符）→ 也换下一种途径。
# 两者都**不是**"这块盘废了"，以前把它们当永久失败直接 break，把后面真正能用的
# 手段（diskpart + override）全掐掉了——2026-11-11 现场就是这么失败的。
TOOL_NOT_STARTED = -1
FALLBACK_REFUSED = -2

# 隐藏分区（ESP/OEM/恢复）没有普通卷的 ``drive_letter``，只有 ``mounted_letter``。
HIDDEN_PARTITION_TYPES = frozenset({"system", "oem", "recovery", "unknown"})

# 建完分区后等「分区表/卷」就绪的上限与轮询间隔（秒）。
VOLUME_READY_TIMEOUT = 12.0
VOLUME_READY_INTERVAL = 0.4

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
        create_repair_attempts: int = CREATE_REPAIR_ATTEMPTS,
        format_attempts: int = FORMAT_ATTEMPTS,
        format_backoff: Sequence[float] = FORMAT_BACKOFF,
        format_plan: Sequence[str] = FORMAT_PLAN,
        format_force: bool = FORMAT_OVERRIDE,
        ready_timeout: float = VOLUME_READY_TIMEOUT,
        ready_interval: float = VOLUME_READY_INTERVAL,
    ) -> None:
        self.shell = shell
        self.acceptance = acceptance or PartitionAcceptance(shell, verdict)
        self.wipe_attempts = max(1, int(wipe_attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self._sleep = sleep
        self._settle_ms = DEFAULT_SETTLE_MS
        self.create_repair_attempts = max(0, int(create_repair_attempts))
        self.format_attempts = max(1, int(format_attempts))
        self.format_backoff = tuple(max(0.0, float(item)) for item in format_backoff)
        self.format_plan = tuple(str(item) for item in format_plan) or ("diskpart",)
        self.format_force = bool(format_force)
        self.ready_timeout = max(0.0, float(ready_timeout))
        self.ready_interval = max(0.05, float(ready_interval))

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

        # ===== 0：动盘之前复核身份（热插拔会让盘号漂移）=====
        guard = self._guard(ctx, log, where="清盘", disk_touched=False)
        if guard is not None:
            return guard

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

        # ===== ③ 单独删 MSR（瞬时错误可重试）+ 立刻复查 =====
        if msr is not None:
            failure = self._delete_msr(ctx, log, int(msr))
            if failure is not None:
                return failure

        # ===== ④ 建分区（**只建几何**：不格式化、不分配盘符）=====
        # 清盘已经动过盘，这里再看一眼：盘要是中途掉线/换了，立刻停手。
        guard = self._guard(ctx, log, where="建分区", disk_touched=True)
        if guard is not None:
            return guard
        self._settle(ctx, log, "建分区前")
        try:
            code, output, repairs = self._run_create(ctx, log)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"无法执行 diskpart：{exc}", disk_touched=True)
        if code != 0:
            return self._fail(
                self._create_failure_message(code, repairs),
                code=code,
                output=output,
                disk_touched=True,
                extra={
                    "diskpart_repairs": repairs,
                    "settle_ms": self._settle_ms,
                    "parallel_activity": activity.describe(
                        exclude=ctx.disk_number, style="plain"
                    ),
                },
            )

        # ===== ⑤ 等卷就绪 + 逐分区格式化（带退避重试与 format.com /X 兜底）=====
        # 现场根因就在这一段：建完分区立刻 format，新卷还在被系统异步挂载，
        # 独占锁拿不到 → 0x80042413「设备使用中」。
        self._wait_volumes(ctx, log)
        for position, part in enumerate(ctx.parts, start=1):
            failure = self._format_one(ctx, log, position, part)
            if failure is not None:
                return failure

        # ===== ⑥ 分区验收（独立的 layout 步骤）=====
        try:
            ok, message, details = self.acceptance.run(ctx.disk_number, ctx.parts, log)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"无法验证分区结果：{exc}", disk_touched=True)
        if not ok and self._repair_after_acceptance(ctx, log):
            # 补做成功（典型：某个分区没格式化 / 盘符没挂上），再验一次
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

    # ------------------------------------------------------------------ 动盘前复核
    def _guard(
        self,
        ctx: PhaseContext,
        log: Any,
        *,
        where: str,
        disk_touched: bool,
    ) -> PhaseOutcome | None:
        """每段 diskpart 之前复核磁盘身份与健康状态；返回非 ``None`` 表示中止。

        为什么值得多两次 PowerShell（各约 0.4 s）：**盘号会变**。批量装机时用户
        拔插、托盘掉线、USB 重连都可能让「磁盘 4」变成另一块盘，而 ``clean``
        是不可逆的。任务开始前的写盘闸门只保证"那一刻"是对的，分区阶段可能已经
        在几分钟之后（排队 + 其它盘在跑）。
        """
        expected = dict(ctx.identity or {})
        if not expected:
            log.write(f"[复核] {where}前缺少确认时的磁盘身份，拒绝继续写盘")
            return self._fail(
                f"{where}前复核未通过：任务缺少确认时的磁盘身份信息。"
                "已中止，不能仅凭磁盘号执行清盘或分区",
                disk_touched=disk_touched,
                extra={"guard_stage": where, "guard_reasons": ["缺少确认时的磁盘身份"]},
            )

        state: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, DISK_GUARD_ATTEMPTS + 1):
            try:
                state = self.shell.disk_state(ctx.disk_number)
                break
            except Exception as exc:  # noqa: BLE001 - 统一在下方按复核失败中止
                last_error = exc
                if attempt < DISK_GUARD_ATTEMPTS:
                    self._sleep(0.3)
        if state is None:
            log.write(f"[复核] {where}前查不到磁盘状态，拒绝继续写盘：{last_error}")
            return self._fail(
                f"{where}前复核未通过：无法读取磁盘身份与健康状态（{last_error}）。"
                "已中止，不能在无法确认目标磁盘时继续写盘",
                disk_touched=disk_touched,
                extra={
                    "guard_stage": where,
                    "guard_reasons": ["磁盘状态查询失败"],
                    "guard_error": str(last_error),
                },
            )

        reasons: list[str] = []
        if state.get("is_offline"):
            reasons.append("磁盘处于脱机状态")
        if state.get("is_read_only"):
            reasons.append("磁盘是只读的")
        health = str(state.get("health_status") or "").strip().lower()
        if health == "unhealthy":
            reasons.append("磁盘健康状态为 Unhealthy")
        actual = build_worker_disk_identity(state)
        if not identity_matches(expected, actual, require_strong=False):
            reasons.append(
                "身份与确认时不一致：这块盘已经不是你确认的那块"
                "（型号/序列号/容量对不上，可能是盘号漂移或换盘）"
            )
        if health and health not in ("healthy", "unknown"):
            log.write(f"[复核] 注意：磁盘健康状态为 {state.get('health_status')}，继续执行")

        if reasons:
            return self._fail(
                f"{where}前复核未通过：{'；'.join(reasons)}。"
                f"已中止，没有继续写这块盘（{ctx.title()}）",
                disk_touched=disk_touched,
                extra={
                    "guard_stage": where,
                    "guard_reasons": list(reasons),
                    "guard_state": {
                        key: state.get(key)
                        for key in (
                            "model",
                            "serial_number",
                            "unique_id",
                            "size_bytes",
                            "is_offline",
                            "is_read_only",
                            "health_status",
                        )
                    },
                },
            )
        log.write(f"[复核] {where}前磁盘正常（{ctx.title()}）")
        return None

    # ------------------------------------------------------------------ MSR
    def _delete_msr(self, ctx: PhaseContext, log: Any, msr: int) -> PhaseOutcome | None:
        """删除 ``convert gpt`` 自动建的 MSR；瞬时错误重试，返回非 ``None`` = 失败。

        这一段以前是"跑一次，失败就整盘作废"。``delete partition override`` 在
        慢盘/USB 桥接上偶发 ``0x80070017``/``0x8007045D``，重扫一次通常就过；
        而且它**可以安全重试**——重试前先复查 MSR 还在不在：上一条可能其实删掉了
        只是报了个错（那种情况下再删会撞"分区不存在"，所以必须先查）。
        """
        total = self.wipe_attempts
        number = int(msr)
        code = 0
        output = ""
        for attempt in range(1, total + 1):
            reason = "删除 MSR 前" if attempt == 1 else f"删除 MSR 前（第 {attempt} 次）"
            self._settle(ctx, log, reason)
            try:
                code, output = self.shell.run_diskpart(
                    msr_delete_lines(ctx.disk_number, number),
                    title="diskpart 脚本（删除 MSR）",
                )
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    f"删除 MSR 分区时无法执行 diskpart：{exc}", disk_touched=True
                )
            if code == 0:
                break
            if not is_transient_diskpart_exit(code) or attempt >= total:
                return self._fail(
                    f"删除 MSR 分区失败（退出码 {describe_diskpart_exit(code)}）："
                    f"{tail(output, 500)}",
                    code=code,
                    output=output,
                    disk_touched=True,
                )
            # 重试前复查：上一条可能"删掉了但报错"，再删会撞"分区不存在"
            try:
                still = self.shell.find_msr(ctx.disk_number, label="复查 MSR")
            except Exception as exc:  # noqa: BLE001 - 查不到就只能按原分区号重试
                log.write(f"复查 MSR 失败（按原分区号重试）：{exc}")
                still = number
            if still is None:
                log.write("复查 MSR：已经不存在（上一条其实是成功的），继续下一步")
                code = 0
                break
            number = int(still)
            wait = self.retry_delay * attempt
            log.write(
                f"删除 MSR 第 {attempt}/{total} 次失败（{describe_diskpart_exit(code)}），"
                f"{wait:.0f}s 后重新扫描并重试"
            )
            if wait > 0:
                self._sleep(wait)
            self.shell.rescan()

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
        return None

    # ------------------------------------------------------------------ 验收补救
    def _repair_after_acceptance(self, ctx: PhaseContext, log: Any) -> bool:
        """验收不通过时，把「没格式化 / 没挂盘符」的分区按原路径再补一次。

        补做复用逐分区那条路径（含 format.com /X 兜底），比单发一条 diskpart
        脚本更可能真的修好；大小/类型不符这类"补不动"的问题不硬来——直接交给
        验收报错，人工判断比乱改安全。
        """
        if self.create_repair_attempts <= 0:
            return False
        try:
            actual = self.shell.query(ctx.disk_number)
        except Exception as exc:  # noqa: BLE001
            log.write(f"验收不通过后无法查询真实分区表，放弃补做：{exc}")
            return False
        pending = [
            (pos, part)
            for pos, part in enumerate(ctx.parts, start=1)
            if _needs_format_or_letter(actual, pos, part)
        ]
        if not pending:
            log.write("验收不通过，但盘上没有可补做的项（大小/类型不符只能人工处理）")
            return False
        log.write(
            "验收不通过：按真实分区表补做 "
            + "、".join(f"#{pos}（{part.label}）" for pos, part in pending)
        )
        for pos, part in pending:
            got = _actual_partition(actual, pos)
            file_system = str(got.get("file_system") or "").strip().upper()
            wanted = str(part.letter or "").strip().upper()[:1]
            letter_only = (
                bool(got)
                and file_system == str(part.file_system or "").strip().upper()
                and bool(wanted)
                and _letter_of(got) != wanted
            )
            if letter_only:
                corrected = self._ensure_letter(ctx, log, pos, part)
                if corrected != wanted or not self._letter_belongs_to(ctx, wanted, pos, part):
                    return False
                continue
            if self._format_one(ctx, log, pos, part) is not None:
                return False
        return True

    # ------------------------------------------------------------------ 建分区（几何）
    def _run_create(self, ctx: PhaseContext, log: Any) -> tuple[int, str, int]:
        """只建分区几何，返回 ``(退出码, 输出, 续跑次数)``。

        瞬时错误（CRC/设备未就绪/卷占用）按**真实分区表补齐缺失项**：重跑原脚本会
        撞「分区已存在」，所以只能按现场状态生成 create 行。这一段不格式化，
        失败也不会留下半格式化的卷。
        """
        code, output = self.shell.run_diskpart(
            create_geometry_lines(ctx.disk_number, ctx.parts),
            title="diskpart 脚本（建分区）",
        )
        if code == 0:
            return 0, output, 0
        if not is_transient_diskpart_exit(code):
            return code, output, 0

        repairs = 0
        log.write(f"建分区未完成（{describe_diskpart_exit(code)}），按真实分区表补齐缺失分区")
        for round_no in range(1, self.create_repair_attempts + 1):
            self._settle(ctx, log, f"补齐分区前（第 {round_no} 次）")
            try:
                self.shell.rescan()
            except Exception as exc:  # noqa: BLE001
                log.write(f"补齐分区前重新扫描失败（已忽略）：{exc}")
            try:
                actual = self.shell.query(ctx.disk_number)
            except Exception as exc:  # noqa: BLE001
                log.write(f"补齐分区前无法查询真实分区表，放弃补齐：{exc}")
                return code, output, repairs
            lines = missing_partition_lines(ctx.disk_number, ctx.parts, actual)
            if not lines:
                log.write("补齐检查：盘上的分区已经与计划一致，无需补做")
                return 0, output, repairs
            repairs += 1
            log.write(f"补齐第 {round_no} 次：只补缺失的 create 命令（合计 {len(lines) - 2} 条）")
            try:
                code, output = self.shell.run_diskpart(
                    lines, title=f"diskpart 脚本（补齐分区 {round_no}）"
                )
            except Exception as exc:  # noqa: BLE001
                log.write(f"补齐分区无法执行 diskpart：{exc}")
                return code, output, repairs
            if code == 0:
                return 0, output, repairs
        return code, output, repairs

    # ------------------------------------------------------------------ 卷就绪
    def _wait_volumes(self, ctx: PhaseContext, log: Any) -> None:
        """等「分区表 + 卷」都跟上真实状态，再开始格式化。

        刚从 ``create`` 出来的分区，Windows 的挂载管理器还在异步挂载/枚举；
        此时立刻 ``format`` 会因为拿不到独占锁而报 ``0x80042413``。这里轮询到
        分区数达标（最多 ``ready_timeout`` 秒），再补一次 settle。
        """
        expected = len(ctx.parts)
        deadline = time.monotonic() + self.ready_timeout
        found = 0
        while True:
            try:
                actual = self.shell.query(ctx.disk_number)
            except Exception as exc:  # noqa: BLE001 - 查不到就按"已就绪"往下走
                log.write(f"等待卷就绪时查询失败（继续格式化）：{exc}")
                break
            found = len(actual.get("partitions") or ())
            if found >= expected:
                log.write(f"卷就绪：盘上已有 {found} 个分区（计划 {expected}）")
                break
            if time.monotonic() >= deadline:
                log.write(
                    f"等待卷就绪超时：只看到 {found} 个分区（计划 {expected}），"
                    "继续尝试格式化（失败会自动重试）"
                )
                break
            self._sleep(self.ready_interval)
        # 注意：这里**不再**多等一次 ``partitionSettleMs``——逐分区的 ``_settle`` 已经
        # 等过（1.4.0 两处都等，白浪费 1 秒/分区）。

    # ------------------------------------------------------------------ 逐分区格式化
    def _format_one(
        self,
        ctx: PhaseContext,
        log: Any,
        position: int,
        part: PlannedPartition,
    ) -> PhaseOutcome | None:
        """格式化并分配盘符**一个**分区；返回非 ``None`` = 失败（已完成归因）。

        现场证据（2026-11-10 / 2026-11-11 两批日志）：
          1. ``diskpart format`` 卡 0% 十几秒 → ``0x80042413 设备使用中``；
          2. 紧接着重试（1 秒后）同样失败；
          3. 6~12 秒后再试仍然失败 —— 卷处于"被占用"的稳定状态；
          4. **diskpart 自己提示「若要继续操作，请使用 OVERRIDE 参数」**。

        所以策略是：第一次就带 ``override``（强制卸载卷，见
        :data:`app.application.partitions.FORMAT_OVERRIDE`），失败后 rescan +
        退避，再用 ``format.com /X``，交替进行。

        两条“这条路走不通”的情况（安全闸拒绝、工具没起来）只记日志并换下一种
        途径，**不算失败**——以前把它们当永久失败直接结束，正好把唯一能用的
        ``override`` 拖掉了（2026-11-11 三块盘的直接原因）。
        """
        code = 0
        output = ""
        history: list[str] = []
        total = self.format_attempts
        for attempt in range(1, total + 1):
            tool = self.format_plan[(attempt - 1) % len(self.format_plan)]
            label = self._tool_label(tool)
            if attempt > 1:
                wait = self._backoff(attempt - 2)
                log.write(
                    f"第 {attempt} 次尝试（{label}）前等待 {wait:.0f}s 并重新扫描磁盘"
                    f"（上一次：{describe_diskpart_exit(code)}）"
                )
                if wait > 0:
                    self._sleep(wait)
                try:
                    self.shell.rescan()
                except Exception as exc:  # noqa: BLE001
                    log.write(f"重试前重新扫描失败（已忽略）：{exc}")
            self._settle(
                ctx, log, f"格式化分区 #{position}（{part.label}，第 {attempt} 次）前"
            )

            if tool == "format.com":
                code, output = self._format_with_tool(ctx, log, position, part)
            else:
                try:
                    code, output = self.shell.run_diskpart(
                        format_lines(
                            ctx.disk_number, position, part, force=self.format_force
                        ),
                        title=(
                            f"diskpart 脚本（格式化 #{position} {part.label}"
                            f"{'，override' if self.format_force else ''}）"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    code, output = TOOL_NOT_STARTED, str(exc)
            if code == 0:
                letter_failure = self._confirm_letter(ctx, log, position, part)
                if letter_failure is not None:
                    return letter_failure
                log.write(
                    f"分区 #{position}（{part.label}）已格式化"
                    + (f"并分配盘符 {part.letter}" if part.letter else "（无盘符）")
                    + (f"（途径 {label}）" if label else "")
                )
                return None

            # 「这条路走不通」≠「这块盘废了」：记下来，换下一种途径继续。
            if code in (FALLBACK_REFUSED, TOOL_NOT_STARTED):
                why = "安全闸拒绝" if code == FALLBACK_REFUSED else "工具未启动"
                reason = tail(output, 200) or why
                log.write(
                    f"分区 #{position}（{part.label}）第 {attempt} 次途径 {label} 不可用"
                    f"（{why}：{reason}），继续下一种途径"
                )
                history.append(f"第{attempt}次 {label}：{why}（{reason}）")
                continue

            log.write(
                f"分区 #{position}（{part.label}）格式化失败"
                f"（{describe_diskpart_exit(code)}，途径 {label}）"
            )
            history.append(f"第{attempt}次 {label}：{describe_diskpart_exit(code)}")
            if not is_transient_diskpart_exit(code):
                break
        return self._partition_failure(
            ctx, position, part, code, output, history=history
        )

    def _tool_label(self, tool: str) -> str:
        """日志里给人看的途径名（diskpart 带 override 时要写出来）。"""
        if tool == "format.com":
            return "format.com /X"
        return "diskpart（含 override）" if self.format_force else "diskpart"

    def _format_with_tool(
        self,
        ctx: PhaseContext,
        log: Any,
        position: int,
        part: PlannedPartition,
    ) -> tuple[int, str]:
        """用 ``format.com /X`` 强制卸载后格式化（兜底途径）。

        **安全闸**：格式化前必须确认"这个盘符挂在我要格式化的那个分区上、
        容量也对得上"，否则宁可失败也绝不格式化别人的卷。

        拒绝时返回 :data:`FALLBACK_REFUSED`（不是错误码，由调用方换下一种途径）。
        """
        letter = self._ensure_letter(ctx, log, position, part)
        if not letter:
            return FALLBACK_REFUSED, "拿不到盘符，无法使用 format.com 兜底"
        if not self._letter_belongs_to(ctx, letter, position, part):
            return FALLBACK_REFUSED, (
                f"盘符 {letter}: 与分区 #{position}（{part.label}）对不上，"
                "放弃 format.com 兜底（防止误格式化其它卷）"
            )
        log.write(
            f"改用 format.com {letter}: /FS:{part.file_system} /Q /V:{part.label} /Y /X（强制卸载）"
        )
        try:
            return self.shell.format_letter(
                letter, file_system=part.file_system, label=part.label
            )
        except Exception as exc:  # noqa: BLE001
            return TOOL_NOT_STARTED, str(exc)

    def _ensure_letter(
        self,
        ctx: PhaseContext,
        log: Any,
        position: int,
        part: PlannedPartition,
    ) -> str:
        """确保这个分区有盘符（format.com 需要盘符）；返回实际可用的字母。

        ESP（EFI 系统分区）是**隐藏卷**：它的 ``drive_letter`` 永远是 ``null``，
        真盘符只出现在 ``mounted_letter`` 里。1.4.0 只看 ``drive_letter``，
        于是每次都以为"没盘符"→ 重新 assign → 再核对仍然"对不上"，
        恰好在最需要兜底的分区上把兜底拒掉（2026-11-11 现场三块盘全部如此）。
        """
        actual = None
        try:
            actual = self.shell.query(ctx.disk_number)
        except Exception as exc:  # noqa: BLE001
            log.write(f"兜底格式化前查询分区失败：{exc}")
        got = _actual_partition(actual, position)
        current = _letter_of(got)
        wanted = str(part.letter or "").strip().upper()[:1]
        if not wanted:
            return ""
        if current == wanted:
            return wanted
        if actual is None:
            return ""
        lines = [
            f"select disk {int(ctx.disk_number)}",
            f"select partition {int(position)}",
        ]
        if current and current != wanted:
            # Windows 可能已自动分配另一个字母；直接 assign 会保留旧字母并
            # 新增一个，而不是替换。先只移除这个分区的旧字母，再挂计划字母。
            lines.append(f"remove letter={current}")
            log.write(f"分区 #{position} 当前盘符为 {current}:，改为计划盘符 {wanted}:")
        lines.extend([f"assign letter={wanted}", "exit"])
        try:
            code, output = self.shell.run_diskpart(
                lines,
                title=f"diskpart 脚本（校正盘符 #{position}）",
            )
        except Exception as exc:  # noqa: BLE001
            log.write(f"兜底格式化前分配盘符失败：{exc}")
            return ""
        if code != 0:
            log.write(
                f"分配盘符 {wanted}: 失败（{describe_diskpart_exit(code)}）："
                f"{tail(output, 200)}"
            )
            return ""
        return wanted

    def _confirm_letter(
        self,
        ctx: PhaseContext,
        log: Any,
        position: int,
        part: PlannedPartition,
    ) -> PhaseOutcome | None:
        """格式化成功后确认盘符；被 Windows 自动改名时立即校正并复查。"""
        wanted = str(part.letter or "").strip().upper()[:1]
        if not wanted:
            return None
        try:
            actual = self.shell.query(ctx.disk_number)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                f"分区 #{position} 格式化后无法查询盘符：{exc}",
                disk_touched=True,
            )
        current = _letter_of(_actual_partition(actual, position))
        if current == wanted:
            return None
        corrected = self._ensure_letter(ctx, log, position, part)
        if corrected == wanted and self._letter_belongs_to(ctx, wanted, position, part):
            return None
        return self._fail(
            f"分区 #{position} 已格式化，但计划盘符 {wanted}: 未能分配或复查",
            disk_touched=True,
            extra={"failed_partition": position, "expected_letter": wanted},
        )

    def _letter_belongs_to(
        self,
        ctx: PhaseContext,
        letter: str,
        position: int,
        part: PlannedPartition,
    ) -> bool:
        """盘符是否真的挂在分区 #position 上（并且容量与计划相符）。

        严格性分两级，**不放松**：

        - 普通卷（有 ``drive_letter``）：必须精确命中这个盘符；
        - 隐藏分区（ESP/OEM/恢复）：允许用 ``mounted_letter`` 认定，但**只有**
          隐藏类型才能走这条道——数据分区如果只有 ``mounted_letter``，一律拒绝。

        两者都要过容量核对（误差 ≤ 2 MB），防止盘号漂移/认错卷。
        """
        try:
            actual = self.shell.query(ctx.disk_number)
        except Exception:  # noqa: BLE001
            return False
        got = _actual_partition(actual, position)
        if not got:
            return False
        wanted = str(letter).strip().upper()[:1]
        drive = str(got.get("drive_letter") or "").strip().upper()[:1]
        mounted = str(got.get("mounted_letter") or "").strip().upper()[:1]
        if drive:
            if drive != wanted:
                return False
        elif mounted:
            if mounted != wanted or not _is_hidden_partition(got):
                return False
        else:
            return False
        if part.size_mb is not None:
            actual_mb = (to_int(got.get("size_bytes")) or 0) / BYTES_PER_MB
            if abs(actual_mb - float(part.size_mb)) > 2:
                return False
        return True

    def _backoff(self, index: int) -> float:
        if not self.format_backoff:
            return 0.0
        return self.format_backoff[min(max(0, index), len(self.format_backoff) - 1)]

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

    def _create_failure_message(self, code: int, repairs: int) -> str:
        message = f"建分区失败（退出码 {describe_diskpart_exit(code)}）"
        if repairs:
            message += f"；已按真实分区表续跑 {repairs} 次仍未完成"
        hint = diskpart_exit_hint(code)
        if hint:
            message += f"。{hint}"
        return message

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

    def _partition_failure(
        self,
        ctx: PhaseContext,
        position: int,
        part: PlannedPartition,
        code: int,
        output: str,
        *,
        history: Sequence[str] = (),
    ) -> PhaseOutcome:
        """格式化失败：**归因到真正失败的那个分区**，而不是一律记在 EFI 上。

        以前不管是第几个分区挂的，界面都标在「创建 EFI 分区」✗ 上——现场看到
        EFI 明明成功却报它失败，误导排查方向（2026-11-10 现场就是第 2 个分区挂的）。

        ``history`` 是每条途径的结果（如"第1次 diskpart（含 override）：0x80042413 …"），
        有它就能在界面/导出包里一眼看出"试过什么、每条为何不行"，
        而不是只看到一个假的 ``0xFFFFFFFF 途径 format.com``。
        """
        tried = "；".join(history)
        if history:
            message = f"分区 #{position}（{part.label}）格式化失败：{len(history)} 次尝试均未成功"
            message += f"（{tried}）"
        elif code in (TOOL_NOT_STARTED, 0xFFFFFFFF):
            # 工具没跑起来：没有"退出码"可言，直接把原因写出来。
            reason = tail(output, 240) or "格式化工具未能启动"
            message = f"分区 #{position}（{part.label}）格式化失败：{reason}"
        elif code == FALLBACK_REFUSED:
            reason = tail(output, 240) or "兜底被安全闸拒绝"
            message = f"分区 #{position}（{part.label}）格式化失败：{reason}"
        else:
            message = (
                f"分区 #{position}（{part.label}）格式化失败"
                f"（退出码 {describe_diskpart_exit(code)}）"
            )
            hint = diskpart_exit_hint(code)
            if hint:
                message += f"。{hint}"
            else:
                # 没有现成提示的码：把原始输出第一段带上，否则界面只剩一个数字。
                detail = tail(output, 240)
                if detail:
                    message += f"：{detail}"
        if position > 1:
            message += f"（前面 {position - 1} 个分区已完成）"

        results: list[PhaseResult] = []
        for key in ("efi", "windows", "data"):
            positions = [
                (pos, item)
                for pos, item in enumerate(ctx.parts, start=1)
                if _step_of_position(pos) == key
            ]
            if not positions:
                continue
            states = [
                "ok" if pos < position else ("failed" if pos == position else "skipped")
                for pos, _item in positions
            ]
            if "failed" in states:
                results.append(
                    PhaseResult.failed(
                        key,
                        message,
                        diskpart_exit=code,
                        diskpart_tail=tail(output),
                        partition_number=position,
                    )
                )
            elif all(state == "ok" for state in states):
                summary = " + ".join(
                    f"{item.label} {item.letter or ''}".strip() for _pos, item in positions
                )
                results.append(
                    PhaseResult.ok(key, f"已创建、格式化并分配盘符（{summary}）")
                )
            else:
                results.append(PhaseResult.skipped(key, "该分区未执行（前面的分区失败）"))
        results.append(PhaseResult.skipped("layout", "分区未完成，未执行分区验收"))

        details: dict[str, Any] = {
            "diskpart_exit": code,
            "diskpart_tail": tail(output),
            "failed_partition": position,
            "settle_ms": self._settle_ms,
            "format_attempts": self.format_attempts,
            "format_force": self.format_force,
            "format_history": list(history),
            "parallel_activity": activity.describe(exclude=ctx.disk_number, style="plain"),
        }
        return PhaseOutcome.failed(
            *results, message=message, disk_touched=True, details=details
        )

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
        label: str = "",
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
            title = str(label or "").strip() or disk_label(disk_number)
            log.write(
                f"{title} 上找不到 ESP 分区，跳过释放盘符 {wanted}"
                "（盘可能还没分区，或分区表已被改动）"
            )
            return
        self.shell.release_letter(disk_number, int(number), wanted)


# ---------------------------------------------------------------- 模块内小工具


def _step_of_position(position: int) -> str:
    """第几个分区 → 界面上的步骤键（EFI / Windows / 数据分区）。"""
    if position <= 1:
        return "efi"
    if position == 2:
        return "windows"
    return "data"


def _letter_of(part: Mapping[str, Any] | None) -> str:
    """这个分区当前的盘符：普通卷看 ``drive_letter``，ESP/隐藏卷看 ``mounted_letter``。"""
    if not isinstance(part, Mapping):
        return ""
    for key in ("drive_letter", "mounted_letter"):
        value = str(part.get(key) or "").strip().upper()[:1]
        if value:
            return value
    return ""


def _is_hidden_partition(part: Mapping[str, Any] | None) -> bool:
    """是不是 ESP/OEM/恢复这类隐藏分区（它们只有 ``mounted_letter``）。"""
    if not isinstance(part, Mapping):
        return False
    kind = str(part.get("type") or "").strip().lower()
    if kind in HIDDEN_PARTITION_TYPES:
        return True
    gpt = str(part.get("gpt_type") or "").strip().lower().strip("{}")
    # c12a7328… = EFI 系统分区；de94bba4… = Windows 恢复分区
    return gpt.startswith(("c12a7328", "de94bba4"))


def _actual_partition(payload: Mapping[str, Any] | Any, number: int) -> Mapping[str, Any]:
    """真实分区表里编号为 ``number`` 的分区（找不到返回空字典）。"""
    partitions = payload.get("partitions") if isinstance(payload, Mapping) else None
    for part in partitions or ():
        if isinstance(part, Mapping) and to_int(part.get("number")) == int(number):
            return part
    return {}


def _needs_format_or_letter(
    payload: Mapping[str, Any] | Any,
    position: int,
    part: PlannedPartition,
) -> bool:
    """这个分区是否还缺「文件系统」或「计划盘符」。"""
    got = _actual_partition(payload, position)
    if not got:
        return True
    file_system = str(got.get("file_system") or "").strip().upper()
    if file_system in ("", "RAW", "UNKNOWN"):
        return True
    if file_system != str(part.file_system or "").strip().upper():
        return True
    return bool(part.letter and _letter_of(got) != str(part.letter).strip().upper()[:1])


__all__ = ["EfiLetterReleaser", "PartitionAcceptance", "PartitionPhase"]
