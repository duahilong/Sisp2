"""执行日志：把命令、退出码、输出写进 ``logDir/disk-runner-*.log``。

独立成模块的原因：分区阶段、装机阶段、收尾释放盘符都要写同一份日志，
而它们不应该为了一个日志类去依赖 ``real_runner``。设置里没填 ``logDir`` 时
落到 :func:`default_log_dir`（``%APPDATA%\\Sisp\\logs``）——否则任务跑起来
一份日志都没有，界面上的「日志 / 查看任务日志」永远是空的。
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# 内存里保留的行数上限：``summary()`` 只取最后 40 行，但历史实现把所有行都留在
# list 里，长时间写镜像（2 小时、每秒好几行）会白占内存。留 1000 行既够“尾部
# 摘要 + 事后抽查”，又与日志文件（不受此限）解耦。
SUMMARY_WINDOW = 1000
# ``summary()`` 返回的最后几行（``real_runner`` 把它挂进步骤 details）。
SUMMARY_LINES = 40


def default_log_dir() -> Path:
    """设置里没填 ``logDir`` 时用的目录：与 ``settings.json`` 同级的 ``logs``。

    历史上这里是「空 = 不写文件」，结果是任务跑完连一份执行日志都没有，
    界面上的日志面板只能是空的——改成始终有一个可写目录。
    """
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Sisp" / "logs"
    return Path.home() / ".sisp" / "logs"


class CommandLog:
    """把命令、退出码、输出写进日志文件；目录不可写时退回内存。"""

    def __init__(
        self,
        log_dir: str | os.PathLike[str] | None,
        *,
        disk_number: int | None = None,
        disk_label: str = "",
        disk_slug: str = "",
    ) -> None:
        self.path: Path | None = None
        # 这块盘的显示名（``Msft Virtual Disk（磁盘 2）``）：日志头、结论、导出统计
        # 都用它，不再只有一个会变的盘号。
        self.disk_label = str(disk_label or "").strip()
        self._lines: deque[str] = deque(maxlen=SUMMARY_WINDOW)
        # 多线程：子进程读取线程与任务线程可能同时写日志。
        self._lock = threading.Lock()
        # ``None`` = 只留内存（单测用）：运行时会由 ``main.py`` 传入
        # ``default_log_dir()``，所以界面上始终有日志可看。
        if log_dir:
            directory = Path(log_dir)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                # 多盘并发时同一秒可能同时创建多份日志：追加进程号 + 6 位随机后缀，
                # 否则两个任务会写进同一个文件（行互相穿插，事后无法按盘追踪）。
                suffix = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
                # 盘号进文件名：任务还在跑（结果里没有 log_path）时，界面靠它
                # 找到正在写的这份日志（``WebApi`` 的兜底查找按 d<N>- 匹配）。
                disk = f"d{int(disk_number)}-" if disk_number is not None else ""
                # 型号 slug 排在 ``d2-`` 之后：既保留“按盘号找日志”的老约定，
                # 又让人在文件夹里一眼看出这是哪块盘。
                slug = f"{str(disk_slug or '').strip()}-" if disk_slug else ""
                self.path = directory / f"disk-runner-{disk}{slug}{stamp}-{suffix}.log"
            except OSError:
                _LOGGER.warning("无法创建日志目录: %s", log_dir, exc_info=True)

    def _append(self, lines: Iterable[str]) -> None:
        """把一组行写进内存与文件（文件只 open/close 一次）。

        ``block()`` 与 ``banner()`` 动辄一次写几十上百行（diskpart 脚本预览、
        命令的完整输出），历史实现每行调一次 ``write()``，就是几十次
        open/close——在 Windows 上这些系统调用比写内容本身贵得多。
        """
        stamp = datetime.now().strftime("%H:%M:%S")
        stamped = [f"[{stamp}] {line}" for line in lines]
        if not stamped:
            return
        with self._lock:
            self._lines.extend(stamped)
            if self.path is None:
                return
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(stamped) + "\n")
            except OSError:
                _LOGGER.warning("写执行日志失败: %s", self.path, exc_info=True)

    def write(self, line: str) -> None:
        self._append([line])

    @contextmanager
    def streaming(self) -> Iterator[Callable[[str], None]]:
        """流式写入器：文件只打开一次，每行 flush。

        ``write()`` 每行 open/close 是为了简单；但 Ghost 的进度输出每秒好几行，
        那种写法会变成 open/close 风暴，日志文件也常常滞后。这里返回一个
        上下文内可用的 ``write`` 函数，退出时关闭句柄。目录不可写时退回内存。
        """
        handle = None
        if self.path is not None:
            try:
                handle = self.path.open("a", encoding="utf-8")
            except OSError:
                _LOGGER.warning("写执行日志失败: %s", self.path, exc_info=True)

        def write(line: str) -> None:
            stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {line}"
            with self._lock:
                self._lines.append(stamped)
                if handle is not None:
                    try:
                        handle.write(stamped + "\n")
                        handle.flush()
                    except (OSError, ValueError):
                        # ``ValueError`` = 句柄已被 ``finally`` 关掉（取消/超时后
                        # 读线程还在写）。吞掉它，别让一句日志把读取线程打崩。
                        _LOGGER.warning("写执行日志失败: %s", self.path, exc_info=True)

        try:
            yield write
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def banner(self, title: str, rows: Iterable[tuple[str, object]] = ()) -> None:
        """写一段“抬头/结论”块：``===== 标题 =====`` + 对齐的 ``键：值``。

        行首统一对齐（中文按显示宽度算），目的是让日志**能被扫读**：出问题的人
        先看抬头知道是哪块盘、哪些盘符，再跳到结论，而不是从几百行命令里翻。
        """
        lines = [f"===== {title} ====="]
        items = [(str(key), str(value)) for key, value in rows if str(value or "").strip()]
        if items:
            width = max(_display_width(key) for key, _value in items)
            for key, value in items:
                pad = " " * max(0, width - _display_width(key))
                lines.append(f"  {key}{pad}  {value}")
        self._append(lines)

    def conclusion(self, ok: bool, rows: Iterable[tuple[str, object]] = ()) -> None:
        """写结论块：成功/失败 + 关键事实（失败步骤、原因、下一步）。"""
        self.banner(f"结论：{'成功' if ok else '失败'}", rows)

    def block(self, title: str, body: str) -> None:
        """整块写入：``--- 标题 ---`` + 缩进的正文（一次 open/close）。"""
        lines = [f"--- {title} ---"]
        lines.extend(f"    {line}" for line in (body or "").splitlines())
        self._append(lines)

    def summary(self) -> str:
        """最后 ``SUMMARY_LINES`` 行（``deque`` 不支持切片，用 islice）。"""
        start = max(0, len(self._lines) - SUMMARY_LINES)
        return "\n".join(itertools.islice(self._lines, start, None))


def _display_width(text: str) -> int:
    """显示宽度：中日韩全角字符算 2（日志抬头对齐用）。"""
    width = 0
    for char in str(text or ""):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


# 历史名字：老代码与测试都在用 ``_CommandLog``，保留别名不破坏调用方。
_CommandLog = CommandLog

__all__ = ["CommandLog", "_CommandLog", "default_log_dir"]
