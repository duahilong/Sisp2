"""日志尾部读取：按文件增量跟踪，不重复读整个文件。

`GET /api/jobs/{id}/log` 在进度页里是**每次轮询都调**的（日志面板展开时每
1.5 秒一次，``view-install.js`` 的 ``pollJobLogs``）。历史实现每次
``Path.read_text()`` 读整个文件再取尾部 ``[-limit:]``——Ghost 还原两小时的日志
能有几十 MB，而每次只要最后 120 行；三块盘并发时就是每 1.5 秒读几十 MB，
和镜像写入抢同一块盘。

这里维护 ``(已消费字节 offset, 尾部环形缓冲, 总行数, 未写完的半行)``，
正常情况下每次只读新增的几百字节。

**语义与 ``read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]``
逐字节等价**，包括：

- ``\\r\\n`` / ``\\r`` / ``\\n`` 三种行终止符（日志用文本模式写入，落盘是 CRLF）；
- 末尾没有换行的半行——``splitlines()`` 也把它算一行，所以返回时临时展开它，
  但不计入状态（否则下次追加内容会重复计数）；
- UTF-8 多字节字符被块边界切开——用增量解码器，不按块 ``decode()``。

不 import 任何应用层模块，方便单独测（``tests/test_log_tail.py``）。
"""

from __future__ import annotations

import codecs
import os
import threading
from collections import OrderedDict, deque

__all__ = ["MAX_TAIL_LINES", "LogTailReader"]

# 与 ``api.job_log`` 的 tail 上限保持一致：小等于此值的 limit 都能精确返回。
MAX_TAIL_LINES = 5000
# 跟踪的文件数上限：正常只有"每块盘一份日志"，留 64 个足够；超过就丢最久未用的。
MAX_TRACKED_FILES = 64


class _Entry:
    """一个文件的跟踪状态。"""

    __slots__ = ("decoder", "offset", "partial", "tail", "total")

    def __init__(self, max_tail: int) -> None:
        self.offset = 0
        self.tail: deque[str] = deque(maxlen=max_tail)
        self.total = 0
        # 末尾没有行终止符的部分：返回时临时当一行，但不计入 total。
        self.partial = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def reset(self, max_tail: int) -> None:
        self.offset = 0
        self.tail = deque(maxlen=max_tail)
        self.total = 0
        self.partial = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")


class LogTailReader:
    """线程安全的增量日志尾部读取器（桌面进程里接口在线程池执行）。"""

    def __init__(
        self,
        *,
        max_tail: int = MAX_TAIL_LINES,
        max_files: int = MAX_TRACKED_FILES,
    ) -> None:
        self._max_tail = max(1, int(max_tail))
        self._max_files = max(1, int(max_files))
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def read(self, path: str | os.PathLike[str], limit: int) -> tuple[list[str], int]:
        """返回 ``(最后 limit 行, 全文行数)``；读不到时返回 ``([], 0)``。"""
        key = str(path)
        try:
            wanted = int(limit)
        except (TypeError, ValueError):
            wanted = self._max_tail
        wanted = max(1, min(wanted, self._max_tail))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(self._max_tail)
                self._entries[key] = entry
                self._prune_locked()
            else:
                self._entries.move_to_end(key)
            self._refresh_locked(key, entry)
            rows = list(entry.tail)
            total = entry.total
            # ``\r`` 是“还没等到 \r\n 后半”的待定终止符：它本身不构成一行。
            if entry.partial and entry.partial != "\r":
                # ``splitlines()`` 把“末尾没有换行的半行”也算一行：临时展开，
                # 不改状态（下次拼接时它还会变成完整行）。
                tail_rows = entry.partial.splitlines()
                rows.extend(tail_rows)
                total += len(tail_rows)
            return rows[-wanted:], total

    # ------------------------------------------------------------------ 内部
    def _prune_locked(self) -> None:
        while len(self._entries) > self._max_files:
            self._entries.popitem(last=False)

    def _refresh_locked(self, key: str, entry: _Entry) -> None:
        # 注意：这里**不吞** OSError。历史实现是 ``read_text()``，文件不存在/被占用
        # 时接口返回 ``E_LOG``；改成静默返回空列表会改变对外行为（界面会以为
        # “日志还没输出”）。
        size = int(os.stat(key).st_size)
        if size < entry.offset:
            # 被截断或换了新文件：状态全部作废，从头再读。
            entry.reset(self._max_tail)
        if size == entry.offset:
            return
        with open(key, "rb") as handle:
            handle.seek(entry.offset)
            chunk = handle.read(size - entry.offset)
        if not chunk:
            return
        entry.offset += len(chunk)
        text = entry.decoder.decode(chunk, False)
        if not text:
            return
        # 上一块以 ``\r`` 结尾时,"" 是待定的 CRLF 后半：
        # - 这块以 ``\n`` 开头 → 两者拼成一个终止符，跳过后面的 ``\n``；
        # - 否则 → 那个 ``\r`` 已经终止了上一行，直接丢掉。
        if entry.partial == "\r":
            body = text[1:] if text.startswith("\n") else text
        else:
            body = entry.partial + text
        self._consume_locked(entry, body)

    def _consume_locked(self, entry: _Entry, body: str) -> None:
        """把新解码的文本切成完整行写进缓冲，剩余部分留给下一次。"""
        if body.endswith("\n"):
            complete, entry.partial = body, ""
        elif body.endswith("\r"):
            # 可能是 ``\r\n`` 的前半：先不消费这个 \r，等下一块拼起来再判断。
            complete, entry.partial = body[:-1], ""
        else:
            index = max(body.rfind("\n"), body.rfind("\r"))
            if index < 0:
                complete, entry.partial = "", body
            else:
                complete, entry.partial = body[: index + 1], body[index + 1 :]
        if not complete:
            return
        for row in complete.splitlines():
            entry.total += 1
            entry.tail.append(row)
