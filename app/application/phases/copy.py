"""收尾阶段：把设置里指定的素材目录拷到第一数据分区根目录。

**为什么单独一个阶段**：它跟镜像/引导没有共享状态——不需要 EFI 盘符、不需要
diskpart、不需要 Ghost；它也补充不了 ``image``/``boot`` 的任何语义，只是"系统
装好之后，再往数据分区里放一份文件"。塞进 :class:`SystemPhase` 只会让那个组合器
变长，而且会把"系统写没写成功"与"素材拷没拷成功"两件事混在一起。

**为什么用 robocopy 而不是 Copy-Item**：

- ``/COPY:DAT`` 保留文件属性（隐藏/系统/只读）与时间戳；
- ``/DCOPY:DAT`` 连目录的时间戳与属性一起保留（``Copy-Item`` 做不到）；
- ``/E`` 建空目录，长路径原生支持，失败有退出码而不是异常。

**绝不删东西**：命令里没有 ``/MIR`` ``/PURGE`` ``/MOV``；``app/safety_guard.py``
还会再把这类开关拦一次（双保险）。所以目标分区里原有的文件不会被清掉。

**失败语义**：设置了这个目录，拷贝就是流程的一部分——失败即整盘失败（该盘"这一
系列流程"没走完），不做"装好了但少拷几个文件还报成功"的模糊处理。
唯一不算失败的是：没设置目录（``skipped``）和源目录为空（``skipped``）。
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.application.partitions import PlannedPartition
from app.application.phases.base import PhaseContext, PhaseOutcome, PhaseResult
from app.application.runner_utils import tail
from app.application.shell import COPY_TIMEOUT, find_program

GROUP = "extras"

#: robocopy 退出码 0-7 都是"成功/有可忽略的差异"，8 起才是真失败。
ROBOCOPY_OK_MAX = 8
#: 时间戳容差：跨文件系统的舍入差异不该被判成"没拷对"。
MTIME_TOLERANCE = 2.0
#: 校验失败时最多报几条，避免消息变成几千字。
MAX_REPORTED = 5
#: 反斜杠：写 ``"\\"`` 在 f-string 里容易触发语法告警，单独起个常量。
BACKSLASH = chr(92)

#: 与 :func:`compare_trees` 比对的三类属性位（Windows 才有意义）。
_ATTRIBUTE_BITS = (
    ("hidden", stat.FILE_ATTRIBUTE_HIDDEN),
    ("system", stat.FILE_ATTRIBUTE_SYSTEM),
    ("readonly", stat.FILE_ATTRIBUTE_READONLY),
)


@dataclass(frozen=True)
class SourceStats:
    """源目录概览（用于空间预判、日志与界面提示）。"""

    files: int
    dirs: int
    bytes: int

    @property
    def empty(self) -> bool:
        return self.files == 0 and self.dirs == 0


def target_partition(parts: Any) -> PlannedPartition | None:
    """拷贝目标 = 计划里 **Windows 之后的第一个数据分区**（固定口径）。

    不按盘符字母猜：``E:``/``F:`` 只是临时分配，顺序由 ``plan_partitions`` 决定。
    加了额外分区（E）时第一个数据分区仍然是 ``role="data"`` 的那个。
    """
    for part in parts or ():
        if getattr(part, "role", "") == "data":
            return part
    return None


def scan_source(root: str | os.PathLike[str]) -> SourceStats:
    """递归统计文件数、目录数与总字节数（不跟随符号链接）。"""
    base = Path(root)
    files = dirs = total = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirs += len(dirnames)
        for name in filenames:
            files += 1
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return SourceStats(files=files, dirs=dirs, bytes=total)


def robocopy_command(
    source: str,
    target: str,
    *,
    program: str = "robocopy",
) -> list[str]:
    """拷到目标根目录的命令行（数据拷贝，不镜像、不删除）。

    ``/R:1 /W:1``：失败文件只重试 1 次、等 1 秒，避免个别坏文件把整步拖成十几分钟；
    ``/NFL /NDL /NJH /NJS``：不刷每个文件名，输出只留汇总，日志不会炸。
    """
    return [
        program,
        str(source),
        str(target),
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/XJ",
        "/R:1",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
    ]


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return str(path)


def _attributes(path: Path) -> dict[str, bool]:
    try:
        raw = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return {}
    return {name: bool(raw & bit) for name, bit in _ATTRIBUTE_BITS if bit}


def _describe(path: Path, root: Path) -> str:
    rel = _relative(root, path)
    return rel if rel != "." else "根目录"


def compare_trees(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    tolerance: float = MTIME_TOLERANCE,
    limit: int = MAX_REPORTED,
) -> list[str]:
    """逐文件比对「相对路径 + 大小 + 修改时间 + 隐藏/系统/只读属性」。

    返回人类可读的差异列表（空 = 完全一致）。只看前 ``limit`` 条，但总数会
    在最后一条里给出——校验失败要能一眼看出"差多少"，而不是翻日志。
    """
    src_root = Path(source)
    dst_root = Path(target)
    problems: list[str] = []
    total = 0

    for dirpath, _dirnames, filenames in os.walk(src_root, followlinks=False):
        here = Path(dirpath)
        if not dst_root.is_dir():
            problems.append(f"目标目录不存在：{dst_root}")
            break
        for name in filenames:
            src_file = here / name
            rel = _relative(src_root, src_file)
            dst_file = dst_root / Path(rel)
            if not dst_file.is_file():
                total += 1
                if len(problems) < limit:
                    problems.append(f"缺少 {rel}")
                continue
            try:
                src_stat = src_file.stat()
                dst_stat = dst_file.stat()
            except OSError as exc:
                total += 1
                if len(problems) < limit:
                    problems.append(f"无法读取 {rel}：{exc}")
                continue
            if src_stat.st_size != dst_stat.st_size:
                total += 1
                if len(problems) < limit:
                    problems.append(
                        f"大小不同 {rel}（源 {src_stat.st_size} / 目标 {dst_stat.st_size}）"
                    )
            if abs(src_stat.st_mtime - dst_stat.st_mtime) > tolerance:
                total += 1
                if len(problems) < limit:
                    problems.append(
                        f"修改时间不同 {rel}（源 {_stamp(src_stat.st_mtime)} / "
                        f"目标 {_stamp(dst_stat.st_mtime)}）"
                    )
            src_attrs = _attributes(src_file)
            dst_attrs = _attributes(dst_file)
            lost = [
                name2
                for name2, present in src_attrs.items()
                if present and not dst_attrs.get(name2, False)
            ]
            if lost:
                total += 1
                if len(problems) < limit:
                    problems.append(f"属性不同 {rel}（目标丢了 {'/'.join(lost)}）")

    if total > len(problems):
        problems.append(f"…共 {total} 处差异")
    return problems


def _stamp(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _format_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


class CopyAssetsPhase:
    """把 ``source`` 目录的全部内容拷到第一数据分区的根目录。"""

    group = GROUP

    def __init__(
        self,
        shell: Any,
        source: str | None = None,
        *,
        robocopy: str | None = None,
        timeout: float = COPY_TIMEOUT,
        target_root: str | None = None,
    ) -> None:
        self.shell = shell
        self.source = (source or "").strip()
        self.robocopy = robocopy
        self.timeout = timeout
        # 测试用（也便于将来拷到挂载点而不是盘符根）：覆盖按盘符推出的根目录。
        self.target_root = target_root

    # ------------------------------------------------------------------ 拼装
    def command(self, target_letter: str) -> list[str]:
        """拷到 ``X:\\`` 根目录（不建同名子目录）。"""
        return robocopy_command(
            self.source,
            self._root(target_letter),
            program=self._program() or "robocopy",
        )

    def _root(self, letter: str) -> str:
        if self.target_root:
            return self.target_root
        drive = str(letter).rstrip(":").rstrip(BACKSLASH)
        return f"{drive}:{BACKSLASH}"

    def _program(self) -> str | None:
        if self.robocopy:
            return self.robocopy
        found = find_program("robocopy.exe") or find_program("robocopy")
        if found:
            return found
        # Discord 说明：find_program 只查 PATH 与 %SystemRoot%\System32，
        # 而测试/最小化环境可能两者都拿不到，因此最后退回 System32 的固定路径。
        return None

    # ------------------------------------------------------------------ 执行
    def run(self, ctx: PhaseContext) -> PhaseOutcome:
        if ctx.cancelled():
            return PhaseOutcome(
                ok=False,
                cancelled=True,
                results=(PhaseResult.skipped("copy", "任务已取消"),),
                message="任务已取消（系统已写入，附加文件未拷贝）",
            )

        if not self.source:
            return PhaseOutcome.succeeded(
                PhaseResult.skipped("copy", "未设置拷贝目录，跳过")
            )

        source = Path(self.source)
        if not source.is_dir():
            return self._fail(
                f"拷贝目录不可用：{source}（设置 → 安装默认值 → 附加文件目录）",
                ctx,
            )

        target = target_partition(ctx.parts)
        if target is None:
            return self._fail(
                "没有数据分区，无法确定拷贝目标（计划里必须有 Windows 之后的分区）",
                ctx,
            )
        if not target.letter:
            return self._fail("数据分区没有盘符，无法确定拷贝目标", ctx)

        root = self._root(target.letter)
        if self._overlaps(source, root):
            return self._fail(
                f"拷贝目录与目标分区重叠：{source} ↔ {root}（会无限递归）", ctx
            )

        stats = scan_source(str(source))
        if stats.empty:
            return PhaseOutcome.succeeded(
                PhaseResult.skipped("copy", f"源目录为空：{source}")
            )

        free = self._free_bytes(root)
        if free is not None and free < stats.bytes:
            return self._fail(
                "数据分区空间不足：需要 "
                f"{_format_bytes(stats.bytes)}，可用 {_format_bytes(free)}",
                ctx,
                bytes=stats.bytes,
                free=free,
            )

        program = self._program()
        if not program:
            return self._fail(
                "找不到 robocopy.exe（Windows 自带，通常在 %SystemRoot%\\System32）",
                ctx,
            )

        if getattr(ctx, "dry_run", False):
            return PhaseOutcome.succeeded(
                PhaseResult.ok(
                    "copy",
                    f"演练：将拷贝 {stats.files} 个文件 / {stats.dirs} 个目录"
                    f"（{_format_bytes(stats.bytes)}）到 {root}",
                    target=root,
                    files=stats.files,
                    dirs=stats.dirs,
                    bytes=stats.bytes,
                )
            )

        command = robocopy_command(str(source), root, program=program)
        ctx.notify("copy", "running", f"拷贝附加文件（{stats.files} 个文件）")
        ctx.log.write(" ".join(command))
        try:
            code, output, elapsed = self.shell.run(command, self.timeout)
        except Exception as exc:  # noqa: BLE001 - 启动失败/超时都算这一步失败
            return self._fail(f"robocopy 无法执行：{exc}", ctx)

        if code >= ROBOCOPY_OK_MAX:
            reason = tail(output, 800) or "robocopy 没有输出"
            return self._fail(
                f"robocopy 失败（退出码 {code}）：{reason}",
                ctx,
                exit=code,
                seconds=round(elapsed, 1),
            )

        problems = compare_trees(str(source), root)
        if problems:
            return self._fail(
                "拷贝后校验未通过：" + "；".join(problems),
                ctx,
                exit=code,
                seconds=round(elapsed, 1),
            )

        return PhaseOutcome.succeeded(
            PhaseResult.ok(
                "copy",
                f"已拷贝 {stats.files} 个文件 / {stats.dirs} 个目录"
                f"（{_format_bytes(stats.bytes)}）到 {root}",
                exit=code,
                target=root,
                files=stats.files,
                dirs=stats.dirs,
                bytes=stats.bytes,
                seconds=round(elapsed, 1),
            )
        )

    # ------------------------------------------------------------------ 内部
    def _fail(self, message: str, ctx: PhaseContext, **details: Any) -> PhaseOutcome:
        ctx.notify("copy", "running", message)
        return PhaseOutcome.failed(
            PhaseResult.failed("copy", message, **details),
            message=f"附加文件拷贝失败：{message}",
        )

    @staticmethod
    def _free_bytes(root: str) -> int | None:
        """目标分区可用字节；拿不到就返回 ``None``（不因为测不出而拦下任务）。"""
        try:
            return int(shutil.disk_usage(root).free)
        except OSError:
            return None

    @staticmethod
    def _overlaps(source: Path, root: str) -> bool:
        """源目录与目标根目录互相包含时拒绝（会无限递归 / 自己拷自己）。"""
        try:
            src = source.resolve(strict=False)
            dst = Path(root).resolve(strict=False)
        except OSError:
            return False
        return src == dst or dst in src.parents or src in dst.parents


__all__ = [
    "GROUP",
    "MAX_REPORTED",
    "MTIME_TOLERANCE",
    "ROBOCOPY_OK_MAX",
    "CopyAssetsPhase",
    "SourceStats",
    "compare_trees",
    "robocopy_command",
    "scan_source",
    "target_partition",
]
