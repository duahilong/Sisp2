"""测试替身：注入式 fake shell / fake process / 分区表构造器。

单独成模块的原因：多个测试文件要用同一套替身（``from tests.fakes import ...``），
而 conftest 里的定义不能直接 import。约定：**不要**给 ``runner`` 的私有方法赋值
来 mock——实现搬家时会静默失效（2026-11-06 清盘事故的形态）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.application.partitions import (
    BASIC_GPT,
    BYTES_PER_MB,
    ESP_GPT,
    PlannedPartition,
)


# ---------------------------------------------------------------- 替身实现
class FakeProcess:
    """``ProcessShell`` 替身：记录命令，按 handler 返回结果。

    ``handler(command, timeout, env) -> (code, output, elapsed)``；
    默认全部成功。dism / bcdboot / 校验脚本都走这里。
    """

    def __init__(self, handler: Any = None) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self._handler = handler

    def run(self, command: Sequence[str], timeout: float, *, env: Any = None):
        self.calls.append(list(command))
        env = dict(env or {})
        self.envs.append(env)
        if self._handler is not None:
            return self._handler(list(command), timeout, env)
        return 0, "ok", 0.1


class FakeDiskpartShell:
    """``DiskpartShell`` 替身：分区阶段需要的四件事全在这里。

    - ``run_diskpart(lines, *, title, timeout)`` → ``(code, output)``
    - ``query(disk_number)`` → 真实分区表 payload（分区验收用）
    - ``find_msr(disk)`` / ``find_esp(disk)`` → 分区号或 ``None``
    - ``release_letter(disk, partition, letter)`` → bool
    """

    def __init__(
        self,
        *,
        diskpart_handler: Any = None,
        query_handler: Any = None,
        msr: Any = None,
        esp: Any = None,
        release_result: bool = True,
    ) -> None:
        self.scripts: list[list[str]] = []
        self.titles: list[str] = []
        self.queries: list[int] = []
        self.released: list[tuple[int, int, str]] = []
        self._diskpart = diskpart_handler
        self._query = query_handler
        self._msr = msr
        self._esp = esp
        self.release_result = release_result

    # 写
    def run_diskpart(self, lines, *, title="diskpart 脚本", timeout=600.0):
        self.scripts.append(list(lines))
        self.titles.append(title)
        if self._diskpart is not None:
            return self._diskpart(list(lines), title, timeout)
        return 0, "diskpart ok"

    def release_letter(self, disk_number: int, partition: int, letter: str) -> bool:
        self.released.append((int(disk_number), int(partition), str(letter)))
        return self.release_result

    # 读
    def query(self, disk_number: int):
        self.queries.append(int(disk_number))
        if self._query is not None:
            return self._query(int(disk_number))
        return {"partition_style": "GPT", "partitions": [], "largest_free": 0}

    def find_msr(self, disk_number: int, *, label: str = "MSR"):
        return self._msr(disk_number) if callable(self._msr) else self._msr

    def find_esp(self, disk_number: int):
        return self._esp(disk_number) if callable(self._esp) else self._esp


def build_snapshot(
    parts: Sequence[PlannedPartition],
    *,
    disk_extra_mb: int = 2,
    largest_free: int = 0,
    style: str = "GPT",
) -> dict[str, Any]:
    """按分区计划造一份"刚建好、验收一定通过"的真实分区表 payload。

    - 显式分区按计划大小；
    - 最后那个 auto 分区吃掉「盘容量 − 显式项 − 2 MB」；
    - ESP 的盘符放在 ``mounted_letter``（隐藏卷在 ``Get-Partition`` 里看不到字母）；
    - ``disk_size_bytes`` = 分区合计 + ``disk_extra_mb``（GPT 首尾）。
    """
    explicit = sum(int(part.size_mb) for part in parts if part.size_mb is not None)
    auto = [part for part in parts if part.size_mb is None]
    sizes: list[int] = []
    if auto:
        # 先给个占位，下面按"剩余"回填
        sizes = [int(part.size_mb) if part.size_mb is not None else 0 for part in parts]
        total_explicit = explicit
        # auto 分区给一个明显高于下限的尺寸（MIN_DATA_GB=20 GB），
        # 否则验收会因为"最后一个分区低于下限"而失败——那是测试想单独覆盖的分支。
        auto_mb = 24 * 1024
        sizes[sizes.index(0)] = auto_mb
        total_explicit += auto_mb
        disk_size_mb = total_explicit + disk_extra_mb
    else:
        sizes = [int(part.size_mb or 0) for part in parts]
        disk_size_mb = sum(sizes) + disk_extra_mb

    offset_mb = 1
    rows: list[dict[str, Any]] = []
    for index, (part, size_mb) in enumerate(zip(parts, sizes, strict=True), start=1):
        rows.append(
            {
                "number": index,
                "size_bytes": size_mb * BYTES_PER_MB,
                "offset_bytes": offset_mb * BYTES_PER_MB,
                "type": "System" if part.role == "efi" else "Basic",
                "gpt_type": "{" + (ESP_GPT if part.role == "efi" else BASIC_GPT) + "}",
                "drive_letter": None if part.role == "efi" else part.letter,
                "mounted_letter": part.letter if part.role == "efi" else None,
                "file_system": part.file_system,
                "label": part.label,
            }
        )
        offset_mb += size_mb
    return {
        "partition_style": style,
        "disk_size_bytes": disk_size_mb * BYTES_PER_MB,
        "largest_free": largest_free,
        "partitions": rows,
    }


class FakeVerify:
    """``BootVerifyPhase`` 替身：默认返回"校验通过"。

    任务级测试（分区/镜像/引导）不关心装机校验的细节，用它可以只看编排；
    校验本身的判据由 ``test_image_gate_verify.py`` 直接测 ``BootVerifyPhase``。
    """

    def __init__(self, ok: bool = True, message: str = "ok", details: Any = None) -> None:
        self.group = "verify"
        self.ok = ok
        self.message = message
        self.details = details or {}
        self.calls = 0

    def run(self, ctx):
        from app.application.phases.base import PhaseOutcome, PhaseResult

        self.calls += 1
        if self.ok:
            return PhaseOutcome.succeeded(PhaseResult.ok("verify", self.message, **self.details))
        return PhaseOutcome.failed(
            PhaseResult.failed("verify", self.message, **self.details),
            message=self.message,
        )
