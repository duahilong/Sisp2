"""执行日志：把命令、退出码、输出写进 ``logDir/disk-runner-*.log``。

独立成模块的原因：分区阶段、装机阶段、收尾释放盘符都要写同一份日志，
而它们不应该为了一个日志类去依赖 ``real_runner``；没有 ``log_dir`` 时
只保留内存（单测用），行为与历史实现逐字一致。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class CommandLog:
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


# 历史名字：老代码与测试都在用 ``_CommandLog``，保留别名不破坏调用方。
_CommandLog = CommandLog

__all__ = ["CommandLog", "_CommandLog"]
