"""真实写盘执行器：diskpart 分区 + dism 应用镜像 + bcdboot 写引导。

``SispService`` 本身不做任何写盘，真实执行必须由调用方注入一个
``real_runner``（签名 ``(spec, cancel_event) -> WorkerResult``）。本模块提供
默认实现 :class:`WindowsDiskRunner`，把一次任务拆成六步，逐步记进
``WorkerResult.steps``，任何一步失败后面的步骤标 ``skipped``：

1. ``efi``：``select disk`` → ``clean`` → 转 GPT → 建 EFI 分区（FAT32 + 盘符）
2. ``windows``：建 Windows 分区（NTFS + 盘符）
3. ``data``：建数据分区（1 个，或向导里额外加的一个；大小与文件系统由
   ``app.application.partitions`` 统一解析）
4. ``image``：``dism /Apply-Image`` 把 ``image_info.image_path`` 应用到 Windows 分区
5. ``boot``：``bcdboot <Windows 盘>\\Windows /s <EFI 盘> /f UEFI``
6. ``verify``：重新扫描该盘的分区，把实际结果记进 ``details``

几个刻意的设计：

- **每一步都先看取消标志**：取消是协作式的，一个正在跑的 diskpart 不会被打断，
  但下一步不会再开始；
- **整体超时**：diskpart 10 分钟、dism 3 小时、bcdboot 2 分钟；超时会杀掉子进程
  （连同 diskpart 自己的子进程）并让该步失败，而不是把任务永远挂住；
- **日志**：给了 ``log_dir`` 就写 ``disk<N>-<时间戳>.log``，命令原文、退出码、
  输出全在里面；``details`` 里只放 ``tail`` 和日志路径，避免 JSON 过大；
- **不做安全判断**：``dry_run=False``、白名单、硬盘身份一致性由
  ``SispService.execute_spec`` 的四道闸门把关，这里假定调用已经获准。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.application.contracts import format_size
from app.application.partitions import (
    MSR_GPT,
    PartitionLayoutError,
    PlannedPartition,
    diskpart_lines,
    plan_partitions,
    strip_msr_lines,
)
from app.models import STEP_FAILED, STEP_OK, STEP_SKIPPED, StepResult, WorkerResult

_LOGGER = logging.getLogger(__name__)

# diskpart ``format quick fs=`` 只认小写全名；界面存的是 NTFS / exFAT。
_FS_TOKENS = {"ntfs": "ntfs", "exfat": "exfat", "fat32": "fat32"}


def data_fs_token(payload: Mapping[str, Any]) -> str:
    """数据分区文件系统：``partition_info.data_fs`` → diskpart 参数。

    非法/缺失时退回 ``ntfs``（与历史行为一致），绝不把用户输入直接拼进脚本。
    """
    info = payload.get("partition_info") if isinstance(payload, Mapping) else None
    raw = str((info or {}).get("data_fs") or "NTFS").strip().lower()
    return _FS_TOKENS.get(raw, "ntfs")


class _StepSink(list):
    """``list[StepResult]``：每 append 一步就向外报一次进度。

    六个步骤的代码路径散落在 ``_run_steps`` 里，用列表子类收集进度比在每个
    append 后面手写回调更不容易漏；``extend`` 也走 append，保证失败分支同样上报。
    """

    def __init__(self, reporter: Any = None) -> None:
        super().__init__()
        self._reporter = reporter

    def append(self, step: Any) -> None:  # type: ignore[override]
        super().append(step)
        if self._reporter is None:
            return
        try:
            self._reporter(step.key, step.status, step.message)
        except Exception:
            _LOGGER.warning("进度回调失败", exc_info=True)

    def extend(self, items: Any) -> None:  # type: ignore[override]
        for item in items:
            self.append(item)

DISKPART_TIMEOUT = 600.0
LETTER_RELEASE_TIMEOUT = 120.0
DISM_TIMEOUT = 3 * 3600.0
BCDBOOT_TIMEOUT = 120.0
VERIFY_TIMEOUT = 120.0
MSR_QUERY_TIMEOUT = 60.0

# 与 service.py 的分区顺序保持一致：EFI → Windows → Data。
_STEP_ORDER = ("efi", "windows", "data")
STEP_TITLES = {
    "efi": "创建 EFI 分区",
    "windows": "创建 Windows 分区",
    "data": "创建数据分区",
    "image": "应用镜像",
    "boot": "写入 UEFI 引导",
    "verify": "校验分区结果",
}

_VERIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$number = [int]$env:VIX_DISK_NUMBER
$efiLetter = [string]$env:VIX_EFI_LETTER
$winLetter = [string]$env:VIX_WINDOWS_LETTER
$disk = Get-Disk -Number $number
$items = foreach ($part in Get-Partition -DiskNumber $number -ErrorAction SilentlyContinue) {
    [pscustomobject]@{
        number        = [int]$part.PartitionNumber
        size_bytes    = [long]$part.Size
        offset_bytes  = [long]$part.Offset
        type          = [string]$part.Type
        gpt_type      = [string]$part.GptType
        drive_letter  = if ($part.DriveLetter) { [string]$part.DriveLetter } else { $null }
        is_active     = [bool]$part.IsActive
        is_hidden     = [bool]$part.IsHidden
    }
}
function Find-First($paths) {
    foreach ($path in $paths) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}
# bcdboot 写完后，EFI 分区里必须有引导文件
$bootFile = $null
if ($efiLetter) {
    $bootFile = Find-First @(
        "${efiLetter}:\EFI\Microsoft\Boot\bootmgfw.efi",
        "${efiLetter}:\EFI\BOOT\BOOTX64.EFI"
    )
}
# Windows 分区里必须有系统文件（真实镜像一定会有）
$systemFile = $null
if ($winLetter) {
    $systemFile = Find-First @(
        "${winLetter}:\Windows\System32\config\SYSTEM",
        "${winLetter}:\Windows\System32\ntoskrnl.exe"
    )
}
[pscustomobject]@{
    disk_number     = $number
    partition_style = [string]$disk.PartitionStyle
    partitions      = @($items)
    boot_file       = $bootFile
    system_file     = $systemFile
} | ConvertTo-Json -Depth 4 -Compress
"""


class RunnerError(RuntimeError):
    """执行器内部的可读错误；会被包进对应步骤的 ``message``。"""


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _existing_dir(value: Any) -> str | None:
    """返回可用的临时目录（不存在则尝试创建）；不可用则返回 None。"""
    text = _to_text(value)
    if not text:
        return None
    try:
        Path(text).mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return text if Path(text).is_dir() else None


def _decode(data: bytes | None) -> str:
    """dism/diskpart 在中文 Windows 上按 OEM 代码页输出，先 mbcs 再兜底。"""
    if not data:
        return ""
    for encoding in ("mbcs", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…（前略）\n" + text[-limit:]


def _is_reserved_partition(part: Mapping[str, Any]) -> bool:
    """这个分区是不是 MSR / 保留分区。

    ``convert gpt`` 会自动在盘首建一个 16 MB MSR，执行器在建分区前按 GPT 类型
    删掉它（见 ``_find_msr_partition``）。verify 里再查一次：多出来一个 Reserved
    就说明删除没生效或者 MSR 又回来了。
    """
    gpt = str(part.get("gpt_type") or "").strip().lower().strip("{}")
    if gpt == MSR_GPT:
        return True
    return str(part.get("type") or "").strip().lower() == "reserved"


def _verify_verdict(
    payload: Mapping[str, Any],
    min_partitions: int = 3,
) -> tuple[bool, str, dict[str, Any]]:
    """把 verify 脚本的输出变成“这块盘现在能不能用”的结论。

    只靠“GPT + 分区数”是不够的：bcdboot 可能退出码 0 但没写入任何文件，镜像
    也可能只应用了一半。所以这里四个条件都查：分区表、分区数、EFI 引导文件、
    Windows 系统文件。纯函数，方便用假 payload 测失败分支。

    “分区数”是**相等**而不是“至少”：``convert gpt`` 自动建的那个 16 MB MSR
    应该在建分区前被删掉，盘上多一个 Reserved 分区就说明删除没生效。
    """
    parts = payload.get("partitions") or []
    if isinstance(parts, Mapping):
        parts = [parts]
    parts = [part for part in parts if isinstance(part, Mapping)]
    style = str(payload.get("partition_style") or "?").strip().upper()
    boot_file = str(payload.get("boot_file") or "").strip()
    system_file = str(payload.get("system_file") or "").strip()

    summary = "、".join(
        f"#{part.get('number')}"
        f"{('(' + str(part.get('drive_letter')) + ':)' if part.get('drive_letter') else '')}"
        f"={format_size(part.get('size_bytes'))}"
        for part in parts
    )

    problems: list[str] = []
    if style != "GPT":
        problems.append(f"分区表不是 GPT（{style}）")
    # 分区数必须与计划一致：MSR 已被删掉，多一个/少一个都是问题。
    expected = max(3, int(min_partitions))
    if len(parts) != expected:
        problems.append(f"分区数与计划不符（期望 {expected} 个，实际 {len(parts)} 个）")
    reserved = [part for part in parts if _is_reserved_partition(part)]
    if reserved:
        problems.append("盘上还有保留分区（MSR），预期布局是 EFI + C + D(+E)")
    if not boot_file:
        problems.append("EFI 分区里没有 bootmgfw.efi（bcdboot 没写进去）")
    if not system_file:
        problems.append("系统分区里没有 Windows 系统文件（镜像没应用完整）")

    details: dict[str, Any] = {
        "partition_style": style,
        "partitions": parts,
        "boot_file": boot_file or None,
        "system_file": system_file or None,
        "problems": problems,
    }
    if problems:
        return False, "校验不符合预期：" + "；".join(problems) + f"（{summary}）", details
    return (
        True,
        f"GPT、{len(parts)} 个分区：{summary}；引导 {Path(boot_file).name}；"
        f"系统 {Path(system_file).name}",
        details,
    )


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


class _CommandLog:
    """把命令、退出码、输出写进日志文件；没有 ``log_dir`` 时只留内存。"""

    def __init__(self, log_dir: str | os.PathLike[str] | None) -> None:
        self.path: Path | None = None
        self._lines: list[str] = []
        if log_dir:
            directory = Path(log_dir)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.path = directory / f"disk-runner-{stamp}-{os.getpid()}.log"
            except OSError:
                _LOGGER.warning("无法创建日志目录: %s", log_dir, exc_info=True)

    def write(self, line: str) -> None:
        stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {line}"
        self._lines.append(stamped)
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(stamped + "\n")
            except OSError:
                _LOGGER.warning("写执行日志失败: %s", self.path, exc_info=True)

    def block(self, title: str, body: str) -> None:
        self.write(f"--- {title} ---")
        for line in (body or "").splitlines():
            self.write(f"    {line}")

    def summary(self) -> str:
        return "\n".join(self._lines[-40:])


class WindowsDiskRunner:
    """默认真实执行器；构造好之后可直接当 ``real_runner`` 传给 ``SispService``。"""

    def __init__(
        self,
        *,
        log_dir: str | os.PathLike[str] | None = None,
        diskpart: str | None = None,
        dism: str | None = None,
        bcdboot: str | None = None,
        powershell: str | None = None,
    ) -> None:
        self.log_dir = log_dir
        self.diskpart = diskpart or self._which("diskpart.exe")
        self.dism = dism or self._which("dism.exe")
        self.bcdboot = bcdboot or self._which("bcdboot.exe")
        self.powershell = powershell or self._which("powershell.exe")

    # ------------------------------------------------------------------ 入口
    def __call__(
        self,
        spec: Any,
        cancel_event: threading.Event,
        on_step: Any = None,
    ) -> WorkerResult:
        return self.run(spec, cancel_event, on_step=on_step)

    def run(
        self,
        spec: Any,
        cancel_event: threading.Event | None = None,
        *,
        on_step: Any = None,
    ) -> WorkerResult:
        cancel_event = cancel_event or threading.Event()
        disk_number = _to_int(getattr(spec, "disk_number", None)) or 0
        payload = dict(getattr(spec, "config_payload", None) or {})
        letters = {
            str(key): str(value).strip().upper()[:1]
            for key, value in dict(getattr(spec, "drive_letters", None) or {}).items()
            if str(value).strip()
        }
        log = _CommandLog(self.log_dir)
        started = datetime.now()
        # 界面设置注入的可选项：临时目录、应答文件、驱动目录、引导文件目录。
        options = dict(getattr(spec, "runner_options", None) or {})
        env = self._runtime_env(options, log)

        steps: list[StepResult] = _StepSink(on_step)
        failure: str | None = None
        image_path = self._image_path(payload)

        log.write(f"任务开始：disk={disk_number} dry_run=False letters={letters}")
        log.write(f"配置：{json.dumps(payload, ensure_ascii=False)[:2000]}")
        self._notify(on_step, "efi", "running", "清盘并重建分区表")

        # 前置条件：缺工具/缺盘符/缺镜像都在动盘之前拦住。
        missing = [
            name
            for name, value in (
                ("diskpart.exe", self.diskpart),
                ("dism.exe", self.dism),
                ("bcdboot.exe", self.bcdboot),
            )
            if not value
        ]
        if missing:
            failure = f"缺少系统工具：{'、'.join(missing)}（真实写盘需要管理员环境里的这些命令）"
            for key in (*_STEP_ORDER, "image", "boot"):
                steps.append(StepResult(key=key, title=STEP_TITLES[key], status=STEP_SKIPPED, message="前置条件不满足"))
        else:
            # tracker 用来保证：只要 diskpart 开始动盘，就尝试释放 EFI 盘符，
            # 避免隐藏 EFI 卷的盘符在下次 clean 后变成幽灵盘符。
            tracker = {"diskpart_ran": False}
            try:
                steps, failure = self._run_steps(
                    disk_number=disk_number,
                    payload=payload,
                    letters=letters,
                    image_path=image_path,
                    cancel_event=cancel_event,
                    log=log,
                    steps=steps,
                    tracker=tracker,
                    options=options,
                    env=env,
                    on_step=on_step,
                )
            finally:
                if tracker["diskpart_ran"]:
                    self._release_efi_letter(
                        disk_number,
                        letters.get("efi"),
                        log,
                        env=env,
                        temp_dir=_existing_dir(options.get("temp_dir")),
                    )

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        success = failure is None and not any(step.failed for step in steps)

        log.write(
            f"任务结束：success={success} 用时 {duration:.1f}s"
            + (f" 失败原因={failure}" if failure else "")
        )
        # WorkerResult 没有 details 字段，把日志位置挂到最后一个步骤上。
        details: dict[str, Any] = {}
        if log.path is not None:
            details = {"log_path": str(log.path), "log_tail": _tail(log.summary())}
            _LOGGER.info("真实执行日志: %s", log.path)
            if steps:
                steps[-1] = replace(steps[-1], details={**steps[-1].details, **details})

        return WorkerResult(
            disk_number=disk_number,
            success=success,
            message=(
                f"硬盘 {disk_number} 真实执行完成"
                if success
                else f"硬盘 {disk_number} 执行失败：{failure or '有步骤失败'}"
            ),
            dry_run=False,
            steps=tuple(steps),
            error=None if success else (failure or "有步骤失败"),
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            duration_seconds=duration,
        )

    @staticmethod
    def _notify(on_step: Any, key: str, status: str, message: str = "") -> None:
        """上报“某一步正在执行”；进度回调可选，异常一律吞掉。"""
        if on_step is None:
            return
        try:
            on_step(key, status, message)
        except Exception:
            _LOGGER.warning("进度回调失败", exc_info=True)

    # ------------------------------------------------------------------ 六步
    def _run_steps(
        self,
        *,
        disk_number: int,
        payload: Mapping[str, Any],
        letters: Mapping[str, str],
        image_path: str | None,
        cancel_event: threading.Event,
        log: _CommandLog,
        steps: list[StepResult],
        tracker: dict[str, bool] | None = None,
        options: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        on_step: Any = None,
    ) -> tuple[list[StepResult], str | None]:
        options = dict(options or {})
        temp_dir = _existing_dir(options.get("temp_dir"))

        # 分区布局的唯一真相：EFI → Windows → 数据分区（可能两个）。
        # c_size_gb 缺失/非法时这里直接报错，还没碰到磁盘。
        try:
            parts = plan_partitions(payload, letters=letters)
        except PartitionLayoutError as exc:
            return steps, str(exc)

        missing_letters = [part.role for part in parts if not part.letter]
        if missing_letters:
            return steps, f"缺少盘符分配：{', '.join(missing_letters)}"
        if not image_path:
            return steps, "配置里缺少 image_info.image_path"
        if not Path(image_path).is_file():
            return steps, f"镜像文件不存在: {image_path}"

        # 1-3：清盘 + 建分区。分两段 diskpart：
        #   ① clean + convert gpt —— 这一步 Windows 会自动在盘首塞一个 16 MB MSR；
        #   ② 按 GPT 类型找到那个 MSR 并删掉，然后建 EFI/C/D(+E)，
        #      最终布局里没有 MSR（产品要求 EFI + C + D(+E)）。
        wipe = self._wipe_script(disk_number=disk_number, log=log)
        if cancel_event.is_set():
            return steps, "任务已取消（未开始写盘）"

        try:
            if tracker is not None:
                tracker["diskpart_ran"] = True
            code, output = self._run_diskpart(wipe, log, env=env, temp_dir=temp_dir)
        except Exception as exc:  # noqa: BLE001 - 统一翻成步骤失败
            steps.append(
                StepResult(
                    key="efi",
                    title=STEP_TITLES["efi"],
                    status=STEP_FAILED,
                    message=f"无法执行 diskpart：{exc}",
                )
            )
            return steps, f"无法执行 diskpart：{exc}"

        if code != 0:
            steps.extend(
                StepResult(
                    key=key,
                    title=STEP_TITLES[key],
                    status=STEP_FAILED if key == "efi" else STEP_SKIPPED,
                    message=(
                        f"diskpart 失败（退出码 {code}）：{_tail(output, 800)}"
                        if key == "efi"
                        else "diskpart 失败，后续步骤未执行"
                    ),
                    details={"diskpart_exit": code, "diskpart_tail": _tail(output)},
                )
                for key in _STEP_ORDER
            )
            return steps, f"diskpart 失败（退出码 {code}）"

        # 找 ``convert gpt`` 自动建出来的 MSR。找不到就不删；查询本身失败则中止，
        # 绕不盲删（``select partition`` 猜错会删掉真分区）。
        try:
            msr_partition = self._find_msr_partition(disk_number, log)
        except Exception as exc:  # noqa: BLE001 - 查不了就别动盘
            steps.append(
                StepResult(
                    key="efi",
                    title=STEP_TITLES["efi"],
                    status=STEP_FAILED,
                    message=f"无法确认 MSR 分区，已中止（避免误删）：{exc}",
                )
            )
            return steps, f"无法确认 MSR 分区，已中止：{exc}"

        script = self._diskpart_script(
            disk_number=disk_number,
            parts=parts,
            log=log,
            msr_partition=msr_partition,
        )
        try:
            code, output = self._run_diskpart(script, log, env=env, temp_dir=temp_dir)
        except Exception as exc:  # noqa: BLE001 - 统一翻成步骤失败
            steps.append(
                StepResult(
                    key="efi",
                    title=STEP_TITLES["efi"],
                    status=STEP_FAILED,
                    message=f"无法执行 diskpart：{exc}",
                )
            )
            return steps, f"无法执行 diskpart：{exc}"

        if code != 0:
            steps.extend(
                StepResult(
                    key=key,
                    title=STEP_TITLES[key],
                    status=STEP_FAILED if key == "efi" else STEP_SKIPPED,
                    message=(
                        f"diskpart 失败（退出码 {code}）：{_tail(output, 800)}"
                        if key == "efi"
                        else "diskpart 失败，后续步骤未执行"
                    ),
                    details={"diskpart_exit": code, "diskpart_tail": _tail(output)},
                )
                for key in _STEP_ORDER
            )
            return steps, f"diskpart 失败（退出码 {code}）"

        by_role = {part.role: part for part in parts}
        efi_part = by_role["efi"]
        windows_part = by_role["windows"]
        data_parts = [part for part in parts if part.is_data]

        steps.append(
            StepResult(
                key="efi",
                title=STEP_TITLES["efi"],
                status=STEP_OK,
                message=f"已创建并格式化（{efi_part.size_text}，盘符 {efi_part.letter}）",
                details={
                    "letter": efi_part.letter,
                    "size": efi_part.size_text,
                    "file_system": efi_part.file_system,
                },
            )
        )
        steps.append(
            StepResult(
                key="windows",
                title=STEP_TITLES["windows"],
                status=STEP_OK,
                message=f"已创建并格式化（{windows_part.size_text}，盘符 {windows_part.letter}）",
                details={
                    "letter": windows_part.letter,
                    "size": windows_part.size_text,
                    "file_system": windows_part.file_system,
                },
            )
        )
        data_summary = " + ".join(f"{part.label} {part.size_text}" for part in data_parts)
        data_letters = "/".join(str(part.letter or "") for part in data_parts)
        steps.append(
            StepResult(
                key="data",
                title=(
                    f"创建数据分区（{len(data_parts)} 个）"
                    if len(data_parts) > 1
                    else STEP_TITLES["data"]
                ),
                status=STEP_OK,
                message=f"已创建并格式化（{data_summary}，盘符 {data_letters}）",
                details={
                    "letters": [part.letter for part in data_parts],
                    "partitions": [part.to_dict() for part in data_parts],
                },
            )
        )

        # 4：应用镜像
        if cancel_event.is_set():
            steps.append(StepResult(key="image", title=STEP_TITLES["image"], status=STEP_SKIPPED, message="任务已取消"))
            return steps, "任务已取消（分区已建好，镜像未应用）"

        self._notify(on_step, "image", "running", "应用镜像（dism）")
        image_index = _to_int((payload.get("image_info") or {}).get("image_index")) or 1
        target = f"{letters['windows']}:\\"
        code, output, elapsed = self._apply_image(image_path, image_index, target, log, options=options, env=env)
        if code != 0:
            steps.append(
                StepResult(
                    key="image",
                    title=STEP_TITLES["image"],
                    status=STEP_FAILED,
                    message=f"dism 失败（退出码 {code}）：{_tail(output, 800)}",
                    details={"exit": code, "image": image_path, "index": image_index, "seconds": round(elapsed, 1)},
                )
            )
            steps.append(StepResult(key="boot", title=STEP_TITLES["boot"], status=STEP_SKIPPED, message="镜像未应用成功，跳过引导"))
            return steps, f"应用镜像失败（dism 退出码 {code}）"

        steps.append(
            StepResult(
                key="image",
                title=STEP_TITLES["image"],
                status=STEP_OK,
                message=f"已应用 {Path(image_path).name}（索引 {image_index}）到 {target}",
                details={"image": image_path, "index": image_index, "seconds": round(elapsed, 1), "tail": _tail(output, 1200)},
            )
        )

        # 5：写引导
        if cancel_event.is_set():
            steps.append(StepResult(key="boot", title=STEP_TITLES["boot"], status=STEP_SKIPPED, message="任务已取消"))
            return steps, "任务已取消（镜像已写入，引导未写）"

        self._notify(on_step, "boot", "running", "写入 UEFI 引导（bcdboot）")
        code, output, elapsed = self._write_boot(letters, log, options=options, env=env)
        if code != 0:
            steps.append(
                StepResult(
                    key="boot",
                    title=STEP_TITLES["boot"],
                    status=STEP_FAILED,
                    message=f"bcdboot 失败（退出码 {code}）：{_tail(output, 800)}",
                    details={"exit": code, "seconds": round(elapsed, 1)},
                )
            )
            return steps, f"写引导失败（bcdboot 退出码 {code}）"

        steps.append(
            StepResult(
                key="boot",
                title=STEP_TITLES["boot"],
                status=STEP_OK,
                message=f"已写入 UEFI 引导（{letters['efi']}: ← {letters['windows']}:\\Windows）",
                details={"exit": code, "seconds": round(elapsed, 1), "tail": _tail(output, 800)},
            )
        )

        # 6：校验
        self._notify(on_step, "verify", "running", "校验分区与引导")
        steps.append(self._verify(disk_number, log, letters, min_partitions=len(parts)))
        if steps[-1].failed:
            return steps, "分区校验失败"
        return steps, None

    # ------------------------------------------------------------------ 具体命令
    def _diskpart_script(
        self,
        *,
        disk_number: int,
        parts: Sequence[PlannedPartition],
        log: _CommandLog,
        msr_partition: int | None = None,
    ) -> list[str]:
        """拼出“建分区”那一段 diskpart 脚本（先删 MSR，再按布局建分区）。"""
        lines = [
            f"select disk {disk_number}",
            *strip_msr_lines(msr_partition),
            *diskpart_lines(parts),
            "exit",
        ]
        log.block("diskpart 脚本（删 MSR + 建分区）", "\n".join(lines))
        return lines

    def _wipe_script(self, *, disk_number: int, log: _CommandLog) -> list[str]:
        """第一段 diskpart：清盘并转 GPT。

        ``convert gpt`` 会顺手在盘首建一个 16 MB 的 MSR（Windows 的行为，不是
        我们脚本里的命令），所以这一段之后要再查一次并删掉它。
        """
        lines = [f"select disk {disk_number}", "clean", "convert gpt", "exit"]
        log.block("diskpart 脚本（清盘 + 转 GPT）", "\n".join(lines))
        return lines

    def _find_msr_partition(self, disk_number: int, log: _CommandLog) -> int | None:
        """按 GPT 类型找出 ``convert gpt`` 自动生成的 MSR 分区号；没有则返回 None。

        必须按类型查、不能假设“分区 1 就是 MSR”：万一某些系统不自动建，
        ``select partition 1`` 会让后面的布局脚本整条中止。
        """
        if not self.powershell:
            raise RunnerError("找不到 powershell.exe，无法确认 MSR 分区")
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$part = Get-Partition -DiskNumber {int(disk_number)} -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.GptType -like '*{MSR_GPT}*' }} | "
            "Select-Object -First 1 -ExpandProperty PartitionNumber; "
            "if ($part) { [int]$part }"
        )
        code, output, _elapsed = self._spawn(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            MSR_QUERY_TIMEOUT,
            log,
        )
        if code != 0:
            raise RunnerError(f"查询 MSR 分区失败（退出码 {code}）：{_tail(output, 400)}")
        text = (output or "").strip()
        number = _to_int(text.splitlines()[-1]) if text else None
        if number is None:
            log.write("没有发现自动生成的 MSR 分区，跳过删除")
            return None
        log.write(f"发现自动生成的 MSR 分区 #{number}，将在建分区前删除")
        return number

    def _run_diskpart(
        self,
        lines: Sequence[str],
        log: _CommandLog,
        timeout: float = DISKPART_TIMEOUT,
        *,
        env: Mapping[str, str] | None = None,
        temp_dir: str | None = None,
    ) -> tuple[int, str]:
        if not self.diskpart:
            raise RunnerError("找不到 diskpart.exe")
        handle, path = tempfile.mkstemp(prefix="sisp-diskpart-", suffix=".txt", dir=temp_dir)
        os.close(handle)
        try:
            # diskpart 只认 ANSI 脚本；这里的脚本全是 ASCII。
            Path(path).write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
            code, output, _elapsed = self._spawn([self.diskpart, "/s", path], timeout, log, env=env)
            return code, output
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _release_efi_letter(
        self,
        disk_number: int,
        letter: str | None,
        log: _CommandLog,
        *,
        env: Mapping[str, str] | None = None,
        temp_dir: str | None = None,
    ) -> None:
        """任务结束后释放 EFI 盘符。

        EFI 是隐藏卷，`clean` 后它的盘符可能残留成幽灵盘符（DriveInfo 里
        IsReady=False），下次 assign 会失败；这里主动 remove letter。
        清理失败只记日志，不影响已经成功的写盘结果。
        """
        if not letter:
            return
        lines = [f"select volume {letter}", f"remove letter={letter}", "exit"]
        log.block("释放 EFI 盘符", "\n".join(lines))
        try:
            code, _output = self._run_diskpart(
                lines, log, timeout=LETTER_RELEASE_TIMEOUT, env=env, temp_dir=temp_dir
            )
        except Exception:  # noqa: BLE001 - 清理失败不能影响任务结果
            log.write(f"释放 EFI 盘符 {letter} 失败（已忽略）")
            return
        if code == 0:
            log.write(f"已释放 EFI 盘符 {letter}")
        else:
            log.write(f"释放 EFI 盘符 {letter} 失败（退出码 {code}），不影响任务结果")

    def _apply_image(
        self,
        image_path: str,
        index: int,
        target: str,
        log: _CommandLog,
        *,
        options: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, float]:
        assert self.dism  # noqa: S101 - 构造期已确保存在，这里只拦编程错误
        options = dict(options or {})
        command = [
            self.dism,
            "/English",
            "/Apply-Image",
            f"/ImageFile:{image_path}",
            f"/Index:{index}",
            f"/ApplyDir:{target}",
            "/CheckIntegrity",
        ]
        # 界面设置里的「无人值守应答文件」和「驱动目录」直接交给 DISM，
        # 在应用镜像的同一步里生效，失败会让整步失败（不静默省略）。
        unattend = _to_text(options.get("unattend_path"))
        if unattend:
            command.append(f"/Apply-Unattend:{unattend}")
        drivers = _to_text(options.get("drivers_path"))
        if drivers:
            command.extend([f"/Add-Driver:{drivers}", "/Recurse"])
        code, output, elapsed = self._spawn(command, DISM_TIMEOUT, log, env=env)
        return code, output, elapsed

    def _write_boot(
        self,
        letters: Mapping[str, str],
        log: _CommandLog,
        *,
        options: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, float]:
        assert self.bcdboot  # noqa: S101 - 构造期已确保存在，这里只拦编程错误
        options = dict(options or {})
        windows = f"{letters['windows']}:\\Windows"
        efi = f"{letters['efi']}:"
        # ``sysBoot`` 是离线引导文件目录：镜像里没有 Windows\Boot 时
        # （精简/自定义镜像）用它当 bcdboot 的源目录。
        boot_source = _to_text(options.get("boot_source"))
        source = boot_source or windows
        if boot_source:
            log.write(f"引导源目录来自界面设置：{boot_source}")
        command = [self.bcdboot, source, "/s", efi, "/f", "UEFI"]
        return self._spawn(command, BCDBOOT_TIMEOUT, log, env=env)

    @staticmethod
    def _runtime_env(
        options: Mapping[str, Any] | None,
        log: _CommandLog,
    ) -> dict[str, str]:
        """子进程环境：界面里的临时目录优先生效（TMP/TEMP）。"""
        env = dict(os.environ)
        configured = _to_text((options or {}).get("temp_dir"))
        if not configured:
            return env
        temp_dir = _existing_dir(configured)
        if not temp_dir:
            log.write(f"临时目录不可用，改用系统默认：{configured}")
            return env
        env["TMP"] = temp_dir
        env["TEMP"] = temp_dir
        log.write(f"临时目录：{temp_dir}")
        return env

    def _verify(
        self,
        disk_number: int,
        log: _CommandLog,
        letters: Mapping[str, str] | None = None,
        *,
        min_partitions: int = 3,
    ) -> StepResult:
        title = STEP_TITLES["verify"]
        if not self.powershell:
            return StepResult(
                key="verify",
                title=title,
                status=STEP_FAILED,
                message="找不到 powershell.exe，无法校验",
            )
        env = dict(os.environ)
        env["VIX_DISK_NUMBER"] = str(disk_number)
        # 校验时 EFI / Windows 的盘符还没释放（释放发生在外层 finally），正好用来查文件。
        letters = dict(letters or {})
        env["VIX_EFI_LETTER"] = str(letters.get("efi") or "")
        env["VIX_WINDOWS_LETTER"] = str(letters.get("windows") or "")
        try:
            completed = subprocess.run(  # noqa: S603 - 固定脚本，编号来自任务
                [self.powershell, "-NoProfile", "-NonInteractive", "-Command", _VERIFY_SCRIPT],
                capture_output=True,
                timeout=VERIFY_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return StepResult(key="verify", title=title, status=STEP_FAILED, message=f"校验执行失败：{exc}")

        raw = _decode(completed.stdout).strip()
        log.block("verify 输出", raw)
        if completed.returncode != 0:
            return StepResult(
                key="verify",
                title=title,
                status=STEP_FAILED,
                message=f"校验脚本失败（退出码 {completed.returncode}）：{_tail(_decode(completed.stderr), 500)}",
                details={"exit": completed.returncode},
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return StepResult(
                key="verify",
                title=title,
                status=STEP_FAILED,
                message=f"校验输出不是合法 JSON：{_tail(raw, 300)}",
            )
        ok, message, details = _verify_verdict(
            payload if isinstance(payload, Mapping) else {},
            min_partitions=min_partitions,
        )
        return StepResult(
            key="verify",
            title=title,
            status=STEP_OK if ok else STEP_FAILED,
            message=message,
            details=details,
        )

    # ------------------------------------------------------------------ 进程
    def _spawn(
        self,
        command: Sequence[str],
        timeout: float,
        log: _CommandLog,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, float]:
        started = datetime.now()
        log.write(f"$ {' '.join(command)}")
        try:
            completed = subprocess.run(  # noqa: S603 - 命令由本模块拼装，参数来自配置
                list(command),
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
                env=dict(env) if env else None,
            )
        except subprocess.TimeoutExpired as exc:
            output = _decode(exc.stdout) + _decode(exc.stderr)
            log.block(f"超时（{timeout:.0f}s）", output)
            raise RunnerError(f"命令超时（{timeout:.0f} 秒）：{command[0]}") from exc
        except OSError as exc:
            raise RunnerError(f"无法启动 {command[0]}：{exc}") from exc

        output = _decode(completed.stdout) + _decode(completed.stderr)
        elapsed = (datetime.now() - started).total_seconds()
        log.block(f"退出码 {completed.returncode}，用时 {elapsed:.1f}s", output)
        return completed.returncode, output, elapsed

    # ------------------------------------------------------------------ 小工具
    @staticmethod
    def _which(name: str) -> str | None:
        found = shutil.which(name)
        if found:
            return found
        if os.name == "nt":
            system32 = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows") / "System32"
            candidate = system32 / name
            if candidate.is_file():
                return str(candidate)
        return None

    @staticmethod
    def _image_path(payload: Mapping[str, Any]) -> str | None:
        info = payload.get("image_info")
        if not isinstance(info, Mapping):
            return None
        return _to_text(info.get("image_path"))


def build_real_runner(
    *,
    log_dir: str | os.PathLike[str] | None = None,
    diskpart: str | None = None,
    dism: str | None = None,
) -> WindowsDiskRunner:
    """工厂函数；``WorkflowController`` 用它给未注入服务的会话配默认执行器。

    ``diskpart`` / ``dism`` 由界面设置（``extDiskpart`` / ``extDism``）传入；
    空值或 ``None`` 时回退到 PATH / System32 探测。
    """
    return WindowsDiskRunner(log_dir=log_dir, diskpart=diskpart, dism=dism)
