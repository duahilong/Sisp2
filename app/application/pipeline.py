"""编排：分区组 → 系统组 → 校验组，以及"失败后谁跳过"的唯一实现。

**为什么必须有这一层**：以前"分区失败就把镜像/引导记成 skipped"这条语义散在
``_partition_phase`` / ``_fail_diskpart`` 里，分区阶段被迫认识下游步骤的名字。
现在分组顺序与停止条件都在这里（数据来自 ``steps.py``），分区阶段只需回答
"我成没成功"，不再关心后面还有谁。

取消语义也在这里三档落地（与历史行为一致）：

- 清盘前取消 → 什么都没发生；
- 清盘后取消 → 盘已清空、未建分区（这条消息由分区阶段给出）；
- 分区完成之后取消 → 分区已建好、镜像未应用。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.application.partitions import (
    PartitionLayoutError,
    allow_small_partitions,
    check_minimums,
    plan_partitions,
)
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.phases.partition import EfiLetterReleaser, PartitionPhase
from app.application.phases.system import SystemPhase
from app.application.phases.verify import BootVerifyPhase
from app.application.steps import (
    GROUP_OF,
    STEP_KEYS,
    following_groups,
    steps_after,
)


@dataclass
class PipelineResult:
    """一次编排的结果：步骤 + 失败原因 + 分区结论（收尾用）。"""

    results: list[PhaseResult] = field(default_factory=list)
    failure: str | None = None
    partition: PhaseOutcome | None = None

    def add(self, item: PhaseResult, ctx: PhaseContext | None = None) -> None:
        """记一个步骤结果，**同时**上报进度（界面进度条靠这条链路）。

        以前每步的 ``on_step`` 由 ``_run_steps`` 逐个调用；拆成阶段以后，
        阶段只负责产出 ``PhaseResult``，所以“结果 → 进度事件”的翻译收敛到
        这里：少了它，真实任务会从头到尾只显示第一条 running 事件。
        """
        self.results.append(item)
        if ctx is not None:
            ctx.notify(item.key, item.status, item.message)

    @property
    def ok(self) -> bool:
        return self.failure is None and not any(
            result.status == "failed" for result in self.results
        )


class JobPipeline:
    """把三个互不依赖的阶段按组串起来。"""

    def __init__(
        self,
        *,
        partition: PartitionPhase,
        system: SystemPhase,
        verify: BootVerifyPhase | None = None,
        cleanup: EfiLetterReleaser | None = None,
    ) -> None:
        self.partition = partition
        self.system = system
        self.verify = verify
        self.cleanup = cleanup

    # ------------------------------------------------------------------ 前置
    def precheck(
        self,
        ctx: PhaseContext,
        *,
        missing_tools: Sequence[str] = (),
    ) -> PipelineResult | None:
        """动盘之前能拦下来的都拦下来（返回 ``None`` 表示可以继续）。

        - 缺系统工具 → 全部步骤记 skipped（与历史行为一致）；
        - 布局非法 / 分区过小 / 缺盘符 / 缺镜像 → 直接失败，**不产生步骤**。
        """
        if missing_tools:
            result = PipelineResult(
                failure=(
                    f"缺少系统工具：{'、'.join(missing_tools)}"
                    "（真实写盘需要管理员环境里的这些命令）"
                )
            )
            for key in STEP_KEYS:
                result.add(PhaseResult.skipped(key, "前置条件不满足"), ctx)
            return result

        if not ctx.parts:
            return PipelineResult(failure="分区计划为空")
        missing_letters = [part.role for part in ctx.parts if not part.letter]
        if missing_letters:
            return PipelineResult(failure=f"缺少盘符分配：{', '.join(missing_letters)}")
        if not ctx.image_path:
            return PipelineResult(failure="配置里缺少 image_info.image_path")
        if not Path(str(ctx.image_path)).is_file():
            return PipelineResult(failure=f"镜像文件不存在: {ctx.image_path}")
        return None

    @staticmethod
    def build_parts(payload: Any, letters: dict[str, str]) -> tuple[Any, str | None]:
        """布局的唯一真相；非法配置在这里就失败（还没碰到磁盘）。"""
        try:
            parts = plan_partitions(payload or {}, letters=letters)
        except PartitionLayoutError as exc:
            return None, str(exc)
        too_small = check_minimums(parts, allow_small=allow_small_partitions(payload or {}))
        if too_small:
            return None, "分区过小，已中止（未碰磁盘）：" + "；".join(too_small)
        return parts, None

    # ------------------------------------------------------------------ 执行
    def run(self, ctx: PhaseContext) -> PipelineResult:
        result = PipelineResult()

        # ===== 组 1：硬盘分区（含分区验收）=====
        part = self.partition.run(ctx)
        result.partition = part
        for item in part.results:
            result.add(item, ctx)
        if not part.ok:
            result.failure = part.message
            if not part.cancelled:
                self._skip_after(result, "partition", part, ctx)
            return result

        # 分区完成之后、动镜像之前的取消检查点
        if ctx.cancelled():
            self._skip_groups(
                result,
                ("system", "verify"),
                "任务已取消（分区已建好，镜像未应用）",
                ctx,
            )
            result.failure = "任务已取消（分区已建好，镜像未应用）"
            return result

        # ===== 组 2：写入系统（镜像 + 引导）=====
        system = self.system.run(ctx)
        for item in system.results:
            result.add(item, ctx)
        if not system.ok:
            result.failure = system.message
            if not system.cancelled:
                self._skip_after(result, "system", system, ctx)
            return result

        # ===== 组 3：装机结果校验 =====
        if self.verify is not None:
            checked = self.verify.run(ctx)
            for item in checked.results:
                result.add(item, ctx)
            if not checked.ok:
                result.failure = checked.message
        return result

    # ------------------------------------------------------------------ 收尾
    def cleanup_letter(
        self,
        ctx: PhaseContext,
        *,
        partition: PhaseOutcome | None,
    ) -> None:
        """任务收尾释放 EFI 临时盘符（唯一入口，``finally`` 里调一次）。"""
        if self.cleanup is None:
            return
        self.cleanup.release(
            disk_number=ctx.disk_number,
            letter=(ctx.letters or {}).get("efi"),
            outcome=partition,
            options=ctx.options,
            log=ctx.log,
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _skip_after(
        result: PipelineResult,
        group: str,
        outcome: PhaseOutcome,
        ctx: PhaseContext | None = None,
    ) -> None:
        """某组失败后：补同组剩余步骤 + 后续整组的 ``skipped``（按 key 去重）。"""
        emitted = {item.key for item in result.results}
        failed = [item.key for item in outcome.results if item.status == "failed"]
        anchor = failed[-1] if failed else None
        if anchor is not None:
            for key in steps_after(group, anchor):
                if key not in emitted:
                    result.add(PhaseResult.skipped(key, "前一步失败，已跳过"), ctx)
                    emitted.add(key)
        reason = _reason_text(group)
        for name in following_groups(group):
            for key in STEP_KEYS:
                if key in emitted or GROUP_OF.get(key) != name:
                    continue
                result.add(PhaseResult.skipped(key, f"未执行（{reason}）"), ctx)
                emitted.add(key)

    @staticmethod
    def _skip_groups(
        result: PipelineResult,
        groups: Sequence[str],
        message: str,
        ctx: PhaseContext | None = None,
    ) -> None:
        emitted = {item.key for item in result.results}
        for key in STEP_KEYS:
            if key in emitted or GROUP_OF.get(key) not in groups:
                continue
            result.add(PhaseResult.skipped(key, message), ctx)


def _reason_text(group: str) -> str:
    """"未执行"的括号说明（让界面一眼看懂是哪个阶段没过）。"""
    if group == "partition":
        return "分区验收未通过，未写入任何镜像"
    if group == "system":
        return "写入系统未完成，未做装机校验"
    return "前一步失败"


__all__ = ["JobPipeline", "PipelineResult"]
