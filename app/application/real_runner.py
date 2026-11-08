"""真实写盘执行器 = 组装 + 编排（不再自己实现分区/镜像/引导/校验）。

历史包袱：这个文件曾经一手包办 diskpart 四段脚本、镜像写入、bcdboot、校验，以及
"分区失败就把后面步骤记成 skipped"的分支——分区阶段因此认识下游步骤名，
测试也只能 ``runner._run_diskpart = fake`` 这种"直接赋值"去 mock（2026-11-06
清盘事故的成因）。

现在职责只剩三件：

1. 把 ``spec`` 翻译成 :class:`~app.application.phases.base.PhaseContext`；
2. 组装壳（:mod:`app.application.shell`）与阶段（:mod:`app.application.phases`），
   交给 :class:`~app.application.pipeline.JobPipeline`；
3. 把编排结果翻译成 ``WorkerResult``（步骤 + 日志尾巴），并保证 ``finally``
   里释放 EFI 临时盘符。

想只跑分区？直接构造 ``PartitionPhase(DiskpartShell(...))``，不需要这个类。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from app.application.command_log import CommandLog, _CommandLog
from app.application.partitions import (
    create_partition_lines,
    diskpart_lines,
    msr_delete_lines,
    partition_verdict,
    preview_segments,
    preview_text,
    wipe_lines,
)
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.phases.copy import CopyAssetsPhase
from app.application.phases.ghost import GhostImagePhase
from app.application.phases.partition import EfiLetterReleaser, PartitionPhase
from app.application.phases.system import SystemPhase
from app.application.pipeline import JobPipeline, PipelineResult
from app.application.runner_utils import (
    RunnerError,
    existing_dir,
    option_flag,
    runtime_env,
    tail,
    to_int,
    to_text,
)
from app.application.shell import (
    BCDBOOT_TIMEOUT,
    DISKPART_TIMEOUT,
    GHOST_TIMEOUT,
    QUERY_ATTEMPTS,
    QUERY_INTERVAL,
    QUERY_TIMEOUT,
    RELEASE_TIMEOUT,
    VERIFY_TIMEOUT,
    DiskpartShell,
    ProcessShell,
    find_program,
)
from app.application.steps import STEP_KEYS, STEP_TITLES
from app.application.uefi import BootProbe, UefiBootPhase
from app.models import STEP_FAILED, STEP_OK, STEP_SKIPPED, StepResult, WorkerResult

_LOGGER = logging.getLogger(__name__)

# 数据分区只允许 NTFS（见 ``partitions.DATA_FS_CHOICES``）；EFI 固定 FAT32。
_FS_TOKENS = {"ntfs": "ntfs"}

# 兼容旧名字（外部脚本与测试在用）。
_partition_verdict = partition_verdict


def _make_workdir(disk_number: int) -> str | None:
    """给单个任务开一个独占的临时工作目录（Ghost 在里面写 ghosterr.txt）。

    多盘并发时不能用共享的日志目录：Ghost 的失败原因固定叫 ``GHOSTERR.TXT``，
    两个进程会互相覆盖，事后无法区分是哪块盘失败。
    """
    try:
        return tempfile.mkdtemp(prefix=f"sisp-ghost-{int(disk_number)}-")
    except OSError:
        _LOGGER.warning("无法创建 Ghost 临时目录", exc_info=True)
        return None


def _remove_workdir(path: str | None) -> None:
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)


def data_fs_token(payload: Mapping[str, Any]) -> str:
    """数据分区文件系统：``partition_info.data_fs`` → diskpart 参数。

    现在已经没有第二种可选值：数据分区恒为 NTFS。仍然过一遍白名单、
    而不是直接返回常量，是为了守住“用户输入不参与拼脚本”这条线——旧任务文件
    或第三方调用者送进来的 ``data_fs`` 只会被忽略，不会变成格式参数。
    """
    info = payload.get("partition_info") if isinstance(payload, Mapping) else None
    raw = str((info or {}).get("data_fs") or "NTFS").strip().lower()
    return _FS_TOKENS.get(raw, "ntfs")


def prune_runner_logs(log_dir: str | os.PathLike[str], retention_days: int) -> int:
    """Remove only this runner's log files older than the configured retention."""
    directory = Path(log_dir)
    if not directory.is_dir():
        return 0
    cutoff = datetime.now().timestamp() - max(1, int(retention_days)) * 86400
    removed = 0
    for pattern in ("disk-runner-*.log", "app.log.*"):
        for path in directory.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                _LOGGER.warning("无法清理过期执行日志: %s", path, exc_info=True)
    return removed


class WindowsDiskRunner:
    """默认真实执行器；构造好之后可直接当 ``real_runner`` 传给 ``SispService``。

    依赖注入（测试用）：

    - ``shell``：``DiskpartShell`` 形状的对象（分区阶段用），不传就按本次任务的
      日志/临时目录现造；
    - ``process``：``ProcessShell`` 形状的对象（Ghost/bcdboot/校验用）；
    - ``uefi``：``UefiBootPhase`` 形状的对象（``.run(ctx)``），写引导 + 校验，
      不传用默认实现（bcdboot 由 ``self.bcdboot`` 决定）；
    - ``pipeline_factory``：``(ctx) -> JobPipeline``，给需要换掉整个编排的测试用。

    这些缝让测试"注入替身"而不是"给 runner 的私有方法赋值"——后者在实现搬家
    时会静默失效，是 2026-11-06 清盘事故的直接原因。
    """

    def __init__(
        self,
        *,
        log_dir: str | os.PathLike[str] | None = None,
        diskpart: str | None = None,
        ghost: str | None = None,
        bcdboot: str | None = None,
        powershell: str | None = None,
        allow_real_disk: bool = False,
        shell: Any = None,
        process: Any = None,
        uefi: Any = None,
        pipeline_factory: Any = None,
        sequencer: Any = None,
        ghost_gate: Any = None,
    ) -> None:
        # 真实写盘授权：默认关闭。生产入口 build_real_runner() 会显式打开；
        # 测试/脚本里直接构造的 runner 只能"读"，执行 diskpart/format/bcdboot/ghost
        # 会被 app.safety_guard 拦下（2026-11-06 清盘事故的直接防线）。
        self.allow_real_disk = bool(allow_real_disk)
        self.log_dir = log_dir
        # diskpart 固定用系统自带的（不再由界面设置提供）；保留参数只供测试注入。
        self.diskpart = diskpart or find_program("diskpart.exe")
        # 装机只走 Ghost：ghost64 优先，取不到退 ghost32。
        self.ghost = ghost or find_program("ghost64.exe") or find_program("ghost32.exe")
        self.bcdboot = bcdboot or find_program("bcdboot.exe")
        self.powershell = powershell or find_program("powershell.exe")
        self._shell = shell
        self._process = process
        self._uefi = uefi
        self._pipeline_factory = pipeline_factory
        # 多盘并发：分区轮次闸门（``PartitionSequencer``）与 Ghost 启动间隔闸门。
        # 单盘时两者都为 None，行为与历史实现完全一致。
        self._sequencer = sequencer
        self._ghost_gate = ghost_gate

    # ------------------------------------------------------------------ 入口
    def __call__(
        self,
        spec: Any,
        cancel_event: threading.Event,
        on_step: Any = None,
    ) -> WorkerResult:
        return self.run(spec, cancel_event, on_step=on_step)

    # ------------------------------------------------------------------ 组装
    def _build_shell(
        self,
        log: CommandLog,
        options: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> Any:
        if self._shell is not None:
            return self._shell
        return DiskpartShell(
            log=log,
            diskpart=self.diskpart,
            powershell=self.powershell,
            allow_real_disk=self.allow_real_disk,
            env=env,
        )

    def _build_process(self, log: CommandLog, env: Mapping[str, str]) -> Any:
        if self._process is not None:
            return self._process
        return ProcessShell(
            log=log,
            allow_real_disk=self.allow_real_disk,
            env=env,
            source="WindowsDiskRunner",
        )

    def _build_pipeline(self, ctx: PhaseContext) -> JobPipeline:
        if self._pipeline_factory is not None:
            return self._pipeline_factory(ctx)
        shell = self._build_shell(ctx.log, ctx.options, ctx.env or {})
        process = self._build_process(ctx.log, ctx.env or {})
        uefi = self._uefi or UefiBootPhase(
            process,
            bcdboot=self.bcdboot,
            probe=BootProbe(process, powershell=self.powershell),
        )
        # ghosterr.txt 落在本任务独占的工作目录（多盘并发时不能共用日志目录，
        # 否则失败原因会互相覆盖）；没有 work_dir 时退回日志目录。
        cwd = ctx.work_dir or existing_dir(self.log_dir)
        # 收尾阶段：设置里留空时阶段自己记 skipped，所以这里无条件装配。
        copy_source = str(ctx.options.get("copy_source") or "")
        return JobPipeline(
            partition=PartitionPhase(shell),
            system=SystemPhase(
                GhostImagePhase(
                    process, ghost=self.ghost, cwd=cwd, start_gate=self._ghost_gate
                ),
                uefi,
            ),
            extras=CopyAssetsPhase(process, copy_source),
            cleanup=EfiLetterReleaser(shell),
            sequencer=self._sequencer,
        )

    # ------------------------------------------------------------------ 执行
    def run(
        self,
        spec: Any,
        cancel_event: threading.Event | None = None,
        *,
        on_step: Any = None,
    ) -> WorkerResult:
        cancel_event = cancel_event or threading.Event()
        disk_number = to_int(getattr(spec, "disk_number", None)) or 0
        payload = dict(getattr(spec, "task_payload", None) or {})
        letters = {
            str(key): str(value).strip().upper()[:1]
            for key, value in dict(getattr(spec, "drive_letters", None) or {}).items()
            if str(value).strip()
        }
        options = dict(getattr(spec, "runner_options", None) or {})
        log = CommandLog(self.log_dir, disk_number=disk_number)
        env = runtime_env()
        started = datetime.now()
        log.write(f"任务开始：disk={disk_number} dry_run=False letters={letters}")
        log.write(f"配置：{json.dumps(payload, ensure_ascii=False)[:2000]}")

        parts, layout_error = JobPipeline.build_parts(payload, letters)
        work_dir = _make_workdir(disk_number)
        ctx = PhaseContext(
            disk_number=disk_number,
            parts=parts or (),
            letters=letters,
            image_path=self._image_path(payload),
            cancel=cancel_event,
            log=log,
            options=options,
            env=env,
            on_step=on_step,
            work_dir=work_dir,
        )

        missing_tools = [
            name
            for name, value in (
                ("diskpart.exe", self.diskpart),
                ("ghost64.exe", self.ghost),
                ("bcdboot.exe", self.bcdboot),
            )
            if not value
        ]

        pipeline = self._build_pipeline(ctx)
        partition_outcome: PhaseOutcome | None = None
        try:
            if layout_error:
                pipeline_result = PipelineResult(failure=layout_error)
            else:
                precheck = pipeline.precheck(ctx, missing_tools=missing_tools)
                if precheck is not None:
                    pipeline_result = precheck
                else:
                    pipeline_result = pipeline.run(ctx)
                    partition_outcome = pipeline_result.partition
        finally:
            # 收尾释放 EFI 盘符：唯一入口，受设置 releaseEfiLetter 控制。
            # 分区阶段自己会记录 disk_touched，没动过盘时这里什么都不做。
            pipeline.cleanup_letter(ctx, partition=partition_outcome)
            # 多盘流水线：无论成败都要放行下一块盘（幂等，pipeline 里已经调过一次）。
            if self._sequencer is not None:
                self._sequencer.done(disk_number)
            self._archive_ghosterr(work_dir, disk_number, log)
            _remove_workdir(work_dir)

        return self._finish(
            disk_number=disk_number,
            log=log,
            started=started,
            pipeline_result=pipeline_result,
        )

    # ------------------------------------------------------------------ 结果
    def _archive_ghosterr(
        self, work_dir: str | None, disk_number: int, log: CommandLog
    ) -> None:
        """把失败时 Ghost 写的 ``GHOSTERR.TXT`` 存到日志目录，再删临时目录。"""
        if not work_dir or not self.log_dir:
            return
        source = Path(work_dir) / "ghosterr.txt"
        try:
            if not source.is_file():
                return
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = Path(self.log_dir) / f"ghosterr-disk{int(disk_number)}-{stamp}.txt"
            shutil.copyfile(source, target)
            log.write(f"Ghost 失败原因已存档：{target}")
        except OSError:
            _LOGGER.warning("归档 ghosterr 失败", exc_info=True)

    def _finish(
        self,
        *,
        disk_number: int,
        log: CommandLog,
        started: datetime,
        pipeline_result: PipelineResult,
    ) -> WorkerResult:
        finished = datetime.now()
        duration = (finished - started).total_seconds()
        steps = tuple(_to_step_result(result) for result in pipeline_result.results)
        success = pipeline_result.ok
        log.write(
            f"任务结束：success={success} 用时 {duration:.1f}s"
            + (f" 失败原因={pipeline_result.failure}" if pipeline_result.failure else "")
        )
        details: dict[str, Any] = {}
        if log.path is not None:
            details = {"log_path": str(log.path), "log_tail": tail(log.summary())}
            _LOGGER.info("真实执行日志: %s", log.path)
            if steps:
                steps = (*steps[:-1], _with_details(steps[-1], details))
        return WorkerResult(
            disk_number=disk_number,
            success=success,
            message=(
                f"硬盘 {disk_number} 真实执行完成"
                if success
                else f"硬盘 {disk_number} 执行失败：{pipeline_result.failure or '有步骤失败'}"
            ),
            dry_run=False,
            steps=steps,
            error=None if success else (pipeline_result.failure or "有步骤失败"),
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------ 小工具
    @staticmethod
    def _image_path(payload: Mapping[str, Any]) -> str | None:
        info = payload.get("image_info")
        if not isinstance(info, Mapping):
            return None
        return to_text(info.get("image_path"))

    @staticmethod
    def _which(name: str) -> str | None:
        return find_program(name)


def _to_step_result(result: PhaseResult) -> StepResult:
    return StepResult(
        key=result.key,
        title=result.title,
        status=result.status,
        message=result.message,
        details=dict(result.details),
    )


def _with_details(step: StepResult, extra: Mapping[str, Any]) -> StepResult:
    return StepResult(
        key=step.key,
        title=step.title,
        status=step.status,
        message=step.message,
        details={**step.details, **extra},
    )


def build_real_runner(
    *,
    log_dir: str | os.PathLike[str] | None = None,
    diskpart: str | None = None,
    ghost: str | None = None,
    bcdboot: str | None = None,
    sequencer: Any = None,
    ghost_gate: Any = None,
) -> WindowsDiskRunner:
    """工厂函数；``WorkflowController`` 用它给未注入服务的会话配默认执行器。

    ``ghost`` / ``bcdboot`` 由界面设置（``extGhost`` / ``extBcdboot``）传入；
    空值或 ``None`` 时回退到 PATH / System32 探测。``diskpart`` 只为测试注入
    而留，生产路径上永远是系统自带的那个。
    """
    # 生产入口：这里是唯一显式打开"真实写盘"的地方。
    return WindowsDiskRunner(
        log_dir=log_dir,
        diskpart=diskpart,
        ghost=ghost,
        bcdboot=bcdboot,
        allow_real_disk=True,
        sequencer=sequencer,
        ghost_gate=ghost_gate,
    )


__all__ = [
    "BCDBOOT_TIMEOUT",
    "DISKPART_TIMEOUT",
    "GHOST_TIMEOUT",
    "QUERY_ATTEMPTS",
    "QUERY_INTERVAL",
    "QUERY_TIMEOUT",
    "RELEASE_TIMEOUT",
    "STEP_FAILED",
    "STEP_KEYS",
    "STEP_OK",
    "STEP_SKIPPED",
    "STEP_TITLES",
    "VERIFY_TIMEOUT",
    "CommandLog",
    "RunnerError",
    "WindowsDiskRunner",
    "_CommandLog",
    "_partition_verdict",
    "build_real_runner",
    "create_partition_lines",
    "data_fs_token",
    "diskpart_lines",
    "msr_delete_lines",
    "option_flag",
    "partition_verdict",
    "preview_segments",
    "preview_text",
    "prune_runner_logs",
    "wipe_lines",
]
